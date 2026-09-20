"""Fixed Domain Ontology definition for Neo4j Knowledge Graph.

Consists of:
- 11 Node Types
- 13 Edge Types with strict Source/Target type compatibility
"""

from typing import Dict, Set, Tuple, Union

# 11 Node Types được phép trong Neo4j
ALLOWED_NODES: Set[str] = {
    "Person",
    "SourceIdentity",
    "Task",
    "Project",
    "Customer",
    "Tenant",
    "SourceItem",
    "Decision",
    "Lesson",
    "Document",
    "Incident",
}

# 13 Edge Types với ma trận Source -> Target được phép
# Value là Tuple[SourceLabels, TargetLabels] (mỗi thành phần có thể là str hoặc tuple các str)
ALLOWED_EDGES: Dict[str, Tuple[Union[str, Tuple[str, ...]], Union[str, Tuple[str, ...]]]] = {
    "HAS_IDENTITY": ("Person", "SourceIdentity"),
    "OWNS": ("Person", "Task"),
    "REQUESTED": ("Person", "Task"),
    "COMMITTED_TO": ("Person", "Task"),
    "BELONGS_TO": ("Task", "Project"),
    "TRACKED_BY": ("Task", "SourceItem"),
    "SUPPORTED_BY": ("Task", "SourceItem"),
    "BLOCKED_BY": ("Task", ("Task", "SourceItem")),
    "WAITING_FOR": (("Person", "Task"), "Person"),
    "AFFECTS": ("Decision", ("Project", "Task")),
    "DERIVED_FROM": ("Lesson", "Incident"),
    "SUPERSEDES": ("Decision", "Decision"),
    "RELATED_TO": ("Task", "Task"),
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
