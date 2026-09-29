"""Unit tests for TaskIntelligenceLifecycle (Layer 4 Lifecycle Wiring & Manual Overrides)."""

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
import pytest

from ptb_contracts.l2_processing import (
    EvidenceRecord,
    EvidenceType,
    InferredStatus,
    StatusTransitionAuditRecord,
    TaskStatus,
    UnifiedTaskCandidate,
)
from ptb_contracts.l4_intelligence import PriorityBreakdown, TaskWithContext
from ptb_intelligence.lifecycle import LifecycleResult, TaskIntelligenceLifecycle
from ptb_intelligence.priority import DeterministicPriorityEngine
from ptb_intelligence.status_machine import StatusInferenceMachine


class MockTaskDomainRepository:
    """Mock repository for TaskIntelligenceLifecycle testing."""

    def __init__(self, initial_tasks: Optional[List[UnifiedTaskCandidate]] = None) -> None:
        self.tasks: Dict[str, UnifiedTaskCandidate] = {
            t.id: t for t in (initial_tasks or [])
        }
        self.audits: List[StatusTransitionAuditRecord] = []
        self.upsert_count: int = 0

    async def get_task_by_id(self, task_id: str) -> Optional[UnifiedTaskCandidate]:
        return self.tasks.get(task_id)

    async def upsert_task_atomic(self, task: UnifiedTaskCandidate) -> str:
        self.tasks[task.id] = task
        self.upsert_count += 1
        return task.id

    async def record_status_transition_audit(self, audit: StatusTransitionAuditRecord) -> str:
        self.audits.append(audit)
        return audit.id


@pytest.fixture
def mock_repo() -> MockTaskDomainRepository:
    return MockTaskDomainRepository()


@pytest.fixture
def lifecycle(mock_repo: MockTaskDomainRepository) -> TaskIntelligenceLifecycle:
    return TaskIntelligenceLifecycle(
        task_repo=mock_repo,
        status_machine=StatusInferenceMachine(),
        priority_engine=DeterministicPriorityEngine(),
    )


@pytest.mark.asyncio
async def test_lifecycle_recalculates_priority_and_auto_transitions(
    lifecycle: TaskIntelligenceLifecycle,
    mock_repo: MockTaskDomainRepository,
):
    """Task lifecycle hook recalculates priority and auto-transitions TODO -> IN_PROGRESS on start signals."""
    now = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)
    ev = EvidenceRecord(
        id="ev-1",
        raw_event_id="raw-1",
        evidence_type=EvidenceType.CHAT_REQUEST,
        source_type="teams",
        timestamp=now,
        snippet="Tôi đang code phần xử lý token này rồi nhé",
    )
    task = UnifiedTaskCandidate(
        id="task-1",
        title="Implement token refresh logic",
        status=TaskStatus.TODO,
        due_date=now + timedelta(hours=6),  # Sát deadline
        evidences=[ev],
    )
    await mock_repo.upsert_task_atomic(task)

    result = await lifecycle.on_task_changed(task, now=now)

    # Status auto-transitioned TODO -> IN_PROGRESS
    assert result.task.status == TaskStatus.IN_PROGRESS
    assert result.transition_result.transition_occurred is True
    assert result.transition_result.old_status == TaskStatus.TODO
    assert result.transition_result.new_status == TaskStatus.IN_PROGRESS

    # Audit recorded
    assert len(mock_repo.audits) == 1
    assert mock_repo.audits[0].new_status == TaskStatus.IN_PROGRESS

    # Priority score calculated
    assert result.task.priority_score >= 30.0
    assert result.priority_breakdown.deadline_score >= 30.0
    assert result.task.inferred_priority_score is None

    # Persisted to repository
    persisted = await mock_repo.get_task_by_id("task-1")
    assert persisted.status == TaskStatus.IN_PROGRESS
    assert persisted.priority_score == result.task.priority_score


@pytest.mark.asyncio
async def test_lifecycle_inferred_likely_done_on_completion_signal(
    lifecycle: TaskIntelligenceLifecycle,
    mock_repo: MockTaskDomainRepository,
):
    """Chat completion signal sets inferred_status=LIKELY_DONE without altering authoritative status."""
    now = datetime(2026, 9, 29, 11, 0, 0, tzinfo=timezone.utc)
    ev = EvidenceRecord(
        id="ev-done",
        raw_event_id="raw-2",
        evidence_type=EvidenceType.COMPLETION_SIGNAL,
        source_type="teams",
        timestamp=now,
        snippet="Phần này em code xong rồi nhé",
    )
    task = UnifiedTaskCandidate(
        id="task-2",
        title="Notification worker",
        status=TaskStatus.IN_PROGRESS,
        evidences=[ev],
    )

    result = await lifecycle.on_task_changed(task, now=now)

    # Status remains IN_PROGRESS (requires authoritative confirmation to close)
    assert result.task.status == TaskStatus.IN_PROGRESS
    assert result.task.inferred_status == InferredStatus.LIKELY_DONE.value
    assert result.transition_result.transition_occurred is False
    assert len(mock_repo.audits) == 0


