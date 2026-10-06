"""Unified test doubles for Personal Task Board unit and E2E testing without live infrastructure.

All test doubles (in-memory repositories, deterministic LLM extractor, and fake Graphiti adapter)
live exclusively under tests/support/ and must never be imported by production runtime code.
"""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from uuid import uuid4

from ptb_contracts import (
    CommitmentRecord,
    EvidenceRecord,
    EvidenceType,
    IngestionCheckpointRecord,
    MergeAuditRecord,
    ParsedMessageContent,
    ProcessingAttemptRecord,
    ProcessingStatus,
    RawEventRecord,
    ReviewQueueItem,
    SourceType,
    StatusTransitionAuditRecord,
    TaskStatus,
    UnifiedTaskCandidate,
)
from ptb_contracts.l3_storage import DecisionNodeRecord, EvidenceNodeRecord, LessonNodeRecord
from ptb_contracts.l4_intelligence import TaskWithContext
from ptb_processing.extractor.llm_extractor import (
    LLMExtractedSchema,
    LLMStructuredExtractor,
    classify_review_status,
)


class InMemoryRawEventRepository:
    """In-memory RawEventRepository implementing durability, idempotency, and retry attempt tracking."""

    def __init__(self, initial_events: Optional[List[RawEventRecord]] = None) -> None:
        self.events: Dict[str, RawEventRecord] = {}
        self._events = self.events
        self.idempotency_index: Dict[str, str] = {}
        self.attempt_records: List[ProcessingAttemptRecord] = []
        self.status_audit_log: List[Dict[str, Any]] = []

        for ev in initial_events or []:
            ev_id = ev.id or str(uuid4())
            ev_copy = ev.model_copy(deep=True)
            ev_copy.id = ev_id
            self.events[ev_id] = ev_copy
            if ev_copy.idempotency_key:
                self.idempotency_index[ev_copy.idempotency_key] = ev_id

    async def persist_raw_event(self, record: RawEventRecord) -> str:
        """MERGE on idempotency_key: returns existing ID if duplicate, else stores new."""
        if record.idempotency_key and record.idempotency_key in self.idempotency_index:
            return self.idempotency_index[record.idempotency_key]

        event_id = record.id or str(uuid4())
        if not record.payload_json and record.raw_payload:
            record.payload_json = json.dumps(record.raw_payload, default=str, ensure_ascii=False)
        if not record.content_hash and record.normalized_text:
            record.content_hash = hashlib.sha256(
                record.normalized_text.encode("utf-8")
            ).hexdigest()

        record_copy = record.model_copy(deep=True)
        record_copy.id = event_id
        self.events[event_id] = record_copy
        if record_copy.idempotency_key:
            self.idempotency_index[record_copy.idempotency_key] = event_id
        return event_id

    async def save_raw_event(self, record: RawEventRecord) -> bool:
        await self.persist_raw_event(record)
        return True

    async def get_by_id(self, event_id: str) -> Optional[RawEventRecord]:
        return self.events.get(event_id)

    async def get_all(self) -> List[RawEventRecord]:
        return list(self.events.values())

    async def get_pending_raw_events(self, limit: int = 50) -> List[RawEventRecord]:
        now = datetime.now(timezone.utc)
        pending: List[RawEventRecord] = []
        valid_statuses = {
            ProcessingStatus.PENDING,
            ProcessingStatus.RETRY,
            ProcessingStatus.pending,
            ProcessingStatus.retry,
            "PENDING",
            "pending",
            "RETRY",
            "retry",
        }
        for ev in self.events.values():
            if getattr(ev, "processing_status", None) in valid_statuses:
                if ev.next_retry_at is None or ev.next_retry_at <= now:
                    pending.append(ev)
        pending.sort(
            key=lambda e: e.event_timestamp or datetime.min.replace(tzinfo=timezone.utc)
        )
        return pending[:limit]

    async def mark_event_status(
        self,
        event_id: str,
        status: Any,
        error: Optional[str] = None,
        next_retry_at: Optional[datetime] = None,
        processed_at: Optional[datetime] = None,
        processor_version: Optional[str] = None,
        **kwargs: Any,
    ) -> bool:
        """Update event status. Increments processing_attempt_count ONLY when transitioning to PROCESSING."""
        self.status_audit_log.append(
            {
                "event_id": event_id,
                "status": status,
                "error": error,
                "next_retry_at": next_retry_at,
                "processed_at": processed_at,
                "processor_version": processor_version,
            }
        )
        if event_id not in self.events:
            return False

        ev = self.events[event_id]
        ev.processing_status = status
        ev.last_processing_error = error
        ev.last_error = error
        ev.next_retry_at = next_retry_at

        status_val = status.value if hasattr(status, "value") else str(status)
        if status_val.upper() == "PROCESSING":
            base_count = (
                ev.processing_attempt_count
                if ev.processing_attempt_count is not None
                else (ev.retry_count or 0)
            )
            ev.processing_attempt_count = base_count + 1
            ev.retry_count = base_count + 1

        if processed_at is not None:
            ev.processed_at = processed_at
        elif status_val.upper() == "PROCESSED" and ev.processed_at is None:
            ev.processed_at = datetime.now(timezone.utc)

        if processor_version is not None:
            ev.processor_version = processor_version

        return True

    async def record_processing_attempt(
        self, attempt_record: ProcessingAttemptRecord
    ) -> bool:
        self.attempt_records.append(attempt_record)
        return True

    @property
    def count(self) -> int:
        return len(self.events)


