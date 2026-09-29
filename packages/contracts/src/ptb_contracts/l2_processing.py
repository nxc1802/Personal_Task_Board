from datetime import datetime, timezone
from enum import Enum
from typing import Any, List, Optional
from uuid import uuid4
from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    DONE = "DONE"
    DISMISSED = "DISMISSED"

    @classmethod
    def _missing_(cls, value: object) -> Any:
        if isinstance(value, str):
            val_norm = value.strip().upper()
            if val_norm == "OPEN":
                return cls.TODO
            for member in cls:
                if member.value == val_norm or member.name == val_norm:
                    return member
        return None


class InferredStatus(str, Enum):
    LIKELY_DONE = "LIKELY_DONE"
    LIKELY_BLOCKED = "LIKELY_BLOCKED"
    IN_PROGRESS = "IN_PROGRESS"


class EvidenceType(str, Enum):
    CHAT_COMMITMENT = "chat_commitment"
    CHAT_REQUEST = "chat_request"
    AGENT_DECISION = "agent_decision"
    AGENT_BUG_FIX = "agent_bug_fix"
    JIRA_TICKET = "jira_ticket"
    SHORTCUT_STORY = "shortcut_story"
    EMAIL_THREAD = "email_thread"
    COMPLETION_SIGNAL = "completion_signal"


class ParsedMessageContent(BaseModel):
    is_quote_reply: bool = Field(default=False)
    quoted_author_raw: Optional[str] = Field(default=None)
    quoted_content_text: Optional[str] = Field(default=None)
    actual_content_text: str = Field(description="Nội dung thực tế của người gửi hiện tại")
    actual_author_raw: Optional[str] = Field(default=None, description="Tác giả thực tế của tin nhắn hiện tại")


class EvidenceRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()), description="UUID v4 của evidence")
    task_id: Optional[str] = Field(default=None, description="ID của task liên kết")
    evidence_type: EvidenceType
    snippet: str = Field(description="Đoạn trích văn bản làm bằng chứng")
    source_type: str
    source_event_id: Optional[str] = Field(default=None, description="ID của raw source event")
    timestamp: datetime
    confidence_score: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="Điểm tin cậy trích xuất")

    # Compatibility aliases
    raw_event_id: Optional[str] = Field(default=None, description="Alias cho source_event_id")
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="Alias cho confidence_score")
    external_url: Optional[str] = None
    author_canonical_id: Optional[str] = Field(default=None, description="ID người gửi theo Person node")
    author_canonical_name: Optional[str] = Field(default=None, description="Tên canonical người gửi")
    extraction_version: str = Field(default="v1.0")

    def model_post_init(self, __context: Any) -> None:
        if self.source_event_id and not self.raw_event_id:
            object.__setattr__(self, "raw_event_id", self.source_event_id)
        elif self.raw_event_id and not self.source_event_id:
            object.__setattr__(self, "source_event_id", self.raw_event_id)
        if self.confidence is not None and self.confidence_score is None:
            object.__setattr__(self, "confidence_score", self.confidence)
        elif self.confidence_score is not None and self.confidence is None:
            object.__setattr__(self, "confidence", self.confidence_score)
        elif self.confidence is None and self.confidence_score is None:
            object.__setattr__(self, "confidence_score", 1.0)
            object.__setattr__(self, "confidence", 1.0)


class CommitmentRecord(BaseModel):
    id: str = Field(description="UUID v4 của commitment")
    title: str = Field(description="Mô tả cam kết cụ thể")
    owner_id: str = Field(description="Person ID của người cam kết")
    requester_id: Optional[str] = Field(default=None, description="Person ID của người nhận cam kết")
    due_date: Optional[datetime] = None
    explicit_deadline: bool = Field(default=False)
    status: str = Field(default="ACTIVE", description="'ACTIVE', 'FULFILLED', 'ABANDONED'")
    task_id: Optional[str] = Field(default=None, description="Task materialize từ commitment")
    evidence_id: Optional[str] = None
    created_at: Optional[datetime] = None


class ExtractedCommitment(BaseModel):
    title: str = Field(description="Mô tả ngắn gọn về hành động hoặc cam kết")
    owner_id: str = Field(description="Canonical person ID của người cam kết thực hiện")
    requester_id: Optional[str] = Field(default=None, description="Canonical person ID của người yêu cầu")
    project_key: Optional[str] = None
    due_date: Optional[datetime] = None
    explicit_deadline: bool = Field(default=False)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_snippet: str = Field(description="Nguyên văn câu chữ thể hiện cam kết")
    raw_event_id: str


