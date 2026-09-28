"""Status Inference Machine for Personal Task Board (Layer 4 Intelligence).

Implements the Task State Machine defined in docs/v1.md (Section 3):
- Automated transitions:
    TODO -> IN_PROGRESS
    TODO -> BLOCKED
    IN_PROGRESS -> BLOCKED
    BLOCKED -> IN_PROGRESS
- DONE Signal:
    LLM / non-authoritative signal only sets inferred_status = 'LIKELY_DONE'.
    Authoritative signal (User action, Jira/Shortcut closed state, merged PR)
    is strictly required to transition authoritative status -> DONE.
- Auditing:
    Generates StatusTransitionAuditRecord with change_actor = 'SYSTEM' (or 'USER').
"""

from datetime import datetime, timezone
import logging
import re
from typing import Any, List, Optional, Union
from uuid import uuid4

from pydantic import BaseModel, Field

from ptb_contracts.l2_processing import (
    EvidenceRecord,
    EvidenceType,
    InferredStatus,
    StatusTransitionAuditRecord,
    TaskStatus,
    UnifiedTaskCandidate,
)
from ptb_contracts.l4_intelligence import TaskWithContext

logger = logging.getLogger("ptb.intelligence.status_machine")


class TransitionResult(BaseModel):
    """Result of status transition evaluation."""
    task_id: str
    old_status: TaskStatus
    new_status: TaskStatus
    inferred_status: Optional[str] = None
    transition_occurred: bool = False
    audit_record: Optional[StatusTransitionAuditRecord] = None
    reason: str = ""


