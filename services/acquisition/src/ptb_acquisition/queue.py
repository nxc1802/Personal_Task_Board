"""Local Ingestion Queue (Contract C12 Boundary).

Chịu trách nhiệm đệm các sự kiện thu thập được từ Layer 1A và Layer 1B,
kiểm tra tính duy nhất qua idempotency_key, và phân phối đến Layer 2 Ingestion Service.
"""

import asyncio
import logging
from typing import Any, Dict, Optional, Set, Union
from ptb_contracts import RawAgentSessionRecord, RawEventRecord

logger = logging.getLogger("ptb.acquisition.queue")

AnyRawRecord = Union[RawEventRecord, RawAgentSessionRecord]


class LocalIngestionQueue:
    def __init__(self, maxsize: int = 1000):
        self._queue: asyncio.Queue[AnyRawRecord] = asyncio.Queue(maxsize=maxsize)
        self._seen_keys: Set[str] = set()
        self._stats: Dict[str, int] = {
            "pushed": 0,
            "deduplicated": 0,
            "consumed": 0,
        }

    @property
    def qsize(self) -> int:
        return self._queue.qsize()

    @property
    def stats(self) -> Dict[str, int]:
        return dict(self._stats)

    def is_seen(self, idempotency_key: str) -> bool:
        return idempotency_key in self._seen_keys

    async def put(self, record: AnyRawRecord) -> bool:
        """Đưa bản ghi vào hàng đợi nếu chưa từng xuất hiện (idempotent)."""
        key = record.idempotency_key
        if key in self._seen_keys:
            self._stats["deduplicated"] += 1
            logger.debug(f"Bỏ qua bản ghi trùng lặp idempotency_key: {key}")
            return False

        self._seen_keys.add(key)
        await self._queue.put(record)
        self._stats["pushed"] += 1
        return True

    async def get(self) -> AnyRawRecord:
        """Lấy một bản ghi ra khỏi hàng đợi để xử lý."""
        record = await self._queue.get()
        self._stats["consumed"] += 1
        return record

    def task_done(self) -> None:
        self._queue.task_done()
