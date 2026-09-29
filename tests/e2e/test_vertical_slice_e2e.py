"""Vertical Slice E2E Integration Test (Wave 6 — Sub-Agent 6B).

Tests the complete end-to-end production flow across all PTB layers using test doubles
exclusively from tests.support.test_doubles:
Teams/Outlook fixture
-> real TeamsNetworkInterceptor / OutlookNetworkInterceptor
-> real AcquisitionPipeline
-> test InMemoryRawEventRepository & InMemoryCheckpointRepository
-> real ProcessingWorker
-> real ProcessingPipeline (with auto-wired TaskIntelligenceLifecycle & GraphMemorySyncWorker)
-> FakeDeterministicLLMExtractor
-> task persisted in InMemoryTaskDomainRepository
-> intelligence automatically computed
-> FakeGraphitiAdapter episodes synchronized
-> FastAPI TestClient
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from ptb_acquisition.pipeline import AcquisitionPipeline
from ptb_acquisition.playwright.outlook_interceptor import OutlookNetworkInterceptor
from ptb_acquisition.playwright.teams_interceptor import TeamsNetworkInterceptor
from ptb_application.api import app, get_application_service
from ptb_application.service import ApplicationService
from ptb_contracts import (
    EvidenceType,
    IngestionCheckpointRecord,
    ProcessingStatus,
    RawEventRecord,
    SourceType,
    TaskStatus,
)
from ptb_graph_memory import GraphMemorySyncWorker, GraphSyncStatus
from ptb_intelligence.lifecycle import TaskIntelligenceLifecycle
from ptb_intelligence.priority import DeterministicPriorityEngine
from ptb_intelligence.status_machine import StatusInferenceMachine
from ptb_processing.pipeline import ProcessingPipeline
from ptb_processing.worker import ProcessingWorker
from tests.support.test_doubles import (
    FakeDeterministicLLMExtractor,
    FakeGraphitiAdapter,
    InMemoryCheckpointRepository,
    InMemoryRawEventRepository,
    InMemoryTaskDomainRepository,
)


FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.mark.fixture_e2e
@pytest.mark.asyncio
async def test_vertical_slice_e2e_teams_flow():
    """Complete 5-step Vertical Slice E2E flow for Teams Web message ingestion & processing."""
    # --------------------------------------------------------------------------
    # Khởi tạo repositories và test doubles từ tests.support.test_doubles
    # --------------------------------------------------------------------------
    raw_event_repo = InMemoryRawEventRepository()
    checkpoint_repo = InMemoryCheckpointRepository()
    fake_graphiti = FakeGraphitiAdapter()
    graph_sync_worker = GraphMemorySyncWorker(memory_client=fake_graphiti)
    task_repo = InMemoryTaskDomainRepository(graph_sync_worker=graph_sync_worker)
    llm_extractor = FakeDeterministicLLMExtractor()

    # Load deterministic priority engine từ config/priority.yaml (authoritative)
    config_file = Path("config/priority.yaml")
    assert config_file.is_file(), "config/priority.yaml must exist"
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

    # Lưu và kiểm tra checkpoint theo composite key (tenant_id, source_type, stream_id)
    ckpt = IngestionCheckpointRecord(
        id="",
        tenant_id="tenant-e2e-slice",
        source_type=SourceType.MS_TEAMS_WEB,
        stream_id=commitment_record.conversation_or_project_id,
        last_external_id=commitment_record.external_id,
        last_event_timestamp=commitment_record.event_timestamp,
    )
    await checkpoint_repo.save_checkpoint(ckpt)
    loaded_ckpt = await checkpoint_repo.get_checkpoint(
        SourceType.MS_TEAMS_WEB,
        commitment_record.conversation_or_project_id,
        tenant_id="tenant-e2e-slice",
    )
    assert loaded_ckpt is not None
    assert loaded_ckpt.last_external_id == commitment_record.external_id

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
    processing_pipeline = ProcessingPipeline(
        task_repo=task_repo,
        llm_extractor=llm_extractor,
        intelligence_lifecycle=lifecycle,
    )

    worker = ProcessingWorker(
        raw_event_repo=raw_event_repo,
        pipeline=processing_pipeline,
        max_retries=3,
        intelligence_lifecycle=lifecycle,
    )

    # Chạy 1 iteration batch processing
    batch_statuses = await worker.process_batch(limit=10)
    assert batch_statuses == [ProcessingStatus.PROCESSED]
    assert len(llm_extractor.calls) == 1

    # Kiểm tra RawEvent trong repo được đánh dấu là PROCESSED và attempt_count == 1
    processed_event = await raw_event_repo.get_by_id(commitment_record.id)
    assert processed_event is not None
    assert processed_event.processing_status == ProcessingStatus.PROCESSED
    assert processed_event.processed_at is not None
    assert processed_event.processing_attempt_count == 1

    # Kiểm tra ProcessingAttemptRecord thành công
    assert len(raw_event_repo.attempt_records) == 1
    assert raw_event_repo.attempt_records[0].status == ProcessingStatus.PROCESSED
    assert raw_event_repo.attempt_records[0].raw_event_id == commitment_record.id
    assert raw_event_repo.attempt_records[0].attempt_number == 1

    # Kiểm tra UnifiedTask được trích xuất và lưu vào TaskDomainRepository
    saved_tasks = await task_repo.list_tasks()
    assert len(saved_tasks) == 1
    created_task = saved_tasks[0]
    assert created_task.status == TaskStatus.TODO
    assert created_task.owner_name == "Dam Quang Cuong"
    assert len(created_task.evidences) == 1
    # Tự động tính toán priority ngay sau khi worker xử lý
    assert created_task.priority_score is not None
    assert created_task.priority_score > 0.0

    # Kiểm tra Evidence lưu trữ đầy đủ provenance trỏ về RawEvent
    evidence = created_task.evidences[0]
    assert evidence.raw_event_id == commitment_record.id
    assert evidence.evidence_type == EvidenceType.CHAT_COMMITMENT
    assert "OPS-88" in created_task.title
    assert "để em fix bug này trước 5h chiều" in evidence.snippet

    # Kiểm tra GraphMemorySyncWorker đã đồng bộ Evidence sang FakeGraphitiAdapter
    assert graph_sync_worker.get_sync_status(evidence.id, "evidence") == GraphSyncStatus.SYNCED
    assert len(fake_graphiti.episodes) == 1
    assert fake_graphiti.episodes[0]["id"] == evidence.id
    assert fake_graphiti.episodes[0]["task_id"] == created_task.id

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

    # Cả 2 evidence đều đã được đồng bộ sang FakeGraphitiAdapter
    assert len(fake_graphiti.episodes) == 2
    for ev in merged_task.evidences:
        assert graph_sync_worker.get_sync_status(ev.id, "evidence") == GraphSyncStatus.SYNCED

    # --------------------------------------------------------------------------
    # BƯỚC 3 (Intelligence): Tự động tính toán priority & inferred status
    # Tuyệt đối KHÔNG gọi thủ công lifecycle.on_task_changed() trong test E2E.
    # Pipeline đã tự động kích hoạt recalculation và persist vào TaskDomainRepository.
    # --------------------------------------------------------------------------
    task_in_db = await task_repo.get_task_by_id(merged_task.id)
    assert task_in_db is not None
    assert task_in_db.priority_score is not None
    assert task_in_db.priority_score > 0.0

    priority_breakdown = priority_engine.calculate_priority(task_in_db)
    assert priority_breakdown.commitment_weight == 10.0
    assert priority_breakdown.production_impact_score > 0.0
    assert priority_breakdown.total_score > 0.0
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
        graph_memory=fake_graphiti,
        processing_pipeline=processing_pipeline,
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
        assert top_task_entry["priority"]["total_score"] == task_in_db.priority_score
        assert top_task_entry["priority"]["total_score"] > 0.0

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
    reingest_result = await pipeline.ingest_event(commitment_record)
    assert reingest_result is False
    assert pipeline.stats["deduplicated"] >= 1

    # Repo đã ghi nhận idempotency_key -> persist_raw_event trả về ID cũ và không tạo mới
    returned_id = await raw_event_repo.persist_raw_event(commitment_record)
    assert returned_id == commitment_record.id

    # Khởi chạy worker lần nữa -> không có pending event nào mới
    empty_batch = await worker.process_batch(limit=10)
    assert empty_batch == []

    # Xác nhận số lượng task trong TaskDomainRepository vẫn duy nhất 1 task
    final_tasks_count = await task_repo.list_tasks()
    assert len(final_tasks_count) == 1


@pytest.mark.fixture_e2e
@pytest.mark.asyncio
async def test_vertical_slice_e2e_outlook_flow():
    """Vertical Slice E2E flow for Outlook Web email request with explicit deadline."""
    raw_event_repo = InMemoryRawEventRepository()
    checkpoint_repo = InMemoryCheckpointRepository()
    fake_graphiti = FakeGraphitiAdapter()
    graph_sync_worker = GraphMemorySyncWorker(memory_client=fake_graphiti)
    task_repo = InMemoryTaskDomainRepository(graph_sync_worker=graph_sync_worker)
    llm_extractor = FakeDeterministicLLMExtractor()

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

    # 2. Processing (với TaskIntelligenceLifecycle & GraphMemorySyncWorker tự động)
    processing_pipeline = ProcessingPipeline(
        task_repo=task_repo,
        llm_extractor=llm_extractor,
        intelligence_lifecycle=lifecycle,
    )
    worker = ProcessingWorker(
        raw_event_repo=raw_event_repo,
        pipeline=processing_pipeline,
        intelligence_lifecycle=lifecycle,
    )
    statuses = await worker.process_batch()
    assert statuses == [ProcessingStatus.PROCESSED]

    tasks = await task_repo.list_tasks()
    assert len(tasks) == 1
    task = tasks[0]
    assert "review" in task.title.lower() or "personal task board" in task.title.lower()
    assert task.explicit_deadline is True

    # 3. Intelligence & Graphiti Sync: Tự động tính toán priority và sync episode
    assert task.priority_score is not None
    assert task.priority_score > 0.0
    assert len(task.evidences) == 1
    assert graph_sync_worker.get_sync_status(task.evidences[0].id, "evidence") == GraphSyncStatus.SYNCED
    assert len(fake_graphiti.episodes) == 1

    # 4. REST API verification
    app_service = ApplicationService(
        task_repo=task_repo,
        raw_event_repo=raw_event_repo,
        checkpoint_repo=checkpoint_repo,
        priority_engine=priority_engine,
        lifecycle=lifecycle,
        graph_memory=fake_graphiti,
        processing_pipeline=processing_pipeline,
    )
    app.dependency_overrides[get_application_service] = lambda: app_service
    with TestClient(app) as client:
        # GET /api/today kiểm tra task có priority tự động ngay sau khi worker xử lý
        resp_today = client.get("/api/today?user_id=default")
        assert resp_today.status_code == 200
        today_data = resp_today.json()
        assert len(today_data.get("top_tasks", [])) >= 1
        assert today_data["top_tasks"][0]["task_id"] == task.id
        assert today_data["top_tasks"][0]["priority"]["total_score"] == task.priority_score
        assert today_data["top_tasks"][0]["priority"]["total_score"] > 0.0

        # GET /api/tasks/{id}
        resp = client.get(f"/api/tasks/{task.id}")
        assert resp.status_code == 200
        assert resp.json()["task"]["id"] == task.id
        assert resp.json()["task"]["priority_score"] == task.priority_score
    app.dependency_overrides.clear()

    # 5. Idempotency: re-ingest duplicate email
    assert await pipeline.ingest_event(email_record) is False
    assert len(await task_repo.list_tasks()) == 1
