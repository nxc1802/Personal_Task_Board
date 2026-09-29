"""Canonical Fixed Domain Ontology definition for Neo4j Single-Store Knowledge Graph.

Consists of:
- 15 Node Types
- Canonical Edge Types with strict Source/Target type compatibility
"""

from typing import Dict, Set, Tuple, Union

# 17 Node Types được phép trong Neo4j Single-Store (docs/v1.md, docs/v1_2.md)
ALLOWED_NODES: Set[str] = {
    "UnifiedTask",
    "Evidence",
    "Commitment",
    "Person",
    "SourceIdentity",
    "Project",
    "Customer",
    "Tenant",
    "RawEvent",
    "IngestionCheckpoint",
    "ProcessingAttempt",
    "Decision",
    "Lesson",
    "Incident",
    "Document",
    "StatusTransitionAudit",
    "MergeAudit",
}

# 5 Trạng thái Task chuẩn hóa (docs/v1_2.md)
CANONICAL_TASK_STATUSES: Set[str] = {
    "TODO",
    "IN_PROGRESS",
    "BLOCKED",
    "DONE",
    "DISMISSED",
}

# Canonical required properties cho các ontology nodes cốt lõi
CANONICAL_NODE_SCHEMAS: Dict[str, Set[str]] = {
    "Person": {
        "canonical_id",
        "canonical_name",
        "primary_email",
    },
    "Evidence": {
        "id",
        "task_id",
        "evidence_type",
        "snippet",
        "source_type",
        "source_event_id",
        "timestamp",
        "confidence_score",
    },
    "MergeAudit": {
        "id",
        "candidate_task_ids",
        "winning_task_id",
        "correlation_score",
        "deterministic_anchors",
        "merge_reason",
        "merged_at",
    },
    "StatusTransitionAudit": {
        "id",
        "task_id",
        "old_status",
        "new_status",
        "change_actor",
        "timestamp",
        "reason",
    },
    "IngestionCheckpoint": {
        "tenant_id",
        "source_type",
        "stream_id",
        "last_external_id",
        "last_event_timestamp",
        "cursor_token",
        "updated_at",
    },
}

EVIDENCE_FIELDS: Set[str] = CANONICAL_NODE_SCHEMAS["Evidence"]
MERGE_AUDIT_FIELDS: Set[str] = CANONICAL_NODE_SCHEMAS["MergeAudit"]
STATUS_TRANSITION_AUDIT_FIELDS: Set[str] = CANONICAL_NODE_SCHEMAS["StatusTransitionAudit"]
INGESTION_CHECKPOINT_FIELDS: Set[str] = CANONICAL_NODE_SCHEMAS["IngestionCheckpoint"]
PERSON_FIELDS: Set[str] = CANONICAL_NODE_SCHEMAS["Person"]

# Edge Types với ma trận Source -> Target được phép
# Value là Tuple[SourceLabels, TargetLabels] (mỗi thành phần có thể là str hoặc tuple các str)
ALLOWED_EDGES: Dict[str, Tuple[Union[str, Tuple[str, ...]], Union[str, Tuple[str, ...]]]] = {
    "HAS_IDENTITY": ("Person", "SourceIdentity"),
    "ASSIGNED_TO": ("Person", "UnifiedTask"),
    "REQUESTED": ("Person", "UnifiedTask"),
    "COMMITTED_TO": ("Person", "UnifiedTask"),
    "HAS_EVIDENCE": (("UnifiedTask", "Commitment"), "Evidence"),
    "MATERIALIZES_AS": ("Commitment", "UnifiedTask"),
    "BELONGS_TO": ("UnifiedTask", "Project"),
    "BLOCKED_BY": ("UnifiedTask", "UnifiedTask"),
    "RELATED_TO": ("UnifiedTask", "UnifiedTask"),
    "DERIVED_FROM": (
        ("Evidence", "Lesson"),
        ("RawEvent", "Incident", "UnifiedTask"),
    ),
    "AFFECTS": ("Decision", ("Project", "UnifiedTask")),
    "FROM_IDENTITY": ("RawEvent", "SourceIdentity"),
    "PROCESSING_ATTEMPT": ("RawEvent", "ProcessingAttempt"),
    "STATUS_AUDIT": ("UnifiedTask", "StatusTransitionAudit"),
    "MERGE_AUDIT": ("UnifiedTask", "MergeAudit"),
    "WAITING_FOR": (("Person", "UnifiedTask"), "Person"),
    "SUPERSEDES": ("Decision", "Decision"),
    # Backwards compatibility aliases
    "OWNS": ("Person", "UnifiedTask"),
}

# Các quan hệ AI trích xuất bắt buộc phải có bằng chứng (evidence) và confidence
AI_EXTRACTED_EDGES: Set[str] = {
    "COMMITTED_TO",
    "REQUESTED",
    "BLOCKED_BY",
    "WAITING_FOR",
    "AFFECTS",
    "RELATED_TO",
}
