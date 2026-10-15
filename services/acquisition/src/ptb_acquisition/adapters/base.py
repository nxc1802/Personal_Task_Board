"""Acquisition Adapter Base Interface.

Định nghĩa hợp đồng chuẩn (Canonical Contract) cho toàn bộ các source adapters trong hệ thống
Personal Task Board v1 theo docs/v1.md và Task W1-03:
- Adapter lifecycle methods (discover, backfill, poll_incremental, health)
- Idempotency & content hash helpers
- Multi-tenant isolation enforcement
- Standard retry policy & rate limiting helpers
- Standard error hierarchy for acquisition failures
"""

from abc import ABC, abstractmethod
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import random
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple, Type, Union
import uuid

from ptb_contracts.l1_acquisition import (
    AdapterHealthReport,
    AdapterHealthStatus,
    IngestionCheckpointRecord,
    ProcessingStatus,
    RateLimitConfig,
    RawEventRecord,
    RetryPolicyConfig,
    SourceSyncState,
    SourceType,
    compute_checkpoint_id,
    compute_content_hash,
    compute_idempotency_key,
    ensure_utc,
)

logger = logging.getLogger("ptb.acquisition.adapters.base")


# =============================================================================
# STANDARD ACQUISITION ERROR HIERARCHY
# =============================================================================

class AcquisitionError(Exception):
    """Lớp cơ sở cho toàn bộ ngoại lệ phát sinh trong quá trình acquisition."""

    def __init__(
        self,
        message: str,
        source_type: Optional[SourceType] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.source_type = source_type
        self.details = details or {}

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}: {self.message} (source={self.source_type})>"


class AdapterAuthenticationError(AcquisitionError):
    """Lỗi xác thực hoặc phân quyền (401 Unauthorized, 403 Forbidden, token hết hạn, thiếu session/cookie)."""
    pass


class AdapterRateLimitError(AcquisitionError):
    """Lỗi vượt ngưỡng tần suất gọi (HTTP 429 Too Many Requests)."""

    def __init__(
        self,
        message: str,
        retry_after_seconds: Optional[float] = None,
        source_type: Optional[SourceType] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, source_type=source_type, details=details)
        self.retry_after_seconds = retry_after_seconds


class AdapterTransientError(AcquisitionError):
    """Lỗi tạm thời từ mạng hoặc máy chủ ngoại vi (HTTP 5xx, timeout, socket reset, ECONNRESET)."""
    pass


class AdapterConfigurationError(AcquisitionError):
    """Lỗi cấu hình adapter (thiếu URL, token, hoặc tham số bắt buộc khi khởi tạo)."""
    pass


class AdapterStreamNotFoundError(AcquisitionError):
    """Stream, kênh, project hoặc mailbox chỉ định không tồn tại tại nguồn ngoại vi."""
    pass


# =============================================================================
# CANONICAL ACQUISITION ADAPTER INTERFACE
# =============================================================================

