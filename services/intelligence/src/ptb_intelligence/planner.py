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


def _safe_timestamp(val: Any, default_ts: float) -> float:
    """Safely convert datetime, date, int/float, or ISO string to UTC timestamp float."""
    if val is None:
        return default_ts
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, datetime):
        dt = val if val.tzinfo is not None else val.replace(tzinfo=timezone.utc)
        return float(dt.timestamp())
    from datetime import date
    if isinstance(val, date):
        dt = datetime(val.year, val.month, val.day, tzinfo=timezone.utc)
        return float(dt.timestamp())
    if isinstance(val, str) and val.strip():
        try:
            cleaned = val.strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(cleaned)
            dt = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
            return float(dt.timestamp())
        except Exception:
            return default_ts
    return default_ts


def _today_sort_key(
    item: Any,
    tasks_map: Optional[Dict[str, Any]] = None,
) -> tuple[int, float, float, float]:
    """Sort key conforming to the Frozen Canonical Sort Contract:
    Primary:   status_rank ASC (IN_PROGRESS=1, TODO=2, BLOCKED=3, DONE=4, DISMISSED=5)
    Secondary: priority_score DESC
    Tertiary:  due_date ASC (nulls last)
    Quaternary: updated_at DESC

    Returns a 4-tuple configured for reverse=True sorting:
        (-rank, score, -due_ts, updated_ts)
    """
    STATUS_RANK = {
        "IN_PROGRESS": 1,
        "TODO": 2,
        "PENDING": 2,
        "BLOCKED": 3,
        "DONE": 4,
        "DISMISSED": 5,
    }
    st = str(item.status.value if hasattr(item.status, "value") else getattr(item, "status", "")).strip().upper()
    rank = STATUS_RANK.get(st, 6)

    if hasattr(item, "priority") and item.priority:
        score = float(getattr(item.priority, "total_score", 0.0) or 0.0)
    elif getattr(item, "priority_override", None) is not None:
        score = float(item.priority_override)
    elif hasattr(item, "priority_score") and item.priority_score is not None:
        score = float(item.priority_score or 0.0)
    else:
        score = 0.0

    due_val = getattr(item, "due_date", None)
    due_ts = _safe_timestamp(due_val, default_ts=9999999999.0)

    updated_val = getattr(item, "updated_at", None)
    if updated_val is None and tasks_map is not None:
        t_id = getattr(item, "task_id", getattr(item, "id", None))
        if t_id and t_id in tasks_map:
            updated_val = getattr(tasks_map[t_id], "updated_at", None)
    updated_ts = _safe_timestamp(updated_val, default_ts=-62135596800.0)

    return (-rank, score, -due_ts, updated_ts)


class TodayBoardPlanner:
    """Plans and synthesizes the daily focus board for the user."""

    def __init__(
        self,
        priority_engine: Optional[DeterministicPriorityEngine] = None,
        forgotten_detector: Optional[ForgottenCommitmentDetector] = None,
        waiting_detector: Optional[WaitingOnDetector] = None,
        max_top_tasks: int = 30,
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
            if getattr(candidate, "priority_override", None) is not None:
                breakdown = breakdown.model_copy(update={"total_score": float(candidate.priority_override)})
            elif getattr(candidate, "priority_score", None) is not None and candidate.priority_score > 0.0 and breakdown.total_score == 0.0:
                breakdown = breakdown.model_copy(update={"total_score": float(candidate.priority_score)})

            # Determine risk
            is_at_risk = False
            risk_reason = None
            if candidate.due_date:
                due_utc = self._ensure_utc(candidate.due_date)
                diff_seconds = (due_utc - ref_time).total_seconds()
                if diff_seconds < 0:
                    is_at_risk = True
                    days_over = abs(int(diff_seconds // 86400))
                    risk_reason = f"Đã quá hạn {days_over} ngày (Overdue by {days_over} day(s))"
                elif diff_seconds <= 86400 and candidate.status == TaskStatus.BLOCKED:
                    is_at_risk = True
                    risk_reason = "Gần hạn chót (<24h) nhưng đang bị chặn (Near deadline but BLOCKED)"
            
            if not is_at_risk and ctx and ctx.days_in_current_status >= 7:
                is_at_risk = True
                risk_reason = f"Stale for {ctx.days_in_current_status} days without update"

            if is_at_risk and risk_reason:
                identified_risks.append(f"Task '{candidate.title}': {risk_reason}")

            # Primary snippet & deep link & source_type
            snippet = candidate.description or candidate.title
            deep_link = None
            source_type = None
            if candidate.evidences:
                snippet = candidate.evidences[0].snippet or snippet
                deep_link = candidate.evidences[0].external_url
                st_obj = candidate.evidences[0].source_type
                source_type = st_obj.value if hasattr(st_obj, "value") else str(st_obj) if st_obj else None

            today_task_items.append(
                TodayTaskItem(
                    task_id=candidate.id,
                    title=candidate.title,
                    status=candidate.status,
                    project_key=candidate.project_key,
                    owner_name=candidate.owner_name or "Me",
                    requester_name=candidate.requester_name,
                    due_date=candidate.due_date,
                    priority=breakdown,
                    primary_evidence_snippet=snippet,
                    deep_link=deep_link,
                    is_at_risk=is_at_risk,
                    risk_reason=risk_reason,
                    source_type=source_type,
                )
            )

        # Sort tasks according to Frozen Canonical Sort Contract (reverse=True)
        # Primary:   status_rank ASC (IN_PROGRESS=1, TODO=2, BLOCKED=3, DONE=4, DISMISSED=5)
        # Secondary: priority_score DESC
        # Tertiary:  due_date ASC (nulls last)
        # Quaternary: updated_at DESC
        def _today_sort_key_inner(item: TodayTaskItem):
            return _today_sort_key(item, tasks_map=tasks_map)

        today_task_items.sort(key=_today_sort_key_inner, reverse=True)
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
                identified_risks.append(f"Overdue commitment '{fc.title}': stale for {fc.days_stale} days with {fc.promised_to_name}")

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
            parts.append(f"{top_tasks_count} việc cần tập trung hôm nay ({top_tasks_count} tasks to focus on today)")
        else:
            parts.append("Hôm nay không có việc tồn đọng (No pending tasks for today)")

        if risks_count > 0:
            parts.append(f"{risks_count} rủi ro tiến độ ({risks_count} risk(s) to review)")

        if waiting_count > 0:
            parts.append(f"{waiting_count} việc đang chờ người khác ({waiting_count} waiting on others)")

        if forgotten_count > 0:
            parts.append(f"{forgotten_count} cam kết cần cập nhật ({forgotten_count} commitments to update)")

        return "Hôm nay (Today): " + ", ".join(parts) + "."
    
    _today_sort_key = staticmethod(_today_sort_key)
