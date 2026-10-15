"""Unit tests for TodayBoardPlanner, ForgottenCommitmentDetector, and WaitingOnDetector."""

from datetime import datetime, timedelta, timezone
import pytest

from ptb_contracts.l2_processing import (
    CommitmentRecord,
    EvidenceRecord,
    EvidenceType,
    TaskStatus,
    UnifiedTaskCandidate,
)
from ptb_contracts.l4_intelligence import TaskWithContext
from ptb_intelligence.detectors import ForgottenCommitmentDetector, WaitingOnDetector
from ptb_intelligence.planner import TodayBoardPlanner


def test_forgotten_commitment_detector():
    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
    detector = ForgottenCommitmentDetector(stale_threshold_days=3)

    # 1. Overdue commitment
    c_overdue = CommitmentRecord(
        id="c-1",
        title="Gửi proposal kiến trúc",
        owner_id="user-me",
        requester_id="user-lead",
        due_date=now - timedelta(days=2),
        created_at=now - timedelta(days=5),
        status="ACTIVE",
    )

    # 2. Stale commitment (> 3 days without updates, no due date)
    c_stale = CommitmentRecord(
        id="c-2",
        title="Review PR của Nam",
        owner_id="user-me",
        requester_id="user-nam",
        created_at=now - timedelta(days=4),
        status="ACTIVE",
    )

    # 3. Fresh commitment (created yesterday)
    c_fresh = CommitmentRecord(
        id="c-3",
        title="Cập nhật tài liệu API",
        owner_id="user-me",
        created_at=now - timedelta(days=1),
        due_date=now + timedelta(days=3),
        status="ACTIVE",
    )

    # 4. Fulfilled commitment
    c_done = CommitmentRecord(
        id="c-4",
        title="Đã bàn giao credential",
        owner_id="user-me",
        created_at=now - timedelta(days=10),
        status="FULFILLED",
    )

    commitments = [c_overdue, c_stale, c_fresh, c_done]
    detected = detector.detect(commitments, now=now)

    assert len(detected) == 2
    c_ids = [d.commitment_id for d in detected]
    assert "c-1" in c_ids
    assert "c-2" in c_ids
    assert "c-3" not in c_ids
    assert "c-4" not in c_ids

    # Verify suggested action for overdue
    overdue_item = next(d for d in detected if d.commitment_id == "c-1")
    assert "quá hạn" in overdue_item.suggested_action.lower()


def test_waiting_on_detector():
    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
    detector = WaitingOnDetector()

    # 1. Blocked task with context
    task_blocked = UnifiedTaskCandidate(
        id="t-block",
        title="Setup CI pipeline for staging",
        status=TaskStatus.BLOCKED,
        evidences=[
            EvidenceRecord(
                id="ev-b",
                raw_event_id="raw-b",
                evidence_type=EvidenceType.CHAT_REQUEST,
                source_type="teams",
                timestamp=now - timedelta(days=3),
                snippet="Chờ DevOps cấp runner credentials",
            )
        ],
    )
    ctx_blocked = TaskWithContext(
        task=task_blocked,
        last_status_change_at=now - timedelta(days=3),
        days_in_current_status=3,
        dependent_people=["DevOps Lead"],
        blocking_tasks=["task-devops-runner"],
    )

    # 2. Normal in progress task (not blocked)
    task_normal = UnifiedTaskCandidate(
        id="t-normal",
        title="Write integration tests",
        status=TaskStatus.IN_PROGRESS,
    )

    detected = detector.detect([ctx_blocked, task_normal], now=now)
    assert len(detected) == 1
    assert detected[0].task_id == "t-block"
    assert detected[0].waiting_for_person_name == "DevOps Lead"
    assert detected[0].waiting_days == 3
    assert "DevOps" in detected[0].reason or "task-devops-runner" in detected[0].reason


