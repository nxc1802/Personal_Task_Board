from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    LIKELY_DONE = "likely_done"
    DONE = "done"
    BLOCKED = "blocked"
    DISMISSED = "dismissed"


class EvidenceType(str, Enum):
    CHAT_COMMITMENT = "chat_commitment"
    CHAT_REQUEST = "chat_request"
    JIRA_TICKET = "jira_ticket"
    SHORTCUT_STORY = "shortcut_story"
    EMAIL_THREAD = "email_thread"
    COMPLETION_SIGNAL = "completion_signal"


class ParsedMessageContent(BaseModel):
    is_quote_reply: bool = Field(default=False)
    quoted_author_raw: Optional[str] = Field(default=None)
    quoted_content_text: Optional[str] = Field(default=None)
    actual_content_text: str = Field(description="Nội dung thực tế của người gửi hiện tại")


class EvidenceRecord(BaseModel):
    id: str = Field(description="UUID v4 của evidence")
    task_id: Optional[str] = Field(default=None, description="ID của task liên kết")
    raw_event_id: str = Field(description="Tham chiếu đến raw_events.id")
    evidence_type: EvidenceType
    source_type: str
    external_url: Optional[str] = None
    author_canonical_id: str = Field(description="ID người gửi theo bảng people")
    timestamp: datetime
    snippet: str = Field(description="Đoạn trích văn bản làm bằng chứng")
    confidence: float = Field(ge=0.0, le=1.0)
    extraction_version: str = Field(default="v1.0")


class ExtractedCommitment(BaseModel):
    title: str = Field(description="Mô tả ngắn gọn về hành động hoặc cam kết")
    owner_id: str = Field(description="Canonical person ID của người cam kết thực hiện")
    requester_id: Optional[str] = Field(default=None, description="Canonical person ID của người yêu cầu")
    project_key: Optional[str] = Field(default=None)
    due_date: Optional[datetime] = None
    explicit_deadline: bool = Field(default=False)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_snippet: str = Field(description="Nguyên văn câu chữ thể hiện cam kết")
    raw_event_id: str


class UnifiedTaskCandidate(BaseModel):
    id: str = Field(description="UUID v4 của unified_task")
    title: str = Field(description="Tiêu đề task được chuẩn hóa")
    description: Optional[str] = None
    status: TaskStatus = Field(default=TaskStatus.OPEN)
    
    owner_canonical_id: str = Field(description="Người chịu trách nhiệm chính (canonical person ID)")
    requester_canonical_id: Optional[str] = Field(default=None, description="Người yêu cầu")
    
    project_key: Optional[str] = Field(default=None, description="e.g. 'OPS', 'CUSTOMER_A'")
    customer_id: Optional[str] = Field(default=None)
    
    due_date: Optional[datetime] = None
    explicit_deadline: bool = Field(default=False)
    
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    review_status: str = Field(default="auto_approved", description="'auto_approved', 'pending_review', 'rejected'")
    
    evidences: List[EvidenceRecord] = Field(default_factory=list)


class ReviewQueueItem(BaseModel):
    id: str = Field(description="UUID của review item")
    raw_event_id: str
    candidate_task: UnifiedTaskCandidate
    reason: str = Field(description="Lý do cần người dùng xác nhận (e.g., 'Confidence trung bình 0.72', 'Attribution chưa chắc chắn')")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
