"""Today Board Planner for Personal Task Board (Layer 4 Intelligence).

Aggregates:
- Prioritized active top tasks
- Tasks waiting on external dependencies (waiting_on_others)
- Overdue or stale commitments (forgotten_commitments)
- System risk assessment and concise executive summary headline
into a canonical TodayBoardView.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Union

from ptb_contracts.l2_processing import CommitmentRecord, TaskStatus, UnifiedTaskCandidate
from ptb_contracts.l4_intelligence import (
    ForgottenCommitmentItem,
    PriorityBreakdown,
    TaskWithContext,
    TodayBoardView,
    TodayTaskItem,
    WaitingOnItem,
)
from ptb_intelligence.detectors import ForgottenCommitmentDetector, WaitingOnDetector
from ptb_intelligence.priority import DeterministicPriorityEngine

logger = logging.getLogger("ptb.intelligence.planner")


class TodayBoardPlanner:
    """Plans and synthesizes the daily focus board for the user."""

    def __init__(
        self,
        priority_engine: Optional[DeterministicPriorityEngine] = None,
        forgotten_detector: Optional[ForgottenCommitmentDetector] = None,
        waiting_detector: Optional[WaitingOnDetector] = None,
        max_top_tasks: int = 10,
    ) -> None:
        self.priority_engine = priority_engine or DeterministicPriorityEngine()
        self.forgotten_detector = forgotten_detector or ForgottenCommitmentDetector()
        self.waiting_detector = waiting_detector or WaitingOnDetector()
        self.max_top_tasks = max_top_tasks

    def _ensure_utc(self, dt: Optional[datetime]) -> Optional[datetime]:
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def plan_today(
        self,
        user_id: str,
        tasks: List[Union[TaskWithContext, UnifiedTaskCandidate, Dict[str, Any]]],
        commitments: Optional[List[Union[CommitmentRecord, Dict[str, Any]]]] = None,
        now: Optional[datetime] = None,
    ) -> TodayBoardView:
        """Generate the comprehensive TodayBoardView.

        Args:
            user_id: Identifier of the current user.
            tasks: List of tasks with context or candidates.
            commitments: Optional list of commitments.
            now: Optional reference timestamp.

        Returns:
            TodayBoardView instance ready for OpenWebUI / MCP consumption.
        """
        ref_time = self._ensure_utc(now) or datetime.now(timezone.utc)
        commitments_list = commitments or []

        # 1. Parse and index tasks
        normalized_tasks: List[tuple[UnifiedTaskCandidate, Optional[TaskWithContext]]] = []
        tasks_map: Dict[str, UnifiedTaskCandidate] = {}

        for item in tasks:
            if isinstance(item, TaskWithContext):
                normalized_tasks.append((item.task, item))
                tasks_map[item.task.id] = item.task
            elif isinstance(item, UnifiedTaskCandidate):
                normalized_tasks.append((item, None))
                tasks_map[item.id] = item
            elif isinstance(item, dict):
                if "task" in item:
                    ctx = TaskWithContext.model_validate(item)
                    normalized_tasks.append((ctx.task, ctx))
                    tasks_map[ctx.task.id] = ctx.task
                else:
                    t = UnifiedTaskCandidate.model_validate(item)
                    normalized_tasks.append((t, None))
                    tasks_map[t.id] = t

        # 2. Prioritize active tasks (filter out DONE and DISMISSED)
        today_task_items: List[TodayTaskItem] = []
        identified_risks: List[str] = []

        for candidate, ctx in normalized_tasks:
            if candidate.status in [TaskStatus.DONE, TaskStatus.DISMISSED]:
                continue

            input_obj = ctx if ctx is not None else candidate
            breakdown = self.priority_engine.calculate_priority(input_obj, now=ref_time)

            # Determine risk
            is_at_risk = False
            risk_reason = None
            if candidate.due_date:
                due_utc = self._ensure_utc(candidate.due_date)
                diff_seconds = (due_utc - ref_time).total_seconds()
                if diff_seconds < 0:
                    is_at_risk = True
                    days_over = abs(int(diff_seconds // 86400))
                    risk_reason = f"Đã quá hạn {days_over} ngày"
                elif diff_seconds <= 86400 and candidate.status == TaskStatus.BLOCKED:
                    is_at_risk = True
                    risk_reason = "Sát deadline (<24h) nhưng đang bị BLOCKED"
            
            if not is_at_risk and ctx and ctx.days_in_current_status >= 7:
                is_at_risk = True
                risk_reason = f"Tồn đọng {ctx.days_in_current_status} ngày chưa cập nhật"

            if is_at_risk and risk_reason:
                identified_risks.append(f"Task '{candidate.title}': {risk_reason}")

            # Primary snippet & deep link
            snippet = candidate.description or candidate.title
            deep_link = None
            if candidate.evidences:
                snippet = candidate.evidences[0].snippet or snippet
                deep_link = candidate.evidences[0].external_url

            today_task_items.append(
                TodayTaskItem(
                    task_id=candidate.id,
                    title=candidate.title,
                    status=candidate.status,
                    project_key=candidate.project_key,
                    owner_name=candidate.owner_name or "Tôi",
                    requester_name=candidate.requester_name,
                    due_date=candidate.due_date,
                    priority=breakdown,
                    primary_evidence_snippet=snippet,
                    deep_link=deep_link,
                    is_at_risk=is_at_risk,
                    risk_reason=risk_reason,
                )
            )

        # Sort tasks by priority total_score descending
        today_task_items.sort(key=lambda x: x.priority.total_score, reverse=True)
        top_tasks = today_task_items[: self.max_top_tasks]

        # 3. Detect waiting items
        waiting_on_others = self.waiting_detector.detect(
            [ctx if ctx is not None else candidate for candidate, ctx in normalized_tasks],
            now=ref_time,
        )

        # 4. Detect forgotten commitments
        forgotten_commitments = self.forgotten_detector.detect(
            commitments_list,
            tasks_by_id=tasks_map,
            now=ref_time,
        )

        for fc in forgotten_commitments:
            if fc.days_stale >= 3:
                identified_risks.append(f"Cam kết trễ '{fc.title}': đã tồn đọng {fc.days_stale} ngày với {fc.promised_to_name}")

        # 5. Build summary headline
        summary_headline = self._generate_summary_headline(
            top_tasks_count=len(top_tasks),
            risks_count=len(identified_risks),
            waiting_count=len(waiting_on_others),
            forgotten_count=len(forgotten_commitments),
        )

        return TodayBoardView(
            generated_at=ref_time,
            user_id=user_id,
            summary_headline=summary_headline,
            top_tasks=top_tasks,
            waiting_on_others=waiting_on_others,
            forgotten_commitments=forgotten_commitments,
            identified_risks=identified_risks,
        )

    def _generate_summary_headline(
        self,
        top_tasks_count: int,
        risks_count: int,
        waiting_count: int,
        forgotten_count: int,
    ) -> str:
        parts = []
        if top_tasks_count > 0:
            parts.append(f"{top_tasks_count} việc cần tập trung hôm nay")
        else:
            parts.append("Không có việc tồn đọng hôm nay")

        if risks_count > 0:
            parts.append(f"{risks_count} rủi ro cần chú ý")

        if waiting_count > 0:
            parts.append(f"{waiting_count} việc đang chờ người khác")

        if forgotten_count > 0:
            parts.append(f"{forgotten_count} cam kết cần cập nhật")

        return "Hôm nay: " + ", ".join(parts) + "."