class InMemoryCheckpointRepository:
    """In-memory CheckpointRepository keyed by composite (tenant_id, source_type, stream_id)."""

    def __init__(self) -> None:
        self.checkpoints: Dict[Tuple[str, str, str], IngestionCheckpointRecord] = {}
        self._checkpoints = self.checkpoints

    @staticmethod
    def _normalize_key(
        source_type: Any, stream_id: str, tenant_id: Optional[str] = "default"
    ) -> Tuple[str, str, str]:
        s_val = source_type.value if hasattr(source_type, "value") else str(source_type)
        t_val = (
            str(tenant_id).strip()
            if (tenant_id is not None and str(tenant_id).strip() != "")
            else "default"
        )
        return (t_val, s_val, stream_id)

    async def get_checkpoint(
        self,
        source_type: Union[str, SourceType],
        stream_id: str,
        tenant_id: str = "default",
    ) -> Optional[IngestionCheckpointRecord]:
        key = self._normalize_key(source_type, stream_id, tenant_id)
        if key in self.checkpoints:
            return self.checkpoints[key].model_copy(deep=True)

        return None

    async def save_checkpoint(self, checkpoint: IngestionCheckpointRecord) -> None:
        key = self._normalize_key(
            checkpoint.source_type, checkpoint.stream_id, checkpoint.tenant_id
        )
        t_val, s_val, strm_val = key
        cp_id = hashlib.sha256(
            f"{t_val}:{s_val}:{strm_val}".encode("utf-8")
        ).hexdigest()
        checkpoint.id = cp_id
        cp_copy = checkpoint.model_copy(deep=True)
        cp_copy.id = cp_id
        cp_copy.tenant_id = t_val
        self.checkpoints[key] = cp_copy

    async def list_checkpoints(self) -> List[IngestionCheckpointRecord]:
        return [cp.model_copy(deep=True) for cp in self.checkpoints.values()]

    async def cleanup_duplicate_checkpoints(self) -> int:
        return 0


