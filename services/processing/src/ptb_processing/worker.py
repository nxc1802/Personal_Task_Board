"""ProcessingWorker: Background worker managing RawEvent processing loop.

Quản lý vòng lặp xử lý:
1. Poll batch PENDING hoặc RETRY RawEvents từ RawEventRepository.
2. Chuyển trạng thái sang PROCESSING.
3. Chạy qua ProcessingPipeline.
4. Ghi nhận ProcessingAttemptRecord (attempt_number, error_message, status).
5. Nếu thành công: mark PROCESSED.
6. Nếu thất bại: Tính retry với exponential backoff. Nếu retry_count >= 3 -> mark FAILED.
   Tuyệt đối không để exception làm crash tiến trình worker.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import List, Optional
from uuid import uuid4

from ptb_contracts.l1_acquisition import (
    ProcessingAttemptRecord,
    ProcessingStatus,
    RawEventRecord,
)
from ptb_database.repositories.raw_event_repo import RawEventRepository
from ptb_processing.pipeline import PipelineResult, ProcessingPipeline

logger = logging.getLogger("ptb.processing.worker")


class ProcessingWorker:
    """Worker xử lý RawEvents nền với cơ chế retry và resilience."""

    def __init__(
        self,
        raw_event_repo: RawEventRepository,
        pipeline: ProcessingPipeline,
        max_retries: int = 3,
        base_backoff_seconds: float = 30.0,
        max_backoff_seconds: float = 3600.0,
        processor_version: str = "v1.0",
    ) -> None:
        self.raw_event_repo = raw_event_repo
        self.pipeline = pipeline
        self.max_retries = max_retries
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.processor_version = processor_version
        self._running = False

    async def process_event(self, raw_event: RawEventRecord) -> ProcessingStatus:
        """Xử lý đơn lẻ một RawEvent, chuyển trạng thái và quản lý retry deterministic."""
        attempt_count = (
            raw_event.processing_attempt_count
            if raw_event.processing_attempt_count is not None
            else (raw_event.retry_count or 0)
        )
        attempt_number = attempt_count + 1

        # 1. Chuyển trạng thái sang PROCESSING
        try:
            await self.raw_event_repo.mark_event_status(
                event_id=raw_event.id,
                status=ProcessingStatus.PROCESSING,
                processor_version=self.processor_version,
            )
        except Exception as mark_err:
            logger.warning(
                "Failed to mark event %s as PROCESSING: %s", raw_event.id, mark_err
            )

        # 2. Chạy qua ProcessingPipeline
        try:
            result = await self.pipeline.process(raw_event)
            now_utc = datetime.now(timezone.utc)

            # 3. Ghi nhận ProcessingAttemptRecord thành công
            attempt_record = ProcessingAttemptRecord(
                id=str(uuid4()),
                raw_event_id=raw_event.id,
                attempt_number=attempt_number,
                status=ProcessingStatus.PROCESSED,
                error_message=None,
                attempted_at=now_utc,
            )
            try:
                await self.raw_event_repo.record_processing_attempt(attempt_record)
            except Exception as att_err:
                logger.error("Failed recording processing attempt: %s", att_err)

            # 4. Mark PROCESSED
            await self.raw_event_repo.mark_event_status(
                event_id=raw_event.id,
                status=ProcessingStatus.PROCESSED,
                processed_at=now_utc,
                processor_version=self.processor_version,
            )
            logger.info("RawEvent %s processed successfully on attempt %d", raw_event.id, attempt_number)
            return ProcessingStatus.PROCESSED

        except Exception as exc:
            error_msg = str(exc)
            now_utc = datetime.now(timezone.utc)
            logger.error(
                "Error processing RawEvent %s (attempt %d/%d): %s",
                raw_event.id,
                attempt_number,
                self.max_retries,
                error_msg,
                exc_info=True,
            )

            # 5. Kiểm tra deterministic retry count
            if attempt_number >= self.max_retries:
                final_status = ProcessingStatus.FAILED
                next_retry_at = None
                logger.warning(
                    "RawEvent %s reached max retries (%d). Marking as FAILED.",
                    raw_event.id,
                    self.max_retries,
                )
            else:
                final_status = ProcessingStatus.RETRY
                # Exponential backoff: base * 2^(attempt - 1)
                backoff_secs = min(
                    self.base_backoff_seconds * (2 ** (attempt_number - 1)),
                    self.max_backoff_seconds,
                )
                next_retry_at = now_utc + timedelta(seconds=backoff_secs)
                logger.info(
                    "RawEvent %s will retry at %s (backoff: %.1fs)",
                    raw_event.id,
                    next_retry_at.isoformat(),
                    backoff_secs,
                )

            # 6. Ghi nhận ProcessingAttemptRecord thất bại
            attempt_record = ProcessingAttemptRecord(
                id=str(uuid4()),
                raw_event_id=raw_event.id,
                attempt_number=attempt_number,
                status=final_status,
                error_message=error_msg,
                attempted_at=now_utc,
            )
            try:
                await self.raw_event_repo.record_processing_attempt(attempt_record)
            except Exception as att_err:
                logger.error("Failed recording processing attempt: %s", att_err)

            # 7. Cập nhật status RETRY hoặc FAILED
            try:
                await self.raw_event_repo.mark_event_status(
                    event_id=raw_event.id,
                    status=final_status,
                    error=error_msg,
                    next_retry_at=next_retry_at,
                    processor_version=self.processor_version,
                )
            except Exception as mark_err:
                logger.error("Failed to mark event status as %s: %s", final_status, mark_err)

            return final_status

    async def process_batch(self, limit: int = 50) -> List[ProcessingStatus]:
        """Poll một batch các RawEvents PENDING/RETRY và xử lý tuần tự."""
        try:
            events = await self.raw_event_repo.get_pending_raw_events(limit=limit)
        except Exception as poll_err:
            logger.error("Failed to fetch pending raw events: %s", poll_err, exc_info=True)
            return []

        statuses: List[ProcessingStatus] = []
        for ev in events:
            try:
                status = await self.process_event(ev)
                statuses.append(status)
            except Exception as ev_err:
                # Tuyệt đối không để exception làm crash tiến trình worker
                logger.error("Unexpected error in process_event for %s: %s", ev.id, ev_err, exc_info=True)
                statuses.append(ProcessingStatus.FAILED)

        return statuses

    async def run_loop(
        self, poll_interval: float = 2.0, max_iterations: Optional[int] = None
    ) -> None:
        """Chạy vòng lặp worker liên tục thăm dò các raw events."""
        self._running = True
        iterations = 0
        logger.info("ProcessingWorker loop started.")

        while self._running:
            if max_iterations is not None and iterations >= max_iterations:
                break

            try:
                statuses = await self.process_batch()
                iterations += 1
                if not statuses:
                    await asyncio.sleep(poll_interval)
            except asyncio.CancelledError:
                logger.info("ProcessingWorker loop cancelled.")
                break
            except Exception as loop_err:
                # Tuyệt đối không để exception làm crash tiến trình worker
                logger.error("Unexpected error in worker loop iteration: %s", loop_err, exc_info=True)
                await asyncio.sleep(poll_interval)

        logger.info("ProcessingWorker loop stopped.")

    def stop(self) -> None:
        """Dừng vòng lặp worker."""
        self._running = False
