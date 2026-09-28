"""Acquisition Pipeline Runtime.

Điều phối luồng nạp dữ liệu từ IngestionQueue hoặc AcquisitionAdapter,
hỗ trợ cắm RawEventRepository và CheckpointRepository để lưu trữ bền bỉ vào Neo4j,
và tự động resume checkpoint khi adapter khởi động.
"""

import asyncio
from datetime import datetime, timezone
import hashlib
import inspect
import json
import logging
from typing import Any, Dict, List, Optional, Protocol, Set, Union, runtime_checkable
import uuid

from ptb_contracts import (
    IngestionCheckpointRecord,
    RawEventRecord,
    SourceType,
)
from ptb_acquisition.adapters.base import AcquisitionAdapter
from ptb_acquisition.queue import LocalIngestionQueue

logger = logging.getLogger("ptb.acquisition.pipeline")


@runtime_checkable
class RawEventRepositoryProtocol(Protocol):
    """Protocol định nghĩa interface của RawEventRepository lưu vào Neo4j."""

    async def persist_raw_event(self, record: RawEventRecord) -> Any:
        ...

    async def save_raw_event(self, record: RawEventRecord) -> bool:
        ...


@runtime_checkable
class CheckpointRepositoryProtocol(Protocol):
    """Protocol định nghĩa interface của CheckpointRepository lưu vào Neo4j."""

    async def get_checkpoint(
        self, source_type: str, stream_id: str
    ) -> Optional[IngestionCheckpointRecord]:
        ...

    async def save_checkpoint(
        self, checkpoint: IngestionCheckpointRecord
    ) -> None:
        ...


class InMemoryRawEventRepository:
    """Kho lưu trữ RawEventRecord trong bộ nhớ (phục vụ testing và fallback)."""

    def __init__(self):
        self._events: Dict[str, RawEventRecord] = {}

    async def save_raw_event(self, record: RawEventRecord) -> bool:
        self._events[record.id] = record
        return True

    async def persist_raw_event(self, record: RawEventRecord) -> str:
        self._events[record.id] = record
        return record.id

    async def get_by_id(self, event_id: str) -> Optional[RawEventRecord]:
        return self._events.get(event_id)

    async def get_all(self) -> List[RawEventRecord]:
        return list(self._events.values())

    async def get_pending_raw_events(self, limit: int = 50) -> List[RawEventRecord]:
        from ptb_contracts.l1_acquisition import ProcessingStatus
        res = []
        for ev in self._events.values():
            if getattr(ev, "processing_status", None) in (ProcessingStatus.PENDING, ProcessingStatus.RETRY):
                res.append(ev)
                if len(res) >= limit:
                    break
        return res

    async def mark_event_status(self, event_id: str, status: Any, **kwargs) -> bool:
        if event_id in self._events:
            ev = self._events[event_id]
            try:
                object.__setattr__(ev, "processing_status", status)
            except Exception:
                pass
            return True
        return False

    async def record_processing_attempt(self, attempt_record: Any) -> bool:
        return True

    @property
    def count(self) -> int:
        return len(self._events)


class InMemoryCheckpointRepository:
    """Kho lưu trữ IngestionCheckpointRecord trong bộ nhớ (phục vụ testing và fallback)."""

    def __init__(self):
        self._checkpoints: Dict[str, IngestionCheckpointRecord] = {}

    def _make_key(self, source_type: Any, stream_id: str) -> str:
        s_val = source_type.value if hasattr(source_type, "value") else str(source_type)
        return f"{s_val}:{stream_id}"

    async def get_checkpoint(
        self, source_type: str, stream_id: str
    ) -> Optional[IngestionCheckpointRecord]:
        key = self._make_key(source_type, stream_id)
        return self._checkpoints.get(key)

    async def save_checkpoint(
        self, checkpoint: IngestionCheckpointRecord
    ) -> None:
        key = self._make_key(checkpoint.source_type, checkpoint.stream_id)
        self._checkpoints[key] = checkpoint


