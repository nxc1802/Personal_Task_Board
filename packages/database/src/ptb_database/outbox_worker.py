"""Transactional Outbox Consumer Worker for Neo4j Knowledge Graph.

Đọc các sự kiện từ bảng graph_outbox_events trong Supabase,
kiểm tra qua Tier 2 Application Validator và nạp vào Neo4j một cách bền bỉ.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ptb_contracts.l3_storage import GraphActionType, GraphOutboxEventPayload
from ptb_database.validator import GraphOntologyValidator

logger = logging.getLogger("ptb.database.outbox_worker")


# Ánh xạ nhãn node với trường khóa chính
NODE_ID_FIELDS = {
    "Person": "canonical_id",
    "SourceIdentity": "identity_key",
    "Task": "task_id",
    "Project": "project_key",
    "Customer": "customer_id",
    "Tenant": "tenant_id",
    "SourceItem": "item_id",
    "Decision": "decision_id",
    "Lesson": "lesson_id",
    "Document": "doc_id",
    "Incident": "incident_id",
}


class OutboxWorker:
    def __init__(self, neo4j_driver: Optional[Any] = None):
        self.driver = neo4j_driver

    async def process_outbox_event(self, event: GraphOutboxEventPayload) -> Dict[str, Any]:
        """Xử lý 1 sự kiện outbox: validate -> build cypher -> execute."""
        try:
            if event.action == GraphActionType.UPSERT_NODE:
                return await self._handle_upsert_node(event)
            elif event.action == GraphActionType.UPSERT_EDGE:
                return await self._handle_upsert_edge(event)
            elif event.action == GraphActionType.INVALIDATE_EDGE:
                return await self._handle_invalidate_edge(event)
            else:
                return {
                    "success": False,
                    "error": f"Hành động không được hỗ trợ: {event.action}",
                    "status": "failed"
                }
        except Exception as e:
            logger.exception("Lỗi khi xử lý outbox event")
            return {
                "success": False,
                "error": str(e),
                "status": "failed"
            }

    async def _handle_upsert_node(self, event: GraphOutboxEventPayload) -> Dict[str, Any]:
        node_label = event.node_label or event.aggregate_type
        props = event.node_properties or {}
        id_field = NODE_ID_FIELDS.get(node_label, "id")
        
        # Đảm bảo thuộc tính ID có trong props
        if id_field not in props:
            props[id_field] = event.aggregate_id

        # Kiểm tra qua Tier 2 Validator
        validation = GraphOntologyValidator.validate_node(node_label, props)
        if not validation.is_valid:
            return {
                "success": False,
                "error": validation.error_message,
                "status": "rejected_schema_violation"
            }

        cypher = f"""
        MERGE (n:{node_label} {{{id_field}: $id}})
        SET n += $props, n.updated_at = datetime()
        """
        params = {"id": event.aggregate_id, "props": props}

        if self.driver:
            async with self.driver.session() as session:
                await session.run(cypher, params)

        return {
            "success": True,
            "action": "upsert_node",
            "cypher": cypher.strip(),
            "status": "completed"
        }

    async def _handle_upsert_edge(self, event: GraphOutboxEventPayload) -> Dict[str, Any]:
        edge_type = event.edge_type
        if not edge_type:
            return {"success": False, "error": "Thiếu edge_type", "status": "failed"}

        # Xác định source và target labels dựa trên payload hoặc aggregate_type
        props = event.edge_properties or {}
        source_label = props.get("source_label", "Task")
        target_label = props.get("target_label", "Person")

        # Kiểm tra qua Tier 2 Validator
        validation = GraphOntologyValidator.validate_edge(
            edge_type=edge_type,
            source_label=source_label,
            target_label=target_label,
            properties={
                "confidence": event.confidence,
                "evidence_id": event.evidence_id,
                **props
            }
        )
        if not validation.is_valid:
            return {
                "success": False,
                "error": validation.error_message,
                "status": "rejected_schema_violation"
            }

        src_id_field = NODE_ID_FIELDS.get(source_label, "id")
        dst_id_field = NODE_ID_FIELDS.get(target_label, "id")

        cypher = f"""
        MATCH (src:{source_label} {{{src_id_field}: $src_id}})
        MATCH (dst:{target_label} {{{dst_id_field}: $dst_id}})
        MERGE (src)-[r:{edge_type}]->(dst)
        SET r += $props,
            r.valid_at = datetime($valid_at),
            r.confidence = $confidence,
            r.evidence_id = $evidence_id
        """
        params = {
            "src_id": event.source_canonical_id,
            "dst_id": event.target_canonical_id,
            "valid_at": event.valid_at.isoformat(),
            "confidence": event.confidence,
            "evidence_id": event.evidence_id,
            "props": props
        }

        if self.driver:
            async with self.driver.session() as session:
                await session.run(cypher, params)

        return {
            "success": True,
            "action": "upsert_edge",
            "cypher": cypher.strip(),
            "status": "completed"
        }

    async def _handle_invalidate_edge(self, event: GraphOutboxEventPayload) -> Dict[str, Any]:
        edge_type = event.edge_type
        props = event.edge_properties or {}
        source_label = props.get("source_label", "Task")
        target_label = props.get("target_label", "Person")
        src_id_field = NODE_ID_FIELDS.get(source_label, "id")
        dst_id_field = NODE_ID_FIELDS.get(target_label, "id")

        cypher = f"""
        MATCH (src:{source_label} {{{src_id_field}: $src_id}})-[r:{edge_type}]->(dst:{target_label} {{{dst_id_field}: $dst_id}})
        WHERE r.invalid_at IS NULL
        SET r.invalid_at = datetime($invalid_at)
        """
        params = {
            "src_id": event.source_canonical_id,
            "dst_id": event.target_canonical_id,
            "invalid_at": datetime.now(timezone.utc).isoformat()
        }

        if self.driver:
            async with self.driver.session() as session:
                await session.run(cypher, params)

        return {
            "success": True,
            "action": "invalidate_edge",
            "cypher": cypher.strip(),
            "status": "completed"
        }
