"""Unit tests for ProcessingWorker and Pipeline Orchestration (Task 1 & Task 2).

Tests:
1. Worker poll -> process -> mark PROCESSED.
2. Worker noise/chatter handling -> heuristic filter -> mark PROCESSED without LLM call.
3. Non-silent retry when LLM fails without allow_heuristic_fallback flag (transitions to RETRY with next_retry_at).
4. Heuristic fallback when PTB_ALLOW_HEURISTIC_FALLBACK=true (marks PROCESSED).
5. Deterministic retry count: attempt 1 (RETRY) -> attempt 2 (RETRY) -> attempt 3 (FAILED).
6. Worker resilience: unexpected exception never crashes the worker loop or batch processing.
7. Exponential backoff calculation for retries.
8. Pipeline correlation auto-merge integration.
9. Pipeline identity resolution invariant rule verification.
"""

from datetime import datetime, timedelta, timezone
import json
from typing import Dict, List, Optional
from unittest.mock import MagicMock, patch
from uuid import uuid4
import pytest

from ptb_contracts import (
    EvidenceRecord,
    EvidenceType,
    ProcessingAttemptRecord,
    ProcessingStatus,
    RawEventRecord,
    SourceType,
    TaskStatus,
    UnifiedTaskCandidate,
)
from ptb_processing import (
    AttributionValidator,
    HeuristicCandidateFilter,
    IdentityResolver,
    LLMExtractionError,
    LLMStructuredExtractor,
    ProcessingPipeline,
    ProcessingWorker,
    TaskCandidateMatcher,
    TaskMerger,
    TeamsQuoteReplyParser,
)
from ptb_processing.extractor.llm_extractor import LLMExtractedSchema
from tests.support.test_doubles import FakeDeterministicLLMExtractor


# ==============================================================================
# FAKE IN-MEMORY REPOSITORIES FOR ISOLATED TESTING
# ==============================================================================

class FakeRawEventRepo:
    """In-memory mock for RawEventRepository."""

    def __init__(self, events: Optional[List[RawEventRecord]] = None) -> None:
        self.events: Dict[str, RawEventRecord] = {e.id: e for e in (events or [])}
        self.status_calls: List[dict] = []
        self.attempt_records: List[ProcessingAttemptRecord] = []

    async def get_pending_raw_events(self, limit: int = 50) -> List[RawEventRecord]:
        now = datetime.now(timezone.utc)
        pending: List[RawEventRecord] = []
        for e in self.events.values():
            if e.processing_status in (
                ProcessingStatus.PENDING,
                ProcessingStatus.RETRY,
                ProcessingStatus.pending,
                ProcessingStatus.retry,
            ):
                if e.next_retry_at is None or e.next_retry_at <= now:
                    pending.append(e)
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
        if event_id in self.events:
            ev = self.events[event_id]
            ev.processing_status = status
            ev.last_processing_error = error
            ev.next_retry_at = next_retry_at
            if processed_at:
                ev.processed_at = processed_at
            if status in (
                ProcessingStatus.PROCESSING,
                ProcessingStatus.processing,
                "processing",
                "PROCESSING",
            ):
                count = (ev.processing_attempt_count or ev.retry_count or 0) + 1
                ev.processing_attempt_count = count
                ev.retry_count = count

        self.status_calls.append({
            "event_id": event_id,
            "status": status,
            "error": error,
            "next_retry_at": next_retry_at,
            "processed_at": processed_at,
            "processor_version": processor_version,
            "attempt_count": self.events[event_id].processing_attempt_count if event_id in self.events else None,
        })

    async def record_processing_attempt(self, attempt: ProcessingAttemptRecord) -> None:
        self.attempt_records.append(attempt)