class InMemoryTaskDomainRepository:
    """In-memory TaskDomainRepository implementing task persistence, duplicate search, graph context, and audits."""

    def __init__(
        self,
        initial_tasks: Optional[List[UnifiedTaskCandidate]] = None,
        graph_sync_worker: Optional[Any] = None,
        embedding_service: Optional[Any] = None,
    ) -> None:
        self.tasks: Dict[str, UnifiedTaskCandidate] = {
            t.id: t.model_copy(deep=True) for t in (initial_tasks or [])
        }
        self._tasks = self.tasks
        self.audits: List[StatusTransitionAuditRecord] = []
        self.merge_audits: List[MergeAuditRecord] = []
        self.commitments: List[CommitmentRecord] = []
        self.graph_sync_worker = graph_sync_worker
        self.embedding_service = embedding_service
        self.embeddings: Dict[str, List[float]] = {}

    async def upsert_task_atomic(self, task: UnifiedTaskCandidate) -> str:
        task_id = task.id or str(uuid4())
        task_copy = task.model_copy(deep=True)
        task_copy.id = task_id
        now = datetime.now(timezone.utc)
        if not task_copy.created_at:
            task_copy.created_at = now
        task_copy.updated_at = now

        for ev in task_copy.evidences:
            if not ev.id:
                ev.id = str(uuid4())
            ev.task_id = task_id

        self.tasks[task_id] = task_copy
        task.id = task_id
        task.created_at = task_copy.created_at
        task.updated_at = task_copy.updated_at

        if self.graph_sync_worker is not None:
            for ev in task_copy.evidences:
                ev_status = self.graph_sync_worker.get_sync_status(ev.id, "evidence")
                status_str = ev_status.value if hasattr(ev_status, "value") else str(ev_status or "")
                if status_str != "SYNCED":
                    ev_payload = ev.model_dump()
                    ev_payload["task_id"] = task_id
                    self.graph_sync_worker.enqueue_sync("evidence", ev.id, ev_payload)
                    await self.graph_sync_worker.sync_episode("evidence", ev.id, ev_payload)

        if self.embedding_service is not None:
            try:
                task_text = f"{task.title}. {task.description or ''}".strip()
                embed_fn = getattr(self.embedding_service, "embed_text", None)
                if callable(embed_fn):
                    vec = embed_fn(task_text)
                    if inspect.isawaitable(vec):
                        vec = await vec
                    await self.set_task_embedding(task_id, vec)
            except Exception:
                pass

        return task_id

    async def set_task_embedding(self, task_id: str, embedding: List[float]) -> None:
        """Set dense embedding vector for UnifiedTask."""
        self.embeddings[task_id] = list(embedding)

    async def get_task_embedding(self, task_id: str) -> Optional[List[float]]:
        """Retrieve dense embedding vector for UnifiedTask."""
        return self.embeddings.get(task_id)

    async def get_task_by_id(self, task_id: str) -> Optional[UnifiedTaskCandidate]:
        t = self.tasks.get(task_id)
        return t.model_copy(deep=True) if t else None

    async def get_task_with_evidences(self, task_id: str) -> Optional[UnifiedTaskCandidate]:
        return await self.get_task_by_id(task_id)

    async def list_tasks(
        self, filters: Optional[dict] = None, **kwargs: Any
    ) -> List[UnifiedTaskCandidate]:
        all_tasks = [t.model_copy(deep=True) for t in self.tasks.values()]
        all_tasks.sort(
            key=lambda t: (
                float(t.priority_score or 0.0),
                t.updated_at or datetime.min.replace(tzinfo=timezone.utc),
            ),
            reverse=True,
        )
        if not filters:
            return all_tasks

        filtered: List[UnifiedTaskCandidate] = []
        now = datetime.now(timezone.utc)
        for t in all_tasks:
            if "status" in filters and filters["status"]:
                req_status = filters["status"]
                if isinstance(req_status, (list, set, tuple)):
                    status_vals = [
                        s.value if hasattr(s, "value") else str(s) for s in req_status
                    ]
                    if t.status.value not in status_vals:
                        continue
                else:
                    status_val = (
                        req_status.value
                        if hasattr(req_status, "value")
                        else str(req_status)
                    )
                    if t.status.value != status_val:
                        continue

            if "project" in filters and filters["project"]:
                if (t.project_key or "").lower() != str(filters["project"]).lower():
                    continue
            if "project_key" in filters and filters["project_key"]:
                if (t.project_key or "").lower() != str(filters["project_key"]).lower():
                    continue

            if "customer" in filters and filters["customer"]:
                if (t.customer_id or "").lower() != str(filters["customer"]).lower():
                    continue
            if "customer_id" in filters and filters["customer_id"]:
                if (t.customer_id or "").lower() != str(filters["customer_id"]).lower():
                    continue

            if "source" in filters and filters["source"]:
                req_source = str(filters["source"]).lower()
                if not any(
                    (ev.source_type or "").lower() == req_source for ev in t.evidences
                ):
                    continue
            if "source_type" in filters and filters["source_type"]:
                req_source = str(filters["source_type"]).lower()
                if not any(
                    (ev.source_type or "").lower() == req_source for ev in t.evidences
                ):
                    continue

            if "owner" in filters and filters["owner"]:
                req_owner = str(filters["owner"]).lower()
                owner_match = (
                    (t.owner_canonical_id or "").lower() == req_owner
                    or (t.owner_name or "").lower() == req_owner
                )
                if not owner_match:
                    continue

            if "priority" in filters and filters["priority"] is not None:
                p_filter = filters["priority"]
                score = float(t.priority_score or 0.0)
                if isinstance(p_filter, dict):
                    min_p = float(p_filter.get("min", 0.0))
                    max_p = float(p_filter.get("max", 100.0))
                    if not (min_p <= score <= max_p):
                        continue
                elif isinstance(p_filter, (int, float)):
                    if score < float(p_filter):
                        continue

            if "due_range" in filters and filters["due_range"]:
                due_range = filters["due_range"]
                if not t.due_date:
                    continue
                t_due = (
                    t.due_date
                    if t.due_date.tzinfo
                    else t.due_date.replace(tzinfo=timezone.utc)
                )
                if isinstance(due_range, dict):
                    start_dt = due_range.get("start")
                    end_dt = due_range.get("end")
                elif isinstance(due_range, (list, tuple)) and len(due_range) == 2:
                    start_dt, end_dt = due_range
                else:
                    start_dt, end_dt = None, None
                if start_dt:
                    start_utc = (
                        start_dt
                        if start_dt.tzinfo
                        else start_dt.replace(tzinfo=timezone.utc)
                    )
                    if t_due < start_utc:
                        continue
                if end_dt:
                    end_utc = (
                        end_dt
                        if end_dt.tzinfo
                        else end_dt.replace(tzinfo=timezone.utc)
                    )
                    if t_due > end_utc:
                        continue

            if "has_deadline" in filters and filters["has_deadline"] is not None:
                has_deadline_flag = bool(t.due_date is not None or t.explicit_deadline)
                if has_deadline_flag != bool(filters["has_deadline"]):
                    continue

            if "stale" in filters and filters["stale"] is not None:
                updated = t.updated_at or t.created_at
                is_stale = False
                if updated:
                    upd_utc = (
                        updated
                        if updated.tzinfo
                        else updated.replace(tzinfo=timezone.utc)
                    )
                    is_stale = (now - upd_utc).total_seconds() >= (
                        3 * 86400
                    ) and t.status not in [TaskStatus.DONE, TaskStatus.DISMISSED]
                if is_stale != bool(filters["stale"]):
                    continue

            if "waiting" in filters and filters["waiting"] is not None:
                is_waiting = t.status == TaskStatus.BLOCKED
                if is_waiting != bool(filters["waiting"]):
                    continue

            if "review_status" in filters and filters["review_status"]:
                if (t.review_status or "").lower() != str(
                    filters["review_status"]
                ).lower():
                    continue

            filtered.append(t)

        if "limit" in filters and isinstance(filters["limit"], int):
            return filtered[: filters["limit"]]
        return filtered

    async def get_all_tasks(self, **kwargs: Any) -> List[UnifiedTaskCandidate]:
        return await self.list_tasks()

    async def get_active_tasks(self, **kwargs: Any) -> List[UnifiedTaskCandidate]:
        return [
            t.model_copy(deep=True)
            for t in self.tasks.values()
            if t.status in (TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED)
        ]

    async def get_active_tasks_with_evidence(
        self, **kwargs: Any
    ) -> List[UnifiedTaskCandidate]:
        return await self.get_active_tasks()

    async def get_task_with_context(self, task_id: str) -> Optional[TaskWithContext]:
        task = self.tasks.get(task_id)
        if not task:
            return None
        now = datetime.now(timezone.utc)
        last_change = task.updated_at or task.created_at or now
        if last_change.tzinfo is None:
            last_change = last_change.replace(tzinfo=timezone.utc)
        days_in_status = max(0, int((now - last_change).total_seconds() // 86400))
        return TaskWithContext(
            task=task.model_copy(deep=True),
            last_status_change_at=last_change,
            days_in_current_status=days_in_status,
            has_completion_evidence=any(
                ev.evidence_type == EvidenceType.COMPLETION_SIGNAL
                or "done" in (ev.snippet or "").lower()
                for ev in task.evidences
            ),
            blocking_tasks=["task-blocker-1"] if task.status == TaskStatus.BLOCKED else [],
            dependent_people=["Alex"] if task.status == TaskStatus.BLOCKED else [],
        )

    async def find_potential_duplicates(
        self,
        candidate: Optional[UnifiedTaskCandidate] = None,
        project_key: Optional[str] = None,
        limit: int = 50,
    ) -> List[UnifiedTaskCandidate]:
        """Return active tasks that could be potential duplicates of candidate."""
        active = await self.get_active_tasks()
        eff_project = project_key or (candidate.project_key if candidate else None)
        results: List[UnifiedTaskCandidate] = []
        for t in active:
            if candidate is not None and t.id == candidate.id:
                continue
            if eff_project and t.project_key and t.project_key.lower() != eff_project.lower():
                continue
            results.append(t)
            if len(results) >= limit:
                break
        return results

    async def get_review_queue(self, limit: int = 20) -> List[ReviewQueueItem]:
        items: List[ReviewQueueItem] = []
        for t in self.tasks.values():
            if t.review_status == "pending_review" or (
                0.40 <= (t.extraction_confidence or 0.0) < 0.65
            ):
                raw_id = t.evidences[0].raw_event_id if t.evidences else "raw-001"
                items.append(
                    ReviewQueueItem(
                        id=f"rev-{t.id}",
                        raw_event_id=raw_id,
                        candidate_task=t.model_copy(deep=True),
                        reason=f"Confidence {t.extraction_confidence} requires human review",
                        created_at=t.created_at or datetime.now(timezone.utc),
                    )
                )
                if len(items) >= limit:
                    break
        return items

    async def get_active_commitments(
        self, user_id: Optional[str] = None
    ) -> List[CommitmentRecord]:
        return [
            c.model_copy(deep=True)
            for c in self.commitments
            if getattr(c, "status", "ACTIVE") == "ACTIVE"
        ]

    async def save_commitment(self, commitment: CommitmentRecord) -> str:
        c_id = commitment.id or str(uuid4())
        c_copy = commitment.model_copy(deep=True)
        c_copy.id = c_id
        commitment.id = c_id
        for idx, existing in enumerate(self.commitments):
            if existing.id == c_id:
                self.commitments[idx] = c_copy
                return c_id
        self.commitments.append(c_copy)
        return c_id

    async def record_status_transition_audit(
        self, audit: StatusTransitionAuditRecord
    ) -> str:
        audit_id = audit.id or str(uuid4())
        audit.id = audit_id
        self.audits.append(audit)
        return audit_id

    async def record_merge_audit(self, audit: MergeAuditRecord) -> str:
        audit_id = audit.id or str(uuid4())
        audit.id = audit_id
        self.merge_audits.append(audit)
        return audit_id

    async def split_task(
        self,
        original_task_id: str,
        evidence_ids_to_detach: list[str],
        new_task_title: Optional[str] = None,
    ) -> UnifiedTaskCandidate:
        orig = self.tasks.get(original_task_id)
        if not orig:
            raise ValueError(f"Task {original_task_id} not found")
        detached = [e for e in orig.evidences if e.id in evidence_ids_to_detach]
        if not detached:
            raise ValueError(
                f"None of {evidence_ids_to_detach} found in task {original_task_id}"
            )

        now = datetime.now(timezone.utc)
        orig.evidences = [
            e for e in orig.evidences if e.id not in evidence_ids_to_detach
        ]
        orig.updated_at = now
        new_id = str(uuid4())
        new_task = UnifiedTaskCandidate(
            id=new_id,
            title=new_task_title or f"Split: {orig.title}",
            description=orig.description,
            status=orig.status,
            inferred_status=orig.inferred_status,
            status_authoritative=orig.status_authoritative,
            owner_canonical_id=orig.owner_canonical_id,
            owner_name=orig.owner_name,
            requester_canonical_id=orig.requester_canonical_id,
            requester_name=orig.requester_name,
            project_key=orig.project_key,
            customer_id=orig.customer_id,
            due_date=orig.due_date,
            explicit_deadline=orig.explicit_deadline,
            priority_score=orig.priority_score,
            priority_override=orig.priority_override,
            inferred_priority_score=orig.inferred_priority_score,
            extraction_confidence=orig.extraction_confidence,
            correlation_confidence=orig.correlation_confidence,
            review_status=orig.review_status,
            evidences=[e.model_copy(update={"task_id": new_id}) for e in detached],
            created_at=now,
            updated_at=now,
        )
        self.tasks[new_id] = new_task
        return new_task.model_copy(deep=True)


class FakeDeterministicLLMExtractor(LLMStructuredExtractor):
    """Deterministic LLM extractor test double that never calls external network APIs."""

    def __init__(
        self,
        custom_handler: Optional[Callable[[str], Optional[LLMExtractedSchema]]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            base_url="http://localhost:0/v1",
            api_key="fake-deterministic-test-key",
            model="fake-deterministic-model",
        )
        self.is_healthy: bool = True
        self.is_fake: bool = True
        self.calls: List[Dict[str, Any]] = []
        self._mock_handler: Optional[Callable[[str], Optional[LLMExtractedSchema]]] = custom_handler

    def set_mock_handler(
        self, handler: Optional[Callable[[str], Optional[LLMExtractedSchema]]]
    ) -> None:
        self._mock_handler = handler

    def _deterministic_extract(
        self,
        text: str,
        quoted_author: Optional[str] = None,
        quoted_content: Optional[str] = None,
        actual_author: Optional[str] = None,
    ) -> LLMExtractedSchema:
        lower_text = text.lower()

        is_commitment = any(
            kw in lower_text
            for kw in [
                "để em",
                "em sẽ",
                "mình sẽ",
                "on it",
                "will do",
                "will fix",
                "đang check",
                "đang xử lý",
            ]
        )
        is_request = any(
            kw in lower_text
            for kw in [
                "anh check",
                "nhờ em",
                "nhờ anh",
                "nhờ ",
                "review giúp",
                "cần làm",
                "pls fix",
                "please check",
                "can you check",
                "nhớ test",
                "hotfix",
            ]
        )

        if is_commitment:
            owner = actual_author or "Assignee"
            requester = quoted_author
            confidence = 0.88 if quoted_content else 0.80
        elif is_request:
            owner = quoted_author or "Dam Quang Cuong"
            requester = actual_author
            confidence = 0.78
        else:
            owner = actual_author
            requester = quoted_author
            confidence = 0.35

        if quoted_content:
            clean_q = re.sub(
                r"^(?:can you|please|nhờ|nhờ anh|nhờ em)\s*",
                "",
                quoted_content,
                flags=re.IGNORECASE,
            ).strip()
            title = clean_q[:100]
        else:
            title = text.strip()[:100]

        if not title:
            title = "Task from message"

        explicit_deadline = bool(
            re.search(
                r"(?:before|by|trước|hạn chót)\s+\d+|(?:\d{1,2}:\d{2}\s*ngày\s*\d{1,2}/\d{1,2})",
                lower_text,
            )
        )

        return LLMExtractedSchema(
            title=title,
            description=f"Auto-extracted context: {text}" if text != title else None,
            owner_name=owner,
            requester_name=requester,
            due_date=None,
            explicit_deadline=explicit_deadline,
            extraction_confidence=confidence,
            evidence_snippet=text[:250],
        )

    def extract_from_parsed(
        self,
        parsed: ParsedMessageContent,
        raw_event_id: str = "raw-local",
        source_type: str = "ms_teams",
        external_url: Optional[str] = None,
        project_key: Optional[str] = None,
    ) -> UnifiedTaskCandidate:
        actual_text = parsed.actual_content_text or ""
        quoted_text = parsed.quoted_content_text or ""
        quoted_author = parsed.quoted_author_raw
        actual_author = parsed.actual_author_raw

        self.calls.append(
            {
                "raw_event_id": raw_event_id,
                "source_type": source_type,
                "actual_text": actual_text,
                "quoted_text": quoted_text,
            }
        )

        extracted_schema: Optional[LLMExtractedSchema] = None
        if self._mock_handler:
            context_parts = []
            if quoted_text:
                context_parts.append(
                    f'Tin nhắn gốc (từ {quoted_author or "Người hỏi"}): "{quoted_text}"'
                )
            context_parts.append(
                f'Tin nhắn phản hồi (từ {actual_author or "Người trả lời"}): "{actual_text}"'
            )
            extracted_schema = self._mock_handler("\n".join(context_parts))

        if extracted_schema is None:
            extracted_schema = self._deterministic_extract(
                text=actual_text or quoted_text,
                quoted_author=quoted_author,
                quoted_content=quoted_text,
                actual_author=actual_author,
            )

        confidence = extracted_schema.extraction_confidence
        review_status = classify_review_status(confidence)

        task_id = str(uuid4())
        ev_id = str(uuid4())
        evidence_snippet = (
            extracted_schema.evidence_snippet or actual_text or quoted_text or ""
        )

        evidence = EvidenceRecord(
            id=ev_id,
            task_id=task_id,
            raw_event_id=raw_event_id,
            evidence_type=(
                EvidenceType.CHAT_COMMITMENT
                if parsed.is_quote_reply
                else EvidenceType.CHAT_REQUEST
            ),
            source_type=source_type,
            external_url=external_url,
            author_canonical_name=actual_author or extracted_schema.owner_name,
            timestamp=datetime.now(timezone.utc),
            snippet=evidence_snippet,
            confidence=confidence,
            extraction_version="v1.0",
        )

        return UnifiedTaskCandidate(
            id=task_id,
            title=extracted_schema.title,
            description=extracted_schema.description,
            status=TaskStatus.TODO,
            owner_name=extracted_schema.owner_name,
            requester_name=extracted_schema.requester_name,
            project_key=project_key,
            due_date=extracted_schema.due_date,
            explicit_deadline=extracted_schema.explicit_deadline,
            extraction_confidence=confidence,
            review_status=review_status,
            evidences=[evidence],
        )


class FakeGraphitiAdapter:
    """In-memory test double for GraphitiMemoryClient and GraphitiAdapter."""

    def __init__(
        self,
        episodes: Optional[List[Dict[str, Any]]] = None,
        is_available: bool = True,
        enabled: bool = True,
    ) -> None:
        self.episodes: List[Dict[str, Any]] = list(episodes or [])
        self.enabled: bool = enabled
        self.is_available: bool = is_available
        self.is_healthy: bool = is_available
        self.last_error: Optional[Exception] = None
        self.adapter = self

    def check_health(self) -> bool:
        return self.enabled and self.is_available and self.last_error is None

    async def add_episode(
        self,
        name: str,
        episode_body: str,
        source_description: str = "",
        reference_time: Any = None,
        source_type: Optional[str] = "text",
        group_id: Optional[str] = None,
        uuid: Optional[str] = None,
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        if not self.is_available:
            self.last_error = RuntimeError(
                f"FakeGraphitiAdapter is offline for episode '{name}'"
            )
            return None

        ep_id = uuid or str(uuid4())
        now_iso = datetime.now(timezone.utc).isoformat()
        ref_iso = (
            reference_time.isoformat()
            if hasattr(reference_time, "isoformat")
            else str(reference_time or now_iso)
        )
        record = {
            "id": ep_id,
            "name": name,
            "type": "EPISODIC",
            "topic": group_id or "ptb_global",
            "summary": source_description or name,
            "content": episode_body,
            "valid_at": ref_iso,
            "invalid_at": None,
            "edges": [],
            "properties": {
                "id": ep_id,
                "name": name,
                "episode_body": episode_body,
                "source_description": source_description,
                "source_type": source_type,
                "group_id": group_id,
            },
        }
        self.episodes.append(record)
        self.last_error = None
        return record

    async def add_evidence_episode(
        self,
        evidence: Union[EvidenceNodeRecord, Dict[str, Any]],
        task_id: Optional[str] = None,
        raw_event_id: Optional[str] = None,
    ) -> str:
        if not self.is_available:
            err = RuntimeError("FakeGraphitiAdapter is offline")
            self.last_error = err
            raise err

        ev_dict = (
            dict(evidence)
            if isinstance(evidence, dict)
            else (
                evidence.model_dump()
                if hasattr(evidence, "model_dump")
                else {"snippet": str(evidence)}
            )
        )
        ev_id = str(ev_dict.get("id") or uuid4())
        snippet = str(ev_dict.get("snippet") or "")
        source_type = str(ev_dict.get("source_type") or "UNKNOWN")
        eff_task_id = task_id or ev_dict.get("task_id")
        eff_raw_id = raw_event_id or ev_dict.get("raw_event_id")
        now_iso = datetime.now(timezone.utc).isoformat()

        record = {
            "id": ev_id,
            "type": "EVIDENCE",
            "topic": str(eff_task_id or ""),
            "summary": f"Evidence ({source_type})",
            "content": snippet,
            "valid_at": now_iso,
            "invalid_at": None,
            "task_id": eff_task_id,
            "raw_event_id": eff_raw_id,
            "edges": [],
            "properties": {
                **ev_dict,
                "id": ev_id,
                "task_id": eff_task_id,
                "raw_event_id": eff_raw_id,
                "episode_type": "EVIDENCE",
            },
        }
        self.episodes.append(record)
        self.last_error = None
        return ev_id

    async def add_decision_episode(
        self,
        decision: Union[DecisionNodeRecord, Dict[str, Any]],
        affects_task_id: Optional[str] = None,
        affects_project_key: Optional[str] = None,
    ) -> str:
        if not self.is_available:
            err = RuntimeError("FakeGraphitiAdapter is offline")
            self.last_error = err
            raise err

        dec_dict = (
            dict(decision)
            if isinstance(decision, dict)
            else (
                decision.model_dump()
                if hasattr(decision, "model_dump")
                else {"summary": str(decision)}
            )
        )
        dec_id = str(dec_dict.get("decision_id") or dec_dict.get("id") or uuid4())
        summary = str(dec_dict.get("summary") or "")
        rationale = str(dec_dict.get("rationale") or "")
        topic = str(dec_dict.get("topic") or affects_project_key or "")
        now_iso = datetime.now(timezone.utc).isoformat()

        record = {
            "id": dec_id,
            "type": "DECISION",
            "topic": topic,
            "summary": summary,
            "content": rationale,
            "valid_at": now_iso,
            "invalid_at": None,
            "edges": [],
            "properties": {
                **dec_dict,
                "decision_id": dec_id,
                "project_key": affects_project_key,
                "task_id": affects_task_id,
                "episode_type": "DECISION",
            },
        }
        self.episodes.append(record)
        self.last_error = None
        return dec_id

    async def add_lesson_episode(
        self,
        lesson: Union[LessonNodeRecord, Dict[str, Any]],
        derived_from_task_id: Optional[str] = None,
        related_incident_id: Optional[str] = None,
    ) -> str:
        if not self.is_available:
            err = RuntimeError("FakeGraphitiAdapter is offline")
            self.last_error = err
            raise err

        les_dict = (
            dict(lesson)
            if isinstance(lesson, dict)
            else (
                lesson.model_dump()
                if hasattr(lesson, "model_dump")
                else {"description": str(lesson)}
            )
        )
        les_id = str(les_dict.get("lesson_id") or les_dict.get("id") or uuid4())
        topic = str(les_dict.get("topic") or "")
        description = str(les_dict.get("description") or "")
        solution = str(les_dict.get("solution") or "")
        now_iso = datetime.now(timezone.utc).isoformat()

        record = {
            "id": les_id,
            "type": "LESSON",
            "topic": topic,
            "summary": topic or description,
            "content": solution or description,
            "valid_at": now_iso,
            "invalid_at": None,
            "edges": [],
            "properties": {
                **les_dict,
                "lesson_id": les_id,
                "derived_from_task_id": derived_from_task_id,
                "related_incident_id": related_incident_id,
                "episode_type": "LESSON",
            },
        }
        self.episodes.append(record)
        self.last_error = None
        return les_id

    async def invalidate_episode(
        self,
        episode_id: str,
        invalidated_at: Optional[datetime] = None,
    ) -> bool:
        inv_iso = (invalidated_at or datetime.now(timezone.utc)).isoformat()
        for ep in self.episodes:
            if ep.get("id") == episode_id:
                ep["invalid_at"] = inv_iso
                return True
        return False

    async def search_context(
        self,
        query: str = "",
        limit: int = 5,
        include_invalidated: bool = False,
    ) -> List[Dict[str, Any]]:
        q = (query or "").strip().lower()
        tokens = [tok for tok in q.split() if tok]
        matched: List[Dict[str, Any]] = []
        for ep in self.episodes:
            if not include_invalidated and ep.get("invalid_at") is not None:
                continue
            if not tokens:
                matched.append(ep)
                continue
            haystack = " ".join(
                str(ep.get(k, ""))
                for k in ("topic", "summary", "content", "name")
            ).lower()
            if q in haystack or any(tok in haystack for tok in tokens):
                matched.append(ep)
        if not matched and self.episodes:
            matched = [
                ep
                for ep in self.episodes
                if include_invalidated or ep.get("invalid_at") is None
            ]
        return matched[:limit]

    async def search(
        self,
        query: str,
        limit: int = 5,
        group_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        return await self.search_context(query=query, limit=limit)

    async def close(self) -> None:
        pass


__all__ = [
    "InMemoryRawEventRepository",
    "InMemoryCheckpointRepository",
    "InMemoryTaskDomainRepository",
    "FakeDeterministicLLMExtractor",
    "FakeGraphitiAdapter",
]
