from datetime import datetime, timezone
from enum import Enum
import hashlib
import logging
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger("ptb.contracts.l1_acquisition")


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
    GITHUB = "github"
    SLACK = "slack"

    @classmethod
    def canonical_channels(cls) -> List["SourceType"]:
        """Danh sách 8 kênh adapter chuẩn của PTB v1."""
        return [
            cls.MS_TEAMS,
            cls.MS_OUTLOOK,
            cls.CODING_AGENT,
            cls.JIRA,
            cls.SHORTCUT,
            cls.CONFLUENCE,
            cls.GITHUB,
            cls.SLACK,
        ]

    def is_web_scraper(self) -> bool:
        return self in (SourceType.MS_TEAMS_WEB, SourceType.MS_OUTLOOK_WEB)

    def is_direct_api(self) -> bool:
        return not self.is_web_scraper()


class SourceSyncState(str, Enum):
    DISABLED = "DISABLED"
    UNCONFIGURED = "UNCONFIGURED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    NEVER_SYNCED = "NEVER_SYNCED"
    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    STANDBY = "STANDBY"
    ERROR = "ERROR"
    NOT_INSTALLED = "NOT_INSTALLED"
    # Backwards compatibility / lowercase aliases
    disabled = "DISABLED"
    unconfigured = "UNCONFIGURED"
    not_configured = "NOT_CONFIGURED"
    never_synced = "NEVER_SYNCED"
    starting = "STARTING"
    healthy = "HEALTHY"
    degraded = "DEGRADED"
    auth_required = "AUTH_REQUIRED"
    standby = "STANDBY"
    error = "ERROR"
    not_installed = "NOT_INSTALLED"


class EventAction(str, Enum):
    """Hành động của sự kiện tại nguồn (hỗ trợ version/update/delete handling)."""
    UPSERT = "upsert"
    UPDATE = "update"
    DELETE = "delete"
    ARCHIVE = "archive"


class AdapterHealthStatus(str, Enum):
    """Trạng thái hoạt động chuẩn hóa cho adapter health check."""
    HEALTHY = "healthy"
    UNCONFIGURED = "unconfigured"
    NOT_CONFIGURED = "not_configured"
    DEGRADED = "degraded"
    AUTH_REQUIRED = "auth_required"
    STANDBY = "standby"
    ERROR = "error"


class StreamDiscoveryRecord(BaseModel):
    """Hợp đồng mô tả stream/kênh/project được phát hiện từ nguồn."""
    stream_id: str = Field(description="Định danh duy nhất của stream trong phạm vi adapter")
    name: str = Field(description="Tên hiển thị người dùng của stream")
    source_type: Optional[SourceType] = Field(default=None, description="Loại nguồn của stream")
    description: Optional[str] = Field(default=None, description="Mô tả chi tiết stream")
    is_active: bool = Field(default=True, description="Trạng thái stream đang kích hoạt hay không")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Metadata bổ sung từ external API")


class AdapterHealthReport(BaseModel):
    """Báo cáo tình trạng sức khỏe adapter chuẩn hóa."""
    source_type: SourceType = Field(description="Loại nguồn của adapter")
    tenant_id: str = Field(description="Định danh tenant")
    configured: bool = Field(description="Adapter đã được cung cấp cấu hình đầy đủ chưa")
    status: Union[AdapterHealthStatus, str] = Field(description="Trạng thái hoạt động hiện tại")
    message: Optional[str] = Field(default=None, description="Thông điệp mô tả trạng thái")
    error: Optional[str] = Field(default=None, description="Chi tiết lỗi nếu có")
    details: Dict[str, Any] = Field(default_factory=dict, description="Thông tin cấu hình/chỉ số vận hành")
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Thời điểm kiểm tra UTC")

    def to_dict(self) -> Dict[str, Any]:
        """Chuyển đổi thành Dict tương thích ngược với API."""
        st_val = self.source_type.value if hasattr(self.source_type, "value") else str(self.source_type)
        stat_val = self.status.value if hasattr(self.status, "value") else str(self.status)
        result: Dict[str, Any] = {
            "source_type": st_val,
            "tenant_id": self.tenant_id,
            "configured": self.configured,
            "status": stat_val,
            "checked_at": self.checked_at.isoformat(),
            **self.details,
        }
        if self.message:
            result["message"] = self.message
        if self.error:
            result["error"] = self.error
        return result


class RetryPolicyConfig(BaseModel):
    """Cấu hình chính sách retry/exponential backoff cho adapter."""
    max_retries: int = Field(default=3, description="Số lần retry tối đa khi gặp lỗi tạm thời")
    initial_delay_seconds: float = Field(default=1.0, description="Độ trễ khởi đầu (giây)")
    backoff_multiplier: float = Field(default=2.0, description="Hệ số nhân exponential backoff")
    max_delay_seconds: float = Field(default=60.0, description="Độ trễ tối đa giữa các lần retry")
    jitter: bool = Field(default=True, description="Bổ sung jitter ngẫu nhiên tránh thundering herd")