class FakeTaskDomainRepo:
    """In-memory mock for TaskDomainRepository."""

    def __init__(self, tasks: Optional[List[UnifiedTaskCandidate]] = None) -> None:
        self.tasks: Dict[str, UnifiedTaskCandidate] = {t.id: t for t in (tasks or [])}
        self.upserted_tasks: List[UnifiedTaskCandidate] = []

    async def list_tasks(self, filters: Optional[dict] = None) -> List[UnifiedTaskCandidate]:
        return list(self.tasks.values())

    async def get_active_tasks(self) -> List[UnifiedTaskCandidate]:
        return [
            t for t in self.tasks.values()
            if t.status in (TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED)
        ]

    async def upsert_task_atomic(self, task: UnifiedTaskCandidate) -> str:
        self.tasks[task.id] = task
        self.upserted_tasks.append(task)
        return task.id


def create_sample_raw_event(
    event_id: str = "raw-ev-001",
    content: str = "<div><p>Để em check issue OPS-88 nhé anh.</p></div>",
    author_name: str = "Dam Quang Cuong",
    author_email: str = "cuong.dam@fpt.com",
    retry_count: int = 0,
    status: ProcessingStatus = ProcessingStatus.PENDING,
) -> RawEventRecord:
    return RawEventRecord(
        id=event_id,
        tenant_id="tenant-fpt-internal",
        source_type=SourceType.MS_TEAMS,
        external_id=f"ext-{event_id}",
        idempotency_key=f"idemp-{event_id}",
        author_external_id=author_email,
        author_display_name=author_name,
        conversation_or_project_id="channel-devops",
        event_timestamp=datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc),
        raw_payload={
            "body": {"content": content, "contentType": "html"},
            "from": {"user": {"displayName": author_name, "id": author_email}},
        },
        normalized_text="Để em check issue OPS-88 nhé anh.",
        processing_status=status,
        retry_count=retry_count,
        processing_attempt_count=retry_count,
    )


# ==============================================================================
# 1. TEST WORKER POLL -> PROCESS -> MARK PROCESSED
# ==============================================================================

@pytest.mark.asyncio
async def test_worker_poll_and_process_success():
    """Test worker poll batch RawEvents, chạy pipeline trích xuất thành công và mark PROCESSED."""
    raw_ev = create_sample_raw_event(
        event_id="raw-success-01",
        content="<blockquote><strong>Huy</strong>: Can you fix OPS-88?</blockquote><p>Để em check nhé.</p>",
    )
    raw_repo = FakeRawEventRepo([raw_ev])
    task_repo = FakeTaskDomainRepo()

    # Sử dụng test double extractor thành công
    extractor = FakeDeterministicLLMExtractor()
    pipeline = ProcessingPipeline(
        task_repo=task_repo,
        llm_extractor=extractor,
    )

    worker = ProcessingWorker(
        raw_event_repo=raw_repo,
        pipeline=pipeline,
        base_backoff_seconds=10.0,
    )

    statuses = await worker.process_batch(limit=10)

    # 1. Kết quả trả về từ batch
    assert statuses == [ProcessingStatus.PROCESSED]

    # 2. RawEvent trong repo được update thành PROCESSED
    assert raw_repo.events["raw-success-01"].processing_status == ProcessingStatus.PROCESSED
    assert raw_repo.events["raw-success-01"].processed_at is not None

    # 3. Ghi nhận ProcessingAttemptRecord
    assert len(raw_repo.attempt_records) == 1
    attempt = raw_repo.attempt_records[0]
    assert attempt.raw_event_id == "raw-success-01"
    assert attempt.attempt_number == 1
    assert attempt.status == ProcessingStatus.PROCESSED
    assert attempt.error_message is None

    # 4. Status chuyển qua PROCESSING trước khi PROCESSED
    status_seq = [c["status"] for c in raw_repo.status_calls if c["event_id"] == "raw-success-01"]
    assert ProcessingStatus.PROCESSING in status_seq
    assert status_seq[-1] == ProcessingStatus.PROCESSED

    # 5. Task được lưu vào TaskDomainRepository
    assert len(task_repo.upserted_tasks) == 1
    saved_task = task_repo.upserted_tasks[0]
    assert "ops-88" in saved_task.title.lower() or "check" in saved_task.title.lower()
    assert saved_task.owner_name == "Dam Quang Cuong"


