"""Anomaly and Pattern Detectors for Personal Task Board (Layer 4 Intelligence).

Implements:
1. ForgottenCommitmentDetector:
   Detects commitments that are overdue or have not been updated for >= 3 days.
2. WaitingOnDetector:
   Detects tasks that are blocked by others along with waiting days and actionable reasons.
"""

from datetime import datetime, timezone
import logging
import re
from typing import Any, Dict, List, Optional, Union

from ptb_contracts.l2_processing import CommitmentRecord, EvidenceType, TaskStatus, UnifiedTaskCandidate
from ptb_contracts.l4_intelligence import (
    ForgottenCommitmentItem,
    TaskWithContext,
    WaitingOnItem,
)

logger = logging.getLogger("ptb.intelligence.detectors")


class ForgottenCommitmentDetector:
    """Detects active commitments that are overdue or stale (>= 3 days without updates)."""

    def __init__(self, stale_threshold_days: int = 3) -> None:
        self.stale_threshold_days = stale_threshold_days

    def _ensure_utc(self, dt: Optional[datetime]) -> Optional[datetime]:
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def detect(
        self,
        commitments: List[Union[CommitmentRecord, Dict[str, Any]]],
        tasks_by_id: Optional[Dict[str, UnifiedTaskCandidate]] = None,
        now: Optional[datetime] = None,
    ) -> List[ForgottenCommitmentItem]:
        """Scan commitments and return forgotten / overdue commitment items.

        Args:
            commitments: List of CommitmentRecord instances or dictionaries.
            tasks_by_id: Optional map of task_id to UnifiedTaskCandidate to check task completion.
            now: Reference timestamp.

        Returns:
            List of ForgottenCommitmentItem.
        """
        ref_time = self._ensure_utc(now) or datetime.now(timezone.utc)
        tasks_map = tasks_by_id or {}
        forgotten_items: List[ForgottenCommitmentItem] = []

        for c_input in commitments:
            if isinstance(c_input, CommitmentRecord):
                c_id = c_input.id
                title = c_input.title
                status = c_input.status
                due_date = self._ensure_utc(c_input.due_date)
                created_at = self._ensure_utc(c_input.created_at) or ref_time
                task_id = c_input.task_id or c_id
                promised_to = getattr(c_input, "requester_id", None) or "Người liên quan"
            elif isinstance(c_input, dict):
                c_id = c_input.get("id") or c_input.get("commitment_id") or ""
                title = c_input.get("title", "")
                status = c_input.get("status", "ACTIVE")
                due_date = self._ensure_utc(c_input.get("due_date"))
                created_at = self._ensure_utc(c_input.get("created_at") or c_input.get("promised_at")) or ref_time
                task_id = c_input.get("task_id") or c_id
                promised_to = c_input.get("promised_to_name") or c_input.get("requester_name") or "Người liên quan"
            else:
                continue

            # Skip completed or abandoned commitments
            if status in ["FULFILLED", "ABANDONED", "DONE", "DISMISSED"]:
                continue

            # If associated task is already DONE, skip
            if task_id in tasks_map and tasks_map[task_id].status == TaskStatus.DONE:
                continue

            # Check if overdue or stale >= 3 days
            is_overdue = False
            days_overdue = 0
            if due_date:
                diff_due = (ref_time - due_date).total_seconds() / 86400.0
                if diff_due > 0:
                    is_overdue = True
                    days_overdue = int(diff_due)

            days_since_created = max(0, int((ref_time - created_at).total_seconds() // 86400))
            is_stale = days_since_created >= self.stale_threshold_days

            if not (is_overdue or is_stale):
                continue

            days_stale = max(days_overdue, days_since_created)

            # Determine conversation snippet & suggested action
            snippet = f"Cam kết: '{title}'"
            if isinstance(c_input, dict) and c_input.get("last_conversation_snippet"):
                snippet = c_input["last_conversation_snippet"]

            if is_overdue:
                suggested_action = f"Cam kết đã quá hạn {days_overdue} ngày. Cần cập nhật tiến độ ngay cho {promised_to}."
            else:
                suggested_action = f"Hỏi cập nhật từ {promised_to} hoặc xác nhận hoàn thành (đã tồn đọng {days_stale} ngày)."

            forgotten_items.append(
                ForgottenCommitmentItem(
                    commitment_id=c_id,
                    task_id=task_id,
                    title=title,
                    promised_to_name=promised_to,
                    promised_at=created_at,
                    days_stale=days_stale,
                    last_conversation_snippet=snippet,
                    suggested_action=suggested_action,
                )
            )

        # Sort by days_stale descending
        forgotten_items.sort(key=lambda x: x.days_stale, reverse=True)
        return forgotten_items


class WaitingOnDetector:
    """Detects tasks that are blocked by external dependencies or team members."""

    def __init__(self) -> None:
        pass

    def _ensure_utc(self, dt: Optional[datetime]) -> Optional[datetime]:
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def detect(
        self,
        tasks_with_context: List[Union[TaskWithContext, UnifiedTaskCandidate, Dict[str, Any]]],
        now: Optional[datetime] = None,
    ) -> List[WaitingOnItem]:
        """Detect blocked tasks and extract waiting duration and reasons.

        Args:
            tasks_with_context: List of tasks with contextual information.
            now: Reference timestamp.

        Returns:
            List of WaitingOnItem.
        """
        ref_time = self._ensure_utc(now) or datetime.now(timezone.utc)
        waiting_items: List[WaitingOnItem] = []

        for item in tasks_with_context:
            task: UnifiedTaskCandidate
            context: Optional[TaskWithContext] = None

            if isinstance(item, TaskWithContext):
                task = item.task
                context = item
            elif isinstance(item, UnifiedTaskCandidate):
                task = item
            elif isinstance(item, dict):
                if "task" in item:
                    ctx = TaskWithContext.model_validate(item)
                    task = ctx.task
                    context = ctx
                else:
                    task = UnifiedTaskCandidate.model_validate(item)
            else:
                continue

            # Check if task is BLOCKED or has blockers
            has_blocking_tasks = context is not None and len(context.blocking_tasks) > 0
            is_blocked_status = task.status == TaskStatus.BLOCKED

            if not (is_blocked_status or has_blocking_tasks):
                continue

            # Determine blocked_since
            blocked_since: datetime
            if context and context.last_status_change_at:
                blocked_since = self._ensure_utc(context.last_status_change_at)
            elif task.updated_at:
                blocked_since = self._ensure_utc(task.updated_at)
            elif task.created_at:
                blocked_since = self._ensure_utc(task.created_at)
            else:
                blocked_since = ref_time

            waiting_days = 0
            if context and context.days_in_current_status > 0:
                waiting_days = context.days_in_current_status
            else:
                waiting_days = max(0, int((ref_time - blocked_since).total_seconds() // 86400))

            # Determine person being waited on
            waiting_for_person = "Đối tác / Thành viên phụ thuộc"
            if context and context.dependent_people:
                waiting_for_person = context.dependent_people[0]
            elif task.requester_name:
                waiting_for_person = task.requester_name

            # Determine reason
            reason = "Bị phụ thuộc chưa hoàn thành"
            if context and context.blocking_tasks:
                reason = f"Đang chờ {len(context.blocking_tasks)} task blocker ({', '.join(context.blocking_tasks[:2])})"
            elif task.evidences:
                for ev in task.evidences:
                    if ev.snippet and ("block" in ev.snippet.lower() or "chờ" in ev.snippet.lower() or "vướng" in ev.snippet.lower()):
                        reason = ev.snippet
                        break

            waiting_items.append(
                WaitingOnItem(
                    task_id=task.id,
                    title=task.title,
                    waiting_for_person_name=waiting_for_person,
                    blocked_since=blocked_since,
                    waiting_days=waiting_days,
                    reason=reason,
                )
            )

        # Sort by waiting_days descending
        waiting_items.sort(key=lambda x: x.waiting_days, reverse=True)
        return waiting_items
