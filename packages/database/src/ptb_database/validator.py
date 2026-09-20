"""Tier 2: Application Allowlist Validator for Neo4j Knowledge Graph.

Ngăn chặn việc nạp các nhãn Node lạ, quan hệ Edge ngoài danh mục cho phép,
hoặc quan hệ không đúng loại thực thể.
"""

from typing import Any, Dict, Optional
from pydantic import BaseModel, Field

from ptb_database.ontology import ALLOWED_NODES, ALLOWED_EDGES, AI_EXTRACTED_EDGES


class ValidationResult(BaseModel):
    is_valid: bool
    error_message: Optional[str] = None


class GraphOntologyValidator:
    @staticmethod
    def validate_node(node_label: str, properties: Dict[str, Any]) -> ValidationResult:
        """Kiểm tra nhãn node và thuộc tính định danh."""
        if node_label not in ALLOWED_NODES:
            return ValidationResult(
                is_valid=False,
                error_message=f"Node label '{node_label}' không nằm trong danh mục ALLOWED_NODES ({sorted(ALLOWED_NODES)})"
            )
        
        # Bắt buộc phải có ID khóa chính tương ứng
        id_fields = {
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
        required_id = id_fields.get(node_label)
        if required_id and required_id not in properties:
            return ValidationResult(
                is_valid=False,
                error_message=f"Node '{node_label}' bắt buộc phải có thuộc tính khóa chính '{required_id}'"
            )

        return ValidationResult(is_valid=True)

    @staticmethod
    def validate_edge(
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
