"""W1-01 Canonical Sort Contract Tests.

Verifies the Frozen Canonical Sort Contract across all layers:
    Primary:   status_rank ASC (IN_PROGRESS=1, TODO=2, BLOCKED=3, DONE=4, DISMISSED=5)
    Secondary: priority_score DESC
    Tertiary:  due_date ASC (nulls last)
    Quaternary: updated_at DESC

Ensures 100% parity between TaskDomainRepository.list_tasks and TodayBoardPlanner.plan_today,
and closes P1-1 sort contract drift defect.
"""

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4
import pytest

from ptb_contracts.l2_processing import TaskStatus, UnifiedTaskCandidate
from ptb_database.repositories.task_repo import (
    TaskDomainRepository,
    _safe_timestamp as repo_safe_timestamp,
    _task_sort_key,
)
from ptb_intelligence.planner import (
    TodayBoardPlanner,
    _safe_timestamp as planner_safe_timestamp,
    _today_sort_key,
)


@pytest.fixture
def ref_time():
    return datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)


def test_status_rank_primary_order(ref_time):
    """Primary: status_rank ASC.
    IN_PROGRESS (1) > TODO (2) > BLOCKED (3) > DONE (4) > DISMISSED (5).
    A lower-priority task with higher status rank MUST sort before a higher-priority task.
    """
    planner = TodayBoardPlanner(max_top_tasks=10)

    # Low priority IN_PROGRESS (score 10) vs High priority TODO (score 99)
    # vs Very high priority BLOCKED (score 100)
    t_blocked = UnifiedTaskCandidate(
        id="t-blocked",
        title="Blocked high",
        status=TaskStatus.BLOCKED,
        priority_override=100.0,
        due_date=ref_time + timedelta(days=1),
    )
    t_todo = UnifiedTaskCandidate(
        id="t-todo",
        title="Todo high",
        status=TaskStatus.TODO,
        priority_override=99.0,
        due_date=ref_time + timedelta(days=2),
    )
    t_in_prog = UnifiedTaskCandidate(
        id="t-inprog",
        title="In-progress low",
        status=TaskStatus.IN_PROGRESS,
        priority_override=10.0,
        due_date=ref_time + timedelta(days=3),
    )

    # 1. Test via TodayBoardPlanner
    board = planner.plan_today(
        user_id="u1",
        tasks=[t_blocked, t_todo, t_in_prog],
        now=ref_time,
    )
    assert [t.task_id for t in board.top_tasks] == ["t-inprog", "t-todo", "t-blocked"]

    # 2. Test via TaskDomainRepository sort key
    repo_sorted = sorted([t_blocked, t_todo, t_in_prog], key=TaskDomainRepository._task_sort_key)
    assert [t.id for t in repo_sorted] == ["t-inprog", "t-todo", "t-blocked"]


def test_p1_1_regression_todo_outranks_higher_priority_blocked(ref_time):
    """P1-1 Regression: TodayBoardPlanner previously used `is_in_progress = 1 if st == 'IN_PROGRESS' else 0`.
    This allowed high-score BLOCKED tasks to improperly outrank TODO tasks.
    Verify TODO strictly outranks BLOCKED regardless of score.
    """
    planner = TodayBoardPlanner(max_top_tasks=10)

    t_blocked_urgent = UnifiedTaskCandidate(
        id="t-blocked-urgent",
        title="Urgent Blocked Task",
        status=TaskStatus.BLOCKED,
        priority_override=99.9,
    )
    t_todo_routine = UnifiedTaskCandidate(
        id="t-todo-routine",
        title="Routine Todo Task",
        status=TaskStatus.TODO,
        priority_override=15.0,
    )

    board = planner.plan_today(
        user_id="u1",
        tasks=[t_blocked_urgent, t_todo_routine],
        now=ref_time,
    )
    assert len(board.top_tasks) == 2
    assert board.top_tasks[0].task_id == "t-todo-routine"
    assert board.top_tasks[0].status == TaskStatus.TODO
    assert board.top_tasks[1].task_id == "t-blocked-urgent"
    assert board.top_tasks[1].status == TaskStatus.BLOCKED


