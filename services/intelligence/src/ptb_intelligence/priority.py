"""Deterministic Priority Engine for Personal Task Board (Layer 4 Intelligence).

Computes a deterministic priority score (0-100) based on mathematical formula
specified in docs/v1.md and config/priority.yaml:
    Score = DeadlineScore (0-35)
          + CustomerImpact (0-25)
          + ProductionImpact (0-20)
          + CommitmentWeight (0-10)
          + StaleAge (0-10)
          - WaitingPenalty (0-10)
          - UncertaintyPenalty (0-15)
"""

from datetime import datetime, timezone
import logging
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Union
import yaml

from ptb_contracts.l2_processing import EvidenceType, TaskStatus, UnifiedTaskCandidate
from ptb_contracts.l4_intelligence import PriorityBreakdown, TaskWithContext

logger = logging.getLogger("ptb.intelligence.priority")

DEFAULT_CONFIG_PATH = "config/priority.yaml"

DEFAULT_WEIGHTS = {
    "deadline_weight": 35.0,
    "customer_impact_weight": 25.0,
    "production_impact_weight": 20.0,
    "commitment_weight": 10.0,
    "stale_age_weight": 10.0,
    "waiting_penalty": 5.0,
    "uncertainty_penalty_max": 15.0,
}


def resolve_config_path(config_path: Optional[Union[str, Path]] = None) -> Optional[Path]:
    """Resolve the priority configuration path from explicit path or default candidates."""
    if config_path:
        return Path(config_path)

    # Search standard workspace locations
    candidates = [
        Path.cwd() / "config" / "priority.yaml",
        Path(__file__).resolve().parents[4] / "config" / "priority.yaml",
        Path("config/priority.yaml"),
    ]
    for p in candidates:
        if p.is_file():
            return p

    return Path.cwd() / "config" / "priority.yaml"