def test_today_board_planner_flow():
    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
    planner = TodayBoardPlanner()

    # Tasks
    # High priority overdue production task
    t_critical = UnifiedTaskCandidate(
        id="t-1",
        title="Fix production outage in auth service",
        status=TaskStatus.IN_PROGRESS,
        due_date=now - timedelta(hours=3),
        customer_id="Enterprise-VIP",
    )

    # Medium priority task
    t_medium = UnifiedTaskCandidate(
        id="t-2",
        title="Refactor database models",
        status=TaskStatus.TODO,
        due_date=now + timedelta(days=2),
    )

    # Blocked task
    t_blocked = UnifiedTaskCandidate(
        id="t-3",
        title="Deploy to staging",
        status=TaskStatus.BLOCKED,
        evidences=[
            EvidenceRecord(
                id="ev-bl",
                raw_event_id="raw-bl",
                evidence_type=EvidenceType.CHAT_REQUEST,
                source_type="teams",
                timestamp=now - timedelta(days=2),
                snippet="Đang bị block bởi hạ tầng",
            )
        ],
    )
    ctx_blocked = TaskWithContext(
        task=t_blocked,
        last_status_change_at=now - timedelta(days=2),
        days_in_current_status=2,
    )

    # Already completed task (must be omitted from top_tasks)
    t_done = UnifiedTaskCandidate(
        id="t-done",
        title="Setup project repository",
        status=TaskStatus.DONE,
    )

    # Commitments
    c_forgotten = CommitmentRecord(
        id="c-f1",
        title="Bàn giao tài liệu thiết kế",
        owner_id="user-me",
        requester_id="user-boss",
        created_at=now - timedelta(days=5),
        status="ACTIVE",
    )

    board = planner.plan_today(
        user_id="user-me",
        tasks=[t_critical, t_medium, ctx_blocked, t_done],
        commitments=[c_forgotten],
        now=now,
    )

    assert board.user_id == "user-me"
    # Done task should NOT be in top_tasks
    assert len(board.top_tasks) == 3
    top_ids = [t.task_id for t in board.top_tasks]
    assert "t-done" not in top_ids

    # Critical task should be sorted first (highest priority score)
    assert board.top_tasks[0].task_id == "t-1"
    assert board.top_tasks[0].priority.total_score >= board.top_tasks[1].priority.total_score
    assert board.top_tasks[0].is_at_risk is True
    assert "quá hạn" in board.top_tasks[0].risk_reason.lower()

    # Blocked task in waiting_on_others
    assert len(board.waiting_on_others) == 1
    assert board.waiting_on_others[0].task_id == "t-3"

    # Forgotten commitment in forgotten_commitments
    assert len(board.forgotten_commitments) == 1
    assert board.forgotten_commitments[0].commitment_id == "c-f1"

    # Risks identified
    assert len(board.identified_risks) > 0
    assert any("Fix production outage" in r for r in board.identified_risks)

    # Headline generated
    assert len(board.summary_headline) > 0
    assert "hôm nay" in board.summary_headline.lower()


