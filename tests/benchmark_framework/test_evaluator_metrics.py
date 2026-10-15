"""Unit tests for metric evaluations, Wilson confidence intervals, and Kendall's tau."""

from datetime import datetime, timezone
import pytest

from ptb_contracts.l2_processing import TaskStatus, UnifiedTaskCandidate

from benchmarks.evaluator.metrics import (
    calculate_percentile,
    canonical_task_sort_key,
    compute_kendall_tau_for_tasks,
    kendall_tau,
    wilson_score_interval,
)


def test_wilson_score_interval_known_values():
    # 10 out of 10 successes
    low, high = wilson_score_interval(10, 10)
    assert low > 0.70
    assert high == 1.0

    # 14 out of 38 (Wave 0 honest benchmark result: 36.8%)
    low, high = wilson_score_interval(14, 38)
    assert 0.20 <= low <= 0.25
    assert 0.50 <= high <= 0.55
    assert low <= 0.3684 <= high

    # 0 out of 10 successes
    low, high = wilson_score_interval(0, 10)
    assert low == 0.0
    assert high < 0.30

    # Zero trials
    assert wilson_score_interval(0, 0) == (0.0, 0.0)


def test_kendall_tau_permutations():
    # Identical rankings -> tau = 1.0
    assert kendall_tau([0, 1, 2, 3], [0, 1, 2, 3]) == 1.0

    # Fully inverted rankings -> tau = -1.0
    assert kendall_tau([0, 1, 2, 3], [3, 2, 1, 0]) == -1.0

    # Partially ordered rankings
    tau = kendall_tau([0, 1, 2, 3], [0, 2, 1, 3])
    assert 0.60 <= tau <= 0.70

    # Edge cases: single element or empty
    assert kendall_tau([1], [1]) == 1.0
    assert kendall_tau([], []) == 1.0


def test_canonical_sort_contract():
    """Verify PTB canonical sort contract:
    Primary:   status_rank ASC (IN_PROGRESS=1, TODO=2, BLOCKED=3, DONE=4, DISMISSED=5)
    Secondary: priority_score DESC
    Tertiary:  due_date ASC (nulls last)
    Quaternary: updated_at DESC
    """
    now = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
    due_early = datetime(2026, 10, 16, 10, 0, 0, tzinfo=timezone.utc)
    due_late = datetime(2026, 10, 20, 10, 0, 0, tzinfo=timezone.utc)

    # 1. IN_PROGRESS must sort before TODO even if TODO has higher priority
    t_in_progress = UnifiedTaskCandidate(
        id="t-in-progress",
        title="In progress task",
        status=TaskStatus.IN_PROGRESS,
        priority_score=50.0,
        due_date=due_late,
        updated_at=now,
    )
    t_todo = UnifiedTaskCandidate(
        id="t-todo",
        title="Todo task",
        status=TaskStatus.TODO,
        priority_score=100.0,
        due_date=due_early,
        updated_at=now,
    )
    assert canonical_task_sort_key(t_in_progress) < canonical_task_sort_key(t_todo)

    # 2. Priority secondary sort (descending: 90 comes before 60)
    t_p90 = UnifiedTaskCandidate(
        id="t-p90",
        title="High priority",
        status=TaskStatus.TODO,
        priority_score=90.0,
        due_date=due_late,
        updated_at=now,
    )
    t_p60 = UnifiedTaskCandidate(
        id="t-p60",
        title="Medium priority",
        status=TaskStatus.TODO,
        priority_score=60.0,
        due_date=due_early,
        updated_at=now,
    )
    assert canonical_task_sort_key(t_p90) < canonical_task_sort_key(t_p60)

    # 3. Tertiary: due_date ASC, nulls last
    t_with_due = UnifiedTaskCandidate(
        id="t-due",
        title="Has due date",
        status=TaskStatus.TODO,
        priority_score=50.0,
        due_date=due_late,
        updated_at=now,
    )
    t_no_due = UnifiedTaskCandidate(
        id="t-no-due",
        title="No due date",
        status=TaskStatus.TODO,
        priority_score=50.0,
        due_date=None,
        updated_at=now,
    )
    assert canonical_task_sort_key(t_with_due) < canonical_task_sort_key(t_no_due)


def test_compute_kendall_tau_for_tasks():
    now = datetime.now(timezone.utc)
    t1 = UnifiedTaskCandidate(id="t1", title="T1", status=TaskStatus.IN_PROGRESS, priority_score=80.0, updated_at=now)
    t2 = UnifiedTaskCandidate(id="t2", title="T2", status=TaskStatus.TODO, priority_score=90.0, updated_at=now)
    t3 = UnifiedTaskCandidate(id="t3", title="T3", status=TaskStatus.DONE, priority_score=50.0, updated_at=now)

    # Perfect canonical order [t1, t2, t3]
    assert compute_kendall_tau_for_tasks([t1, t2, t3]) == 1.0


def test_calculate_percentile():
    values = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    p50 = calculate_percentile(values, 50.0)
    p95 = calculate_percentile(values, 95.0)

    assert p50 == 55.0
    assert p95 == 95.5
    assert calculate_percentile([], 95.0) == 0.0
