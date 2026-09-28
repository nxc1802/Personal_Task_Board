from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from ptb_contracts.l2_processing import TaskStatus


class CanonicalPersonRecord(BaseModel):
    id: str = Field(description="UUID v4 của person")
    workspace_id: Optional[str] = None
    canonical_name: str
    primary_email: str
    avatar_url: Optional[str] = None
    is_current_user: bool = False
    created_at: Optional[datetime] = None


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
    confidence: float = 1.0
    source_type: str
    external_url: Optional[str] = None
    timestamp: datetime
    raw_event_id: str


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