# ==============================================================================
# 2. TEST HEURISTIC NOISE FILTER (NO LLM CALL, MARKS PROCESSED)
# ==============================================================================

@pytest.mark.asyncio
async def test_worker_noise_chatter_filtered_and_marked_processed():
    """Tin nhắn không có tín hiệu cam kết/yêu cầu -> mark PROCESSED mà không gọi LLM."""
    raw_ev = create_sample_raw_event(
        event_id="raw-noise-01",
        content="<p>Chào buổi sáng cả nhà! Chúc một tuần làm việc hiệu quả.</p>",
    )
    raw_repo = FakeRawEventRepo([raw_ev])
    task_repo = FakeTaskDomainRepo()

    # Extractor nếu bị gọi sẽ raise error để chứng minh không bị gọi
    mock_extractor = MagicMock(spec=LLMStructuredExtractor)
    mock_extractor.extract_async.side_effect = RuntimeError("LLM should not be called for chatter!")

    pipeline = ProcessingPipeline(
        task_repo=task_repo,
        llm_extractor=mock_extractor,
    )
    worker = ProcessingWorker(raw_event_repo=raw_repo, pipeline=pipeline)

    statuses = await worker.process_batch()

    assert statuses == [ProcessingStatus.PROCESSED]
    assert raw_repo.events["raw-noise-01"].processing_status == ProcessingStatus.PROCESSED
    mock_extractor.extract_async.assert_not_called()
    assert len(task_repo.upserted_tasks) == 0


# ==============================================================================
# 3. TEST NON-SILENT RETRY WHEN LLM FAILS (WITHOUT ALLOW_FALLBACK FLAG)
# ==============================================================================

@pytest.mark.asyncio
async def test_worker_non_silent_retry_when_llm_fails_without_fallback():
    """Khi LLM lỗi và PTB_ALLOW_HEURISTIC_FALLBACK=False:

    KHÔNG silent fallback sang heuristic, chuyển trạng thái sang RETRY kèm next_retry_at.
    """
    raw_ev = create_sample_raw_event(
        event_id="raw-fail-01",
        content="<p>Em sẽ xử lý bug này trước 5h chiều.</p>",
        retry_count=0,
    )
    raw_repo = FakeRawEventRepo([raw_ev])
    task_repo = FakeTaskDomainRepo()

    # Cấu hình LLMStructuredExtractor với api_key giả lập
    extractor = LLMStructuredExtractor(
        api_key="sk-test-key",
    )

    # Giả lập urllib.request.urlopen ném HTTP 500 Server Error
    mock_urlopen = MagicMock()
    mock_urlopen.side_effect = Exception("OpenAI API 500: Internal Server Error")

    with patch("urllib.request.urlopen", mock_urlopen):
        pipeline = ProcessingPipeline(
            task_repo=task_repo,
            llm_extractor=extractor,
        )
        worker = ProcessingWorker(
            raw_event_repo=raw_repo,
            pipeline=pipeline,
            base_backoff_seconds=30.0,
            max_retries=3,
        )

        before_time = datetime.now(timezone.utc)
        statuses = await worker.process_batch()

        # 1. Trạng thái trả về là RETRY
        assert statuses == [ProcessingStatus.RETRY]

        # 2. Event trong repo được chuyển sang RETRY
        updated_ev = raw_repo.events["raw-fail-01"]
        assert updated_ev.processing_status == ProcessingStatus.RETRY
        assert "500: Internal Server Error" in (updated_ev.last_processing_error or "")

        # 3. Có next_retry_at và nằm trong tương lai dựa trên exponential backoff (30s)
        assert updated_ev.next_retry_at is not None
        assert updated_ev.next_retry_at >= before_time + timedelta(seconds=28)

        # 4. Ghi nhận ProcessingAttemptRecord thất bại
        assert len(raw_repo.attempt_records) == 1
        att = raw_repo.attempt_records[0]
        assert att.raw_event_id == "raw-fail-01"
        assert att.attempt_number == 1
        assert att.status == ProcessingStatus.RETRY
        assert "500: Internal Server Error" in (att.error_message or "")

        # 5. Không có task nào được lưu vào repository (vì không silent fallback)
        assert len(task_repo.upserted_tasks) == 0