def load_priority_config(config_path: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """Load priority configuration from YAML file.

    Returns dict containing weights, thresholds, and keywords.
    Safely falls back to empty dict on missing file, I/O error, or YAML syntax error.
    """
    resolved = resolve_config_path(config_path)
    if not resolved or not resolved.is_file():
        logger.debug("Priority config file not found at %s. Using default values.", resolved)
        return {}

    try:
        with open(resolved, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            if isinstance(data, dict):
                return data
            logger.warning("Priority config at %s is not a dictionary. Using defaults.", resolved)
            return {}
    except Exception as ex:
        logger.warning(
            "Failed to load priority config from %s: %s. Falling back to default values.",
            resolved,
            ex,
        )
        return {}


class DeterministicPriorityEngine:
    """Calculates deterministic, reproducible priority scores with transparent explanations."""

    # Keywords for production and critical incidents
    PROD_HIGH_KEYWORDS = [
        "production", "prod", "hotfix", "outage", "p0", "sev-1", "sev1", "down",
        "data loss", "crash", "incident", "security breach", "vulnerability"
    ]
    PROD_MEDIUM_KEYWORDS = [
        "staging", "uat", "bug", "defect", "p1", "sev-2", "sev2", "exception",
        "error", "failure", "broken", "regression"
    ]

    # Keywords for customer impact
    CUSTOMER_KEYWORDS = [
        "customer", "client", "khách hàng", "partner", "vip", "tenant",
        "enterprise", "sla", "contract"
    ]

    def __init__(
        self,
        config_path: Optional[Union[str, Path]] = None,
        deadline_weight: Optional[float] = None,
        customer_impact_weight: Optional[float] = None,
        production_impact_weight: Optional[float] = None,
        commitment_weight: Optional[float] = None,
        stale_age_weight: Optional[float] = None,
        waiting_penalty: Optional[float] = None,
        uncertainty_penalty_max: Optional[float] = None,
        # Backward compatibility aliases
        max_deadline_score: Optional[float] = None,
        max_customer_score: Optional[float] = None,
        max_production_score: Optional[float] = None,
        max_commitment_score: Optional[float] = None,
        max_stale_score: Optional[float] = None,
        auto_load_config: bool = True,
    ) -> None:
        self.config_path = config_path

        # Initial defaults
        self.deadline_weight = DEFAULT_WEIGHTS["deadline_weight"]
        self.customer_impact_weight = DEFAULT_WEIGHTS["customer_impact_weight"]
        self.production_impact_weight = DEFAULT_WEIGHTS["production_impact_weight"]
        self.commitment_weight = DEFAULT_WEIGHTS["commitment_weight"]
        self.stale_age_weight = DEFAULT_WEIGHTS["stale_age_weight"]
        self.waiting_penalty = DEFAULT_WEIGHTS["waiting_penalty"]
        self.uncertainty_penalty_max = DEFAULT_WEIGHTS["uncertainty_penalty_max"]

        self.prod_high_keywords = list(self.PROD_HIGH_KEYWORDS)
        self.prod_medium_keywords = list(self.PROD_MEDIUM_KEYWORDS)
        self.customer_keywords = list(self.CUSTOMER_KEYWORDS)

        # 1. Load config from YAML if enabled
        if auto_load_config:
            self.load_config(config_path)

        # 2. Explicit constructor overrides take precedence
        if deadline_weight is not None:
            self.deadline_weight = float(deadline_weight)
        elif max_deadline_score is not None:
            self.deadline_weight = float(max_deadline_score)

        if customer_impact_weight is not None:
            self.customer_impact_weight = float(customer_impact_weight)
        elif max_customer_score is not None:
            self.customer_impact_weight = float(max_customer_score)

        if production_impact_weight is not None:
            self.production_impact_weight = float(production_impact_weight)
        elif max_production_score is not None:
            self.production_impact_weight = float(max_production_score)

        if commitment_weight is not None:
            self.commitment_weight = float(commitment_weight)
        elif max_commitment_score is not None:
            self.commitment_weight = float(max_commitment_score)

        if stale_age_weight is not None:
            self.stale_age_weight = float(stale_age_weight)
        elif max_stale_score is not None:
            self.stale_age_weight = float(max_stale_score)

        if waiting_penalty is not None:
            self.waiting_penalty = float(waiting_penalty)

        if uncertainty_penalty_max is not None:
            self.uncertainty_penalty_max = float(uncertainty_penalty_max)

    def load_config(self, config_path: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
        """Load configuration from config/priority.yaml or custom path with safe fallback."""
        target_path = config_path or self.config_path
        config = load_priority_config(target_path)
        weights = config.get("weights", {})

        if "deadline_weight" in weights:
            try:
                self.deadline_weight = float(weights["deadline_weight"])
            except (ValueError, TypeError):
                pass

        if "customer_impact_weight" in weights:
            try:
                self.customer_impact_weight = float(weights["customer_impact_weight"])
            except (ValueError, TypeError):
                pass

        if "production_impact_weight" in weights:
            try:
                self.production_impact_weight = float(weights["production_impact_weight"])
            except (ValueError, TypeError):
                pass

        if "commitment_weight" in weights:
            try:
                self.commitment_weight = float(weights["commitment_weight"])
            except (ValueError, TypeError):
                pass

        if "stale_age_weight" in weights:
            try:
                self.stale_age_weight = float(weights["stale_age_weight"])
            except (ValueError, TypeError):
                pass

        if "waiting_penalty" in weights:
            try:
                self.waiting_penalty = float(weights["waiting_penalty"])
            except (ValueError, TypeError):
                pass

        if "uncertainty_penalty_max" in weights:
            try:
                self.uncertainty_penalty_max = float(weights["uncertainty_penalty_max"])
            except (ValueError, TypeError):
                pass

        keywords = config.get("keywords", {})
        if "prod_high" in keywords and isinstance(keywords["prod_high"], list):
            self.prod_high_keywords = [str(k).lower() for k in keywords["prod_high"]]
        if "prod_medium" in keywords and isinstance(keywords["prod_medium"], list):
            self.prod_medium_keywords = [str(k).lower() for k in keywords["prod_medium"]]
        if "customer" in keywords and isinstance(keywords["customer"], list):
            self.customer_keywords = [str(k).lower() for k in keywords["customer"]]

        return config

    @classmethod
    def from_config(cls, config_path: Optional[Union[str, Path]] = None) -> "DeterministicPriorityEngine":
        """Factory method to instantiate DeterministicPriorityEngine from config file."""
        return cls(config_path=config_path)

    # Backward compatibility properties
    @property
    def max_deadline_score(self) -> float:
        return self.deadline_weight

    @max_deadline_score.setter
    def max_deadline_score(self, value: float) -> None:
        self.deadline_weight = value

    @property
    def max_customer_score(self) -> float:
        return self.customer_impact_weight

    @max_customer_score.setter
    def max_customer_score(self, value: float) -> None:
        self.customer_impact_weight = value

    @property
    def max_production_score(self) -> float:
        return self.production_impact_weight

    @max_production_score.setter
    def max_production_score(self, value: float) -> None:
        self.production_impact_weight = value

    @property
    def max_commitment_score(self) -> float:
        return self.commitment_weight

    @max_commitment_score.setter
    def max_commitment_score(self, value: float) -> None:
        self.commitment_weight = value

    @property
    def max_stale_score(self) -> float:
        return self.stale_age_weight

    @max_stale_score.setter
    def max_stale_score(self, value: float) -> None:
        self.stale_age_weight = value

    def _ensure_utc(self, dt: Optional[datetime]) -> Optional[datetime]:
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def calculate_priority(
        self,
        task_input: Union[TaskWithContext, UnifiedTaskCandidate, Dict[str, Any]],
        now: Optional[datetime] = None,
    ) -> PriorityBreakdown:
        """Calculate the comprehensive priority breakdown for a given task.

        Args:
            task_input: TaskWithContext, UnifiedTaskCandidate, or equivalent dictionary.
            now: Optional reference timestamp (defaults to current UTC time).

        Returns:
            PriorityBreakdown model containing component scores and formatted explanation.
        """
        ref_time = self._ensure_utc(now) or datetime.now(timezone.utc)

        # Normalize task fields
        task_candidate, context = self._extract_task_and_context(task_input)

        # 1. Deadline Score
        deadline_score, deadline_reason = self._compute_deadline_score(task_candidate, ref_time)

        # 2. Customer Impact
        customer_score, customer_reason = self._compute_customer_impact(task_candidate)

        # 3. Production Impact
        prod_score, prod_reason = self._compute_production_impact(task_candidate, context)

        # 4. Commitment Weight
        commit_weight, commit_reason = self._compute_commitment_weight(task_candidate, context)

        # 5. Stale Age
        stale_score, stale_reason = self._compute_stale_age(task_candidate, context, ref_time)

        # 6. Waiting Penalty
        waiting_penalty, waiting_reason = self._compute_waiting_penalty(task_candidate, context)

        # 7. Uncertainty Deduction
        uncertainty_deduction, uncertainty_reason = self._compute_uncertainty_deduction(task_candidate)

        # Calculate Total Score clamped [0.0, 100.0]
        raw_total = (
            deadline_score
            + customer_score
            + prod_score
            + commit_weight
            + stale_score
            - waiting_penalty
            - uncertainty_deduction
        )
        total_score = round(max(0.0, min(100.0, raw_total)), 1)

        # Generate human/LLM transparent explanation
        explanation = self._build_explanation(
            total_score=total_score,
            reasons=[
                deadline_reason,
                customer_reason,
                prod_reason,
                commit_reason,
                stale_reason,
                waiting_reason,
                uncertainty_reason,
            ],
        )

        return PriorityBreakdown(
            total_score=total_score,
            deadline_score=round(deadline_score, 1),
            customer_impact_score=round(customer_score, 1),
            production_impact_score=round(prod_score, 1),
            commitment_weight=round(commit_weight, 1),
            waiting_penalty=round(waiting_penalty, 1),
            stale_age_score=round(stale_score, 1),
            uncertainty_deduction=round(uncertainty_deduction, 1),
            llm_explanation=explanation,
        )

    def _extract_task_and_context(
        self,
        task_input: Union[TaskWithContext, UnifiedTaskCandidate, Dict[str, Any]],
    ) -> tuple[UnifiedTaskCandidate, Optional[TaskWithContext]]:
        if isinstance(task_input, TaskWithContext):
            return task_input.task, task_input
        elif isinstance(task_input, UnifiedTaskCandidate):
            return task_input, None
        elif isinstance(task_input, dict):
            if "task" in task_input and isinstance(task_input["task"], (dict, UnifiedTaskCandidate)):
                ctx = TaskWithContext.model_validate(task_input)
                return ctx.task, ctx
            candidate = UnifiedTaskCandidate.model_validate(task_input)
            return candidate, None
        raise ValueError(f"Unsupported task input type: {type(task_input)}")

    def _compute_deadline_score(
        self,
        task: UnifiedTaskCandidate,
        ref_time: datetime,
    ) -> tuple[float, Optional[str]]:
        if not task.due_date:
            return 0.0, None

        due_date = self._ensure_utc(task.due_date)
        diff_seconds = (due_date - ref_time).total_seconds()
        diff_hours = diff_seconds / 3600.0
        diff_days = diff_seconds / 86400.0

        scale = self.deadline_weight / 35.0

        if diff_seconds < 0:
            # Overdue tasks receive maximum deadline score
            days_overdue = abs(round(diff_days, 1))
            return self.deadline_weight, f"Đã quá hạn {days_overdue} ngày (+{self.deadline_weight:.1f})"

        if diff_hours <= 24.0:
            # Sát deadline trong 24h
            base_score = 30.0 + (24.0 - max(0.0, diff_hours)) / 24.0 * 5.0
            score = min(self.deadline_weight, base_score * scale)
            return score, f"Sát deadline trong {round(diff_hours, 1)}h (+{score:.1f})"

        if diff_days <= 3.0:
            # Sắp đến hạn trong 3 ngày
            base_score = 20.0 + (3.0 - diff_days) / 2.0 * 10.0
            score = base_score * scale
            return score, f"Hạn chót trong {round(diff_days, 1)} ngày (+{score:.1f})"

        if diff_days <= 7.0:
            # Trong vòng 1 tuần
            base_score = 10.0 + (7.0 - diff_days) / 4.0 * 10.0
            score = base_score * scale
            return score, f"Hạn chót trong tuần ({round(diff_days, 1)} ngày) (+{score:.1f})"

        if diff_days <= 14.0:
            base_score = 2.0 + (14.0 - diff_days) / 7.0 * 8.0
            score = base_score * scale
            return score, f"Hạn chót trong 2 tuần (+{score:.1f})"

        base_score = max(0.0, 2.0 - (diff_days - 14.0) * 0.1)
        score = base_score * scale
        return score, f"Hạn chót xa ({round(diff_days, 1)} ngày) (+{score:.1f})" if score > 0 else None

    def _compute_customer_impact(
        self,
        task: UnifiedTaskCandidate,
    ) -> tuple[float, Optional[str]]:
        scale = self.customer_impact_weight / 25.0
        if task.customer_id:
            return self.customer_impact_weight, f"Task khách hàng cụ thể ({task.customer_id}) (+{self.customer_impact_weight:.1f})"

        text_to_search = f"{task.title} {task.description or ''} {task.project_key or ''}".lower()
        for kw in self.customer_keywords:
            if re.search(r"\b" + re.escape(kw) + r"\b", text_to_search):
                score = 18.0 * scale
                return score, f"Liên quan khách hàng / đối tác (+{score:.1f})"

        return 0.0, None

    def _compute_production_impact(
        self,
        task: UnifiedTaskCandidate,
        context: Optional[TaskWithContext],
    ) -> tuple[float, Optional[str]]:
        scale = self.production_impact_weight / 20.0
        evidence_text = " ".join([ev.snippet for ev in task.evidences]).lower()
        full_text = f"{task.title} {task.description or ''} {evidence_text}".lower()

        # Check for AGENT_BUG_FIX evidence
        has_bug_evidence = any(
            ev.evidence_type == EvidenceType.AGENT_BUG_FIX for ev in task.evidences
        )

        for kw in self.prod_high_keywords:
            if re.search(r"\b" + re.escape(kw) + r"\b", full_text):
                return self.production_impact_weight, f"Sự cố / lỗi Production (+{self.production_impact_weight:.1f})"

        for kw in self.prod_medium_keywords:
            if re.search(r"\b" + re.escape(kw) + r"\b", full_text) or has_bug_evidence:
                score = 12.0 * scale
                return score, f"Lỗi Staging / bug (+{score:.1f})"

        return 0.0, None

    def _compute_commitment_weight(
        self,
        task: UnifiedTaskCandidate,
        context: Optional[TaskWithContext],
    ) -> tuple[float, Optional[str]]:
        has_commit_evidence = any(
            ev.evidence_type == EvidenceType.CHAT_COMMITMENT for ev in task.evidences
        )

        has_commit_relation = False
        if context and context.relations:
            has_commit_relation = any(
                r.relation_type == "COMMITTED_TO" for r in context.relations
            )

        if has_commit_evidence or has_commit_relation:
            return self.commitment_weight, f"Cam kết trực tiếp từ user (+{self.commitment_weight:.1f})"

        return 0.0, None

    def _compute_stale_age(
        self,
        task: UnifiedTaskCandidate,
        context: Optional[TaskWithContext],
        ref_time: datetime,
    ) -> tuple[float, Optional[str]]:
        scale = self.stale_age_weight / 10.0
        days_stale = 0.0
        if context and context.days_in_current_status > 0:
            days_stale = float(context.days_in_current_status)
        elif task.created_at:
            created_at = self._ensure_utc(task.created_at)
            days_stale = max(0.0, (ref_time - created_at).total_seconds() / 86400.0)

        if days_stale <= 1.0:
            return 0.0, None

        if days_stale <= 3.0:
            score = (days_stale * 1.5) * scale
            return score, f"Tồn đọng {round(days_stale, 1)} ngày (+{score:.1f})"

        if days_stale <= 7.0:
            score = (4.5 + (days_stale - 3.0) * 1.0) * scale
            return score, f"Tồn đọng {round(days_stale, 1)} ngày (+{score:.1f})"

        score = min(self.stale_age_weight, (8.5 + (days_stale - 7.0) * 0.5) * scale)
        return score, f"Tồn đọng lâu ({round(days_stale, 1)} ngày) (+{score:.1f})"

    def _compute_waiting_penalty(
        self,
        task: UnifiedTaskCandidate,
        context: Optional[TaskWithContext],
    ) -> tuple[float, Optional[str]]:
        is_blocked = task.status == TaskStatus.BLOCKED
        has_blockers = False
        if context and context.blocking_tasks:
            has_blockers = True

        if is_blocked or has_blockers:
            penalty = self.waiting_penalty
            return penalty, f"Đang bị block / chờ người khác (-{penalty:.1f})"

        return 0.0, None

    def _compute_uncertainty_deduction(
        self,
        task: UnifiedTaskCandidate,
    ) -> tuple[float, Optional[str]]:
        confidence = task.extraction_confidence
        if confidence >= 0.85:
            return 0.0, None

        scale = self.uncertainty_penalty_max / 15.0
        deduction = min(self.uncertainty_penalty_max, max(0.0, (0.85 - confidence) * 25.0 * scale))
        if deduction > 0:
            return deduction, f"Độ tin cậy trích xuất chưa cao ({confidence:.2f}) (-{deduction:.1f})"
        return 0.0, None

    def _build_explanation(
        self,
        total_score: float,
        reasons: List[Optional[str]],
    ) -> str:
        valid_reasons = [r for r in reasons if r]
        if not valid_reasons:
            return f"Score {total_score}/100: Ưu tiên bình thường."
        return f"Score {total_score}/100: " + ", ".join(valid_reasons) + "."