class StatusInferenceMachine:
    """Manages authoritative state transitions, status inference, and audit logging."""

    # Keywords for status heuristics
    START_KEYWORDS = [
        "bắt đầu", "đang làm", "đang code", "đang sửa", "đang triển khai",
        "working on", "started", "in progress", "investigating", "picking up", "take this"
    ]

    BLOCKED_KEYWORDS = [
        "bị block", "bị vướng", "chờ", "waiting for", "waiting on", "blocked by",
        "dependency on", "thiếu quyền", "chưa có api", "pending dependency"
    ]

    UNBLOCKED_KEYWORDS = [
        "unblocked", "đã có api", "đã cấp quyền", "dependency resolved",
        "hết block", "unblock", "no longer blocked"
    ]

    DONE_KEYWORDS = [
        "xong rồi", "đã xong", "hoàn thành", "done", "fixed", "resolved", "completed",
        "đã merge", "merged", "đã gửi", "shipped"
    ]

    def infer_status(
        self,
        candidate: Union[TaskWithContext, UnifiedTaskCandidate],
        context: Optional[TaskWithContext] = None,
        now: Optional[datetime] = None,
    ) -> TransitionResult:
        """Infer task status and evaluate state transition.

        Args:
            candidate: UnifiedTaskCandidate or TaskWithContext.
            context: Optional TaskWithContext for graph relation information.
            now: Optional reference timestamp.

        Returns:
            TransitionResult with old/new status, inferred_status, transition_occurred, audit_record.
        """
        task_input: Union[TaskWithContext, UnifiedTaskCandidate] = candidate
        if context is not None:
            if isinstance(candidate, UnifiedTaskCandidate):
                context.task = candidate
            task_input = context

        return self.evaluate_transition(
            task_input=task_input,
            is_authoritative=False,
            actor="SYSTEM",
            now=now,
        )

    def evaluate_transition(
        self,
        task_input: Union[TaskWithContext, UnifiedTaskCandidate],
        new_evidences: Optional[List[EvidenceRecord]] = None,
        is_authoritative: bool = False,
        actor: str = "SYSTEM",
        now: Optional[datetime] = None,
    ) -> TransitionResult:
        """Evaluate whether a task should transition its authoritative or inferred status.

        Args:
            task_input: TaskWithContext or UnifiedTaskCandidate.
            new_evidences: Optional new EvidenceRecords to evaluate.
            is_authoritative: True if the signal is from an authoritative source
                              (User action, Jira/Shortcut closed state, merged PR).
            actor: 'SYSTEM' or 'USER'.
            now: Optional timestamp.

        Returns:
            TransitionResult with old/new status, inferred_status, and audit record if changed.
        """
        ref_time = now or datetime.now(timezone.utc)
        if ref_time.tzinfo is None:
            ref_time = ref_time.replace(tzinfo=timezone.utc)

        task, context = self._extract_task_and_context(task_input)
        evidences = (new_evidences or []) + (task.evidences or [])

        current_status = task.status
        current_inferred = task.inferred_status

        # Combine snippets to evaluate signals
        snippets = [ev.snippet for ev in evidences if ev.snippet]
        combined_text = " ".join(snippets).lower()

        # Check for unblocked signals
        has_unblocked_signal = any(
            re.search(r"\b" + re.escape(kw) + r"\b", combined_text) for kw in self.UNBLOCKED_KEYWORDS
        )
        no_blocking_tasks = context is None or len(context.blocking_tasks) == 0

        # If currently BLOCKED and received an unblock signal without authoritative close, unblock to IN_PROGRESS
        if current_status == TaskStatus.BLOCKED and has_unblocked_signal and no_blocking_tasks and not is_authoritative:
            audit = self._create_audit_record(
                task_id=task.id,
                old_status=current_status,
                new_status=TaskStatus.IN_PROGRESS,
                reason="Blocker resolved and work resumed",
                evidences=evidences,
                confidence=0.85,
                actor="SYSTEM",
                changed_at=ref_time,
            )
            return TransitionResult(
                task_id=task.id,
                old_status=current_status,
                new_status=TaskStatus.IN_PROGRESS,
                inferred_status=None,
                transition_occurred=True,
                audit_record=audit,
                reason="Auto transition BLOCKED -> IN_PROGRESS (blocker resolved)",
            )

        # Check for completion evidence
        has_completion_signal = (
            any(ev.evidence_type == EvidenceType.COMPLETION_SIGNAL for ev in evidences)
            or any(re.search(r"\b" + re.escape(kw) + r"\b", combined_text) for kw in self.DONE_KEYWORDS)
            or (context is not None and context.has_completion_evidence)
        )

        # 1. EVALUATE DONE SIGNAL
        if has_completion_signal:
            if is_authoritative:
                # Authoritative signal -> Can transition to DONE
                if current_status != TaskStatus.DONE:
                    audit = self._create_audit_record(
                        task_id=task.id,
                        old_status=current_status,
                        new_status=TaskStatus.DONE,
                        reason="Authoritative completion event (User/Jira/Shortcut/PR)",
                        evidences=evidences,
                        confidence=1.0,
                        actor=actor,
                        changed_at=ref_time,
                    )
                    return TransitionResult(
                        task_id=task.id,
                        old_status=current_status,
                        new_status=TaskStatus.DONE,
                        inferred_status=None,  # Reset inferred status
                        transition_occurred=True,
                        audit_record=audit,
                        reason="Authoritative completion signal detected",
                    )
                return TransitionResult(
                    task_id=task.id,
                    old_status=current_status,
                    new_status=current_status,
                    inferred_status=None,
                    transition_occurred=False,
                )
            else:
                # Non-authoritative signal -> ONLY set inferred_status = LIKELY_DONE
                # Authoritative status remains unchanged!
                new_inferred = InferredStatus.LIKELY_DONE.value
                return TransitionResult(
                    task_id=task.id,
                    old_status=current_status,
                    new_status=current_status,
                    inferred_status=new_inferred,
                    transition_occurred=False,
                    reason="Inferred as LIKELY_DONE by LLM/message heuristic (requires authoritative confirmation to close)",
                )

        # 2. EVALUATE BLOCKED SIGNALS
        has_blocked_signal = (
            any(re.search(r"\b" + re.escape(kw) + r"\b", combined_text) for kw in self.BLOCKED_KEYWORDS)
            or (context is not None and len(context.blocking_tasks) > 0)
        )

        if has_blocked_signal:
            if current_status in [TaskStatus.TODO, TaskStatus.IN_PROGRESS]:
                audit = self._create_audit_record(
                    task_id=task.id,
                    old_status=current_status,
                    new_status=TaskStatus.BLOCKED,
                    reason="Blocker detected in communication or dependency graph",
                    evidences=evidences,
                    confidence=0.9,
                    actor="SYSTEM",
                    changed_at=ref_time,
                )
                return TransitionResult(
                    task_id=task.id,
                    old_status=current_status,
                    new_status=TaskStatus.BLOCKED,
                    inferred_status=None,
                    transition_occurred=True,
                    audit_record=audit,
                    reason="Auto transition to BLOCKED due to detected dependencies/blockers",
                )

        # 3. EVALUATE UNBLOCKED SIGNALS
        has_unblocked_signal = any(
            re.search(r"\b" + re.escape(kw) + r"\b", combined_text) for kw in self.UNBLOCKED_KEYWORDS
        )
        if current_status == TaskStatus.BLOCKED:
            no_blocking_tasks = context is None or len(context.blocking_tasks) == 0
            if has_unblocked_signal and no_blocking_tasks:
                audit = self._create_audit_record(
                    task_id=task.id,
                    old_status=current_status,
                    new_status=TaskStatus.IN_PROGRESS,
                    reason="Blocker resolved and work resumed",
                    evidences=evidences,
                    confidence=0.85,
                    actor="SYSTEM",
                    changed_at=ref_time,
                )
                return TransitionResult(
                    task_id=task.id,
                    old_status=current_status,
                    new_status=TaskStatus.IN_PROGRESS,
                    inferred_status=None,
                    transition_occurred=True,
                    audit_record=audit,
                    reason="Auto transition BLOCKED -> IN_PROGRESS (blocker resolved)",
                )

        # 4. EVALUATE START / IN-PROGRESS SIGNALS
        has_start_signal = any(
            re.search(r"\b" + re.escape(kw) + r"\b", combined_text) for kw in self.START_KEYWORDS
        )
        if current_status == TaskStatus.TODO and has_start_signal:
            audit = self._create_audit_record(
                task_id=task.id,
                old_status=current_status,
                new_status=TaskStatus.IN_PROGRESS,
                reason="Work start signals detected in evidence",
                evidences=evidences,
                confidence=0.85,
                actor="SYSTEM",
                changed_at=ref_time,
            )
            return TransitionResult(
                task_id=task.id,
                old_status=current_status,
                new_status=TaskStatus.IN_PROGRESS,
                inferred_status=None,
                transition_occurred=True,
                audit_record=audit,
                reason="Auto transition TODO -> IN_PROGRESS (active work started)",
            )

        # No transition
        return TransitionResult(
            task_id=task.id,
            old_status=current_status,
            new_status=current_status,
            inferred_status=current_inferred,
            transition_occurred=False,
            reason="No state transition criteria met",
        )

    def _extract_task_and_context(
        self,
        task_input: Union[TaskWithContext, UnifiedTaskCandidate],
    ) -> tuple[UnifiedTaskCandidate, Optional[TaskWithContext]]:
        if isinstance(task_input, TaskWithContext):
            return task_input.task, task_input
        return task_input, None

    def _create_audit_record(
        self,
        task_id: str,
        old_status: TaskStatus,
        new_status: TaskStatus,
        reason: str,
        evidences: List[EvidenceRecord],
        confidence: float,
        actor: str,
        changed_at: datetime,
    ) -> StatusTransitionAuditRecord:
        ev_ids = [ev.id for ev in evidences if ev.id]
        return StatusTransitionAuditRecord(
            id=str(uuid4()),
            task_id=task_id,
            old_status=old_status,
            new_status=new_status,
            reason=reason,
            source_evidence_ids=ev_ids,
            confidence=confidence,
            changed_at=changed_at,
            change_actor=actor,
        )
