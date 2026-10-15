"""Statistical and mathematical metric computations for Benchmark Scorecard.

Includes:
- Wilson 95% Confidence Interval for binomial rates
- Kendall's tau-b rank correlation coefficient for task ordering
- Canonical sort ranking calculator
- Percentile calculation for latency metrics
"""

from __future__ import annotations

from datetime import datetime
import math
from typing import Any, List, Optional, Sequence, Tuple

from ptb_contracts.l2_processing import TaskStatus, UnifiedTaskCandidate

STATUS_RANK_MAP = {
    TaskStatus.IN_PROGRESS: 1,
    "IN_PROGRESS": 1,
    TaskStatus.TODO: 2,
    "TODO": 2,
    TaskStatus.BLOCKED: 3,
    "BLOCKED": 3,
    TaskStatus.DONE: 4,
    "DONE": 4,
    TaskStatus.DISMISSED: 5,
    "DISMISSED": 5,
    "CANCELLED": 5,
}


def wilson_score_interval(
    k: int,
    n: int,
    confidence: float = 0.95,
) -> Tuple[float, float]:
    """Calculate Wilson score 95% confidence interval for a proportion k / n.

    Args:
        k: Number of positive outcomes (successes).
        n: Total number of trials.
        confidence: Confidence level (default 0.95 -> z = 1.959964).

    Returns:
        (lower_bound, upper_bound) rounded to 4 decimals, bounded within [0.0, 1.0].
    """
    if n <= 0:
        return 0.0, 0.0

    k = max(0, min(k, n))
    p = k / n
    # For 95% confidence, z = 1.959963984540054
    z = 1.959963984540054 if abs(confidence - 0.95) < 1e-4 else 1.959963984540054

    denom = 1.0 + (z * z) / n
    center = (p + (z * z) / (2.0 * n)) / denom
    margin = (z / denom) * math.sqrt((p * (1.0 - p)) / n + (z * z) / (4.0 * n * n))

    lower = max(0.0, center - margin)
    upper = min(1.0, center + margin)

    return round(lower, 4), round(upper, 4)


def kendall_tau(
    ranks_x: Sequence[float | int],
    ranks_y: Sequence[float | int],
) -> float:
    """Compute Kendall's tau-b rank correlation between two ranking sequences.

    Returns:
        Correlation in [-1.0, 1.0], or 1.0 if sequence length < 2.
    """
    n = len(ranks_x)
    if n != len(ranks_y):
        raise ValueError(f"Sequences must have equal length: {len(ranks_x)} vs {len(ranks_y)}")
    if n <= 1:
        return 1.0

    c = 0
    d = 0
    tx = 0
    ty = 0

    for i in range(n):
        for j in range(i + 1, n):
            dx = ranks_x[i] - ranks_x[j]
            dy = ranks_y[i] - ranks_y[j]

            if dx == 0 and dy == 0:
                tx += 1
                ty += 1
            elif dx == 0:
                tx += 1
            elif dy == 0:
                ty += 1
            elif (dx > 0 and dy > 0) or (dx < 0 and dy < 0):
                c += 1
            else:
                d += 1

    denom = math.sqrt((c + d + tx) * (c + d + ty))
    if denom == 0.0:
        return 1.0
    return round((c - d) / denom, 4)


def canonical_task_sort_key(task: UnifiedTaskCandidate) -> Tuple[int, float, float, float]:
    """Compute 4-tuple key matching the canonical PTB sort contract:

    Primary:   status_rank ASC  (IN_PROGRESS=1, TODO=2, BLOCKED=3, DONE=4, DISMISSED=5)
    Secondary: priority_score DESC
    Tertiary:  due_date ASC (nulls last)
    Quaternary: updated_at DESC
    """
    status_str = task.status.value if hasattr(task.status, "value") else str(task.status)
    status_rank = STATUS_RANK_MAP.get(status_str, 99)

    # Invert priority_score for descending
    p_score = -(task.priority_score if task.priority_score is not None else 0.0)

    # Tertiary: due_date ASC, nulls last -> float('inf')
    if task.due_date is not None:
        due_val = (
            task.due_date.timestamp()
            if hasattr(task.due_date, "timestamp")
            else 0.0
        )
    else:
        due_val = float("inf")

    # Quaternary: updated_at DESC
    if task.updated_at is not None:
        upd_val = -(
            task.updated_at.timestamp()
            if hasattr(task.updated_at, "timestamp")
            else 0.0
        )
    else:
        upd_val = 0.0

    return (status_rank, p_score, due_val, upd_val)


def compute_kendall_tau_for_tasks(
    actual_tasks: List[UnifiedTaskCandidate],
) -> float:
    """Compute Kendall's tau between actual ordering of tasks and their canonical expected ordering."""
    if len(actual_tasks) <= 1:
        return 1.0

    # Sort tasks according to canonical sort contract
    expected_sorted = sorted(actual_tasks, key=canonical_task_sort_key)
    expected_order_map = {t.id: idx for idx, t in enumerate(expected_sorted)}

    ranks_actual = list(range(len(actual_tasks)))
    ranks_expected = [expected_order_map[t.id] for t in actual_tasks]

    return kendall_tau(ranks_actual, ranks_expected)


def calculate_percentile(values: Sequence[float], p: float) -> float:
    """Calculate the p-th percentile (0 <= p <= 100) using linear interpolation."""
    if not values:
        return 0.0
    s_vals = sorted(values)
    k = (len(s_vals) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return round(float(s_vals[int(k)]), 2)
    d0 = s_vals[int(f)] * (c - k)
    d1 = s_vals[int(c)] * (k - f)
    return round(float(d0 + d1), 2)
