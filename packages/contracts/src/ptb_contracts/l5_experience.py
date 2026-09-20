from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from ptb_contracts.l4_intelligence import DecisionRecord, LessonRecord


class TaskActionResponse(BaseModel):
    success: bool
    task_id: str
    message: str
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReviewActionResponse(BaseModel):
    success: bool
    review_id: str
    action_taken: str = Field(description="'approved', 'rejected', 'edited'")
    message: str


class CoverageTenantStatus(BaseModel):
    tenant_id: str
    tenant_name: str
    source_type: str
    status: str = Field(description="'healthy', 'warning', 'error', 'syncing'")
    last_successful_sync: Optional[datetime] = None
    items_synced_total: int = 0
    error_message: Optional[str] = None


class CoverageStatusResponse(BaseModel):
    tenants: List[CoverageTenantStatus]
    overall_health: str = Field(description="'healthy', 'degraded', 'critical'")
    last_checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class KnowledgeSearchRequest(BaseModel):
    query: str
    project_key: Optional[str] = None
    knowledge_type: str = Field(default="all", description="'decision', 'lesson', 'all'")
    limit: int = Field(default=5, le=20)


class KnowledgeSearchResponse(BaseModel):
    decisions: List[DecisionRecord] = Field(default_factory=list)
    lessons: List[LessonRecord] = Field(default_factory=list)
    synthesis_summary: Optional[str] = None


class MCPToolCallRequest(BaseModel):
    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)


class MCPToolCallResult(BaseModel):
    tool_name: str
    content: str
    is_error: bool = False
