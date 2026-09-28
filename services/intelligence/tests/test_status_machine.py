"""Unit tests for StatusInferenceMachine (Task State Machine & Auditing)."""

from datetime import datetime, timezone
import pytest

from ptb_contracts.l2_processing import (
    EvidenceRecord,
    EvidenceType,
    InferredStatus,
    TaskStatus,
    UnifiedTaskCandidate,
)
from ptb_contracts.l4_intelligence import TaskWithContext
from ptb_intelligence.status_machine import StatusInferenceMachine


@pytest.fixture
def machine():
    return StatusInferenceMachine()


def test_todo_to_in_progress_auto_transition(machine):
    now = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)
    ev = EvidenceRecord(
        id="ev-start-1",
        raw_event_id="raw-1",
        evidence_type=EvidenceType.CHAT_REQUEST,
        source_type="teams",
        timestamp=now,
        snippet="Tôi đang code phần xử lý token này rồi nhé",
    )
    task = UnifiedTaskCandidate(
        id="task-100",
        title="Implement token refresh logic",
        status=TaskStatus.TODO,
        evidences=[ev],
    )

    result = machine.evaluate_transition(task, now=now)
    assert result.transition_occurred is True
    assert result.old_status == TaskStatus.TODO
    assert result.new_status == TaskStatus.IN_PROGRESS
    assert result.inferred_status is None
    assert result.audit_record is not None
    assert result.audit_record.change_actor == "SYSTEM"
    assert result.audit_record.old_status == TaskStatus.TODO
    assert result.audit_record.new_status == TaskStatus.IN_PROGRESS
    assert "ev-start-1" in result.audit_record.source_evidence_ids


def test_in_progress_to_blocked_auto_transition(machine):
    now = datetime(2026, 9, 28, 11, 0, 0, tzinfo=timezone.utc)
    ev = EvidenceRecord(
        id="ev-block-1",
        raw_event_id="raw-2",
        evidence_type=EvidenceType.CHAT_REQUEST,
        source_type="teams",
        timestamp=now,
        snippet="Hiện tại bị vướng quyền truy cập database nên chưa thể test tiếp",
    )
    task = UnifiedTaskCandidate(
        id="task-101",
        title="Run migration scripts",
        status=TaskStatus.IN_PROGRESS,
        evidences=[ev],
    )

    result = machine.evaluate_transition(task, now=now)
    assert result.transition_occurred is True
    assert result.old_status == TaskStatus.IN_PROGRESS
    assert result.new_status == TaskStatus.BLOCKED
    assert result.audit_record is not None
    assert result.audit_record.change_actor == "SYSTEM"


def test_blocked_by_context_blocking_tasks(machine):
    now = datetime(2026, 9, 28, 11, 30, 0, tzinfo=timezone.utc)
    task = UnifiedTaskCandidate(
        id="task-102",
        title="Deploy microservice",
        status=TaskStatus.TODO,
    )
    ctx = TaskWithContext(
        task=task,
        last_status_change_at=now,
        blocking_tasks=["task-parent-setup"],
    )

    result = machine.evaluate_transition(ctx, now=now)
    assert result.transition_occurred is True
    assert result.new_status == TaskStatus.BLOCKED
    assert result.audit_record.change_actor == "SYSTEM"


def test_blocked_to_in_progress_when_unblocked(machine):
    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
    ev = EvidenceRecord(
        id="ev-unblock",
        raw_event_id="raw-3",
        evidence_type=EvidenceType.CHAT_REQUEST,
        source_type="teams",
        timestamp=now,
        snippet="Đã cấp quyền truy cập xong, unblocked rồi nhé!",
    )
    task = UnifiedTaskCandidate(
        id="task-103",
        title="Run migration scripts",
        status=TaskStatus.BLOCKED,
        evidences=[ev],
    )

    result = machine.evaluate_transition(task, now=now)
    assert result.transition_occurred is True
    assert result.old_status == TaskStatus.BLOCKED
    assert result.new_status == TaskStatus.IN_PROGRESS
    assert result.audit_record.change_actor == "SYSTEM"


def test_non_authoritative_done_signal_only_sets_inferred_likely_done(machine):
    """LLM or chat message stating 'done' MUST NOT change authoritative status to DONE."""
    now = datetime(2026, 9, 28, 14, 0, 0, tzinfo=timezone.utc)
    ev = EvidenceRecord(
        id="ev-done-chat",
        raw_event_id="raw-4",
        evidence_type=EvidenceType.COMPLETION_SIGNAL,
        source_type="teams",
        timestamp=now,
        snippet="Phần này em code xong rồi nhé",
    )
    task = UnifiedTaskCandidate(
        id="task-104",
        title="Create notification worker",
        status=TaskStatus.IN_PROGRESS,
        evidences=[ev],
    )

    # Calling with is_authoritative=False (default)
    result = machine.evaluate_transition(task, is_authoritative=False, now=now)

    # Authoritative status MUST remain IN_PROGRESS
    assert result.transition_occurred is False
    assert result.old_status == TaskStatus.IN_PROGRESS
    assert result.new_status == TaskStatus.IN_PROGRESS
    # Only inferred_status becomes LIKELY_DONE
    assert result.inferred_status == InferredStatus.LIKELY_DONE.value
    # No authoritative transition audit record to DONE
    assert result.audit_record is None


def test_authoritative_done_signal_transitions_to_done(machine):
    """Authoritative signal (Jira closed, merged PR, User click) transitions to DONE."""
    now = datetime(2026, 9, 28, 15, 0, 0, tzinfo=timezone.utc)
    ev = EvidenceRecord(
        id="ev-pr-merged",
        raw_event_id="raw-5",
        evidence_type=EvidenceType.COMPLETION_SIGNAL,
        source_type="git",
        timestamp=now,
        snippet="PR #42 merged into main: Closes task-105",
    )
    task = UnifiedTaskCandidate(
        id="task-105",
        title="Create notification worker",
        status=TaskStatus.IN_PROGRESS,
        inferred_status="LIKELY_DONE",
        evidences=[ev],
    )

    # Calling with is_authoritative=True
    result = machine.evaluate_transition(
        task,
        is_authoritative=True,
        actor="USER",
        now=now,
    )

    assert result.transition_occurred is True
    assert result.old_status == TaskStatus.IN_PROGRESS
    assert result.new_status == TaskStatus.DONE
    assert result.inferred_status is None
    assert result.audit_record is not None
    assert result.audit_record.change_actor == "USER"
    assert result.audit_record.new_status == TaskStatus.DONE
