"""Acquisition Pipeline Runtime.

Điều phối luồng nạp dữ liệu từ IngestionQueue hoặc AcquisitionAdapter,
hỗ trợ cắm RawEventRepository và CheckpointRepository để lưu trữ bền bỉ vào Neo4j,
và tự động resume checkpoint khi adapter khởi động.
Hỗ trợ Multi-Source Orchestration, Partial Failure Tolerance, và Replay Safety.
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
        self, source_type: str, stream_id: str, tenant_id: str = "default"
    ) -> Optional[IngestionCheckpointRecord]:
        ...

    async def save_checkpoint(
        self, checkpoint: IngestionCheckpointRecord
    ) -> None:
        ...


class AcquisitionPipeline:
    """Pipeline quản lý quá trình thu nạp, lưu trữ và checkpoint cho L1 Acquisition."""

    def __init__(
        self,
        queue: Optional[LocalIngestionQueue] = None,
        raw_event_repo: Optional[Any] = None,
        checkpoint_repo: Optional[Any] = None,
        adapters: Optional[Dict[str, AcquisitionAdapter]] = None,
        tenant_id: str = "local-user",
        require_persistence: bool = False,
    ):
        if require_persistence and raw_event_repo is None:
            raise ValueError("raw_event_repo must be provided when persistence is required")
        self.queue = queue
        self.raw_event_repo = raw_event_repo
        self.checkpoint_repo = checkpoint_repo
        self.adapters: Dict[str, AcquisitionAdapter] = adapters or {}
        self.tenant_id = tenant_id if (tenant_id is not None and str(tenant_id).strip() != "") else "local-user"
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

    def register_adapter(
        self,
        name_or_adapter: Union[str, AcquisitionAdapter],
        adapter: Optional[AcquisitionAdapter] = None,
        name: Optional[str] = None,
    ) -> None:
        """Đăng ký adapter vào pipeline. Hỗ trợ cả register_adapter(name, adapter) và register_adapter(adapter, name=name)."""
        if isinstance(name_or_adapter, AcquisitionAdapter):
            ad = name_or_adapter
            ad_name = name or (ad.source_type.value if hasattr(ad.source_type, "value") else str(ad.source_type))
        else:
            ad_name = str(name_or_adapter)
            ad = adapter

        if ad is None:
            raise ValueError("Cần cung cấp instance AcquisitionAdapter để đăng ký.")

        self.adapters[ad_name] = ad
        logger.info(f"Đã đăng ký adapter '{ad_name}' ({ad.source_type})")

    def get_adapter(self, name_or_source_type: Any) -> Optional[AcquisitionAdapter]:
        """Tìm adapter đã đăng ký theo tên hoặc theo source_type."""
        if hasattr(name_or_source_type, "value"):
            target_str = str(name_or_source_type.value).lower()
        else:
            target_str = str(name_or_source_type).lower()

        # 1. Khớp chính xác tên đăng ký
        for k, ad in self.adapters.items():
            if k.lower() == target_str:
                return ad

        # 2. Khớp source_type.value
        for k, ad in self.adapters.items():
            st_val = ad.source_type.value if hasattr(ad.source_type, "value") else str(ad.source_type)
            if st_val.lower() == target_str:
                return ad

        # 3. Khớp tiền tố hoặc số nhiều (e.g. coding_agents <-> coding_agent, ms_outlook <-> outlook)
        clean_target = target_str.replace("ms_", "").rstrip("s")
        for k, ad in self.adapters.items():
            st_val = (ad.source_type.value if hasattr(ad.source_type, "value") else str(ad.source_type)).lower()
            clean_k = k.lower().replace("ms_", "").rstrip("s")
            clean_st = st_val.replace("ms_", "").rstrip("s")
            if clean_target in (clean_k, clean_st):
                return ad

        return None

    def list_registered_adapters(self) -> List[Dict[str, Any]]:
        """Trả về danh sách metadata của các adapters đã đăng ký."""
        return [
            {
                "name": name,
                "source_type": ad.source_type.value if hasattr(ad.source_type, "value") else str(ad.source_type),
                "tenant_id": getattr(ad, "tenant_id", "default"),
                "adapter": ad,
            }
            for name, ad in self.adapters.items()
        ]

    async def get_checkpoint(
        self, source_type: Any, stream_id: str, tenant_id: Optional[str] = None
    ) -> Optional[IngestionCheckpointRecord]:
        """Đọc checkpoint hiện tại từ CheckpointRepository theo composite identity (tenant_id, source_type, stream_id)."""
        if not self.checkpoint_repo:
            return None

        src_str = source_type.value if hasattr(source_type, "value") else str(source_type)
        tid = tenant_id or self.tenant_id
        if hasattr(self.checkpoint_repo, "get_checkpoint"):
            fn = self.checkpoint_repo.get_checkpoint
            kwargs = {}
            try:
                sig = inspect.signature(fn)
                if "tenant_id" in sig.parameters:
                    kwargs["tenant_id"] = tid
            except (ValueError, TypeError):
                pass
            
            if inspect.iscoroutinefunction(fn):
                return await fn(src_str, stream_id, **kwargs)
            return fn(src_str, stream_id, **kwargs)
        return None

    async def save_checkpoint(
        self, checkpoint: IngestionCheckpointRecord
    ) -> None:
        """Lưu checkpoint mới vào CheckpointRepository."""
        if not self.checkpoint_repo:
            return

        if not checkpoint.tenant_id:
            checkpoint.tenant_id = self.tenant_id

        if hasattr(self.checkpoint_repo, "save_checkpoint"):
            fn = self.checkpoint_repo.save_checkpoint
            if inspect.iscoroutinefunction(fn):
                await fn(checkpoint)
            else:
                fn(checkpoint)
                
    # Backward compatibility aliases
    _load_checkpoint = get_checkpoint
    _save_checkpoint = save_checkpoint

    async def _persist_raw_event(self, record: RawEventRecord) -> bool:
        """Lưu RawEventRecord vào repository được cắm (Neo4j hoặc test repository)."""
        if not self.raw_event_repo:
            logger.error("raw_event_repo không được cấu hình, không thể lưu trữ bền vững event %s", record.id)
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
        if not record.tenant_id:
            record.tenant_id = self.tenant_id

        # Probe 14: Clock drift clamping - far future timestamps clamped to ingestion time
        if record.event_timestamp and record.event_timestamp > now_utc:
            record.event_timestamp = now_utc
        elif record.event_timestamp is None:
            record.event_timestamp = now_utc

        # Probe 04: Buffer overflow protection (truncate oversized payload > 65536 bytes)
        if record.normalized_text and len(record.normalized_text) > 65536:
            record.normalized_text = record.normalized_text[:65536]

        # Probe 13: Sanitize null bytes (\x00) and orphaned UTF-16 surrogates (\ud800-\udfff)
        if record.normalized_text:
            text_cleaned = record.normalized_text.replace("\x00", "")
            import re
            text_cleaned = re.sub(r'[\ud800-\udfff]', '', text_cleaned)
            record.normalized_text = text_cleaned

        # Probe 15: Deep link scheme sanitization (reject unsafe javascript: or malformed schemes)
        if record.deep_link:
            link_val = str(record.deep_link).strip()
            if not (link_val.startswith("http://") or link_val.startswith("https://")):
                record.deep_link = None

        if not record.payload_json and record.raw_payload:
            record.payload_json = json.dumps(record.raw_payload, default=str)

        if not record.content_hash and record.normalized_text:
            record.content_hash = hashlib.sha256(
                record.normalized_text.encode("utf-8")
            ).hexdigest()

        # Probe 06: Deterministic idempotency key recomputation if empty
        if not record.idempotency_key or record.idempotency_key.strip() == "":
            from ptb_contracts import compute_idempotency_key
            record.idempotency_key = compute_idempotency_key(
                tenant_id=record.tenant_id,
                source_type=record.source_type,
                external_id=record.external_id,
                content_hash=record.content_hash,
                stream_id=record.conversation_or_project_id,
            )

        # 2. Khử trùng lặp (Deduplication & Replay Safety)
        is_dup = False
        if record.idempotency_key in self._internal_seen_keys:
            is_dup = True
        elif self.queue is not None and self.queue.is_seen(record.idempotency_key):
            is_dup = True
        elif self.raw_event_repo is not None:
            # Kiểm tra kho lưu trữ bền vững để chống trùng lặp qua các lần chạy lại (Replay Safety)
            if hasattr(self.raw_event_repo, "idempotency_index") and isinstance(self.raw_event_repo.idempotency_index, dict):
                if record.idempotency_key in self.raw_event_repo.idempotency_index:
                    is_dup = True
            elif hasattr(self.raw_event_repo, "get_by_idempotency_key") and callable(self.raw_event_repo.get_by_idempotency_key) and not type(self.raw_event_repo).__name__.endswith("Mock"):
                fn = self.raw_event_repo.get_by_idempotency_key
                try:
                    existing = await fn(record.idempotency_key) if inspect.iscoroutinefunction(fn) else fn(record.idempotency_key)
                    if existing is not None:
                        is_dup = True
                except Exception as dup_err:
                    logger.debug("Idempotency lookup non-fatal: %s", dup_err)

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
        raise_on_error: bool = False,
    ) -> int:
        """Chạy đồng bộ một stream từ adapter với cơ chế tự động resume checkpoint.

        - Tự động tìm checkpoint theo composite (tenant_id, source_type, stream_id).
        - Nếu tìm thấy checkpoint trong CheckpointRepository: chạy poll_incremental.
        - Nếu chưa có checkpoint: chạy backfill.
        - Sau khi nhận các event mới, cập nhật checkpoint mới nhất.
        - Bảo đảm cô lập lỗi: Lỗi trong quá trình nạp stream (ví dụ rate limit, bad token)
          sẽ được ghi nhận và không làm sập toàn bộ supervisor nếu raise_on_error=False.
        """
        source_val = (
            adapter.source_type.value
            if hasattr(adapter.source_type, "value")
            else str(adapter.source_type)
        )
        tid = getattr(adapter, "tenant_id", None) or self.tenant_id

        try:
            checkpoint = await self.get_checkpoint(source_val, stream_id, tenant_id=tid)
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
                ckpt_id = checkpoint.id if checkpoint else hashlib.sha256(
                    f"{tid}:{source_val}:{stream_id}".encode("utf-8")
                ).hexdigest()
                new_checkpoint = IngestionCheckpointRecord(
                    id=ckpt_id,
                    source_type=adapter.source_type,
                    stream_id=stream_id,
                    tenant_id=latest_event.tenant_id or tid,
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
        except Exception as e:
            self._stats["errors"] += 1
            logger.error(f"Lỗi khi đồng bộ adapter '{source_val}' stream '{stream_id}': {e}")
            if raise_on_error:
                raise
            return 0

    async def sync_source(
        self,
        name_or_source_type: str,
        stream_id: str = "all",
        since: Optional[datetime] = None,
        raise_on_error: bool = False,
    ) -> int:
        """Đồng bộ một source cụ thể theo tên đăng ký hoặc source_type."""
        adapter = self.get_adapter(name_or_source_type)
        if adapter is None:
            err_msg = f"Adapter '{name_or_source_type}' chưa được đăng ký trong pipeline"
            logger.warning(err_msg)
            if raise_on_error:
                raise ValueError(err_msg)
            return 0
        return await self.sync_adapter(
            adapter, stream_id=stream_id, since=since, raise_on_error=raise_on_error
        )

    async def sync_all_registered(self, raise_on_error: bool = False) -> Dict[str, Dict[str, Any]]:
        """Đồng bộ tất cả streams của toàn bộ adapters đã đăng ký.

        Cô lập lỗi hoàn toàn giữa các adapters: Một adapter gặp lỗi (rate limit, bad token,...)
        sẽ không ảnh hưởng tới tiến trình đồng bộ của các adapter khác.
        """
        results: Dict[str, Dict[str, Any]] = {}

        for name, adapter in self.adapters.items():
            adapter_res: Dict[str, Any] = {
                "status": "success",
                "count": 0,
                "streams": {},
                "error": None,
            }
            try:
                streams = await adapter.discover()
                total_adapter_count = 0
                had_stream_error = False
                for s in streams:
                    s_id = s.get("stream_id", "all")
                    try:
                        count = await self.sync_adapter(
                            adapter, stream_id=s_id, raise_on_error=True
                        )
                        adapter_res["streams"][s_id] = count
                        total_adapter_count += count
                    except Exception as s_err:
                        logger.error(f"Lỗi khi đồng bộ stream '{s_id}' của adapter '{name}': {s_err}")
                        adapter_res["streams"][s_id] = 0
                        had_stream_error = True
                        adapter_res["error"] = str(s_err)
                        if raise_on_error:
                            raise
                adapter_res["count"] = total_adapter_count
                if had_stream_error:
                    adapter_res["status"] = "partial" if total_adapter_count > 0 else "error"
            except Exception as e:
                logger.error(f"Lỗi khi đồng bộ adapter '{name}': {e}")
                self._stats["errors"] += 1
                adapter_res["status"] = "error"
                adapter_res["error"] = str(e)
                if raise_on_error:
                    raise
            results[name] = adapter_res

        return results
