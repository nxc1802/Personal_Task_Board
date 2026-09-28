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
    FAILED = "failed"
    SKIPPED = "skipped"


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
    created_at: Optional[datetime] = Field(default=None)


class IngestionCheckpointRecord(BaseModel):
    id: str = Field(description="UUID hoặc key của checkpoint")
    source_type: SourceType
    stream_id: str = Field(description="Định danh stream/channel/repo/session")
    tenant_id: str = Field(default="default")
    last_external_id: Optional[str] = None
    last_event_timestamp: Optional[datetime] = None
    cursor_token: Optional[str] = None
    updated_at: Optional[datetime] = None


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