class AcquisitionAdapter(ABC):
    """Hợp đồng trừu tượng cơ sở chuẩn hóa cho toàn bộ Acquisition Adapters."""

    def __init__(
        self,
        source_type: SourceType,
        tenant_id: str = "local-user",
        retry_policy: Optional[RetryPolicyConfig] = None,
        rate_limit_config: Optional[RateLimitConfig] = None,
    ) -> None:
        self.source_type = source_type
        self.tenant_id = tenant_id if (tenant_id is not None and str(tenant_id).strip() != "") else "default"
        self.retry_policy = retry_policy or RetryPolicyConfig()
        self.rate_limit_config = rate_limit_config or RateLimitConfig()

    # -------------------------------------------------------------------------
    # Lifecycle Abstract Methods
    # -------------------------------------------------------------------------

    @abstractmethod
    async def discover(self) -> List[Dict[str, Any]]:
        """Liệt kê các streams, channels, workspaces hoặc project keys có sẵn tại nguồn.
        
        Trả về danh sách các dict chứa tối thiểu:
        - 'stream_id': định danh duy nhất của stream
        - 'name': tên hiển thị của stream
        """
        pass

    @abstractmethod
    async def backfill(
        self, stream_id: str, since: Optional[datetime] = None
    ) -> AsyncIterator[RawEventRecord]:
        """Thu thập dữ liệu lịch sử/quá khứ từ stream_id chỉ định từ mốc thời gian since.
        
        Mỗi event emit ra phải là RawEventRecord chuẩn hóa và đảm bảo tính bất biến:
        - tenant_id phải trùng khớp với adapter.tenant_id
        - source_type phải trùng khớp với adapter.source_type
        - event_timestamp phải là timezone UTC
        - content_hash và idempotency_key phải được tính toán deterministic
        """
        pass

    @abstractmethod
    async def poll_incremental(
        self, stream_id: str, checkpoint: Optional[IngestionCheckpointRecord] = None
    ) -> AsyncIterator[RawEventRecord]:
        """Thu thập dữ liệu mới phát sinh kể từ checkpoint / cursor.
        
        Nếu checkpoint là None, adapter có thể chuyển tiếp sang backfill hoặc thu thập từ mốc mặc định.
        Re-fetching cùng một dữ liệu ngoại vi bắt buộc phải sinh ra cùng idempotency_key
        để tầng lưu trữ bền vững (Neo4j MERGE) hoặc pipeline deduplication loại bỏ 100% trùng lặp.
        """
        pass

    @abstractmethod
    async def health(self) -> Dict[str, Any]:
        """Trả về báo cáo tình trạng hoạt động của adapter.
        
        Bắt buộc chứa:
        - 'source_type': chuỗi string loại nguồn
        - 'tenant_id': định danh tenant
        - 'configured': boolean (True nếu đủ cấu hình/credentials, False nếu ở chế độ standby)
        - 'status': giá trị thuộc AdapterHealthStatus hoặc SourceSyncState
        """
        pass

    # -------------------------------------------------------------------------
    # Concrete Lifecycle Helpers & Invariant Guarantees
    # -------------------------------------------------------------------------

    async def poll_with_cursor(
        self,
        stream_id: str,
        cursor: Optional[IngestionCheckpointRecord] = None,
    ) -> AsyncIterator[RawEventRecord]:
        """Alias thuận tiện cho poll_incremental nhận cursor token hoặc IngestionCheckpointRecord."""
        async for record in self.poll_incremental(stream_id, checkpoint=cursor):
            yield record

    def compute_content_hash(self, text: Optional[str]) -> str:
        """Tính SHA-256 hash chuẩn hóa cho nội dung văn bản."""
        return compute_content_hash(text)

    def compute_idempotency_key(
        self,
        external_id: str,
        content_hash: Optional[str] = None,
        stream_id: Optional[str] = None,
    ) -> str:
        """Tính Idempotency Key deterministic chuẩn hóa gắn chặt với tenant_id và source_type của adapter."""
        return compute_idempotency_key(
            tenant_id=self.tenant_id,
            source_type=self.source_type,
            external_id=external_id,
            content_hash=content_hash,
            stream_id=stream_id,
        )

    def compute_checkpoint_id(self, stream_id: str) -> str:
        """Tính Checkpoint ID deterministic cho một stream_id cụ thể của adapter."""
        return compute_checkpoint_id(
            tenant_id=self.tenant_id,
            source_type=self.source_type,
            stream_id=stream_id,
        )

    def build_raw_event(
        self,
        external_id: str,
        content: str,
        author_id: str,
        stream_id: str,
        event_timestamp: Optional[datetime] = None,
        deep_link: Optional[str] = None,
        raw_payload: Optional[Dict[str, Any]] = None,
        parent_external_id: Optional[str] = None,
        author_name: Optional[str] = None,
    ) -> RawEventRecord:
        """Hàm dựng chuẩn hóa RawEventRecord đảm bảo toàn bộ invariants của PTB v1."""
        now_utc = datetime.now(timezone.utc)
        ev_ts = ensure_utc(event_timestamp) or now_utc
        norm_text = content.strip()
        chash = self.compute_content_hash(norm_text)
        ikey = self.compute_idempotency_key(
            external_id=external_id,
            content_hash=chash,
            stream_id=stream_id,
        )
        payload = raw_payload or {}

        return RawEventRecord(
            id=f"raw-{self.source_type.value}-{uuid.uuid4().hex[:12]}",
            tenant_id=self.tenant_id,
            source_type=self.source_type,
            external_id=str(external_id),
            parent_external_id=str(parent_external_id) if parent_external_id else None,
            idempotency_key=ikey,
            event_timestamp=ev_ts,
            captured_at=now_utc,
            author_external_id=str(author_id),
            author_display_name=str(author_name) if author_name else None,
            conversation_or_project_id=str(stream_id),
            deep_link=str(deep_link) if deep_link else None,
            raw_payload=payload,
            payload_json=json.dumps(payload, default=str),
            normalized_text=norm_text,
            content_hash=chash,
            processing_status=ProcessingStatus.PENDING,
            created_at=now_utc,
        )

    def validate_record(self, record: RawEventRecord) -> bool:
        """Xác thực bảo vệ Multi-Tenant Isolation và tính hợp lệ của RawEventRecord."""
        if record.tenant_id != self.tenant_id:
            logger.error(
                "Phát hiện rò rỉ tenant: record.tenant_id '%s' != adapter.tenant_id '%s'",
                record.tenant_id,
                self.tenant_id,
            )
            return False

        if record.source_type != self.source_type:
            logger.error(
                "Sai lệch source_type: record.source_type '%s' != adapter.source_type '%s'",
                record.source_type,
                self.source_type,
            )
            return False

        if not record.external_id:
            logger.error("RawEventRecord thiếu external_id bắt buộc")
            return False

        if not record.idempotency_key:
            logger.error("RawEventRecord thiếu idempotency_key bắt buộc")
            return False

        return True

    # -------------------------------------------------------------------------
    # Resilience & Rate Limiting Helpers
    # -------------------------------------------------------------------------

    async def execute_with_retry(
        self,
        async_fn: Callable[..., Any],
        *args: Any,
        retry_policy: Optional[RetryPolicyConfig] = None,
        retryable_exceptions: Optional[Tuple[Type[Exception], ...]] = None,
        **kwargs: Any,
    ) -> Any:
        """Thực thi một async callable với cơ chế Exponential Backoff & Random Jitter."""
        policy = retry_policy or self.retry_policy
        exceptions_to_retry = retryable_exceptions or (
            AdapterTransientError,
            ConnectionError,
            TimeoutError,
            asyncio.TimeoutError,
        )

        attempt = 0
        current_delay = policy.initial_delay_seconds

        while True:
            attempt += 1
            try:
                return await async_fn(*args, **kwargs)
            except exceptions_to_retry as exc:
                if attempt > policy.max_retries:
                    logger.error(
                        "Đã vượt quá số lần retry (%d/%d) cho %s: %s",
                        attempt - 1,
                        policy.max_retries,
                        getattr(async_fn, "__name__", str(async_fn)),
                        exc,
                    )
                    raise

                jitter_delta = random.uniform(0, 0.5 * current_delay) if policy.jitter else 0.0
                delay = min(current_delay + jitter_delta, policy.max_delay_seconds)
                logger.warning(
                    "Lỗi tạm thời khi gọi %s (lần %d/%d): %s. Đang chờ %.2fs trước khi thử lại...",
                    getattr(async_fn, "__name__", str(async_fn)),
                    attempt,
                    policy.max_retries,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                current_delay = min(current_delay * policy.backoff_multiplier, policy.max_delay_seconds)

    async def handle_rate_limit(self, retry_after: Optional[float] = None) -> None:
        """Xử lý giới hạn tốc độ (HTTP 429) theo chuẩn."""
        wait_seconds = retry_after if retry_after is not None else 5.0
        logger.warning(
            "Adapter %s bị rate limit (429). Cần tạm dừng %.1f giây...",
            self.source_type.value,
            wait_seconds,
        )
        if wait_seconds > self.rate_limit_config.burst_limit * 6:
            raise AdapterRateLimitError(
                f"Rate limit quá lớn ({wait_seconds}s), tạm dừng adapter để tránh tắc nghẽn",
                retry_after_seconds=wait_seconds,
                source_type=self.source_type,
            )
        await asyncio.sleep(wait_seconds)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}(source_type={self.source_type}, tenant_id={self.tenant_id})>"