class UnifiedTaskCandidate(BaseModel):
    id: str = Field(description="UUID v4 của unified_task")
    title: str = Field(description="Tiêu đề task được chuẩn hóa")
    description: Optional[str] = None
    status: TaskStatus = Field(default=TaskStatus.TODO)
    inferred_status: Optional[str] = Field(default=None, description="e.g. 'LIKELY_DONE'")
    
    owner_canonical_id: Optional[str] = Field(default=None, description="Người chịu trách nhiệm chính (canonical person ID)")
    owner_name: Optional[str] = Field(default=None, description="Tên hiển thị chuẩn hóa của owner")
    requester_canonical_id: Optional[str] = Field(default=None, description="Người yêu cầu")
    requester_name: Optional[str] = Field(default=None, description="Tên hiển thị chuẩn hóa của requester")
    
    project_key: Optional[str] = Field(default=None, description="e.g. 'OPS', 'CUSTOMER_A'")
    customer_id: Optional[str] = Field(default=None)
    
    due_date: Optional[datetime] = None
    explicit_deadline: bool = Field(default=False)
    priority_score: float = Field(default=0.0, ge=0.0, le=100.0)
    priority_override: Optional[float] = Field(default=None, description="Manual priority override from user")
    inferred_priority_score: Optional[float] = Field(default=None, description="Inferred priority score when override is active")
    status_authoritative: bool = Field(default=False, description="True if status was set manually by user or authoritative source")
    
    extraction_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    correlation_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    review_status: str = Field(default="auto_approved", description="'auto_approved', 'pending_review', 'rejected'")
    
    # Audit trail metadata from Correlation & Merge
    candidate_task_ids: List[str] = Field(default_factory=list, description="Danh sách task candidates được so sánh khi merge")
    winning_task_id: Optional[str] = Field(default=None, description="ID task đích nếu task này là kết quả merge")
    correlation_score: Optional[float] = Field(default=None, description="Correlation score khi merge")
    deterministic_anchors: List[str] = Field(default_factory=list, description="Các deterministic anchors khớp khi merge")
    merge_reason: Optional[str] = Field(default=None, description="Lý do chi tiết gộp task")
    merge_audit: Optional["MergeAuditRecord"] = Field(default=None, description="Chi tiết audit trail của lần merge gần nhất")

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    evidences: List[EvidenceRecord] = Field(default_factory=list)


class MergeAuditRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()), description="UUID v4 của merge audit record")
    candidate_task_ids: List[str] = Field(default_factory=list, description="Danh sách task candidates được so sánh")
    winning_task_id: str = Field(description="ID task đích được gộp vào")
    correlation_score: float = Field(default=0.0, description="Điểm correlation_confidence")
    deterministic_anchors: List[str] = Field(default_factory=list, description="Danh sách anchors khớp")
    merge_reason: str = Field(default="", description="Lý do chi tiết gộp task")
    merged_at: Optional[datetime] = Field(default=None, description="Thời điểm gộp task")

    # Compatibility aliases
    semantic_score: float = Field(default=0.0, description="Điểm tương đồng ngữ nghĩa")
    processor_version: str = Field(default="v1.1", description="Phiên bản processor")
    created_at: Optional[datetime] = None

    def model_post_init(self, __context: Any) -> None:
        now_val = datetime.now(timezone.utc)
        if self.created_at and not self.merged_at:
            object.__setattr__(self, "merged_at", self.created_at)
        elif self.merged_at and not self.created_at:
            object.__setattr__(self, "created_at", self.merged_at)
        elif not self.merged_at and not self.created_at:
            object.__setattr__(self, "merged_at", now_val)
            object.__setattr__(self, "created_at", now_val)


class StatusTransitionAuditRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()), description="UUID v4 của audit record")
    task_id: str
    old_status: TaskStatus
    new_status: TaskStatus
    change_actor: str = Field(default="SYSTEM", description="'SYSTEM' | 'USER'")
    timestamp: Optional[datetime] = Field(default=None, description="Thời điểm thay đổi trạng thái")
    reason: str

    # Compatibility aliases
    source_evidence_ids: List[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    changed_at: Optional[datetime] = None

    def model_post_init(self, __context: Any) -> None:
        now_val = datetime.now(timezone.utc)
        if self.changed_at and not self.timestamp:
            object.__setattr__(self, "timestamp", self.changed_at)
        elif self.timestamp and not self.changed_at:
            object.__setattr__(self, "changed_at", self.timestamp)
        elif not self.timestamp and not self.changed_at:
            object.__setattr__(self, "timestamp", now_val)
            object.__setattr__(self, "changed_at", now_val)


class ReviewQueueItem(BaseModel):
    id: str = Field(description="UUID của review item")
    raw_event_id: str
    candidate_task: UnifiedTaskCandidate
    reason: str = Field(description="Lý do cần người dùng xác nhận (e.g., 'Confidence trung bình 0.72', 'Attribution chưa chắc chắn')")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# Canonical Ontology Aliases
Evidence = EvidenceRecord
MergeAudit = MergeAuditRecord
StatusTransitionAudit = StatusTransitionAuditRecord