@pytest.mark.asyncio
async def test_lifecycle_preserves_status_authoritative_manual_override(
    lifecycle: TaskIntelligenceLifecycle,
    mock_repo: MockTaskDomainRepository,
):
    """When status_authoritative is True, user manual choice is preserved even if signals indicate transition."""
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    ev = EvidenceRecord(
        id="ev-start",
        raw_event_id="raw-3",
        evidence_type=EvidenceType.CHAT_REQUEST,
        source_type="teams",
        timestamp=now,
        snippet="Tôi bắt đầu làm việc này rồi nhé",
    )
    # User manually keeps status at TODO (status_authoritative=True)
    task = UnifiedTaskCandidate(
        id="task-3",
        title="Manual user task",
        status=TaskStatus.TODO,
        status_authoritative=True,
        evidences=[ev],
    )

    result = await lifecycle.on_task_changed(task, now=now)

    # Status MUST remain TODO because user set status_authoritative=True
    assert result.task.status == TaskStatus.TODO
    # Suggestion is captured in inferred_status
    assert result.task.inferred_status == "IN_PROGRESS"
    # No auto-transition audit was recorded to alter user's authoritative status
    assert len(mock_repo.audits) == 0


@pytest.mark.asyncio
async def test_lifecycle_preserves_priority_override_manual(
    lifecycle: TaskIntelligenceLifecycle,
    mock_repo: MockTaskDomainRepository,
):
    """When priority_override is set, manual score is preserved and engine calculation saved to inferred_priority_score."""
    now = datetime(2026, 9, 29, 13, 0, 0, tzinfo=timezone.utc)
    task = UnifiedTaskCandidate(
        id="task-4",
        title="Low priority background clean up",
        status=TaskStatus.TODO,
        priority_score=95.0,
        priority_override=95.0,  # User manually elevated priority to 95.0
    )

    result = await lifecycle.on_task_changed(task, now=now)

    # Authoritative priority_score remains user's override (95.0)
    assert result.task.priority_score == 95.0
    # Inferred priority score holds the deterministic engine calculation (near 0)
    assert result.task.inferred_priority_score is not None
    assert result.task.inferred_priority_score < 10.0
    assert result.priority_breakdown.total_score < 10.0


@pytest.mark.asyncio
async def test_lifecycle_both_manual_overrides_respected(
    lifecycle: TaskIntelligenceLifecycle,
    mock_repo: MockTaskDomainRepository,
):
    """Test both status_authoritative and priority_override simultaneously respected."""
    now = datetime(2026, 9, 29, 14, 0, 0, tzinfo=timezone.utc)
    ev = EvidenceRecord(
        id="ev-unblock",
        raw_event_id="raw-4",
        evidence_type=EvidenceType.CHAT_REQUEST,
        source_type="teams",
        timestamp=now,
        snippet="Unblocked rồi nhé, đã cấp quyền",
    )
    task = UnifiedTaskCandidate(
        id="task-5",
        title="Production hotfix pending customer confirmation",
        status=TaskStatus.BLOCKED,
        status_authoritative=True,
        priority_score=88.0,
        priority_override=88.0,
        evidences=[ev],
    )

    result = await lifecycle.on_task_changed(task, now=now)

    assert result.task.status == TaskStatus.BLOCKED  # Preserved
    assert result.task.inferred_status == "IN_PROGRESS"  # Suggested
    assert result.task.priority_score == 88.0  # Preserved override
    assert result.task.inferred_priority_score is not None


@pytest.mark.asyncio
async def test_lifecycle_with_task_id_string_input(
    lifecycle: TaskIntelligenceLifecycle,
    mock_repo: MockTaskDomainRepository,
):
    """Calling on_task_changed with a task_id string fetches from repo and updates."""
    now = datetime(2026, 9, 29, 15, 0, 0, tzinfo=timezone.utc)
    task = UnifiedTaskCandidate(
        id="task-str-1",
        title="Check string id hook",
        status=TaskStatus.TODO,
        due_date=now - timedelta(days=1),  # Overdue
    )
    await mock_repo.upsert_task_atomic(task)

    result = await lifecycle.on_task_changed("task-str-1", now=now)
    assert result.task.id == "task-str-1"
    assert result.task.priority_score >= 35.0


@pytest.mark.asyncio
async def test_lifecycle_result_unpacking_and_delegation(
    lifecycle: TaskIntelligenceLifecycle,
):
    """LifecycleResult can be unpacked as a 3-tuple or accessed via task property delegation."""
    now = datetime(2026, 9, 29, 16, 0, 0, tzinfo=timezone.utc)
    task = UnifiedTaskCandidate(
        id="task-delegation",
        title="Delegation task",
        status=TaskStatus.TODO,
    )

    # 1. Unpacking
    t, breakdown, transition = await lifecycle.on_task_changed(task, now=now)
    assert t.id == "task-delegation"
    assert isinstance(breakdown, PriorityBreakdown)
    assert transition.task_id == "task-delegation"

    # 2. Direct attribute delegation
    res = await lifecycle.on_task_changed(task, now=now)
    assert res.title == "Delegation task"
    assert res.status == TaskStatus.TODO
    assert res.id == "task-delegation"
