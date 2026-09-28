"""TaskIntelligenceLifecycle: Coordinates Layer 4 intelligence updates for task changes.

Triggers on task or evidence modifications (called from ProcessingWorker or ApplicationService):
1. Evaluates status inference and transitions via StatusInferenceMachine.infer_status.
2. Re-calculates priority score and breakdown via DeterministicPriorityEngine.calculate_priority.
3. Strictly respects manual overrides:
   - If task has priority_override: keeps user override authoritative on task.priority_score,
     saving calculated score into inferred_priority_score.
   - If task has status_authoritative == True: keeps user's status authoritative on task.status,
     saving status machine suggestion into task.inferred_status.
4. Persists the updated domain candidate and any status transition audits to Neo4j via TaskDomainRepository.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any, Optional, Union

from ptb_contracts.l2_processing import (
    StatusTransitionAuditRecord,
    TaskStatus,
    UnifiedTaskCandidate,
)
from ptb_contracts.l4_intelligence import PriorityBreakdown, TaskWithContext
from ptb_database.repositories.task_repo import TaskDomainRepository
from ptb_intelligence.priority import DeterministicPriorityEngine
from ptb_intelligence.status_machine import StatusInferenceMachine, TransitionResult

logger = logging.getLogger("ptb.intelligence.lifecycle")


@dataclass
class LifecycleResult:
    """Result container for task lifecycle processing with flexible unpacking and property delegation."""

    task: UnifiedTaskCandidate
    priority_breakdown: PriorityBreakdown
    transition_result: TransitionResult

    def __iter__(self):
        return iter((self.task, self.priority_breakdown, self.transition_result))

    def __getattr__(self, name: str) -> Any:
        return getattr(self.task, name)


class TaskIntelligenceLifecycle:
    """Coordinates intelligence updates (priority and status inference) on task lifecycle events."""

    def __init__(
        self,
        task_repo: Optional[TaskDomainRepository] = None,
        status_machine: Optional[StatusInferenceMachine] = None,
        priority_engine: Optional[DeterministicPriorityEngine] = None,
    ) -> None:
        self.task_repo = task_repo
        self.status_machine = status_machine or StatusInferenceMachine()
        self.priority_engine = priority_engine or DeterministicPriorityEngine()

    async def on_task_changed(
        self,
        candidate: Union[UnifiedTaskCandidate, TaskWithContext, str],
        context: Optional[TaskWithContext] = None,
        now: Optional[datetime] = None,
    ) -> LifecycleResult:
        """Handle task or evidence update.

        Args:
            candidate: UnifiedTaskCandidate, TaskWithContext, or task_id str.
            context: Optional contextual graph information.
            now: Optional reference timestamp.

        Returns:
            LifecycleResult containing the updated task, priority breakdown, and transition result.
        """
        ref_time = now or datetime.now(timezone.utc)
        if ref_time.tzinfo is None:
            ref_time = ref_time.replace(tzinfo=timezone.utc)

        # 1. Resolve task candidate and context
        task: UnifiedTaskCandidate
        ctx: Optional[TaskWithContext] = context

        if isinstance(candidate, str):
            if not self.task_repo:
                raise ValueError("TaskDomainRepository is required when task_id string is passed.")
            loaded = await self.task_repo.get_task_by_id(candidate)
            if not loaded:
                raise ValueError(f"Task with id '{candidate}' not found.")
            task = loaded
        elif isinstance(candidate, TaskWithContext):
            task = candidate.task
            if ctx is None:
                ctx = candidate
        elif isinstance(candidate, UnifiedTaskCandidate):
            task = candidate
        else:
            raise TypeError(f"Unsupported candidate type: {type(candidate)}")

        # 2. Evaluate Status Inference
        transition_result = self.status_machine.infer_status(
            candidate=task,
            context=ctx,
            now=ref_time,
        )

        is_status_authoritative = getattr(task, "status_authoritative", False) is True

        if is_status_authoritative:
            # Respect user's authoritative status: DO NOT change task.status
            # If transition was suggested or inferred status detected, save suggestion to inferred_status
            if transition_result.inferred_status:
                task.inferred_status = transition_result.inferred_status
            elif transition_result.transition_occurred and transition_result.new_status != task.status:
                new_status_val = (
                    transition_result.new_status.value
                    if hasattr(transition_result.new_status, "value")
                    else str(transition_result.new_status)
                )
                task.inferred_status = new_status_val
            logger.info(
                "Task %s has status_authoritative=True. Preserving status %s (inferred suggestion: %s)",
                task.id,
                task.status.value if hasattr(task.status, "value") else task.status,
                task.inferred_status,
            )
        else:
            # Auto-transition if applicable
            if transition_result.transition_occurred:
                logger.info(
                    "Task %s auto-transitioned from %s to %s (reason: %s)",
                    task.id,
                    transition_result.old_status,
                    transition_result.new_status,
                    transition_result.reason,
                )
                task.status = transition_result.new_status
                task.inferred_status = transition_result.inferred_status

                # Record transition audit if repo available
                if (
                    transition_result.audit_record
                    and self.task_repo
                    and hasattr(self.task_repo, "record_status_transition_audit")
                ):
                    try:
                        await self.task_repo.record_status_transition_audit(transition_result.audit_record)
                    except Exception as audit_err:
                        logger.warning(
                            "Failed to record status transition audit for %s: %s",
                            task.id,
                            audit_err,
                        )
            else:
                task.inferred_status = transition_result.inferred_status

        # 3. Calculate Deterministic Priority
        eval_input = ctx if ctx is not None else task
        priority_breakdown = self.priority_engine.calculate_priority(eval_input, now=ref_time)

        # 4. Respect Priority Manual Override
        priority_override = getattr(task, "priority_override", None)
        if priority_override is not None:
            # User manually set priority
            task.priority_score = float(priority_override)
            task.inferred_priority_score = priority_breakdown.total_score
            logger.info(
                "Task %s has priority_override=%.1f. Preserved priority_score=%.1f (calculated: %.1f)",
                task.id,
                float(priority_override),
                task.priority_score,
                priority_breakdown.total_score,
            )
        else:
            task.priority_score = priority_breakdown.total_score
            task.inferred_priority_score = None

        # 5. Persist to Neo4j domain repository
        task.updated_at = ref_time
        if self.task_repo and hasattr(self.task_repo, "upsert_task_atomic"):
            await self.task_repo.upsert_task_atomic(task)

        return LifecycleResult(
            task=task,
            priority_breakdown=priority_breakdown,
            transition_result=transition_result,
        )
