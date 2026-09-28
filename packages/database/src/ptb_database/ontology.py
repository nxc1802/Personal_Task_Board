"""Canonical Fixed Domain Ontology definition for Neo4j Single-Store Knowledge Graph.

Consists of:
- 15 Node Types
- Canonical Edge Types with strict Source/Target type compatibility
"""

from typing import Dict, Set, Tuple, Union

# 15 Node Types được phép trong Neo4j Single-Store (docs/v1.md)
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
}

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