def test_priority_score_secondary_order(ref_time):
    """Secondary: priority_score DESC for tasks with the same status."""
    planner = TodayBoardPlanner(max_top_tasks=10)

    t1 = UnifiedTaskCandidate(id="t-low", title="Low", status=TaskStatus.TODO, priority_override=20.0)
    t2 = UnifiedTaskCandidate(id="t-mid", title="Mid", status=TaskStatus.TODO, priority_override=50.0)
    t3 = UnifiedTaskCandidate(id="t-high", title="High", status=TaskStatus.TODO, priority_override=90.0)

    board = planner.plan_today(user_id="u1", tasks=[t1, t2, t3], now=ref_time)
    assert [t.task_id for t in board.top_tasks] == ["t-high", "t-mid", "t-low"]

    repo_sorted = sorted([t1, t2, t3], key=TaskDomainRepository._task_sort_key)
    assert [t.id for t in repo_sorted] == ["t-high", "t-mid", "t-low"]


def test_due_date_tertiary_order_nulls_last(ref_time):
    """Tertiary: due_date ASC (nulls last) for tasks with same status and same priority_score."""
    planner = TodayBoardPlanner(max_top_tasks=10)

    t_early = UnifiedTaskCandidate(
        id="t-early",
        title="Due Tomorrow",
        status=TaskStatus.TODO,
        priority_override=50.0,
        due_date=ref_time + timedelta(days=1),
    )
    t_late = UnifiedTaskCandidate(
        id="t-late",
        title="Due Next Week",
        status=TaskStatus.TODO,
        priority_override=50.0,
        due_date=ref_time + timedelta(days=7),
    )
    t_none = UnifiedTaskCandidate(
        id="t-none",
        title="No Due Date",
        status=TaskStatus.TODO,
        priority_override=50.0,
        due_date=None,
    )

    board = planner.plan_today(user_id="u1", tasks=[t_none, t_late, t_early], now=ref_time)
    assert [t.task_id for t in board.top_tasks] == ["t-early", "t-late", "t-none"]

    repo_sorted = sorted([t_none, t_late, t_early], key=TaskDomainRepository._task_sort_key)
    assert [t.id for t in repo_sorted] == ["t-early", "t-late", "t-none"]


def test_updated_at_quaternary_order_descending(ref_time):
    """Quaternary: updated_at DESC for tasks with same status, priority_score, and due_date."""
    planner = TodayBoardPlanner(max_top_tasks=10)

    t_new = UnifiedTaskCandidate(
        id="t-new",
        title="Updated 1h ago",
        status=TaskStatus.TODO,
        priority_override=50.0,
        due_date=ref_time + timedelta(days=3),
        updated_at=ref_time - timedelta(hours=1),
    )
    t_old = UnifiedTaskCandidate(
        id="t-old",
        title="Updated 2d ago",
        status=TaskStatus.TODO,
        priority_override=50.0,
        due_date=ref_time + timedelta(days=3),
        updated_at=ref_time - timedelta(days=2),
    )
    t_none = UnifiedTaskCandidate(
        id="t-none",
        title="No updated_at",
        status=TaskStatus.TODO,
        priority_override=50.0,
        due_date=ref_time + timedelta(days=3),
        updated_at=None,
    )

    board = planner.plan_today(user_id="u1", tasks=[t_none, t_old, t_new], now=ref_time)
    assert [t.task_id for t in board.top_tasks] == ["t-new", "t-old", "t-none"]

    repo_sorted = sorted([t_none, t_old, t_new], key=TaskDomainRepository._task_sort_key)
    assert [t.id for t in repo_sorted] == ["t-new", "t-old", "t-none"]


def test_empty_dataset_handling(ref_time):
    """Empty task dataset handled cleanly across planner and repo sort keys."""
    planner = TodayBoardPlanner()
    board = planner.plan_today(user_id="u1", tasks=[], now=ref_time)
    assert board.top_tasks == []
    assert "không có việc tồn đọng" in board.summary_headline.lower()

    repo_sorted = sorted([], key=TaskDomainRepository._task_sort_key)
    assert repo_sorted == []


