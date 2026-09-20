from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field

from ptb_contracts.l2_processing import TaskStatus, UnifiedTaskCandidate


class GraphRelationInfo(BaseModel):
    relation_type: str = Field(description="OWNS, COMMITTED_TO, REQUESTED, BLOCKED_BY, AFFECTS")
    target_entity_type: str = Field(description="Person, Task, Project, Decision")
    target_entity_id: str
    target_title_or_name: str
    valid_since: datetime
    confidence: float = 1.0


class TaskWithContext(BaseModel):
    task: UnifiedTaskCandidate
    last_status_change_at: datetime
    days_in_current_status: int = 0
    has_completion_evidence: bool = False
    
    relations: List[GraphRelationInfo] = Field(default_factory=list)
    blocking_tasks: List[str] = Field(default_factory=list, description="Danh sách Task ID đang block task này")
    dependent_people: List[str] = Field(default_factory=list, description="Danh sách người đang chờ kết quả task này")
    related_decisions: List[str] = Field(default_factory=list, description="Các quyết định liên quan")
    past_lessons_learned: List[str] = Field(default_factory=list, description="Kinh nghiệm liên quan")


class PriorityBreakdown(BaseModel):
    total_score: float = Field(description="Điểm ưu tiên tổng hợp (0 - 100)")
    deadline_score: float = 0.0
    customer_impact_score: float = 0.0
    production_impact_score: float = 0.0
    commitment_weight: float = 0.0
    waiting_penalty: float = 0.0
    stale_age_score: float = 0.0
    uncertainty_deduction: float = 0.0
    llm_explanation: str = Field(default="", description="Giải thích lý do ưu tiên ngắn gọn")


class TodayTaskItem(BaseModel):
    task_id: str
    title: str
    status: TaskStatus
    project_key: Optional[str] = None
    owner_name: str
    requester_name: Optional[str] = None
    due_date: Optional[datetime] = None
    priority: PriorityBreakdown
    primary_evidence_snippet: str
    deep_link: Optional[str] = None
    is_at_risk: bool = False
    risk_reason: Optional[str] = None


class ForgottenCommitmentItem(BaseModel):
    commitment_id: str
    task_id: str
    title: str
    promised_to_name: str
    promised_at: datetime
    days_stale: int
    last_conversation_snippet: str
    suggested_action: str = Field(description="Hành động gợi ý, e.g. 'Hỏi cập nhật từ Huy', 'Xác nhận hoàn thành'")


class WaitingOnItem(BaseModel):
    task_id: str
    title: str
    waiting_for_person_name: str
    blocked_since: datetime
    waiting_days: int
    reason: str


class TodayBoardView(BaseModel):
    generated_at: datetime
    user_id: str
    summary_headline: str = Field(description="Tóm tắt 1 dòng tình hình hôm nay")
    top_tasks: List[TodayTaskItem]
    waiting_on_others: List[WaitingOnItem] = Field(default_factory=list)
    forgotten_commitments: List[ForgottenCommitmentItem] = Field(default_factory=list)
    identified_risks: List[str] = Field(default_factory=list)


class DecisionRecord(BaseModel):
    decision_id: str
    project_key: Optional[str] = None
    summary: str
    rationale: str
    decided_by: str
    decided_at: datetime


class LessonRecord(BaseModel):
    lesson_id: str
    topic: str
    description: str
    solution: str
    related_incident_id: Optional[str] = None
    recorded_at: datetime