# ==============================================================================
# 4. TEST STRICT ZERO-FALLBACK ON LLM OUTAGE
# ==============================================================================

@pytest.mark.asyncio
async def test_worker_strict_zero_fallback_never_creates_heuristic_task_on_error():
    """Khi LLM lỗi, hệ thống tuân thủ zero-fallback: KHÔNG sinh task heuristic, chuyển RETRY."""
    raw_ev = create_sample_raw_event(
        event_id="raw-fallback-01",
        content="<p>Em sẽ xử lý bug này trước 5h chiều.</p>",
    )
    raw_repo = FakeRawEventRepo([raw_ev])
    task_repo = FakeTaskDomainRepo()

    extractor = LLMStructuredExtractor(
        api_key="sk-test-key",
    )

    # Giả lập LLM call ném Exception
    with patch.object(extractor, "_call_openai_completion", side_effect=ConnectionError("Connection timed out")):
        pipeline = ProcessingPipeline(task_repo=task_repo, llm_extractor=extractor)
        worker = ProcessingWorker(raw_event_repo=raw_repo, pipeline=pipeline)

        statuses = await worker.process_batch()

        # Strict zero-fallback: không fallback -> chuyển RETRY
        assert statuses == [ProcessingStatus.RETRY]
        assert raw_repo.events["raw-fallback-01"].processing_status == ProcessingStatus.RETRY
        assert len(task_repo.upserted_tasks) == 0


# ==============================================================================
# 5. TEST DETERMINISTIC RETRY COUNT: ATTEMPT 1 -> 2 -> 3 (FAILED)
# ==============================================================================

@pytest.mark.asyncio
async def test_worker_deterministic_retry_count_3_marks_failed():
    """Test deterministic retry: attempt 1 (RETRY), attempt 2 (RETRY), attempt 3 (FAILED)."""
    raw_ev = create_sample_raw_event(
        event_id="raw-retry-exhausted",
        content="<p>Em sẽ check log ngay.</p>",
        retry_count=0,
    )
    raw_repo = FakeRawEventRepo([raw_ev])
    task_repo = FakeTaskDomainRepo()

    # Luôn raise error để ép retry
    failing_extractor = LLMStructuredExtractor(
        api_key="sk-key",
    )

    with patch.object(failing_extractor, "_call_openai_completion", side_effect=Exception("Simulated LLM outage")):
        pipeline = ProcessingPipeline(task_repo=task_repo, llm_extractor=failing_extractor)
        worker = ProcessingWorker(
            raw_event_repo=raw_repo,
            pipeline=pipeline,
            max_retries=3,
            base_backoff_seconds=10.0,
        )

        # --- LẦN 1: attempt_number = 1 -> RETRY ---
        status_1 = await worker.process_event(raw_repo.events["raw-retry-exhausted"])
        assert status_1 == ProcessingStatus.RETRY
        assert raw_repo.events["raw-retry-exhausted"].processing_status == ProcessingStatus.RETRY
        assert raw_repo.events["raw-retry-exhausted"].retry_count == 1
        assert raw_repo.events["raw-retry-exhausted"].processing_attempt_count == 1
        assert raw_repo.attempt_records[-1].attempt_number == 1
        assert raw_repo.attempt_records[-1].status == ProcessingStatus.RETRY
        assert raw_repo.events["raw-retry-exhausted"].next_retry_at is not None

        # --- LẦN 2: attempt_number = 2 -> RETRY ---
        status_2 = await worker.process_event(raw_repo.events["raw-retry-exhausted"])
        assert status_2 == ProcessingStatus.RETRY
        assert raw_repo.events["raw-retry-exhausted"].processing_status == ProcessingStatus.RETRY
        assert raw_repo.events["raw-retry-exhausted"].retry_count == 2
        assert raw_repo.events["raw-retry-exhausted"].processing_attempt_count == 2
        assert raw_repo.attempt_records[-1].attempt_number == 2
        assert raw_repo.attempt_records[-1].status == ProcessingStatus.RETRY

        # --- LẦN 3: attempt_number = 3 -> FAILED (max_retries=3 đạt ngưỡng) ---
        status_3 = await worker.process_event(raw_repo.events["raw-retry-exhausted"])
        assert status_3 == ProcessingStatus.FAILED
        assert raw_repo.events["raw-retry-exhausted"].processing_status == ProcessingStatus.FAILED
        assert raw_repo.events["raw-retry-exhausted"].retry_count == 3
        assert raw_repo.events["raw-retry-exhausted"].processing_attempt_count == 3
        assert raw_repo.attempt_records[-1].attempt_number == 3
        assert raw_repo.attempt_records[-1].status == ProcessingStatus.FAILED
        # Khi FAILED, next_retry_at phải bị xóa
        assert raw_repo.events["raw-retry-exhausted"].next_retry_at is None