def test_planner_canonical_sort_contract_all_dimensions():
    """Verify TodayBoardPlanner implements the Frozen Canonical Sort Contract:
    Primary:   status_rank ASC (IN_PROGRESS=1, TODO=2, BLOCKED=3, DONE=4, DISMISSED=5)
    Secondary: priority_score DESC
    Tertiary:  due_date ASC (nulls last)
    Quaternary: updated_at DESC
    """
    now = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
    planner = TodayBoardPlanner(max_top_tasks=20)

    # 1. P1-1 Regression: TODO with lower priority must rank BEFORE BLOCKED with higher priority
    t_blocked_high = UnifiedTaskCandidate(
        id="t-blocked-90",
        title="Blocked task with high priority",
        status=TaskStatus.BLOCKED,
        priority_override=90.0,
        due_date=now + timedelta(days=10),
    )
    t_todo_mid = UnifiedTaskCandidate(
        id="t-todo-40",
        title="Todo task with moderate priority",
        status=TaskStatus.TODO,
        priority_override=40.0,
        due_date=now + timedelta(days=10),
    )
    t_in_prog_low = UnifiedTaskCandidate(
        id="t-inprog-10",
        title="In-progress task with low priority",
        status=TaskStatus.IN_PROGRESS,
        priority_override=10.0,
        due_date=now + timedelta(days=10),
    )

    board = planner.plan_today(
        user_id="test-user",
        tasks=[t_blocked_high, t_todo_mid, t_in_prog_low],
        now=now,
    )
    top_ids = [t.task_id for t in board.top_tasks]
    assert top_ids == ["t-inprog-10", "t-todo-40", "t-blocked-90"], (
        "Status rank must take precedence: IN_PROGRESS (rank 1) > TODO (rank 2) > BLOCKED (rank 3)"
    )

    # 2. Priority score DESC within same status
    t_todo_high = UnifiedTaskCandidate(
        id="t-todo-95",
        title="Todo high priority",
        status=TaskStatus.TODO,
        priority_override=95.0,
    )
    t_todo_low = UnifiedTaskCandidate(
        id="t-todo-15",
        title="Todo low priority",
        status=TaskStatus.TODO,
        priority_override=15.0,
    )
    board2 = planner.plan_today(
        user_id="test-user",
        tasks=[t_todo_low, t_todo_high],
        now=now,
    )
    assert [t.task_id for t in board2.top_tasks] == ["t-todo-95", "t-todo-15"]

    # 3. Tertiary: due_date ASC (nulls last) for same status and same priority
    t_due_early = UnifiedTaskCandidate(
        id="t-due-early",
        title="Due early",
        status=TaskStatus.TODO,
        priority_override=50.0,
        due_date=now + timedelta(days=1),
    )
    t_due_late = UnifiedTaskCandidate(
        id="t-due-late",
        title="Due late",
        status=TaskStatus.TODO,
        priority_override=50.0,
        due_date=now + timedelta(days=5),
    )
    t_due_none = UnifiedTaskCandidate(
        id="t-due-none",
        title="Due none",
        status=TaskStatus.TODO,
        priority_override=50.0,
        due_date=None,
    )
    board3 = planner.plan_today(
        user_id="test-user",
        tasks=[t_due_none, t_due_late, t_due_early],
        now=now,
    )
    assert [t.task_id for t in board3.top_tasks] == ["t-due-early", "t-due-late", "t-due-none"], (
        "Tertiary tiebreaker: earlier due_date must rank before later, and nulls must sort last"
    )

    # 4. Quaternary: updated_at DESC for same status, same priority, same due_date
    t_upd_recent = UnifiedTaskCandidate(
        id="t-upd-recent",
        title="Updated recently",
        status=TaskStatus.TODO,
        priority_override=50.0,
        due_date=now + timedelta(days=2),
        updated_at=now - timedelta(hours=1),
    )
    t_upd_older = UnifiedTaskCandidate(
        id="t-upd-older",
        title="Updated older",
        status=TaskStatus.TODO,
        priority_override=50.0,
        due_date=now + timedelta(days=2),
        updated_at=now - timedelta(days=2),
    )
    t_upd_none = UnifiedTaskCandidate(
        id="t-upd-none",
        title="Never updated",
        status=TaskStatus.TODO,
        priority_override=50.0,
        due_date=now + timedelta(days=2),
        updated_at=None,
    )
    board4 = planner.plan_today(
        user_id="test-user",
        tasks=[t_upd_none, t_upd_older, t_upd_recent],
        now=now,
    )
    assert [t.task_id for t in board4.top_tasks] == ["t-upd-recent", "t-upd-older", "t-upd-none"], (
        "Quaternary tiebreaker: more recent updated_at must rank first, and None last"
    )

    # 5. Empty dataset handling
    board_empty = planner.plan_today(user_id="test-user", tasks=[], now=now)
    assert len(board_empty.top_tasks) == 0
    assert "không có việc tồn đọng" in board_empty.summary_headline.lower()

