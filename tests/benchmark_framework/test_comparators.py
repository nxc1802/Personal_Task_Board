"""Unit tests for StrictDeterministicComparator verifying zero-compromise matching standards."""

from datetime import datetime, timezone
import pytest

from ptb_contracts.l1_acquisition import RawEventRecord
from ptb_contracts.l2_processing import EvidenceRecord, EvidenceType, TaskStatus, UnifiedTaskCandidate

from benchmarks.evaluator.comparators import StrictDeterministicComparator
from benchmarks.models import (
    BenchmarkSample,
    ClarityLabel,
    ControlType,
    EvalVerdict,
    FailureCategory,
    GroundTruthSpec,
)
from benchmarks.runner.runner import RunTrace, StepSnapshot


def make_evidence(source_event_id: str = "raw-test-01") -> EvidenceRecord:
    return EvidenceRecord(
        source_event_id=source_event_id,
        raw_event_id=source_event_id,
        evidence_type=EvidenceType.CHAT_COMMITMENT,
        snippet="evidence snippet",
        source_type="ms_teams",
        timestamp=datetime(2026, 10, 15, 10, 0, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def base_raw_event() -> RawEventRecord:
    return RawEventRecord(
        id="raw-test-01",
        tenant_id="tenant-fpt-internal",
        source_type="ms_teams",
        external_id="ext-01",
        conversation_or_project_id="conv-01",
        event_timestamp=datetime(2026, 10, 15, 10, 0, 0, tzinfo=timezone.utc),
        author_external_id="cuong.dam@company.com",
        idempotency_key="idemp-test-01",
    )


def test_strict_status_prohibits_todo_equaling_in_progress(base_raw_event: RawEventRecord):
    """CRITICAL AUDIT RULE: TODO must NEVER equal IN_PROGRESS."""
    sample = BenchmarkSample(
        sample_id="case-status-01",
        raw_event=base_raw_event,
        ground_truth=GroundTruthSpec(
            expected_is_task=True,
            expected_status=TaskStatus.TODO,
        ),
    )

    task_in_progress = UnifiedTaskCandidate(
        id="task-01",
        title="Sample task",
        status=TaskStatus.IN_PROGRESS,  # Mismatch!
        evidences=[make_evidence("raw-test-01")],
    )

    snapshot = StepSnapshot(
        step_index=0,
        sample_id="case-status-01",
        raw_event_id="raw-test-01",
        ingested_as_new=True,
        should_extract=True,
        merged=False,
        target_task_id=None,
        task_id="task-01",
        resulting_task=task_in_progress,
        active_task_count=1,
        tasks_by_id={"task-01": task_in_progress},
        raw_event_count=1,
        latency_ms=10.0,
    )

    trace = RunTrace(mode="fixture", dataset_version="1.0", snapshots=[snapshot], final_tasks=[task_in_progress])
    result = StrictDeterministicComparator.evaluate_case(sample, snapshot, trace)

    assert result.verdict == EvalVerdict.FAIL
    assert result.failure_category == FailureCategory.STATUS_MISMATCH
    assert "Strict status mismatch" in (result.failure_message or "")


def test_strict_deadline_fails_when_due_date_is_none(base_raw_event: RawEventRecord):
    """Catches the production defect where fallback hardcoded due_date=None."""
    due_expected = datetime(2026, 10, 20, 17, 0, 0, tzinfo=timezone.utc)
    sample = BenchmarkSample(
        sample_id="case-deadline-none",
        raw_event=base_raw_event,
        ground_truth=GroundTruthSpec(
            expected_is_task=True,
            expected_due_date=due_expected,
            expected_explicit_deadline=True,
        ),
    )

    task_with_none_date = UnifiedTaskCandidate(
        id="task-02",
        title="Task missing date",
        status=TaskStatus.TODO,
        due_date=None,  # Hardcoded None defect
        explicit_deadline=False,
        evidences=[make_evidence("raw-test-01")],
    )

    snapshot = StepSnapshot(
        step_index=0,
        sample_id="case-deadline-none",
        raw_event_id="raw-test-01",
        ingested_as_new=True,
        should_extract=True,
        merged=False,
        target_task_id=None,
        task_id="task-02",
        resulting_task=task_with_none_date,
        active_task_count=1,
        tasks_by_id={"task-02": task_with_none_date},
        raw_event_count=1,
        latency_ms=10.0,
    )

    trace = RunTrace(mode="fixture", dataset_version="1.0", snapshots=[snapshot], final_tasks=[task_with_none_date])
    result = StrictDeterministicComparator.evaluate_case(sample, snapshot, trace)

    assert result.verdict == EvalVerdict.FAIL
    assert result.failure_category == FailureCategory.DEADLINE_MISMATCH
    assert any("due_date is None" in m.message for m in result.mismatches)


def test_strict_attribution_exact_canonical_id_matching(base_raw_event: RawEventRecord):
    """Verifies that owner and requester attribution requires exact canonical ID match."""
    sample = BenchmarkSample(
        sample_id="case-attr-01",
        raw_event=base_raw_event,
        ground_truth=GroundTruthSpec(
            expected_is_task=True,
            expected_owner_id="cuong.dam",
            expected_requester_id="lead.tran",
        ),
    )

    # Inverted or mismatched attribution
    task_wrong_attr = UnifiedTaskCandidate(
        id="task-03",
        title="Task attribution mismatch",
        status=TaskStatus.TODO,
        owner_canonical_id="lead.tran",  # Inverted!
        requester_canonical_id="cuong.dam",
        evidences=[make_evidence("raw-test-01")],
    )

    snapshot = StepSnapshot(
        step_index=0,
        sample_id="case-attr-01",
        raw_event_id="raw-test-01",
        ingested_as_new=True,
        should_extract=True,
        merged=False,
        target_task_id=None,
        task_id="task-03",
        resulting_task=task_wrong_attr,
        active_task_count=1,
        tasks_by_id={"task-03": task_wrong_attr},
        raw_event_count=1,
        latency_ms=10.0,
    )

    trace = RunTrace(mode="fixture", dataset_version="1.0", snapshots=[snapshot], final_tasks=[task_wrong_attr])
    result = StrictDeterministicComparator.evaluate_case(sample, snapshot, trace)

    assert result.verdict == EvalVerdict.FAIL
    assert result.failure_category in (FailureCategory.ATTRIBUTION_OWNER, FailureCategory.ATTRIBUTION_REQUESTER)


def test_noise_leak_detection(base_raw_event: RawEventRecord):
    """Non-task sample producing a task must be flagged as NOISE_LEAK."""
    sample = BenchmarkSample(
        sample_id="case-noise-01",
        raw_event=base_raw_event,
        ground_truth=GroundTruthSpec(expected_is_task=False),
    )

    leaked_task = UnifiedTaskCandidate(
        id="task-leaked",
        title="Chitchat converted to task",
        status=TaskStatus.TODO,
        evidences=[make_evidence("raw-test-01")],
    )

    snapshot = StepSnapshot(
        step_index=0,
        sample_id="case-noise-01",
        raw_event_id="raw-test-01",
        ingested_as_new=True,
        should_extract=True,
        merged=False,
        target_task_id=None,
        task_id="task-leaked",
        resulting_task=leaked_task,
        active_task_count=1,
        tasks_by_id={"task-leaked": leaked_task},
        raw_event_count=1,
        latency_ms=10.0,
    )

    trace = RunTrace(mode="fixture", dataset_version="1.0", snapshots=[snapshot], final_tasks=[leaked_task])
    result = StrictDeterministicComparator.evaluate_case(sample, snapshot, trace)

    assert result.verdict == EvalVerdict.FAIL
    assert result.failure_category == FailureCategory.NOISE_LEAK
