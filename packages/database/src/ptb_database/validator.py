"""Tier 2: Application Allowlist Validator for Neo4j Knowledge Graph.

Ngăn chặn việc nạp các nhãn Node lạ, quan hệ Edge ngoài danh mục cho phép,
hoặc quan hệ không đúng loại thực thể theo Canonical Ontology trong docs/v1.md.
"""

from typing import Any, Dict, Optional, Tuple, Union
from pydantic import BaseModel, Field

from ptb_database.ontology import (
    ALLOWED_NODES,
    ALLOWED_EDGES,
    AI_EXTRACTED_EDGES,
    CANONICAL_TASK_STATUSES,
    CANONICAL_NODE_SCHEMAS,
)


class ValidationResult(BaseModel):
    is_valid: bool
    error_message: Optional[str] = None


class GraphOntologyValidator:
    # Bắt buộc phải có ít nhất 1 thuộc tính khóa chính tương ứng
    ID_FIELDS: Dict[str, Tuple[str, ...]] = {
        "Person": ("canonical_id", "id"),
        "SourceIdentity": ("identity_key", "id"),
        "UnifiedTask": ("id", "task_id"),
        "Evidence": ("id", "evidence_id"),
        "Commitment": ("id", "commitment_id"),
        "Project": ("project_key", "id"),
        "Customer": ("customer_id", "id"),
        "Tenant": ("tenant_id", "id"),
        "RawEvent": ("id", "raw_event_id"),
        "IngestionCheckpoint": ("id", "stream_id"),
        "ProcessingAttempt": ("id", "attempt_id"),
        "Decision": ("decision_id", "id"),
        "Lesson": ("lesson_id", "id"),
        "Document": ("doc_id", "id"),
        "Incident": ("incident_id", "id"),
        "StatusTransitionAudit": ("id", "audit_id"),
        "MergeAudit": ("id", "audit_id"),
    }

    @classmethod
    def validate_node(cls, node_label: str, properties: Dict[str, Any]) -> ValidationResult:
        """Kiểm tra nhãn node, thuộc tính định danh và vocabulary trạng thái."""
        if node_label not in ALLOWED_NODES:
            return ValidationResult(
                is_valid=False,
                error_message=f"Node label '{node_label}' không nằm trong danh mục ALLOWED_NODES ({sorted(ALLOWED_NODES)})"
            )
        
        allowed_ids = cls.ID_FIELDS.get(node_label)
        if allowed_ids:
            if not any(k in properties for k in allowed_ids):
                return ValidationResult(
                    is_valid=False,
                    error_message=f"Node '{node_label}' bắt buộc phải có thuộc tính khóa chính trong {allowed_ids}"
                )

        # Chuẩn hóa kiểm tra TaskStatus (5 uppercase statuses)
        if node_label == "UnifiedTask" and "status" in properties:
            status_val = properties["status"]
            if hasattr(status_val, "value"):
                status_val = status_val.value
            if status_val not in CANONICAL_TASK_STATUSES:
                return ValidationResult(
                    is_valid=False,
                    error_message=f"TaskStatus '{status_val}' không hợp lệ. Phải là một trong 5 trạng thái uppercase: {sorted(CANONICAL_TASK_STATUSES)}"
                )

        if node_label == "StatusTransitionAudit":
            for status_key in ("old_status", "new_status"):
                if status_key in properties:
                    s_val = properties[status_key]
                    if hasattr(s_val, "value"):
                        s_val = s_val.value
                    if s_val not in CANONICAL_TASK_STATUSES:
                        return ValidationResult(
                            is_valid=False,
                            error_message=f"{status_key} '{s_val}' không hợp lệ. Phải là một trong 5 trạng thái uppercase: {sorted(CANONICAL_TASK_STATUSES)}"
                        )

        return ValidationResult(is_valid=True)

    @classmethod
    def validate_canonical_schema(
        cls,
        node_label: str,
        properties: Dict[str, Any],
    ) -> ValidationResult:
        """Kiểm tra đối soát 100% vocabulary và các trường bắt buộc của schema node theo ontology."""
        if node_label not in CANONICAL_NODE_SCHEMAS:
            return cls.validate_node(node_label, properties)

        base_res = cls.validate_node(node_label, properties)
        if not base_res.is_valid:
            return base_res

        required_fields = CANONICAL_NODE_SCHEMAS[node_label]
        props = dict(properties)

        # Xử lý aliases tương thích giữa Python/TypeScript/Cypher
        if node_label == "Evidence":
            if "raw_event_id" in props and "source_event_id" not in props:
                props["source_event_id"] = props["raw_event_id"]
            if "confidence" in props and "confidence_score" not in props:
                props["confidence_score"] = props["confidence"]
        elif node_label == "MergeAudit":
            if "created_at" in props and "merged_at" not in props:
                props["merged_at"] = props["created_at"]
        elif node_label == "StatusTransitionAudit":
            if "changed_at" in props and "timestamp" not in props:
                props["timestamp"] = props["changed_at"]

        missing = [f for f in required_fields if f not in props or props[f] is None]
        if missing:
            return ValidationResult(
                is_valid=False,
                error_message=f"Node '{node_label}' thiếu các trường schema canonical bắt buộc: {sorted(missing)}"
            )

        return ValidationResult(is_valid=True)

    @classmethod
    def validate_edge(
        cls,
        edge_type: str,
        source_label: str,
        target_label: str,
        properties: Optional[Dict[str, Any]] = None
    ) -> ValidationResult:
        """Kiểm tra quan hệ cạnh và tính tương thích giữa Source và Target."""
        if edge_type not in ALLOWED_EDGES:
            return ValidationResult(
                is_valid=False,
                error_message=f"Edge type '{edge_type}' không nằm trong danh mục ALLOWED_EDGES ({sorted(ALLOWED_EDGES.keys())})"
            )

        allowed_source, allowed_target = ALLOWED_EDGES[edge_type]

        # Kiểm tra source label
        if isinstance(allowed_source, tuple):
            if source_label not in allowed_source:
                return ValidationResult(
                    is_valid=False,
                    error_message=f"Source label '{source_label}' không hợp lệ cho edge '{edge_type}'. Phải là một trong {allowed_source}"
                )
        elif source_label != allowed_source:
            return ValidationResult(
                is_valid=False,
                error_message=f"Source label '{source_label}' không hợp lệ cho edge '{edge_type}'. Phải là '{allowed_source}'"
            )

        # Kiểm tra target label
        if isinstance(allowed_target, tuple):
            if target_label not in allowed_target:
                return ValidationResult(
                    is_valid=False,
                    error_message=f"Target label '{target_label}' không hợp lệ cho edge '{edge_type}'. Phải là một trong {allowed_target}"
                )
        elif target_label != allowed_target:
            return ValidationResult(
                is_valid=False,
                error_message=f"Target label '{target_label}' không hợp lệ cho edge '{edge_type}'. Phải là '{allowed_target}'"
            )

        # Kiểm tra thuộc tính bắt buộc của AI-extracted edges
        props = properties or {}
        if edge_type in AI_EXTRACTED_EDGES:
            confidence = props.get("confidence")
            if confidence is None or not (0.0 <= float(confidence) <= 1.0):
                return ValidationResult(
                    is_valid=False,
                    error_message=f"Edge '{edge_type}' là quan hệ do AI trích xuất, bắt buộc phải có 'confidence' từ 0.0 đến 1.0"
                )
            if "evidence_id" not in props and "evidence_ids" not in props:
                return ValidationResult(
                    is_valid=False,
                    error_message=f"Edge '{edge_type}' bắt buộc phải có 'evidence_id' làm bằng chứng"
                )

        return ValidationResult(is_valid=True)

    @classmethod
    def validate_or_raise(
        cls,
        edge_type: str,
        source_label: str,
        target_label: str,
        properties: Optional[Dict[str, Any]] = None
    ) -> None:
        res = cls.validate_edge(edge_type, source_label, target_label, properties)
        if not res.is_valid:
            raise ValueError(res.error_message)