# ==============================================================================
# 6. TEST EXPONENTIAL BACKOFF CALCULATION
# ==============================================================================

@pytest.mark.asyncio
async def test_worker_exponential_backoff_timing():
    """Kiểm tra công thức tính delay: base * 2^(attempt - 1)."""
    raw_ev = create_sample_raw_event(
        event_id="raw-backoff-test",
        content="<p>Em sẽ check log ngay.</p>",
    )
    raw_repo = FakeRawEventRepo([raw_ev])
    extractor = LLMStructuredExtractor(api_key="sk-test")

    base_backoff = 20.0
    worker = ProcessingWorker(
        raw_event_repo=raw_repo,
        pipeline=ProcessingPipeline(llm_extractor=extractor),
        base_backoff_seconds=base_backoff,
        max_retries=5,
    )

    with patch.object(extractor, "_call_openai_completion", side_effect=Exception("API Error")):
        # Attempt 1: backoff = 20 * 2^0 = 20s
        t0 = datetime.now(timezone.utc)
        await worker.process_event(raw_ev)
        retry_at_1 = raw_repo.events["raw-backoff-test"].next_retry_at
        diff_1 = (retry_at_1 - t0).total_seconds()
        assert 19.0 <= diff_1 <= 22.0

        # Attempt 2: backoff = 20 * 2^1 = 40s
        t1 = datetime.now(timezone.utc)
        await worker.process_event(raw_repo.events["raw-backoff-test"])
        retry_at_2 = raw_repo.events["raw-backoff-test"].next_retry_at
        diff_2 = (retry_at_2 - t1).total_seconds()
        assert 39.0 <= diff_2 <= 42.0


# ==============================================================================
# 7. TEST RESILIENCE: UNEXPECTED EXCEPTION NEVER CRASHES WORKER
# ==============================================================================

@pytest.mark.asyncio
async def test_worker_never_crashes_on_unexpected_exception():
    """Worker tuyệt đối không để exception làm crash tiến trình."""
    ev1 = create_sample_raw_event("ev-crash-1", content="<p>Em sẽ làm task này</p>")
    ev2 = create_sample_raw_event("ev-success-2", content="<p>Em sẽ làm task kia</p>")
    raw_repo = FakeRawEventRepo([ev1, ev2])

    mock_pipeline = MagicMock(spec=ProcessingPipeline)
    # Event 1 ném exception nghiêm trọng
    # Event 2 xử lý bình thường
    async def side_effect(ev):
        if ev.id == "ev-crash-1":
            raise ValueError("Corrupted memory buffer or unhandled bug")
        from ptb_processing.pipeline import PipelineResult
        return PipelineResult(raw_event_id=ev.id, status=ProcessingStatus.PROCESSED)

    mock_pipeline.process.side_effect = side_effect
    worker = ProcessingWorker(raw_event_repo=raw_repo, pipeline=mock_pipeline)

    # Chạy process_batch không được ném exception ra ngoài
    statuses = await worker.process_batch(limit=10)

    assert len(statuses) == 2
    assert statuses[0] == ProcessingStatus.RETRY  # ev1 failed nhưng được retry
    assert statuses[1] == ProcessingStatus.PROCESSED  # ev2 thành công bình thường


