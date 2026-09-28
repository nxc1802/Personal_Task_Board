"""RawEventRepository: Repository for managing RawEvent and ProcessingAttempt nodes in Neo4j."""

from datetime import datetime, timezone
import json
import logging
from typing import Any, Dict, List, Optional
from uuid import uuid4

from neo4j import AsyncDriver
from ptb_contracts.l1_acquisition import (
    ProcessingAttemptRecord,
    ProcessingStatus,
    RawEventRecord,
    SourceType,
)
from ptb_database.neo4j_client import Neo4jClient

logger = logging.getLogger("ptb.database.repositories.raw_event_repo")


class RawEventRepository:
    """Repository quản lý RawEvent, Ingestion Journal và ProcessingAttempt trong Neo4j."""

    def __init__(self, neo4j_client: Neo4jClient) -> None:
        self.neo4j_client = neo4j_client

    def _get_driver(self) -> AsyncDriver:
        return self.neo4j_client.get_driver()

    async def persist_raw_event(self, record: RawEventRecord) -> str:
        """Lưu vào node (:RawEvent) với MERGE trên idempotency_key.
        
        Lưu payload_json (serialized string), content_hash, normalized_text,
        source_type, external_id, tenant_id, event_timestamp, processing_status,
        retry_count=0. Trả về event id.
        """
        driver = self._get_driver()
        event_id = record.id or str(uuid4())

        # Serialize payload_json nếu chưa có
        payload_json = record.payload_json
        if payload_json is None:
            payload_json = json.dumps(record.raw_payload or {}, default=str, ensure_ascii=False)

        source_type = record.source_type.value if hasattr(record.source_type, "value") else str(record.source_type)
        status = record.processing_status.value if hasattr(record.processing_status, "value") else str(record.processing_status)

        event_timestamp = (
            record.event_timestamp.isoformat()
            if hasattr(record.event_timestamp, "isoformat")
            else str(record.event_timestamp)
        )
        captured_at = (
            record.captured_at.isoformat()
            if record.captured_at and hasattr(record.captured_at, "isoformat")
            else datetime.now(timezone.utc).isoformat()
        )
        created_at = (
            record.created_at.isoformat()
            if record.created_at and hasattr(record.created_at, "isoformat")
            else datetime.now(timezone.utc).isoformat()
        )

        cypher = """
        MERGE (re:RawEvent {idempotency_key: $idempotency_key})
        ON CREATE SET
            re.id = $id,
            re.tenant_id = $tenant_id,
            re.source_type = $source_type,
            re.external_id = $external_id,
            re.parent_external_id = $parent_external_id,
            re.idempotency_key = $idempotency_key,
            re.event_timestamp = $event_timestamp,
            re.captured_at = $captured_at,
            re.author_external_id = $author_external_id,
            re.author_display_name = $author_display_name,
            re.conversation_or_project_id = $conversation_or_project_id,
            re.deep_link = $deep_link,
            re.payload_json = $payload_json,
            re.normalized_text = $normalized_text,
            re.content_hash = $content_hash,
            re.processing_status = $processing_status,
            re.retry_count = 0,
            re.last_error = $last_error,
            re.created_at = $created_at
        RETURN re.id AS id
        """
        params = {
            "id": event_id,
            "tenant_id": record.tenant_id,
            "source_type": source_type,
            "external_id": record.external_id,
            "parent_external_id": record.parent_external_id,
            "idempotency_key": record.idempotency_key,
            "event_timestamp": event_timestamp,
            "captured_at": captured_at,
            "author_external_id": record.author_external_id,
            "author_display_name": record.author_display_name,
            "conversation_or_project_id": record.conversation_or_project_id,
            "deep_link": record.deep_link,
            "payload_json": payload_json,
            "normalized_text": record.normalized_text,
            "content_hash": record.content_hash,
            "processing_status": status,
            "last_error": record.last_error,
            "created_at": created_at,
        }

        async with driver.session(database=self.neo4j_client.database) as session:
            result = await session.run(cypher, params)
            row = await result.single()
            if row and row["id"]:
                return str(row["id"])
            return event_id

    async def get_pending_raw_events(self, limit: int = 50) -> list[RawEventRecord]:
        """Query các RawEvent có processing_status = 'pending' ORDER BY event_timestamp ASC."""
        driver = self._get_driver()
        cypher = """
        MATCH (re:RawEvent)
        WHERE re.processing_status = 'pending'
        RETURN re
        ORDER BY re.event_timestamp ASC
        LIMIT $limit
        """
        async with driver.session(database=self.neo4j_client.database) as session:
            result = await session.run(cypher, {"limit": limit})
            events: list[RawEventRecord] = []
            async for row in result:
                node = row["re"]
                data = dict(node)
                # Phục hồi raw_payload từ payload_json nếu cần
                if "raw_payload" not in data or not data["raw_payload"]:
                    if data.get("payload_json"):
                        try:
                            data["raw_payload"] = json.loads(data["payload_json"])
                        except Exception:
                            data["raw_payload"] = {}
                    else:
                        data["raw_payload"] = {}
                # Chuyển đổi timestamp ISO format sang datetime object nếu cần
                for dt_field in ["event_timestamp", "captured_at", "created_at"]:
                    if dt_field in data and isinstance(data[dt_field], str):
                        try:
                            data[dt_field] = datetime.fromisoformat(data[dt_field])
                        except Exception:
                            pass
                events.append(RawEventRecord.model_validate(data))
            return events

    async def mark_event_status(
        self,
        event_id: str,
        status: ProcessingStatus,
        error: Optional[str] = None,
    ) -> None:
        """Cập nhật processing_status và last_error, tăng retry_count nếu failed."""
        driver = self._get_driver()
        status_val = status.value if hasattr(status, "value") else str(status)
        updated_at = datetime.now(timezone.utc).isoformat()

        cypher = """
        MATCH (re:RawEvent {id: $event_id})
        SET re.processing_status = $status,
            re.last_error = $error,
            re.retry_count = CASE WHEN $status = 'failed' THEN coalesce(re.retry_count, 0) + 1 ELSE coalesce(re.retry_count, 0) END,
            re.updated_at = $updated_at
        """
        params = {
            "event_id": event_id,
            "status": status_val,
            "error": error,
            "updated_at": updated_at,
        }
        async with driver.session(database=self.neo4j_client.database) as session:
            await session.run(cypher, params)

    async def record_processing_attempt(self, attempt: ProcessingAttemptRecord) -> None:
        """Tạo node (:ProcessingAttempt) và liên kết (:RawEvent)-[:PROCESSING_ATTEMPT]->(:ProcessingAttempt)."""
        driver = self._get_driver()
        status_val = attempt.status.value if hasattr(attempt.status, "value") else str(attempt.status)
        attempted_at = (
            attempt.attempted_at.isoformat()
            if attempt.attempted_at and hasattr(attempt.attempted_at, "isoformat")
            else datetime.now(timezone.utc).isoformat()
        )
        attempt_id = attempt.id or str(uuid4())

        cypher = """
        MATCH (re:RawEvent {id: $raw_event_id})
        MERGE (pa:ProcessingAttempt {id: $id})
        SET pa.attempt_number = $attempt_number,
            pa.status = $status,
            pa.error_message = $error_message,
            pa.attempted_at = $attempted_at
        MERGE (re)-[:PROCESSING_ATTEMPT]->(pa)
        """
        params = {
            "raw_event_id": attempt.raw_event_id,
            "id": attempt_id,
            "attempt_number": attempt.attempt_number,
            "status": status_val,
            "error_message": attempt.error_message,
            "attempted_at": attempted_at,
        }
        async with driver.session(database=self.neo4j_client.database) as session:
            await session.run(cypher, params)