def test_canonical_sort_parity_across_layers(ref_time):
    """Verify 100% parity across list_tasks key and plan_today sort key on diverse tasks."""
    planner = TodayBoardPlanner(max_top_tasks=100)

    test_tasks = [
        UnifiedTaskCandidate(
            id="t-01",
            title="Active In-Progress 1",
            status=TaskStatus.IN_PROGRESS,
            priority_score=45.0,
            priority_override=45.0,
            due_date=ref_time + timedelta(days=2),
            updated_at=ref_time - timedelta(hours=3),
        ),
        UnifiedTaskCandidate(
            id="t-02",
            title="Active In-Progress 2 High Priority",
            status=TaskStatus.IN_PROGRESS,
            priority_score=85.0,
            priority_override=85.0,
            due_date=ref_time + timedelta(days=5),
            updated_at=ref_time - timedelta(hours=1),
        ),
        UnifiedTaskCandidate(
            id="t-03",
            title="Active In-Progress 3 Early Due",
            status=TaskStatus.IN_PROGRESS,
            priority_score=45.0,
            priority_override=45.0,
            due_date=ref_time + timedelta(days=1),
            updated_at=ref_time - timedelta(hours=5),
        ),
        UnifiedTaskCandidate(
            id="t-04",
            title="Todo High Priority",
            status=TaskStatus.TODO,
            priority_score=92.0,
            priority_override=92.0,
            due_date=ref_time + timedelta(days=3),
            updated_at=ref_time - timedelta(hours=2),
        ),
        UnifiedTaskCandidate(
            id="t-05",
            title="Todo Mid Priority Early Due",
            status=TaskStatus.TODO,
            priority_score=60.0,
            priority_override=60.0,
            due_date=ref_time + timedelta(days=1),
            updated_at=ref_time - timedelta(hours=4),
        ),
        UnifiedTaskCandidate(
            id="t-06",
            title="Todo Mid Priority Late Due",
            status=TaskStatus.TODO,
            priority_score=60.0,
            priority_override=60.0,
            due_date=ref_time + timedelta(days=10),
            updated_at=ref_time - timedelta(hours=4),
        ),
        UnifiedTaskCandidate(
            id="t-07",
            title="Todo Mid Priority No Due",
            status=TaskStatus.TODO,
            priority_score=60.0,
            priority_override=60.0,
            due_date=None,
            updated_at=ref_time - timedelta(hours=4),
        ),
        UnifiedTaskCandidate(
            id="t-08",
            title="Blocked Very High Priority",
            status=TaskStatus.BLOCKED,
            priority_score=99.0,
            priority_override=99.0,
            due_date=ref_time + timedelta(days=1),
            updated_at=ref_time - timedelta(minutes=10),
        ),
        UnifiedTaskCandidate(
            id="t-09",
            title="Blocked Low Priority",
            status=TaskStatus.BLOCKED,
            priority_score=20.0,
            priority_override=20.0,
            due_date=None,
            updated_at=ref_time - timedelta(days=5),
        ),
    ]

    # Plan today
    board = planner.plan_today(user_id="u1", tasks=test_tasks, now=ref_time)
    planner_order = [t.task_id for t in board.top_tasks]

    # Task repo sort
    repo_sorted = sorted(test_tasks, key=TaskDomainRepository._task_sort_key)
    repo_order = [t.id for t in repo_sorted]

    assert planner_order == repo_order, (
        f"Mismatch between planner and repo sort!\nPlanner: {planner_order}\nRepo:    {repo_order}"
    )

    # Detailed verification of the exact expected order:
    expected = [
        "t-02",  # IN_PROGRESS (rank 1), score 85.0
        "t-03",  # IN_PROGRESS (rank 1), score 45.0, due +1d
        "t-01",  # IN_PROGRESS (rank 1), score 45.0, due +2d
        "t-04",  # TODO (rank 2), score 92.0
        "t-05",  # TODO (rank 2), score 60.0, due +1d
        "t-06",  # TODO (rank 2), score 60.0, due +10d
        "t-07",  # TODO (rank 2), score 60.0, due None
        "t-08",  # BLOCKED (rank 3), score 99.0
        "t-09",  # BLOCKED (rank 3), score 20.0
    ]
    assert planner_order == expected


def test_safe_timestamp_helper_robustness():
    """Verify _safe_timestamp handles all date, datetime, string, float, and null types consistently."""
    for fn in [repo_safe_timestamp, planner_safe_timestamp]:
        assert fn(None, 999.0) == 999.0
        assert fn(12345.67, 0.0) == 12345.67
        
        # Datetime aware
        dt_aware = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
        assert fn(dt_aware, 0.0) == dt_aware.timestamp()
        
        # Datetime naive
        dt_naive = datetime(2026, 10, 15, 12, 0, 0)
        assert fn(dt_naive, 0.0) == dt_aware.timestamp()
        
        # Date object
        d = date(2026, 10, 15)
        dt_d = datetime(2026, 10, 15, 0, 0, 0, tzinfo=timezone.utc)
        assert fn(d, 0.0) == dt_d.timestamp()
        
        # ISO strings
        assert fn("2026-10-15T12:00:00Z", 0.0) == dt_aware.timestamp()
        assert fn("2026-10-15T12:00:00+00:00", 0.0) == dt_aware.timestamp()
        assert fn("2026-10-15", 0.0) == dt_d.timestamp()
        
        # Invalid strings
        assert fn("not-a-date", 42.0) == 42.0
        assert fn("", 42.0) == 42.0