# ==============================================================================
# 8. TEST PIPELINE CORRELATION AUTO-MERGE INTEGRATION
# ==============================================================================

@pytest.mark.asyncio
async def test_pipeline_correlation_auto_merge():
    """Test pipeline tự động tìm task cùng anchor (OPS-88) và auto-merge."""
    existing_task = UnifiedTaskCandidate(
        id="task-existing-ops88",
        title="Resolve OPS-88 deployment bug",
        status=TaskStatus.TODO,
        owner_name="Dam Quang Cuong",
        evidences=[
            EvidenceRecord(
                id="ev-01",
                task_id="task-existing-ops88",
                raw_event_id="raw-prev-01",
                evidence_type=EvidenceType.JIRA_TICKET,
                source_type="jira",
                timestamp=datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc),
                snippet="OPS-88: Deployment bug on production",
            )
        ],
    )
    task_repo = FakeTaskDomainRepo([existing_task])

    # Candidate mới cũng có OPS-88
    raw_ev = create_sample_raw_event(
        event_id="raw-merge-01",
        content="<p>Để em fix issue OPS-88 chiều nay nhé.</p>",
    )

    extractor = FakeDeterministicLLMExtractor()
    pipeline = ProcessingPipeline(task_repo=task_repo, llm_extractor=extractor)

    res = await pipeline.process(raw_ev)

    assert res.status == ProcessingStatus.PROCESSED
    assert res.merged is True
    assert res.target_task_id == "task-existing-ops88"
    assert res.candidate.id == "task-existing-ops88"
    # Evidence mới đã được gộp vào task cũ
    assert len(res.candidate.evidences) == 2


# ==============================================================================
# 9. TEST PIPELINE IDENTITY RESOLUTION INVARIANT RULE
# ==============================================================================

@pytest.mark.asyncio
async def test_pipeline_identity_resolution_invariant():
    """Quy tắc bất biến: Chỉ exact match mới gán canonical ID; fuzzy name không được tự merge."""
    resolver = IdentityResolver()
    resolver.register_person(
        person_id="person-canonical-100",
        canonical_name="Nguyen Van Huy",
        primary_email="huy.nguyen@fpt.com",
    )

    task_repo = FakeTaskDomainRepo()
    extractor = FakeDeterministicLLMExtractor()
    pipeline = ProcessingPipeline(
        task_repo=task_repo,
        identity_resolver=resolver,
        llm_extractor=extractor,
    )

    # 1. Case EXACT MATCH email -> canonical_id được gán
    raw_exact = create_sample_raw_event(
        event_id="raw-exact-id",
        content="<p>Để em kiểm tra server hạ tầng nhé.</p>",
        author_name="Nguyen Van Huy",
        author_email="huy.nguyen@fpt.com",
    )
    res_exact = await pipeline.process(raw_exact)
    assert res_exact.candidate.owner_canonical_id == "person-canonical-100"

    # 2. Case FUZZY NAME nhưng email/ID không khớp -> TUYỆT ĐỐI KHÔNG gán person-canonical-100
    raw_fuzzy = create_sample_raw_event(
        event_id="raw-fuzzy-id",
        content="<p>Để em chuẩn bị slide thuyết trình demo nhé.</p>",
        author_name="Nguyễn Văn Huy",
        author_email="unknown.other@client.com",
    )
    res_fuzzy = await pipeline.process(raw_fuzzy)
    assert res_fuzzy.candidate.owner_canonical_id is None


# ==============================================================================
# 10. TEST CANONICAL ENVIRONMENT VARIABLES CONFIGURATION
# ==============================================================================

