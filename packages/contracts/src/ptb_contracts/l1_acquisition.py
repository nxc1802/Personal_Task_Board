from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


class SourceType(str, Enum):
    MS_TEAMS = "ms_teams"
    MS_OUTLOOK = "ms_outlook"
    JIRA = "jira"
    SHORTCUT = "shortcut"
    CONFLUENCE = "confluence"


class ProcessingStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
    SKIPPED = "skipped"


class RawEventRecord(BaseModel):
    id: str = Field(description="UUID v4 của raw event trong Supabase")
    tenant_id: str = Field(description="Định danh tenant (e.g., 'tenant-fpt-internal', 'client-tenant')")
    source_type: SourceType = Field(description="Loại nguồn dữ liệu")
    external_id: str = Field(description="ID gốc từ hệ thống ngoại vi (message ID, ticket ID)")
    parent_external_id: Optional[str] = Field(default=None, description="ID của thread hoặc parent ticket nếu có")
    idempotency_key: str = Field(description="Hash duy nhất: SHA256(tenant_id + source_type + external_id + payload_hash)")
    
    event_timestamp: datetime = Field(description="Thời gian phát sinh event tại nguồn")
    author_external_id: str = Field(description="ID người gửi tại nguồn (email, accountId, AAD ObjectId)")
    author_display_name: Optional[str] = Field(default=None, description="Tên hiển thị tại nguồn")
    conversation_or_project_id: str = Field(description="Channel ID, Chat ID, hoặc Project Key")
    deep_link: Optional[str] = Field(default=None, description="URL dẫn thẳng đến tin nhắn/ticket gốc")
    
    raw_payload: Dict[str, Any] = Field(description="Toàn bộ JSON response nhận từ Source API")
    
    processing_status: ProcessingStatus = Field(default=ProcessingStatus.PENDING)
    retry_count: int = Field(default=0)
    last_error: Optional[str] = Field(default=None)
    created_at: Optional[datetime] = Field(default=None)


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