class RateLimitConfig(BaseModel):
    """Cấu hình rate limiting chuẩn cho adapter."""
    requests_per_minute: int = Field(default=60, description="Giới hạn số request mỗi phút")
    burst_limit: int = Field(default=10, description="Giới hạn burst requests")
    retry_after_header: str = Field(default="Retry-After", description="Tên header HTTP chứa thời gian chờ")


# --- Canonical Invariant & Hash Helpers ---

def ensure_utc(dt: Optional[Union[datetime, str]]) -> Optional[datetime]:
    """Đảm bảo datetime luôn có timezone UTC chuẩn hóa."""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            cleaned = dt.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(cleaned)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception as e:
            logger.warning("Không thể parse ISO datetime '%s': %s", dt, e)
            return datetime.now(timezone.utc)
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def compute_content_hash(text: Optional[str]) -> str:
    """Tính SHA-256 hash chuẩn hóa cho nội dung văn bản (Normalized Content Hash)."""
    raw_bytes = (text or "").strip().encode("utf-8")
    return hashlib.sha256(raw_bytes).hexdigest()


def compute_idempotency_key(
    tenant_id: str,
    source_type: Union[str, SourceType],
    external_id: str,
    content_hash: Optional[str] = None,
    stream_id: Optional[str] = None,
) -> str:
    """Tính Idempotency Key chuẩn hóa deterministic: SHA256(tenant_id:source_type:stream_id:external_id:content_hash)."""
    tid_val = str(tenant_id).strip() if (tenant_id is not None and str(tenant_id).strip() != "") else "default"
    st_val = source_type.value if hasattr(source_type, "value") else str(source_type)
    stream_val = str(stream_id or "").strip()
    ext_val = str(external_id).strip()
    chash_val = str(content_hash or "").strip()[:16] if content_hash else ""
    raw = f"{tid_val}:{st_val}:{stream_val}:{ext_val}:{chash_val}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def compute_checkpoint_id(
    tenant_id: str,
    source_type: Union[str, SourceType],
    stream_id: str,
) -> str:
    """Tính Checkpoint ID deterministic: SHA256(tenant_id:source_type:stream_id)."""
    tid_val = str(tenant_id).strip() if (tenant_id is not None and str(tenant_id).strip() != "") else "default"
    st_val = source_type.value if hasattr(source_type, "value") else str(source_type)
    stream_val = str(stream_id).strip()
    raw = f"{tid_val}:{st_val}:{stream_val}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


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

    @model_validator(mode="before")
    @classmethod
    def preprocess_raw_event(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Probe 01: Fallback null/missing event_timestamp to captured_at or UTC now
            if data.get("event_timestamp") is None:
                data["event_timestamp"] = data.get("captured_at") or datetime.now(timezone.utc)
            # Probe 05: Unrecognized source_type fallback to generic SourceType.CODING_AGENT
            st = data.get("source_type")
            if isinstance(st, str):
                valid_vals = {item.value for item in SourceType}
                if st not in valid_vals:
                    data["source_type"] = SourceType.CODING_AGENT
            elif st is None:
                data["source_type"] = SourceType.CODING_AGENT
            # Handle probe typo or missing conversation_or_project_id
            if "conversation_or_project_id=" in data and "conversation_or_project_id" not in data:
                data["conversation_or_project_id"] = data.pop("conversation_or_project_id=")
            if not data.get("conversation_or_project_id"):
                data["conversation_or_project_id"] = "default"
            # Author fallback
            if data.get("author_external_id") is None:
                data["author_external_id"] = "system-anonymous"
            # Idempotency key fallback
            if not data.get("idempotency_key"):
                tid = str(data.get("tenant_id") or "default")
                st_val = data.get("source_type")
                ext_id = str(data.get("external_id") or "")
                data["idempotency_key"] = compute_idempotency_key(tid, st_val, ext_id)
        return data

    def model_post_init(self, __context: Any) -> None:
        if self.retry_count and not self.processing_attempt_count:
            object.__setattr__(self, "processing_attempt_count", self.retry_count)
        elif self.processing_attempt_count and not self.retry_count:
            object.__setattr__(self, "retry_count", self.processing_attempt_count)
        if self.last_error and not self.last_processing_error:
            object.__setattr__(self, "last_processing_error", self.last_error)
        elif self.last_processing_error and not self.last_error:
            object.__setattr__(self, "last_error", self.last_processing_error)

    @property
    def stream_id(self) -> str:
        """Alias for conversation_or_project_id."""
        return self.conversation_or_project_id


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
            object.__setattr__(
                self,
                "id",
                compute_checkpoint_id(self.tenant_id, self.source_type, self.stream_id),
            )
        if self.last_event_timestamp is not None:
            object.__setattr__(self, "last_event_timestamp", ensure_utc(self.last_event_timestamp))
        if self.updated_at is not None:
            object.__setattr__(self, "updated_at", ensure_utc(self.updated_at))


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
