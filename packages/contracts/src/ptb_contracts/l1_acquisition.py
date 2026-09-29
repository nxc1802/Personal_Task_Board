from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


class SourceType(str, Enum):
    MS_TEAMS = "ms_teams"
    MS_OUTLOOK = "ms_outlook"
    MS_TEAMS_WEB = "ms_teams_web"
    MS_OUTLOOK_WEB = "ms_outlook_web"
    CODING_AGENT = "coding_agent"
    JIRA = "jira"
    SHORTCUT = "shortcut"
    CONFLUENCE = "confluence"
    GIT = "git"


class SourceSyncState(str, Enum):
    DISABLED = "DISABLED"
    UNCONFIGURED = "UNCONFIGURED"
    NEVER_SYNCED = "NEVER_SYNCED"
    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    ERROR = "ERROR"
    NOT_INSTALLED = "NOT_INSTALLED"


class AgentType(str, Enum):
    CURSOR = "cursor"
    CLAUDE_CODE = "claude_code"
    ANTIGRAVITY = "antigravity"
    CODEX = "codex"
    GITHUB_COPILOT = "github_copilot"
    WINDSURF = "windsurf"
    CONTINUE = "continue"
    AIDER = "aider"
    CLINE = "cline"
    ROO_CODE = "roo_code"


class ProcessingStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    RETRY = "retry"
    FAILED = "failed"
    SKIPPED = "skipped"
    # Backwards compatibility / uppercase aliases
    pending = "pending"
    processing = "processing"
    processed = "processed"
    retry = "retry"
    failed = "failed"
    skipped = "skipped"


class RawAgentSessionRecord(BaseModel):
    session_id: str = Field(description="Định danh phiên làm việc của Coding Agent")
    agent_type: AgentType = Field(description="Loại coding agent (cursor, claude_code, antigravity)")
    workspace_path: str = Field(description="Đường dẫn thư mục project đang làm việc")
    turn_index: int = Field(description="Thứ tự turn hội thoại")
    message_role: str = Field(description="Vai trò tin nhắn ('user', 'assistant', 'tool_call', 'tool_result')")
    content: str = Field(description="Nội dung trao đổi hoặc code plan")
    tool_invocations: Optional[list[Dict[str, Any]]] = Field(default=None, description="Danh sách tool calls")
    timestamp: datetime = Field(description="Thời điểm phát sinh turn")
    idempotency_key: str = Field(description="Hash duy nhất: SHA256(session_id + turn_index + message_role)")


class RawEventRecord(BaseModel):
    id: str = Field(description="UUID v4 của raw event trong Neo4j Ingestion Journal")
    tenant_id: str = Field(description="Định danh tenant (e.g., 'tenant-fpt-internal', 'client-tenant', 'local-user')")
    source_type: SourceType = Field(description="Loại nguồn dữ liệu")
    external_id: str = Field(description="ID gốc từ hệ thống ngoại vi (message ID, ticket ID)")
    parent_external_id: Optional[str] = Field(default=None, description="ID của thread hoặc parent ticket nếu có")
    idempotency_key: str = Field(description="Hash duy nhất: SHA256(tenant_id + source_type + external_id + payload_hash)")
    
    event_timestamp: datetime = Field(description="Thời gian phát sinh event tại nguồn")
    captured_at: Optional[datetime] = Field(default=None, description="Thời điểm ingest vào hệ thống")
    author_external_id: str = Field(description="ID người gửi tại nguồn (email, accountId, AAD ObjectId)")
    author_display_name: Optional[str] = Field(default=None, description="Tên hiển thị tại nguồn")
    conversation_or_project_id: str = Field(description="Channel ID, Chat ID, hoặc Project Key")
    deep_link: Optional[str] = Field(default=None, description="URL dẫn thẳng đến tin nhắn/ticket gốc")
    
    raw_payload: Dict[str, Any] = Field(default_factory=dict, description="Toàn bộ JSON response nhận từ Source API hoặc Playwright")
    payload_json: Optional[str] = Field(default=None, description="Chuỗi JSON serialized của raw_payload để lưu vào Neo4j property")
    normalized_text: Optional[str] = Field(default=None, description="Nội dung văn bản trích xuất đã chuẩn hóa")
    content_hash: Optional[str] = Field(default=None, description="SHA256 của nội dung văn bản chuẩn hóa")
    
    processing_status: ProcessingStatus = Field(default=ProcessingStatus.PENDING)
    retry_count: int = Field(default=0)
    last_error: Optional[str] = Field(default=None)
    processing_attempt_count: int = Field(default=0, description="Số lần đã thử xử lý event này")
    last_processing_error: Optional[str] = Field(default=None, description="Lỗi gần nhất trong quá trình processing")
    next_retry_at: Optional[datetime] = Field(default=None, description="Thời điểm được phép retry tiếp theo")
    processed_at: Optional[datetime] = Field(default=None, description="Thời điểm hoàn tất xử lý thành công")
    processor_version: Optional[str] = Field(default=None, description="Phiên bản pipeline/LLM xử lý event")
    created_at: Optional[datetime] = Field(default=None)

    def model_post_init(self, __context: Any) -> None:
        if self.retry_count and not self.processing_attempt_count:
            object.__setattr__(self, "processing_attempt_count", self.retry_count)
        elif self.processing_attempt_count and not self.retry_count:
            object.__setattr__(self, "retry_count", self.processing_attempt_count)
        if self.last_error and not self.last_processing_error:
            object.__setattr__(self, "last_processing_error", self.last_error)
        elif self.last_processing_error and not self.last_error:
            object.__setattr__(self, "last_error", self.last_processing_error)


class IngestionCheckpointRecord(BaseModel):
    id: Optional[str] = Field(default=None, description="UUID hoặc deterministic key của checkpoint")
    tenant_id: str = Field(default="default", description="Định danh tenant")
    source_type: SourceType = Field(description="Loại nguồn dữ liệu")
    stream_id: str = Field(description="Định danh stream/channel/repo/session")
    last_external_id: Optional[str] = None
    last_event_timestamp: Optional[datetime] = None
    cursor_token: Optional[str] = None
    updated_at: Optional[datetime] = None

    def model_post_init(self, __context: Any) -> None:
        if not self.id:
            st_val = self.source_type.value if hasattr(self.source_type, "value") else str(self.source_type)
            object.__setattr__(self, "id", f"{self.tenant_id}:{st_val}:{self.stream_id}")


# Canonical Ontology Aliases
IngestionCheckpoint = IngestionCheckpointRecord


class ProcessingAttemptRecord(BaseModel):
    id: str = Field(description="UUID v4 của processing attempt")
    raw_event_id: str
    attempt_number: int = 1
    status: ProcessingStatus
    error_message: Optional[str] = None
    attempted_at: Optional[datetime] = None


class SourceConnectionConfig(BaseModel):
    id: str
    user_id: str
    tenant_id: str
    source_type: SourceType
    auth_type: str = Field(default="oauth2", description="'oauth2' hoặc 'api_token'")
    encrypted_credentials: str
    token_expires_at: Optional[datetime] = None
    is_active: bool = True


class SyncCheckpointState(BaseModel):
    id: str
    connection_id: str
    sync_mode: str = Field(description="'initial_backfill' hoặc 'incremental'")
    page_token: Optional[str] = None
    delta_token: Optional[str] = None
    last_event_timestamp: Optional[datetime] = None
    last_successful_sync_at: Optional[datetime] = None
    status: str = Field(default="healthy", description="'healthy', 'syncing', 'error'")
    error_message: Optional[str] = None
