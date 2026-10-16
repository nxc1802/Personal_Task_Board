"""Independent in-memory domain and event repositories for Benchmark fixture mode.

Completely independent of tests.support.test_doubles to guarantee strict isolation
and prevent test mock leakage into benchmark evaluations.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Set
from uuid import uuid4

from ptb_contracts.l1_acquisition import ProcessingStatus, RawEventRecord
from ptb_contracts.l2_processing import (
    CommitmentRecord,
    MergeAuditRecord,
    StatusTransitionAuditRecord,
    TaskStatus,
    UnifiedTaskCandidate,
)

logger = logging.getLogger("ptb.benchmarks.in_memory_repo")


class BenchmarkInMemoryTaskRepository:
    """In-memory Task Domain Repository implementing production TaskDomainRepository contract."""

    def __init__(self, graph_sync_worker: Optional[Any] = None) -> None:
        self.tasks: Dict[str, UnifiedTaskCandidate] = {}
        self.audits: List[StatusTransitionAuditRecord] = []
        self.merge_audits: List[MergeAuditRecord] = []
        self.commitments: List[CommitmentRecord] = []
        self.graph_sync_worker = graph_sync_worker

    async def upsert_task_atomic(self, task: UnifiedTaskCandidate) -> str:
        tid = task.id or str(uuid4())
        task.id = tid
        self.tasks[tid] = task.model_copy(deep=True)
        if self.graph_sync_worker and hasattr(self.graph_sync_worker, "enqueue"):
            for ev in task.evidences:
                await self.graph_sync_worker.enqueue(ev)
        return tid

    async def get_task_by_id(self, task_id: str) -> Optional[UnifiedTaskCandidate]:
        t = self.tasks.get(task_id)
        return t.model_copy(deep=True) if t else None

    async def get_active_tasks(self) -> List[UnifiedTaskCandidate]:
        return [
            t.model_copy(deep=True)
            for t in self.tasks.values()
            if t.status in (TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, TaskStatus.DONE)
        ]

    async def list_tasks(self, filter_dict: Optional[Dict[str, Any]] = None) -> List[UnifiedTaskCandidate]:
        res = list(self.tasks.values())
        if filter_dict:
            if "status" in filter_dict:
                allowed_statuses = set(filter_dict["status"])
                res = [t for t in res if t.status in allowed_statuses or str(t.status) in allowed_statuses]
            if "owner_canonical_id" in filter_dict:
                res = [t for t in res if t.owner_canonical_id == filter_dict["owner_canonical_id"]]
        return [t.model_copy(deep=True) for t in res]

    async def record_merge_audit(self, audit: MergeAuditRecord) -> None:
        self.merge_audits.append(audit)

    async def record_status_transition_audit(self, audit: StatusTransitionAuditRecord) -> None:
        self.audits.append(audit)

    async def save_commitment(self, commitment: CommitmentRecord) -> None:
        self.commitments.append(commitment)

    def clear(self) -> None:
        self.tasks.clear()
        self.audits.clear()
        self.merge_audits.clear()
        self.commitments.clear()


class BenchmarkInMemoryRawEventRepository:
    """In-memory Raw Event Repository implementing RawEvent persistence."""

    def __init__(self) -> None:
        self.events: Dict[str, RawEventRecord] = {}
        self.idempotency_index: Dict[str, str] = {}
        self.attempt_records: List[Dict[str, Any]] = []
        self.status_audit_log: List[Dict[str, Any]] = []

    async def persist_raw_event(self, record: RawEventRecord) -> bool:
        return await self.save_raw_event(record)

    async def save_raw_event(self, record: RawEventRecord) -> bool:
        if record.idempotency_key in self.idempotency_index:
            return False
        self.events[record.id] = record.model_copy(deep=True)
        self.idempotency_index[record.idempotency_key] = record.id
        return True

    async def get_by_id(self, event_id: str) -> Optional[RawEventRecord]:
        ev = self.events.get(event_id)
        return ev.model_copy(deep=True) if ev else None

    async def mark_event_status(
        self,
        event_id: str,
        status: ProcessingStatus,
        processor_version: Optional[str] = None,
        last_error: Optional[str] = None,
    ) -> bool:
        if event_id not in self.events:
            return False
        ev = self.events[event_id]
        ev.processing_status = status
        if processor_version:
            ev.processor_version = processor_version
        if last_error:
            ev.last_processing_error = last_error
        self.status_audit_log.append({
            "event_id": event_id,
            "status": status,
            "timestamp": datetime.now(timezone.utc),
        })
        return True

    async def record_processing_attempt(
        self,
        event_id: str,
        attempt: int,
        error: Optional[str] = None,
    ) -> None:
        self.attempt_records.append({
            "event_id": event_id,
            "attempt": attempt,
            "error": error,
            "timestamp": datetime.now(timezone.utc),
        })

    @property
    def count(self) -> int:
        return len(self.events)

    def clear(self) -> None:
        self.events.clear()
        self.idempotency_index.clear()
        self.attempt_records.clear()
        self.status_audit_log.clear()


class BenchmarkInMemoryCheckpointRepository:
    """In-memory Checkpoint Repository for acquisition watermark management."""

    def __init__(self) -> None:
        self.checkpoints: Dict[str, Any] = {}

    async def get_checkpoint(self, source_type: str, stream_id: str) -> Optional[Any]:
        return self.checkpoints.get(f"{source_type}:{stream_id}")

    async def save_checkpoint(self, checkpoint: Any) -> None:
        key = f"{checkpoint.source_type}:{checkpoint.stream_id}"
        self.checkpoints[key] = checkpoint

    def clear(self) -> None:
        self.checkpoints.clear()


class BenchmarkGraphMemorySink:
    """Lightweight in-memory episodic graph memory sink for benchmark tracing."""

    def __init__(self) -> None:
        self.episodes: List[Dict[str, Any]] = []

    async def add_episode(self, episode_data: Dict[str, Any]) -> None:
        self.episodes.append(episode_data)

    def clear(self) -> None:
        self.episodes.clear()
