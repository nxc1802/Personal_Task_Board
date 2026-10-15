"""Strict Deterministic and Semantic Comparators for Benchmark Framework.

Enforces zero-compromise evaluation standards:
- Exact UTC deadline comparison (±0 seconds tolerance, never accepts keyword presence).
- Exact state machine status enum matching (strictly prohibits TODO == IN_PROGRESS).
- Exact Persona ID match for owner and requester attribution.
- Exact noise rejection without leakage.
- Strict correlation merge invariants.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from ptb_contracts.l2_processing import UnifiedTaskCandidate

from benchmarks.models import (
    BenchmarkSample,
    CaseEvaluationResult,
    EvalVerdict,
    FailureCategory,
    FieldMismatch,
)
from benchmarks.runner.runner import RunTrace, StepSnapshot


def normalize_utc_timestamp(dt: Any) -> Optional[str]:
    """Normalize datetime object to UTC ISO 8601 string (%Y-%m-%dT%H:%M:%SZ)."""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            parsed = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return dt
    if hasattr(dt, "astimezone"):
        utc_dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(dt)


class StrictDeterministicComparator:
    """Strict comparator evaluating production outputs against ground truth specs."""

    @staticmethod
    def _tokenize(text: str) -> Set[str]:
        if not text:
            return set()
        return {
            tok
            for tok in re.findall(r"[a-zA-Z0-9_\-\u00C0-\u1EF9]+", text.lower())
            if len(tok) >= 2
        }

    @classmethod
    def compare_title_keywords(
        cls,
        actual_title: str,
        expected_keywords: List[str],
        actual_description: Optional[str] = None,
    ) -> Tuple[bool, float]:
        if not expected_keywords:
            return True, 1.0
        haystack = f"{actual_title or ''} {actual_description or ''}".lower()
        matched = sum(1 for kw in expected_keywords if kw.lower() in haystack)
        coverage = matched / len(expected_keywords)
        return coverage >= 1.0, round(coverage, 4)

    @classmethod
    def evaluate_case(
        cls,
        sample: BenchmarkSample,
        snapshot: StepSnapshot,
        trace: RunTrace,
    ) -> CaseEvaluationResult:
        """Evaluate a single test case against its ground truth specification."""
        gt = sample.ground_truth
        source_type_str = (
            sample.raw_event.source_type.value
            if hasattr(sample.raw_event.source_type, "value")
            else str(sample.raw_event.source_type)
        )

        # 0. Infrastructure / PENDING check
        if snapshot.is_pending:
            return CaseEvaluationResult(
                case_id=sample.sample_id,
                case_category=sample.case_category,
                source_type=source_type_str,
                verdict=EvalVerdict.PENDING,
                actual={},
                expected={"expected_is_task": gt.expected_is_task},
                failure_category=FailureCategory.INFRASTRUCTURE_UNAVAILABLE,
                failure_message=snapshot.pending_reason or "Infrastructure unavailable",
                latency_ms=snapshot.latency_ms,
                trace={"step_index": snapshot.step_index},
            )

        # 0b. Execution exception check
        if snapshot.error:
            return CaseEvaluationResult(
                case_id=sample.sample_id,
                case_category=sample.case_category,
                source_type=source_type_str,
                verdict=EvalVerdict.FAIL,
                actual={"error": snapshot.error},
                expected={"expected_is_task": gt.expected_is_task},
                failure_category=FailureCategory.EXECUTION_ERROR,
                failure_message=f"Pipeline error: {snapshot.error}",
                latency_ms=snapshot.latency_ms,
                trace={"step_index": snapshot.step_index},
            )

        mismatches: List[FieldMismatch] = []
        failure_category: Optional[FailureCategory] = None
        failure_message: Optional[str] = None

        # 1. Filter Check (Task vs Noise)
        if not gt.expected_is_task:
            if snapshot.resulting_task is not None:
                mismatches.append(
                    FieldMismatch(
                        sample_id=sample.sample_id,
                        module="Filter",
                        field="expected_is_task",
                        expected=False,
                        actual=True,
                        message="Noise event incorrectly generated a UnifiedTaskCandidate",
                    )
                )
                failure_category = FailureCategory.NOISE_LEAK
                failure_message = "Noise/negation sample leaked through filter as a task."

            return CaseEvaluationResult(
                case_id=sample.sample_id,
                case_category=sample.case_category,
                source_type=source_type_str,
                verdict=EvalVerdict.PASS if not mismatches else EvalVerdict.FAIL,
                actual={
                    "should_extract": snapshot.should_extract,
                    "task_created": snapshot.resulting_task is not None,
                },
                expected={"expected_is_task": False},
                failure_category=failure_category,
                failure_message=failure_message,
                mismatches=mismatches,
                latency_ms=snapshot.latency_ms,
                trace={"step_index": snapshot.step_index},
            )

        # 2. Extraction Check (Task expected)
        task = snapshot.resulting_task
        if task is None:
            mismatches.append(
                FieldMismatch(
                    sample_id=sample.sample_id,
                    module="Extractor",
                    field="resulting_task",
                    expected="UnifiedTaskCandidate",
                    actual=None,
                    message="Valid task event failed to produce a UnifiedTaskCandidate",
                )
            )
            return CaseEvaluationResult(
                case_id=sample.sample_id,
                case_category=sample.case_category,
                source_type=source_type_str,
                verdict=EvalVerdict.FAIL,
                actual={"task_created": False},
                expected={"expected_is_task": True},
                failure_category=FailureCategory.EXTRACTION_MISS,
                failure_message="Task extraction missed a valid task commitment.",
                mismatches=mismatches,
                latency_ms=snapshot.latency_ms,
                trace={"step_index": snapshot.step_index},
            )

        # 3. Attribution Check (Owner & Requester exact canonical ID match)
        if gt.expected_owner_id is not None:
            if task.owner_canonical_id != gt.expected_owner_id:
                mismatches.append(
                    FieldMismatch(
                        sample_id=sample.sample_id,
                        module="Attribution",
                        field="owner_canonical_id",
                        expected=gt.expected_owner_id,
                        actual=task.owner_canonical_id,
                        message=(
                            f"Owner ID mismatch: expected '{gt.expected_owner_id}', "
                            f"got '{task.owner_canonical_id}'"
                        ),
                    )
                )
                if failure_category is None:
                    failure_category = FailureCategory.ATTRIBUTION_OWNER
                    failure_message = f"Owner ID mismatch: expected '{gt.expected_owner_id}', got '{task.owner_canonical_id}'"

        if gt.expected_requester_id is not None:
            if task.requester_canonical_id != gt.expected_requester_id:
                mismatches.append(
                    FieldMismatch(
                        sample_id=sample.sample_id,
                        module="Attribution",
                        field="requester_canonical_id",
                        expected=gt.expected_requester_id,
                        actual=task.requester_canonical_id,
                        message=(
                            f"Requester ID mismatch: expected '{gt.expected_requester_id}', "
                            f"got '{task.requester_canonical_id}'"
                        ),
                    )
                )
                if failure_category is None:
                    failure_category = FailureCategory.ATTRIBUTION_REQUESTER
                    failure_message = f"Requester ID mismatch: expected '{gt.expected_requester_id}', got '{task.requester_canonical_id}'"

        # 4. Strict Deadline Check (Exact UTC Timestamp Match +-0s)
        exp_due_norm = normalize_utc_timestamp(gt.expected_due_date)
        act_due_norm = normalize_utc_timestamp(task.due_date)

        if exp_due_norm is not None:
            if act_due_norm is None:
                mismatches.append(
                    FieldMismatch(
                        sample_id=sample.sample_id,
                        module="Extractor",
                        field="due_date",
                        expected=exp_due_norm,
                        actual=None,
                        message=f"Hard failure: due_date is None, expected '{exp_due_norm}'",
                    )
                )
                if failure_category is None:
                    failure_category = FailureCategory.DEADLINE_MISMATCH
                    failure_message = f"Due date is None (expected '{exp_due_norm}')"
            elif act_due_norm != exp_due_norm:
                mismatches.append(
                    FieldMismatch(
                        sample_id=sample.sample_id,
                        module="Extractor",
                        field="due_date",
                        expected=exp_due_norm,
                        actual=act_due_norm,
                        message=f"Due date mismatch: expected '{exp_due_norm}', got '{act_due_norm}'",
                    )
                )
                if failure_category is None:
                    failure_category = FailureCategory.DEADLINE_MISMATCH
                    failure_message = f"Due date mismatch: expected '{exp_due_norm}', got '{act_due_norm}'"

            if bool(task.explicit_deadline) != bool(gt.expected_explicit_deadline):
                mismatches.append(
                    FieldMismatch(
                        sample_id=sample.sample_id,
                        module="Extractor",
                        field="explicit_deadline",
                        expected=gt.expected_explicit_deadline,
                        actual=task.explicit_deadline,
                        message=f"Explicit deadline mismatch: expected {gt.expected_explicit_deadline}, got {task.explicit_deadline}",
                    )
                )
                if failure_category is None:
                    failure_category = FailureCategory.DEADLINE_MISMATCH
                    failure_message = f"Explicit deadline flag mismatch: expected {gt.expected_explicit_deadline}"

        # 5. Strict Status Check (Exact State Machine Enum Match)
        if gt.expected_status is not None:
            act_status_str = task.status.value if hasattr(task.status, "value") else str(task.status)
            exp_status_str = gt.expected_status.value if hasattr(gt.expected_status, "value") else str(gt.expected_status)
            if act_status_str != exp_status_str:
                mismatches.append(
                    FieldMismatch(
                        sample_id=sample.sample_id,
                        module="StatusMachine",
                        field="status",
                        expected=exp_status_str,
                        actual=act_status_str,
                        message=f"Strict status mismatch: expected '{exp_status_str}', got '{act_status_str}'",
                    )
                )
                if failure_category is None:
                    failure_category = FailureCategory.STATUS_MISMATCH
                    failure_message = f"Strict status mismatch: expected '{exp_status_str}', got '{act_status_str}'"

        # 6. Correlation Merging Check
        if gt.correlation.expected_merge:
            if not snapshot.merged:
                mismatches.append(
                    FieldMismatch(
                        sample_id=sample.sample_id,
                        module="Correlation",
                        field="merged",
                        expected=True,
                        actual=False,
                        message="Event was expected to merge into an existing task, but was created as new",
                    )
                )
                if failure_category is None:
                    failure_category = FailureCategory.CORRELATION_MISSED_MERGE
                    failure_message = "Missed merge: event did not merge into target task."
        else:
            if snapshot.merged:
                mismatches.append(
                    FieldMismatch(
                        sample_id=sample.sample_id,
                        module="Correlation",
                        field="merged",
                        expected=False,
                        actual=True,
                        message=f"False merge: event unexpectedly merged into task {snapshot.target_task_id}",
                    )
                )
                if failure_category is None:
                    failure_category = FailureCategory.CORRELATION_FALSE_MERGE
                    failure_message = f"False merge: event erroneously merged into {snapshot.target_task_id}"

        # 7. Semantic Title Coverage
        title_ok, coverage = cls.compare_title_keywords(
            actual_title=task.title,
            expected_keywords=gt.expected_title_keywords,
            actual_description=task.description,
        )
        if not title_ok:
            mismatches.append(
                FieldMismatch(
                    sample_id=sample.sample_id,
                    module="Semantic",
                    field="title_keywords",
                    expected=gt.expected_title_keywords,
                    actual=task.title,
                    message=f"Missing keywords in title/description (coverage: {coverage:.2f})",
                )
            )
            if failure_category is None:
                failure_category = FailureCategory.EXTRACTION_MISS
                failure_message = f"Missing keywords in title/description (coverage: {coverage:.2f})"

        # 8. Provenance Evidence Check
        if not task.evidences:
            mismatches.append(
                FieldMismatch(
                    sample_id=sample.sample_id,
                    module="Lineage",
                    field="evidences",
                    expected=">=1 evidence record",
                    actual=0,
                    message="Task has no associated evidence records (orphan task)",
                )
            )
            if failure_category is None:
                failure_category = FailureCategory.LINEAGE_ORPHAN
                failure_message = "Task has zero evidence records (orphan task)."

        is_passed = len(mismatches) == 0
        actual_dict = {
            "task_id": task.id,
            "title": task.title,
            "status": task.status.value if hasattr(task.status, "value") else str(task.status),
            "owner_canonical_id": task.owner_canonical_id,
            "requester_canonical_id": task.requester_canonical_id,
            "due_date": act_due_norm,
            "explicit_deadline": task.explicit_deadline,
            "merged": snapshot.merged,
            "evidence_count": len(task.evidences),
        }
        expected_dict = {
            "expected_is_task": True,
            "expected_status": gt.expected_status.value if gt.expected_status and hasattr(gt.expected_status, "value") else str(gt.expected_status) if gt.expected_status else None,
            "expected_owner_id": gt.expected_owner_id,
            "expected_requester_id": gt.expected_requester_id,
            "expected_due_date": exp_due_norm,
            "expected_explicit_deadline": gt.expected_explicit_deadline,
            "expected_merge": gt.correlation.expected_merge,
        }

        return CaseEvaluationResult(
            case_id=sample.sample_id,
            case_category=sample.case_category,
            source_type=source_type_str,
            verdict=EvalVerdict.PASS if is_passed else EvalVerdict.FAIL,
            actual=actual_dict,
            expected=expected_dict,
            failure_category=failure_category,
            failure_message=failure_message,
            mismatches=mismatches,
            latency_ms=snapshot.latency_ms,
            semantic_score=coverage,
            trace={
                "step_index": snapshot.step_index,
                "target_task_id": snapshot.target_task_id,
            },
        )
