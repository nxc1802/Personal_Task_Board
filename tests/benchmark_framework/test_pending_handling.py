"""Unit tests ensuring PENDING and SKIPPED cases never inflate benchmark scores."""

from datetime import datetime, timezone
import pytest

from ptb_contracts.l1_acquisition import RawEventRecord
from ptb_contracts.l2_processing import EvidenceRecord, EvidenceType, TaskStatus, UnifiedTaskCandidate

from benchmarks.evaluator.scorecard import BenchmarkMetricEvaluator
from benchmarks.models import (
    BenchmarkDataset,
    BenchmarkSample,
    DatasetManifest,
    EvalVerdict,
    FailureCategory,
    GroundTruthSpec,
)
from benchmarks.runner.runner import RunTrace, StepSnapshot


def make_evidence(source_event_id: str = "raw-01") -> EvidenceRecord:
    return EvidenceRecord(
        source_event_id=source_event_id,
        raw_event_id=source_event_id,
        evidence_type=EvidenceType.CHAT_COMMITMENT,
        snippet="evidence snippet",
        source_type="ms_teams",
        timestamp=datetime(2026, 10, 15, 10, 0, 0, tzinfo=timezone.utc),
    )


def test_pending_cases_do_not_inflate_pass_rate_or_verdict():
    """Verify anti-inflation rule: PENDING cases reduce the effective pass rate denominator."""
    samples = []
    snapshots = []
    tasks = []

    # 10 cases total: 3 PASS, 0 FAIL, 7 PENDING
    for i in range(10):
        ev = RawEventRecord(
            id=f"raw-pen-{i}",
            tenant_id="tenant-fpt-internal",
            source_type="ms_teams",
            external_id=f"ext-{i}",
            conversation_or_project_id="conv-01",
            event_timestamp=datetime(2026, 10, 15, 10, 0, 0, tzinfo=timezone.utc),
            author_external_id="cuong.dam@company.com",
            idempotency_key=f"idemp-{i}",
        )
        sample = BenchmarkSample(
            sample_id=f"sample-{i}",
            raw_event=ev,
            ground_truth=GroundTruthSpec(
                expected_is_task=True,
                expected_status=TaskStatus.TODO,
            ),
        )
        samples.append(sample)

        if i < 3:
            # 3 PASS cases
            t = UnifiedTaskCandidate(
                id=f"task-{i}",
                title=f"Task {i}",
                status=TaskStatus.TODO,
                evidences=[make_evidence(f"raw-pen-{i}")],
            )
            tasks.append(t)
            snap = StepSnapshot(
                step_index=i,
                sample_id=f"sample-{i}",
                raw_event_id=f"raw-pen-{i}",
                ingested_as_new=True,
                should_extract=True,
                merged=False,
                target_task_id=None,
                task_id=f"task-{i}",
                resulting_task=t,
                active_task_count=len(tasks),
                tasks_by_id={t.id: t for t in tasks},
                raw_event_count=i + 1,
                latency_ms=15.0,
                is_pending=False,
            )
        else:
            # 7 PENDING cases (e.g. infrastructure or live OAuth unavailable)
            snap = StepSnapshot(
                step_index=i,
                sample_id=f"sample-{i}",
                raw_event_id=f"raw-pen-{i}",
                ingested_as_new=False,
                should_extract=False,
                merged=False,
                target_task_id=None,
                task_id=None,
                resulting_task=None,
                active_task_count=len(tasks),
                tasks_by_id={t.id: t for t in tasks},
                raw_event_count=3,
                latency_ms=2.0,
                is_pending=True,
                pending_reason="Live OAuth unavailable",
            )
        snapshots.append(snap)

    manifest = DatasetManifest(
        dataset_name="TEST-PENDING",
        version="1.0.0",
        created_at=datetime.now(timezone.utc),
    )
    dataset = BenchmarkDataset(manifest=manifest, samples=samples)
    clean_trace = RunTrace(
        mode="fixture",
        dataset_version="1.0.0",
        snapshots=snapshots,
        final_tasks=tasks,
    )

    scorecard = BenchmarkMetricEvaluator.evaluate(dataset, clean_trace)

    assert scorecard.total_cases == 10
    assert scorecard.passed_cases == 3
    assert scorecard.failed_cases == 0
    assert scorecard.pending_cases == 7

    # Anti-inflation check: pass rate must be 3/10 (30.0%), NOT 3/3 (100.0%)!
    assert scorecard.pass_rate == 0.30

    # Overall verdict must be PENDING, NEVER PASS
    assert scorecard.overall_verdict == EvalVerdict.PENDING


def test_failure_overrides_pending_verdict():
    """If there is even 1 failure alongside pending cases, overall verdict must be FAIL."""
    ev = RawEventRecord(
        id="raw-fail-01",
        tenant_id="tenant-fpt-internal",
        source_type="ms_teams",
        external_id="ext-fail",
        conversation_or_project_id="conv-01",
        event_timestamp=datetime(2026, 10, 15, 10, 0, 0, tzinfo=timezone.utc),
        author_external_id="cuong.dam@company.com",
        idempotency_key="idemp-fail",
    )
    s_fail = BenchmarkSample(
        sample_id="sample-fail",
        raw_event=ev,
        ground_truth=GroundTruthSpec(expected_is_task=True),
    )
    s_pending = BenchmarkSample(
        sample_id="sample-pending",
        raw_event=ev,
        ground_truth=GroundTruthSpec(expected_is_task=True),
    )

    snap_fail = StepSnapshot(
        step_index=0,
        sample_id="sample-fail",
        raw_event_id="raw-fail-01",
        ingested_as_new=True,
        should_extract=False,  # Missed extraction!
        merged=False,
        target_task_id=None,
        task_id=None,
        resulting_task=None,
        active_task_count=0,
        tasks_by_id={},
        raw_event_count=1,
        latency_ms=10.0,
    )
    snap_pending = StepSnapshot(
        step_index=1,
        sample_id="sample-pending",
        raw_event_id="raw-fail-01",
        ingested_as_new=False,
        should_extract=False,
        merged=False,
        target_task_id=None,
        task_id=None,
        resulting_task=None,
        active_task_count=0,
        tasks_by_id={},
        raw_event_count=1,
        latency_ms=2.0,
        is_pending=True,
        pending_reason="OAuth required",
    )

    manifest = DatasetManifest(dataset_name="TEST-FAIL-PENDING", version="1.0.0")
    dataset = BenchmarkDataset(manifest=manifest, samples=[s_fail, s_pending])
    clean_trace = RunTrace(mode="fixture", dataset_version="1.0.0", snapshots=[snap_fail, snap_pending])

    scorecard = BenchmarkMetricEvaluator.evaluate(dataset, clean_trace)

    assert scorecard.failed_cases == 1
    assert scorecard.pending_cases == 1
    assert scorecard.overall_verdict == EvalVerdict.FAIL
