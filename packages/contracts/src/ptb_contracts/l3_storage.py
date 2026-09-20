from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class GraphActionType(str, Enum):
    UPSERT_NODE = "upsert_node"
    UPSERT_EDGE = "upsert_edge"
    INVALIDATE_EDGE = "invalidate_edge"


class CanonicalPersonRecord(BaseModel):
    id: str = Field(description="UUID v4 của person")
    workspace_id: str
    canonical_name: str
    primary_email: str
    avatar_url: Optional[str] = None
    is_current_user: bool = False
    created_at: Optional[datetime] = None


class SourceIdentityRecord(BaseModel):
    id: str
    person_id: str
    tenant_id: str
    source_type: str
    external_id: str
    external_username: Optional[str] = None
    external_display_name: Optional[str] = None


class GraphOutboxEventPayload(BaseModel):
    outbox_id: Optional[str] = None
    aggregate_type: str = Field(description="Task, Person, Project, Decision")
    aggregate_id: str = Field(description="ID thực thể trong Supabase")
    action: GraphActionType
    
    node_label: Optional[str] = Field(default=None, description="Node label trong Fixed Ontology")
    node_properties: Optional[Dict[str, Any]] = None
    
    edge_type: Optional[str] = Field(default=None, description="Edge type trong Fixed Ontology")
    source_canonical_id: Optional[str] = None
    target_canonical_id: Optional[str] = None
    edge_properties: Optional[Dict[str, Any]] = None
    
    valid_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    confidence: float = Field(default=1.0)
    evidence_id: Optional[str] = None


class GraphNeighborhoodQuery(BaseModel):
    center_node_id: str
    center_label: str
    depth: int = Field(default=1, le=3)
    relationship_types: Optional[List[str]] = None