class AcquisitionPipeline:
    """Pipeline quản lý quá trình thu nạp, lưu trữ và checkpoint cho L1 Acquisition."""

    def __init__(
        self,
        queue: Optional[LocalIngestionQueue] = None,
        raw_event_repo: Optional[Any] = None,
        checkpoint_repo: Optional[Any] = None,
        adapters: Optional[Dict[str, AcquisitionAdapter]] = None,
        tenant_id: str = "local-user",
    ):
        self.queue = queue
        self.raw_event_repo = raw_event_repo
        self.checkpoint_repo = checkpoint_repo
        self.adapters: Dict[str, AcquisitionAdapter] = adapters or {}
        self.tenant_id = tenant_id
        self._internal_seen_keys: Set[str] = set()

        self._stats: Dict[str, int] = {
            "ingested": 0,
            "deduplicated": 0,
            "persisted": 0,
            "errors": 0,
        }

    @property
    def stats(self) -> Dict[str, int]:
        return dict(self._stats)

    def register_adapter(self, name: str, adapter: AcquisitionAdapter) -> None:
        """Đăng ký adapter vào pipeline."""
        self.adapters[name] = adapter
        logger.info(f"Đã đăng ký adapter '{name}' ({adapter.source_type})")

    async def get_checkpoint(
        self, source_type: Any, stream_id: str
    ) -> Optional[IngestionCheckpointRecord]:
        """Đọc checkpoint hiện tại từ CheckpointRepository."""
        if not self.checkpoint_repo:
            return None

        src_str = source_type.value if hasattr(source_type, "value") else str(source_type)
        if hasattr(self.checkpoint_repo, "get_checkpoint"):
            fn = self.checkpoint_repo.get_checkpoint
            if inspect.iscoroutinefunction(fn):
                return await fn(src_str, stream_id)
            return fn(src_str, stream_id)
        return None

    async def save_checkpoint(
        self, checkpoint: IngestionCheckpointRecord
    ) -> None:
        """Lưu checkpoint mới vào CheckpointRepository."""
        if not self.checkpoint_repo:
            return

        if hasattr(self.checkpoint_repo, "save_checkpoint"):
            fn = self.checkpoint_repo.save_checkpoint
            if inspect.iscoroutinefunction(fn):
                await fn(checkpoint)
            else:
                fn(checkpoint)

    async def _persist_raw_event(self, record: RawEventRecord) -> bool:
        """Lưu RawEventRecord vào repository được cắm (Neo4j hoặc in-memory)."""
        if not self.raw_event_repo:
            return False

        try:
            # Hỗ trợ linh hoạt các tên hàm repository
            if hasattr(self.raw_event_repo, "persist_raw_event"):
                fn = self.raw_event_repo.persist_raw_event
            elif hasattr(self.raw_event_repo, "save_raw_event"):
                fn = self.raw_event_repo.save_raw_event
            elif hasattr(self.raw_event_repo, "save"):
                fn = self.raw_event_repo.save
            elif hasattr(self.raw_event_repo, "insert"):
                fn = self.raw_event_repo.insert
            else:
                logger.warning("raw_event_repo không có phương thức persist_raw_event / save_raw_event / save / insert")
                return False

            if inspect.iscoroutinefunction(fn):
                res = await fn(record)
            else:
                res = fn(record)

            if res is False:
                self._stats["errors"] += 1
                return False

            self._stats["persisted"] += 1
            return True
        except Exception as e:
            self._stats["errors"] += 1
            logger.error(f"Lỗi khi lưu raw event {record.id} vào repository: {e}")
            return False

    async def ingest_event(self, record: RawEventRecord) -> bool:
        """Đưa RawEventRecord vào pipeline: chuẩn hóa, deduplicate và lưu trữ bền vững (Persist-First).
        
        Ưu tiên ghi trực tiếp vào RawEventRepository (Neo4j), sau khi ghi thành công mới
        chuyển tiếp qua worker queue nếu có.
        """
        # 1. Bổ sung các trường chuẩn hóa nếu thiếu
        now_utc = datetime.now(timezone.utc)
        if record.captured_at is None:
            record.captured_at = now_utc
        if record.created_at is None:
            record.created_at = now_utc

        if not record.payload_json and record.raw_payload:
            record.payload_json = json.dumps(record.raw_payload, default=str)

        if not record.content_hash and record.normalized_text:
            record.content_hash = hashlib.sha256(
                record.normalized_text.encode("utf-8")
            ).hexdigest()

        # 2. Khử trùng lặp (Deduplication)
        is_dup = False
        if record.idempotency_key in self._internal_seen_keys:
            is_dup = True
        elif self.queue is not None and self.queue.is_seen(record.idempotency_key):
            is_dup = True

        if is_dup:
            self._stats["deduplicated"] += 1
            logger.debug(f"Bỏ qua event trùng lặp trong pipeline: {record.idempotency_key}")
            return False

        self._internal_seen_keys.add(record.idempotency_key)

        # 3. Ưu tiên ghi trực tiếp vào RawEventRepository (Neo4j Durable Persist-First)
        if self.raw_event_repo is not None:
            persisted = await self._persist_raw_event(record)
            if not persisted:
                logger.error(f"Ghi thất bại vào RawEventRepository: {record.id}")
                return False
        else:
            self._stats["persisted"] += 1

        self._stats["ingested"] += 1

        # 4. Sau khi lưu bền vững thành công, mới chuyển tiếp qua worker queue nếu cần
        if self.queue is not None:
            await self.queue.put(record)

        return True

    async def sync_adapter(
        self,
        adapter: AcquisitionAdapter,
        stream_id: str = "all",
        since: Optional[datetime] = None,
    ) -> int:
        """Chạy đồng bộ một stream từ adapter với cơ chế tự động resume checkpoint.

        - Nếu tìm thấy checkpoint trong CheckpointRepository: chạy poll_incremental.
        - Nếu chưa có checkpoint: chạy backfill.
        - Sau khi nhận các event mới, cập nhật checkpoint mới nhất.
        """
        source_val = (
            adapter.source_type.value
            if hasattr(adapter.source_type, "value")
            else str(adapter.source_type)
        )

        checkpoint = await self.get_checkpoint(source_val, stream_id)
        if checkpoint and checkpoint.last_event_timestamp is not None:
            logger.info(
                f"Resume adapter {source_val} stream '{stream_id}' từ checkpoint: "
                f"{checkpoint.last_event_timestamp.isoformat()}"
            )
            event_stream = adapter.poll_incremental(stream_id, checkpoint=checkpoint)
        else:
            logger.info(
                f"Không có checkpoint cho {source_val} stream '{stream_id}', bắt đầu backfill (since={since})"
            )
            event_stream = adapter.backfill(stream_id, since=since)

        ingested_count = 0
        latest_event: Optional[RawEventRecord] = None

        async for record in event_stream:
            success = await self.ingest_event(record)
            if success:
                ingested_count += 1
                # Cập nhật mốc thời gian lớn nhất để làm checkpoint
                if (
                    latest_event is None
                    or record.event_timestamp > latest_event.event_timestamp
                ):
                    latest_event = record

        # Cập nhật checkpoint vào kho lưu trữ nếu có event mới
        if latest_event is not None and self.checkpoint_repo is not None:
            new_checkpoint = IngestionCheckpointRecord(
                id=checkpoint.id if checkpoint else f"ckpt-{uuid.uuid4().hex[:12]}",
                source_type=adapter.source_type,
                stream_id=stream_id,
                tenant_id=latest_event.tenant_id,
                last_external_id=latest_event.external_id,
                last_event_timestamp=latest_event.event_timestamp,
                cursor_token=None,
                updated_at=datetime.now(timezone.utc),
            )
            await self.save_checkpoint(new_checkpoint)
            logger.info(
                f"Đã cập nhật checkpoint cho {source_val} stream '{stream_id}' tới mốc: "
                f"{latest_event.event_timestamp.isoformat()}"
            )

        return ingested_count

    async def sync_all_registered(self) -> Dict[str, Dict[str, int]]:
        """Đồng bộ tất cả streams của toàn bộ adapters đã đăng ký."""
        results: Dict[str, Dict[str, int]] = {}

        for name, adapter in self.adapters.items():
            adapter_results: Dict[str, int] = {}
            try:
                streams = await adapter.discover()
                for s in streams:
                    s_id = s.get("stream_id", "all")
                    count = await self.sync_adapter(adapter, stream_id=s_id)
                    adapter_results[s_id] = count
            except Exception as e:
                logger.error(f"Lỗi khi đồng bộ adapter '{name}': {e}")
            results[name] = adapter_results

        return results
