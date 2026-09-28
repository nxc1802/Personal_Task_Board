"""Vertical Slice E2E Integration Test (Phase R17).

Tests the complete end-to-end flow across all PTB layers:
- Step 1 (Acquisition): Ingest fixture raw events via AcquisitionPipeline -> Persist into RawEventRepository with PENDING status.
- Step 2 (Processing): ProcessingWorker (1 iteration) -> Scans PENDING events -> Parse quote/reply -> Heuristic filter ->
                       Extract UnifiedTaskCandidate & Evidence -> Correlation / Merge if anchor present -> Save UnifiedTask
                       into TaskDomainRepository -> Mark raw event as PROCESSED.
- Step 3 (Intelligence): TaskIntelligenceLifecycle -> DeterministicPriorityEngine (config/priority.yaml) -> Priority breakdown & inferred status.
- Step 4 (Application & REST): FastAPI TestClient -> GET /api/today (task with priority score) -> POST /api/tasks/{id}/status ->
                              Update to IN_PROGRESS, enforce status_authoritative, verify audit log.
- Step 5 (Checkpoint & Idempotency): Re-run ingestion with identical payload -> Deduplication recognized, no duplicate tasks created.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient

from ptb_acquisition.pipeline import AcquisitionPipeline
from ptb_acquisition.playwright.outlook_interceptor import OutlookNetworkInterceptor
from ptb_acquisition.playwright.teams_interceptor import TeamsNetworkInterceptor
from ptb_application.api import app, get_application_service
from ptb_application.service import ApplicationService
from ptb_contracts import (
    CommitmentRecord,
    EvidenceRecord,
    EvidenceType,
    IngestionCheckpointRecord,
    MergeAuditRecord,
    ProcessingAttemptRecord,
    ProcessingStatus,
    RawEventRecord,
    ReviewQueueItem,
    SourceType,
    StatusTransitionAuditRecord,
    TaskStatus,
    UnifiedTaskCandidate,
)
from ptb_contracts.l4_intelligence import TaskWithContext
from ptb_intelligence.lifecycle import TaskIntelligenceLifecycle
from ptb_intelligence.priority import DeterministicPriorityEngine
from ptb_intelligence.status_machine import StatusInferenceMachine
from ptb_processing.extractor.llm_extractor import LLMStructuredExtractor
from ptb_processing.pipeline import ProcessingPipeline
from ptb_processing.worker import ProcessingWorker


FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


# ==============================================================================
# IN-MEMORY REPOSITORIES & CLIENT FIXTURES FOR E2E INTEGRATION
# ==============================================================================

class InMemoryRawEventRepository:
    """In-memory RawEventRepository implementing durability and idempotency checks."""

    def __init__(self) -> None:
        self.events: Dict[str, RawEventRecord] = {}
        self.idempotency_index: Dict[str, str] = {}
        self.attempt_records: List[ProcessingAttemptRecord] = []
        self.status_audit_log: List[dict] = []

    async def persist_raw_event(self, record: RawEventRecord) -> str:
        """MERGE on idempotency_key: returns existing ID if duplicate, else stores new."""
        if record.idempotency_key in self.idempotency_index:
            existing_id = self.idempotency_index[record.idempotency_key]
            return existing_id

        event_id = record.id or str(uuid4())
        record_copy = record.model_copy(deep=True)
        record_copy.id = event_id
        if not record_copy.payload_json and record_copy.raw_payload:
            record_copy.payload_json = json.dumps(record_copy.raw_payload, default=str)
        if not record_copy.content_hash and record_copy.normalized_text:
            record_copy.content_hash = hashlib.sha256(record_copy.normalized_text.encode("utf-8")).hexdigest()

        self.events[event_id] = record_copy
        self.idempotency_index[record.idempotency_key] = event_id
        return event_id

    async def save_raw_event(self, record: RawEventRecord) -> bool:
        await self.persist_raw_event(record)
        return True

    async def get_by_id(self, event_id: str) -> Optional[RawEventRecord]:
        return self.events.get(event_id)

    async def get_pending_raw_events(self, limit: int = 50) -> List[RawEventRecord]:
        now = datetime.now(timezone.utc)
        pending: List[RawEventRecord] = []
        for ev in self.events.values():
            if ev.processing_status in (
                ProcessingStatus.PENDING,
                ProcessingStatus.RETRY,
                ProcessingStatus.pending,
                ProcessingStatus.retry,
            ):
                if ev.next_retry_at is None or ev.next_retry_at <= now:
                    pending.append(ev)
        # Order by event_timestamp ASC
        pending.sort(key=lambda e: e.event_timestamp or datetime.min.replace(tzinfo=timezone.utc))
        return pending[:limit]

    async def mark_event_status(
        self,
        event_id: str,
        status: ProcessingStatus,
        error: Optional[str] = None,
        next_retry_at: Optional[datetime] = None,
        processed_at: Optional[datetime] = None,
        processor_version: Optional[str] = None,
    ) -> None:
        self.status_audit_log.append({
            "event_id": event_id,
            "status": status,
            "error": error,
            "next_retry_at": next_retry_at,
            "processed_at": processed_at,
            "processor_version": processor_version,
        })
        if event_id in self.events:
            ev = self.events[event_id]
            ev.processing_status = status
            ev.last_processing_error = error
            ev.next_retry_at = next_retry_at
            if processed_at:
                ev.processed_at = processed_at
            if processor_version:
                ev.processor_version = processor_version
            if status in (ProcessingStatus.RETRY, ProcessingStatus.FAILED):
                ev.retry_count = (ev.retry_count or 0) + 1
                ev.processing_attempt_count = (ev.processing_attempt_count or 0) + 1

    async def record_processing_attempt(self, attempt: ProcessingAttemptRecord) -> None:
        self.attempt_records.append(attempt)


class InMemoryTaskDomainRepository:
    """In-memory TaskDomainRepository implementing task storage, graph context, and audits."""

    def __init__(self, initial_tasks: Optional[List[UnifiedTaskCandidate]] = None) -> None:
        self.tasks: Dict[str, UnifiedTaskCandidate] = {
            t.id: t.model_copy(deep=True) for t in (initial_tasks or [])
        }
        self.audits: List[StatusTransitionAuditRecord] = []
        self.merge_audits: List[MergeAuditRecord] = []
        self.commitments: List[CommitmentRecord] = []

    async def upsert_task_atomic(self, task: UnifiedTaskCandidate) -> str:
        task_id = task.id or str(uuid4())
        task_copy = task.model_copy(deep=True)
        task_copy.id = task_id
        now = datetime.now(timezone.utc)
        if not task_copy.created_at:
            task_copy.created_at = now
        task_copy.updated_at = now

        self.tasks[task_id] = task_copy
        return task_id

    async def get_task_by_id(self, task_id: str) -> Optional[UnifiedTaskCandidate]:
        t = self.tasks.get(task_id)
        return t.model_copy(deep=True) if t else None

    async def list_tasks(self, filters: Optional[dict] = None) -> List[UnifiedTaskCandidate]:
        all_tasks = [t.model_copy(deep=True) for t in self.tasks.values()]
        if not filters:
            return all_tasks

        filtered = []
        for t in all_tasks:
            if "status" in filters and filters["status"]:
                req_status = filters["status"]
                if isinstance(req_status, (list, set, tuple)):
                    status_vals = [s.value if hasattr(s, "value") else str(s) for s in req_status]
                    if t.status.value not in status_vals:
                        continue
                else:
                    status_val = req_status.value if hasattr(req_status, "value") else str(req_status)
                    if t.status.value != status_val:
                        continue

            if "project" in filters and filters["project"]:
                if (t.project_key or "").lower() != str(filters["project"]).lower():
                    continue

            if "customer" in filters and filters["customer"]:
                if (t.customer_id or "").lower() != str(filters["customer"]).lower():
                    continue

            if "source" in filters and filters["source"]:
                req_source = str(filters["source"]).lower()
                if not any((ev.source_type or "").lower() == req_source for ev in t.evidences):
                    continue

            filtered.append(t)

        if "limit" in filters and isinstance(filters["limit"], int):
            return filtered[:filters["limit"]]
        return filtered

    async def get_active_tasks(self) -> List[UnifiedTaskCandidate]:
        return [
            t.model_copy(deep=True) for t in self.tasks.values()
            if t.status in (TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED)
        ]

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

    async def get_review_queue(self, limit: int = 20) -> List[ReviewQueueItem]:
        items: List[ReviewQueueItem] = []
        for t in self.tasks.values():
            if t.review_status == "pending_review" or (0.40 <= (t.extraction_confidence or 0.0) < 0.65):
                raw_id = t.evidences[0].raw_event_id if t.evidences else "raw-001"
                items.append(ReviewQueueItem(
                    id=f"rev-{t.id}",
                    raw_event_id=raw_id,
                    candidate_task=t.model_copy(deep=True),
                    reason=f"Confidence {t.extraction_confidence} requires human review",
                    created_at=t.created_at or datetime.now(timezone.utc),
                ))
                if len(items) >= limit:
                    break
        return items

    async def get_active_commitments(self, user_id: Optional[str] = None) -> List[CommitmentRecord]:
        return list(self.commitments)

    async def record_status_transition_audit(self, audit: StatusTransitionAuditRecord) -> str:
        self.audits.append(audit)
        return audit.id

    async def record_merge_audit(self, audit: MergeAuditRecord) -> str:
        self.merge_audits.append(audit)
        return audit.id

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
            raise ValueError(f"None of {evidence_ids_to_detach} found in task {original_task_id}")

        orig.evidences = [e for e in orig.evidences if e.id not in evidence_ids_to_detach]
        new_id = str(uuid4())
        new_task = UnifiedTaskCandidate(
            id=new_id,
            title=new_task_title or f"Split: {orig.title}",
            description=orig.description,
            status=orig.status,
            evidences=[e.model_copy(update={"task_id": new_id}) for e in detached],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self.tasks[new_id] = new_task
        return new_task


class InMemoryCheckpointRepository:
    """In-memory CheckpointRepository tracking source sync positions."""

    def __init__(self) -> None:
        self.checkpoints: Dict[str, IngestionCheckpointRecord] = {}

    def _key(self, source_type: Any, stream_id: str, tenant_id: Optional[str] = None) -> str:
        st = source_type.value if hasattr(source_type, "value") else str(source_type)
        t = tenant_id or "default"
        return f"{t}:{st}:{stream_id}"

    async def get_checkpoint(
        self, source_type: str, stream_id: str, tenant_id: Optional[str] = None
    ) -> Optional[IngestionCheckpointRecord]:
        key = self._key(source_type, stream_id, tenant_id)
        return self.checkpoints.get(key)

    async def save_checkpoint(self, checkpoint: IngestionCheckpointRecord) -> None:
        key = self._key(checkpoint.source_type, checkpoint.stream_id, checkpoint.tenant_id)
        self.checkpoints[key] = checkpoint.model_copy(deep=True)

    async def list_checkpoints(self) -> List[IngestionCheckpointRecord]:
        return list(self.checkpoints.values())

    async def cleanup_duplicate_checkpoints(self) -> int:
        return 0


class MockGraphitiMemoryClient:
    """Mock Graphiti memory client for knowledge search during E2E."""

    def __init__(self, episodes: Optional[List[Dict[str, Any]]] = None) -> None:
        self.episodes = episodes or []

    async def search_context(self, query: str, limit: int = 5, include_invalidated: bool = False) -> List[Dict[str, Any]]:
        return self.episodes[:limit]


# ==============================================================================
# VERTICAL SLICE E2E INTEGRATION TEST (5 STEPS)
# ==============================================================================

@pytest.mark.asyncio
async def test_vertical_slice_e2e_teams_flow():
    """Complete 5-step Vertical Slice E2E flow for Teams Web message ingestion & processing."""
    # --------------------------------------------------------------------------
    # Khởi tạo repositories và dependencies
    # --------------------------------------------------------------------------
    raw_event_repo = InMemoryRawEventRepository()
    task_repo = InMemoryTaskDomainRepository()
    checkpoint_repo = InMemoryCheckpointRepository()

    # Load deterministic priority engine từ config/priority.yaml (authoritative)
    config_file = Path("config/priority.yaml")
    assert config_file.is_file(), "config/priority.yaml must exist for Phase R17"
    priority_engine = DeterministicPriorityEngine(config_path=config_file)
    assert priority_engine.deadline_weight == 35.0
    assert priority_engine.commitment_weight == 10.0
    assert priority_engine.production_impact_weight == 20.0

    lifecycle = TaskIntelligenceLifecycle(
        task_repo=task_repo,
        priority_engine=priority_engine,
        status_machine=StatusInferenceMachine(),
    )

    # --------------------------------------------------------------------------
    # BƯỚC 1 (Acquisition): Ingest fixture raw events qua AcquisitionPipeline
    # --------------------------------------------------------------------------
    fixture_path = FIXTURES_DIR / "teams_messages_fixture.json"
    with open(fixture_path, "r", encoding="utf-8") as f:
        teams_payload = json.load(f)

    interceptor = TeamsNetworkInterceptor(tenant_id="tenant-e2e-slice")
    raw_records = interceptor.parse_payload(teams_payload)
    assert len(raw_records) >= 1

    # Chọn message 1: có quoted reply, mention, task commitment "để em fix bug này trước 5h chiều", anchor OPS-88
    commitment_record = raw_records[0]

    pipeline = AcquisitionPipeline(
        raw_event_repo=raw_event_repo,
        checkpoint_repo=checkpoint_repo,
        tenant_id="tenant-e2e-slice",
    )

    # Ingest event vào pipeline
    ingested = await pipeline.ingest_event(commitment_record)
    assert ingested is True
    assert pipeline.stats["ingested"] == 1
    assert pipeline.stats["persisted"] == 1

    # Xác nhận event được persist vào RawEventRepository với trạng thái PENDING
    pending_events = await raw_event_repo.get_pending_raw_events()
    assert len(pending_events) == 1
    assert pending_events[0].id == commitment_record.id
    assert pending_events[0].processing_status == ProcessingStatus.PENDING
    assert pending_events[0].idempotency_key == commitment_record.idempotency_key
    assert pending_events[0].payload_json is not None

    # --------------------------------------------------------------------------
    # BƯỚC 2 (Processing): Khởi chạy ProcessingWorker (1 iteration)
    # --------------------------------------------------------------------------
    # Cấu hình ProcessingPipeline với mock extractor
    extractor = LLMStructuredExtractor(mock_mode=True)
    processing_pipeline = ProcessingPipeline(
        task_repo=task_repo,
        llm_extractor=extractor,
    )

    worker = ProcessingWorker(
        raw_event_repo=raw_event_repo,
        pipeline=processing_pipeline,
        max_retries=3,
    )

    # Chạy 1 iteration batch processing
    batch_statuses = await worker.process_batch(limit=10)
    assert batch_statuses == [ProcessingStatus.PROCESSED]

    # Kiểm tra RawEvent trong repo được đánh dấu là PROCESSED
    processed_event = await raw_event_repo.get_by_id(commitment_record.id)
    assert processed_event is not None
    assert processed_event.processing_status == ProcessingStatus.PROCESSED
    assert processed_event.processed_at is not None

    # Kiểm tra ProcessingAttemptRecord thành công
    assert len(raw_event_repo.attempt_records) == 1
    assert raw_event_repo.attempt_records[0].status == ProcessingStatus.PROCESSED
    assert raw_event_repo.attempt_records[0].raw_event_id == commitment_record.id

    # Kiểm tra UnifiedTask được trích xuất và lưu vào TaskDomainRepository
    saved_tasks = await task_repo.list_tasks()
    assert len(saved_tasks) == 1
    created_task = saved_tasks[0]
    assert created_task.status == TaskStatus.TODO
    assert created_task.owner_name == "Dam Quang Cuong"
    assert len(created_task.evidences) == 1

    # Kiểm tra Evidence lưu trữ đầy đủ provenance trỏ về RawEvent
    evidence = created_task.evidences[0]
    assert evidence.raw_event_id == commitment_record.id
    assert evidence.evidence_type == EvidenceType.CHAT_COMMITMENT
    assert "OPS-88" in created_task.title
    assert "để em fix bug này trước 5h chiều" in evidence.snippet

    # --------------------------------------------------------------------------
    # BƯỚC 2B (Correlation / Merge nếu có anchor):
    # Giả lập 1 raw event thứ hai cũng chứa anchor OPS-88 từ git/chat
    # --------------------------------------------------------------------------
    second_raw_event = RawEventRecord(
        id="raw-followup-ops88",
        tenant_id="tenant-e2e-slice",
        source_type=SourceType.MS_TEAMS_WEB,
        external_id="msg-followup-99",
        idempotency_key=hashlib.sha256(b"followup-ops88-key").hexdigest(),
        event_timestamp=datetime.now(timezone.utc),
        author_external_id="lead.tran@company.com",
        author_display_name="Tran Van Lead",
        conversation_or_project_id="19:devops_core_team@thread.v2",
        raw_payload={"body": {"content": "<p>Nhớ test kỹ hotfix OPS-88 trước khi deploy nhé.</p>"}},
        normalized_text="Nhớ test kỹ hotfix OPS-88 trước khi deploy nhé.",
        processing_status=ProcessingStatus.PENDING,
    )
    await pipeline.ingest_event(second_raw_event)
    assert len(await raw_event_repo.get_pending_raw_events()) == 1

    # Chạy worker lần 2 -> Tự động nhận diện anchor OPS-88 và auto-merge vào task cũ
    batch_2 = await worker.process_batch(limit=10)
    assert batch_2 == [ProcessingStatus.PROCESSED]

    # Kiểm tra không tạo thêm task mới mà gộp vào task hiện tại
    tasks_after_merge = await task_repo.list_tasks()
    assert len(tasks_after_merge) == 1
    merged_task = tasks_after_merge[0]
    assert merged_task.id == created_task.id
    # Cả 2 evidence từ 2 raw events đều được bảo toàn 100%
    assert len(merged_task.evidences) == 2
    assert any(e.raw_event_id == commitment_record.id for e in merged_task.evidences)
    assert any(e.raw_event_id == "raw-followup-ops88" for e in merged_task.evidences)
    assert len(task_repo.merge_audits) == 1
    assert "OPS-88" in str(task_repo.merge_audits[0].deterministic_anchors)

    # --------------------------------------------------------------------------
    # BƯỚC 3 (Intelligence): Kích hoạt TaskIntelligenceLifecycle & Priority Engine
    # --------------------------------------------------------------------------
    lifecycle_res = await lifecycle.on_task_changed(merged_task.id)
    updated_task, priority_breakdown, transition_result = lifecycle_res

    # Điểm ưu tiên được tính toán dựa trên config/priority.yaml:
    # Có CHAT_COMMITMENT -> commitment_weight = 10.0
    # Có keyword "OPS-88" / "hotfix" / "production" -> production_impact_score > 0
    assert priority_breakdown.commitment_weight == 10.0
    assert priority_breakdown.production_impact_score > 0.0
    assert priority_breakdown.total_score > 0.0
    assert updated_task.priority_score == priority_breakdown.total_score

    # Kiểm tra dữ liệu được lưu bền bỉ trong TaskDomainRepository
    task_in_db = await task_repo.get_task_by_id(merged_task.id)
    assert task_in_db is not None
    assert task_in_db.priority_score == priority_breakdown.total_score

    # --------------------------------------------------------------------------
    # BƯỚC 4 (Application & REST): Gọi REST API qua FastAPI TestClient
    # --------------------------------------------------------------------------
    app_service = ApplicationService(
        task_repo=task_repo,
        raw_event_repo=raw_event_repo,
        checkpoint_repo=checkpoint_repo,
        priority_engine=priority_engine,
        lifecycle=lifecycle,
        graph_memory=MockGraphitiMemoryClient(),
    )

    app.dependency_overrides[get_application_service] = lambda: app_service

    with TestClient(app) as client:
        # 4a. GET /api/today: task xuất hiện trên Today Board với priority score chính xác
        resp_today = client.get("/api/today?user_id=default")
        assert resp_today.status_code == 200
        today_data = resp_today.json()
        assert "top_tasks" in today_data
        assert len(today_data["top_tasks"]) >= 1

        top_task_entry = today_data["top_tasks"][0]
        assert top_task_entry["task_id"] == merged_task.id
        assert top_task_entry["priority"]["total_score"] == priority_breakdown.total_score

        # Kiểm tra chi tiết task qua GET /api/tasks/{id}
        resp_detail = client.get(f"/api/tasks/{merged_task.id}")
        assert resp_detail.status_code == 200
        assert resp_detail.json()["task"]["priority_score"] == priority_breakdown.total_score

        # 4b. POST /api/tasks/{id}/status: chuyển sang IN_PROGRESS
        status_payload = {"status": "IN_PROGRESS", "actor": "USER"}
        resp_status = client.post(f"/api/tasks/{merged_task.id}/status", json=status_payload)
        assert resp_status.status_code == 200
        assert resp_status.json()["success"] is True

        # Xác nhận status_authoritative và trạng thái trong repo
        final_task = await task_repo.get_task_by_id(merged_task.id)
        assert final_task is not None
        assert final_task.status == TaskStatus.IN_PROGRESS
        assert final_task.status_authoritative is True

        # Xác nhận audit log được ghi lại đầy đủ
        assert len(task_repo.audits) >= 1
        latest_audit = task_repo.audits[-1]
        assert latest_audit.task_id == merged_task.id
        assert latest_audit.old_status == TaskStatus.TODO
        assert latest_audit.new_status == TaskStatus.IN_PROGRESS
        assert latest_audit.change_actor == "USER"

    app.dependency_overrides.clear()

    # --------------------------------------------------------------------------
    # BƯỚC 5 (Checkpoint & Idempotency): Tái chạy ingestion cùng payload ban đầu
    # --------------------------------------------------------------------------
    # 5a. Thử ingest lại cùng raw_record ban đầu qua AcquisitionPipeline
    reingest_result = await pipeline.ingest_event(commitment_record)
    assert reingest_result is False
    assert pipeline.stats["deduplicated"] >= 1

    # 5b. Thử ingest qua pipeline mới (mô phỏng restart hệ thống)
    fresh_pipeline = AcquisitionPipeline(
        raw_event_repo=raw_event_repo,
        checkpoint_repo=checkpoint_repo,
        tenant_id="tenant-e2e-slice",
    )
    # Repo đã ghi nhận idempotency_key -> persist_raw_event trả về ID cũ và không tạo mới
    returned_id = await raw_event_repo.persist_raw_event(commitment_record)
    assert returned_id == commitment_record.id

    # 5c. Khởi chạy worker lần nữa -> không có pending event nào mới
    empty_batch = await worker.process_batch(limit=10)
    assert empty_batch == []

    # 5d. Xác nhận số lượng task trong TaskDomainRepository vẫn duy nhất 1 task
    final_tasks_count = await task_repo.list_tasks()
    assert len(final_tasks_count) == 1


@pytest.mark.asyncio
async def test_vertical_slice_e2e_outlook_flow():
    """Vertical Slice E2E flow for Outlook Web email request with explicit deadline."""
    raw_event_repo = InMemoryRawEventRepository()
    task_repo = InMemoryTaskDomainRepository()
    checkpoint_repo = InMemoryCheckpointRepository()
    priority_engine = DeterministicPriorityEngine(config_path="config/priority.yaml")
    lifecycle = TaskIntelligenceLifecycle(task_repo=task_repo, priority_engine=priority_engine)

    # 1. Acquisition: Outlook Fixture
    fixture_path = FIXTURES_DIR / "outlook_messages_fixture.json"
    with open(fixture_path, "r", encoding="utf-8") as f:
        outlook_payload = json.load(f)

    interceptor = OutlookNetworkInterceptor(tenant_id="tenant-e2e-outlook")
    raw_records = interceptor.parse_payload(outlook_payload)
    email_record = raw_records[0]

    pipeline = AcquisitionPipeline(
        raw_event_repo=raw_event_repo,
        checkpoint_repo=checkpoint_repo,
        tenant_id="tenant-e2e-outlook",
    )
    await pipeline.ingest_event(email_record)

    # 2. Processing
    worker = ProcessingWorker(
        raw_event_repo=raw_event_repo,
        pipeline=ProcessingPipeline(task_repo=task_repo, llm_extractor=LLMStructuredExtractor(mock_mode=True)),
    )
    statuses = await worker.process_batch()
    assert statuses == [ProcessingStatus.PROCESSED]

    tasks = await task_repo.list_tasks()
    assert len(tasks) == 1
    task = tasks[0]
    assert "review" in task.title.lower() or "personal task board" in task.title.lower()
    assert task.explicit_deadline is True

    # 3. Intelligence: Tính toán priority
    lifecycle_res = await lifecycle.on_task_changed(task.id)
    assert lifecycle_res.priority_breakdown.total_score > 0.0

    # 4. REST API verification
    app_service = ApplicationService(
        task_repo=task_repo,
        raw_event_repo=raw_event_repo,
        checkpoint_repo=checkpoint_repo,
        priority_engine=priority_engine,
        lifecycle=lifecycle,
        graph_memory=MockGraphitiMemoryClient(),
    )
    app.dependency_overrides[get_application_service] = lambda: app_service
    with TestClient(app) as client:
        resp = client.get(f"/api/tasks/{task.id}")
        assert resp.status_code == 200
        assert resp.json()["task"]["id"] == task.id
    app.dependency_overrides.clear()

    # 5. Idempotency: re-ingest duplicate email
    assert await pipeline.ingest_event(email_record) is False
    assert len(await task_repo.list_tasks()) == 1
