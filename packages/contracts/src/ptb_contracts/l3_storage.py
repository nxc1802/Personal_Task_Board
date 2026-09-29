from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from ptb_contracts.l2_processing import TaskStatus


class CanonicalPersonRecord(BaseModel):
    canonical_id: str = Field(default="", description="Canonical ID của Person (duy nhất trên ontology)")
    id: Optional[str] = Field(default=None, description="UUID v4 hoặc alias của Person")
    workspace_id: Optional[str] = None
    canonical_name: str
    primary_email: str
    avatar_url: Optional[str] = None
    is_current_user: bool = False
    created_at: Optional[datetime] = None

    def model_post_init(self, __context: Any) -> None:
        if not self.canonical_id and self.id:
            object.__setattr__(self, "canonical_id", self.id)
        elif not self.id and self.canonical_id:
            object.__setattr__(self, "id", self.canonical_id)


class SourceIdentityRecord(BaseModel):
    id: str
    person_id: Optional[str] = None
    identity_key: str = Field(description="tenant_id:source_type:external_id")
    tenant_id: str
    source_type: str
    external_id: str
    external_username: Optional[str] = None
    external_display_name: Optional[str] = None


class UnifiedTaskRecord(BaseModel):
    id: str = Field(description="UUID v4 của UnifiedTask")
    title: str
    description: Optional[str] = None
    status: TaskStatus = TaskStatus.TODO
    inferred_status: Optional[str] = None
    priority_score: float = 0.0
    due_date: Optional[datetime] = None
    explicit_deadline: bool = False
    project_key: Optional[str] = None
    customer_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class EvidenceNodeRecord(BaseModel):
    id: str = Field(description="UUID v4 của Evidence")
    snippet: str
    confidence_score: float = Field(default=1.0, description="Điểm tin cậy")
    confidence: Optional[float] = None
    source_type: str
    external_url: Optional[str] = None
    timestamp: datetime
    source_event_id: Optional[str] = None
    raw_event_id: Optional[str] = None
    graph_sync_status: Optional[str] = Field(default="PENDING", description="Trạng thái sync: PENDING, SYNCING, SYNCED, RETRY, FAILED")
    graph_sync_attempts: Optional[int] = Field(default=0, description="Số lần thử sync")
    graph_synced_at: Optional[str] = None
    graph_last_error: Optional[str] = None

    def model_post_init(self, __context: Any) -> None:
        if self.source_event_id and not self.raw_event_id:
            object.__setattr__(self, "raw_event_id", self.source_event_id)
        elif self.raw_event_id and not self.source_event_id:
            object.__setattr__(self, "source_event_id", self.raw_event_id)
        if self.confidence is not None and "confidence_score" not in self.model_fields_set:
            object.__setattr__(self, "confidence_score", self.confidence)
        elif self.confidence_score is not None and self.confidence is None:
            object.__setattr__(self, "confidence", self.confidence_score)


class CommitmentNodeRecord(BaseModel):
    id: str = Field(description="UUID v4 của Commitment")
    title: str
    status: str = "ACTIVE"
    due_date: Optional[datetime] = None
    explicit_deadline: bool = False
    created_at: Optional[datetime] = None


class ProjectRecord(BaseModel):
    project_key: str
    name: str
    description: Optional[str] = None


class CustomerRecord(BaseModel):
    customer_id: str
    name: str


class TenantRecord(BaseModel):
    tenant_id: str
    name: str
    source_type: str


class DecisionNodeRecord(BaseModel):
    decision_id: str
    summary: str
    rationale: str
    topic: Optional[str] = None
    decided_by: str
    decided_at: datetime


class LessonNodeRecord(BaseModel):
    lesson_id: str
    topic: str
    description: str
    solution: str
    recorded_at: datetime


class IncidentNodeRecord(BaseModel):
    incident_id: str
    title: str
    severity: str
    occurred_at: datetime


class DocumentNodeRecord(BaseModel):
    doc_id: str
    title: str
    url: Optional[str] = None


class GraphNeighborhoodQuery(BaseModel):
    center_node_id: str
    center_label: str
    depth: int = Field(default=1, le=3)
    relationship_types: Optional[List[str]] = None


# Canonical Ontology Aliases
Person = CanonicalPersonRecord
UnifiedTask = UnifiedTaskRecord
SourceIdentity = SourceIdentityRecord
EvidenceNode = EvidenceNodeRecord
CommitmentNode = CommitmentNodeRecord
Project = ProjectRecord
Customer = CustomerRecord
Tenant = TenantRecord
Decision = DecisionNodeRecord
Lesson = LessonNodeRecord
Incident = IncidentNodeRecord
Document = DocumentNodeRecord