def test_llm_extractor_reads_canonical_env_vars(monkeypatch):
    """Test đọc cấu hình canonical từ environment: OPENAI_API_KEY, OPENAI_BASE_URL, PTB_EXTRACTION_MODEL."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-custom-canonical-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://custom-gateway.openai.azure.com/v1")
    monkeypatch.setenv("PTB_EXTRACTION_MODEL", "gpt-4o-custom")

    extractor = LLMStructuredExtractor()
    assert extractor.api_key == "sk-custom-canonical-key"
    assert extractor.base_url == "https://custom-gateway.openai.azure.com/v1"
    assert extractor.model == "gpt-4o-custom"


# ==============================================================================
# 11. TEST WORKER RUN_LOOP AND STOP
# ==============================================================================

@pytest.mark.asyncio
async def test_worker_run_loop_and_stop():
    """Test vòng lặp worker run_loop có thể chạy với max_iterations và dừng sạch sẽ."""
    raw_ev = create_sample_raw_event("raw-loop-01", "<p>Để em xử lý task này nhé.</p>")
    raw_repo = FakeRawEventRepo([raw_ev])
    extractor = FakeDeterministicLLMExtractor()
    pipeline = ProcessingPipeline(llm_extractor=extractor)
    worker = ProcessingWorker(raw_event_repo=raw_repo, pipeline=pipeline)

    # Chạy loop với max_iterations = 2
    await worker.run_loop(poll_interval=0.01, max_iterations=2)
    assert raw_repo.events["raw-loop-01"].processing_status == ProcessingStatus.PROCESSED
    assert worker._running is True
    worker.stop()
    assert worker._running is False


# ==============================================================================
# 12. TEST RAW EVENT RETRY LIFECYCLE: NO DOUBLE INCREMENT
# ==============================================================================

@pytest.mark.asyncio
async def test_raw_event_retry_lifecycle_no_double_increment():
    """Vòng đời retry: PENDING -> PROCESSING (attempt=1) -> RETRY (attempt=1)
    -> PROCESSING (attempt=2) -> FAILED (attempt=2).

    Xác nhận không có trường hợp attempt count bị tăng 2 lần cho cùng 1 lượt attempt.
    """
    raw_ev = create_sample_raw_event(
        event_id="raw-retry-lifecycle-01",
        content="<p>Em sẽ fix issue OPS-99 ngay lập tức.</p>",
        retry_count=0,
    )
    raw_repo = FakeRawEventRepo([raw_ev])

    failing_extractor = LLMStructuredExtractor(
        api_key="sk-key",
    )

    with patch.object(failing_extractor, "_call_openai_completion", side_effect=Exception("Simulated transient error")):
        pipeline = ProcessingPipeline(llm_extractor=failing_extractor)
        # Giả sử max_retries = 2 để test chu kỳ 2 attempts: attempt 1 -> RETRY, attempt 2 -> FAILED
        worker = ProcessingWorker(
            raw_event_repo=raw_repo,
            pipeline=pipeline,
            max_retries=2,
            base_backoff_seconds=5.0,
        )

        # -------------------------------------------------------------
        # VÒNG 1: PENDING -> PROCESSING (attempt=1) -> RETRY (attempt=1)
        # -------------------------------------------------------------
        ev = raw_repo.events["raw-retry-lifecycle-01"]
        assert ev.processing_attempt_count == 0
        assert ev.retry_count == 0
        assert ev.processing_status == ProcessingStatus.PENDING

        status_1 = await worker.process_event(ev)
        assert status_1 == ProcessingStatus.RETRY
        assert ev.processing_status == ProcessingStatus.RETRY
        # Attempt count phải là 1 (tăng 1 lần khi sang PROCESSING, KHÔNG tăng lần 2 khi sang RETRY)
        assert ev.processing_attempt_count == 1
        assert ev.retry_count == 1
        assert ev.next_retry_at is not None

        # Kiểm tra lịch sử gọi mark_event_status của lượt 1
        calls_for_ev = [c for c in raw_repo.status_calls if c["event_id"] == "raw-retry-lifecycle-01"]
        assert len(calls_for_ev) == 2
        # Call 1: PROCESSING -> attempt_count = 1
        assert calls_for_ev[0]["status"] == ProcessingStatus.PROCESSING
        assert calls_for_ev[0]["attempt_count"] == 1
        # Call 2: RETRY -> attempt_count VẪN là 1 (không tăng lần thứ hai!)
        assert calls_for_ev[1]["status"] == ProcessingStatus.RETRY
        assert calls_for_ev[1]["attempt_count"] == 1

        # -------------------------------------------------------------
        # VÒNG 2: RETRY -> PROCESSING (attempt=2) -> FAILED (attempt=2)
        # -------------------------------------------------------------
        status_2 = await worker.process_event(ev)
        assert status_2 == ProcessingStatus.FAILED
        assert ev.processing_status == ProcessingStatus.FAILED
        # Attempt count phải là 2 (tăng khi sang PROCESSING lượt 2, KHÔNG tăng khi sang FAILED)
        assert ev.processing_attempt_count == 2
        assert ev.retry_count == 2
        assert ev.next_retry_at is None

        # Kiểm tra lịch sử gọi mark_event_status của cả 2 lượt
        calls_for_ev = [c for c in raw_repo.status_calls if c["event_id"] == "raw-retry-lifecycle-01"]
        assert len(calls_for_ev) == 4
        # Call 3: PROCESSING -> attempt_count = 2
        assert calls_for_ev[2]["status"] == ProcessingStatus.PROCESSING
        assert calls_for_ev[2]["attempt_count"] == 2
        # Call 4: FAILED -> attempt_count VẪN là 2 (không tăng thêm!)
        assert calls_for_ev[3]["status"] == ProcessingStatus.FAILED
        assert calls_for_ev[3]["attempt_count"] == 2


# ==============================================================================
# 13. TEST BATCH RETRY LIFECYCLE: 3 ATTEMPTS TO FAILED
# ==============================================================================

@pytest.mark.asyncio
async def test_worker_process_batch_retry_lifecycle_3_attempts():
    """Test process_batch qua nhiều iterations:
    PENDING -> PROCESSING (attempt=1) -> RETRY (attempt=1)
    -> PROCESSING (attempt=2) -> RETRY (attempt=2)
    -> PROCESSING (attempt=3) -> FAILED (attempt=3).
    """
    raw_ev = create_sample_raw_event(
        event_id="raw-batch-lifecycle",
        content="<p>Em sẽ xử lý gấp lỗi OPS-100 ngay hôm nay.</p>",
        retry_count=0,
    )
    raw_repo = FakeRawEventRepo([raw_ev])

    failing_extractor = LLMStructuredExtractor(
        api_key="sk-key",
    )

    with patch.object(failing_extractor, "_call_openai_completion", side_effect=Exception("Persistent error")):
        pipeline = ProcessingPipeline(llm_extractor=failing_extractor)
        worker = ProcessingWorker(
            raw_event_repo=raw_repo,
            pipeline=pipeline,
            max_retries=3,
            base_backoff_seconds=0.01,
        )

        # Batch 1: PENDING -> RETRY (attempt 1)
        statuses_1 = await worker.process_batch()
        assert statuses_1 == [ProcessingStatus.RETRY]
        ev = raw_repo.events["raw-batch-lifecycle"]
        assert ev.processing_status == ProcessingStatus.RETRY
        assert ev.processing_attempt_count == 1
        assert ev.retry_count == 1

        # Reset next_retry_at để batch 2 có thể poll
        ev.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)

        # Batch 2: RETRY -> RETRY (attempt 2)
        statuses_2 = await worker.process_batch()
        assert statuses_2 == [ProcessingStatus.RETRY]
        assert ev.processing_status == ProcessingStatus.RETRY
        assert ev.processing_attempt_count == 2
        assert ev.retry_count == 2

        # Reset next_retry_at để batch 3 có thể poll
        ev.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)

        # Batch 3: RETRY -> FAILED (attempt 3)
        statuses_3 = await worker.process_batch()
        assert statuses_3 == [ProcessingStatus.FAILED]
        assert ev.processing_status == ProcessingStatus.FAILED
        assert ev.processing_attempt_count == 3
        assert ev.retry_count == 3
        assert ev.next_retry_at is None


