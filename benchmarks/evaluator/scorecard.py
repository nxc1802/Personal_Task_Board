"""Multi-Dimensional Metric Evaluator synthesizing the 10-dimension benchmark scorecard."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Set
from collections import defaultdict

from ptb_contracts.l2_processing import TaskStatus, UnifiedTaskCandidate

from benchmarks.evaluator.comparators import StrictDeterministicComparator
from benchmarks.evaluator.metrics import (
    calculate_percentile,
    canonical_task_sort_key,
    compute_kendall_tau_for_tasks,
    wilson_score_interval,
)
from benchmarks.models import (
    BenchmarkDataset,
    BenchmarkScorecard,
    CaseEvaluationResult,
    ConfidenceInterval,
    DimensionScore,
    EvalVerdict,
    FailureCategory,
)
from benchmarks.runner.runner import RunTrace

logger = logging.getLogger("ptb.benchmarks.scorecard")


class BenchmarkMetricEvaluator:
    """Evaluates RunTrace and BenchmarkDataset to compute the authoritative 10-dimension scorecard."""

    @classmethod
    def evaluate(
        cls,
        dataset: BenchmarkDataset,
        clean_trace: RunTrace,
        replay_trace: Optional[RunTrace] = None,
        filter_case_id: Optional[str] = None,
    ) -> BenchmarkScorecard:
        case_results: List[CaseEvaluationResult] = []
        eval_samples = (
            [s for s in dataset.samples if s.sample_id == filter_case_id]
            if filter_case_id
            else dataset.samples
        )

        # 1. Evaluate individual cases
        for sample in eval_samples:
            snap = clean_trace.get_snapshot(sample.sample_id)
            if snap is None:
                case_results.append(
                    CaseEvaluationResult(
                        case_id=sample.sample_id,
                        case_category=sample.case_category,
                        source_type=(
                            sample.raw_event.source_type.value
                            if hasattr(sample.raw_event.source_type, "value")
                            else str(sample.raw_event.source_type)
                        ),
                        verdict=EvalVerdict.FAIL,
                        failure_category=FailureCategory.EXECUTION_ERROR,
                        failure_message=f"Missing execution snapshot for sample {sample.sample_id}",
                    )
                )
            else:
                res = StrictDeterministicComparator.evaluate_case(
                    sample=sample,
                    snapshot=snap,
                    trace=clean_trace,
                )
                case_results.append(res)

        # 1b. Include probe evaluation results (for PTB-BENCH-500 full evaluation)
        if getattr(clean_trace, "probe_results", None) and getattr(dataset.manifest, "dataset_name", "") != "PTB-MINI-SMOKE":
            case_results.extend(clean_trace.probe_results)

        total_cases = len(case_results)
        passed_cases = sum(1 for c in case_results if c.verdict == EvalVerdict.PASS)
        failed_cases = sum(1 for c in case_results if c.verdict == EvalVerdict.FAIL)
        pending_cases = sum(1 for c in case_results if c.verdict == EvalVerdict.PENDING)
        skipped_cases = sum(1 for c in case_results if c.verdict == EvalVerdict.SKIPPED)

        # Denominator rule: PENDING cases DO NOT count as PASS, they reduce pass rate
        pass_rate = round(passed_cases / total_cases, 4) if total_cases > 0 else 0.0

        dimensions: List[DimensionScore] = []

        # -------------------------------------------------------------
        # Dimension 1: Task Extraction (Macro-F1 across channels)
        # -------------------------------------------------------------
        channel_stats: Dict[str, Dict[str, int]] = defaultdict(lambda: {"TP": 0, "FP": 0, "FN": 0, "TN": 0})
        for sample in eval_samples:
            source = (
                sample.raw_event.source_type.value
                if hasattr(sample.raw_event.source_type, "value")
                else str(sample.raw_event.source_type)
            )
            snap = clean_trace.get_snapshot(sample.sample_id)
            if snap and snap.is_pending:
                continue
            is_task_exp = sample.ground_truth.expected_is_task
            extracted = snap.resulting_task is not None if snap else False

            if is_task_exp and extracted:
                channel_stats[source]["TP"] += 1
            elif not is_task_exp and extracted:
                channel_stats[source]["FP"] += 1
            elif is_task_exp and not extracted:
                channel_stats[source]["FN"] += 1
            else:
                channel_stats[source]["TN"] += 1

        f1_scores: List[float] = []
        channel_details: Dict[str, Any] = {}
        for ch, s in channel_stats.items():
            tp, fp, fn = s["TP"], s["FP"], s["FN"]
            prec = tp / (tp + fp) if (tp + fp) > 0 else 1.0 if tp == 0 and fp == 0 else 0.0
            rec = tp / (tp + fn) if (tp + fn) > 0 else 1.0 if tp == 0 and fn == 0 else 0.0
            f1 = (2 * prec * rec) / (prec + rec) if (prec + rec) > 0 else 0.0
            f1_scores.append(f1)
            channel_details[ch] = {
                "TP": tp,
                "FP": fp,
                "FN": fn,
                "TN": s["TN"],
                "precision": round(prec, 4),
                "recall": round(rec, 4),
                "f1": round(f1, 4),
            }

        macro_f1 = sum(f1_scores) / len(f1_scores) if f1_scores else 0.0
        tot_tp = sum(s["TP"] for s in channel_stats.values())
        tot_task_cases = sum(s["TP"] + s["FN"] for s in channel_stats.values())
        if tot_task_cases > 0:
            ci1_low, ci1_high = wilson_score_interval(tot_tp, tot_task_cases)
        else:
            tot_events = sum(s["TP"] + s["FP"] + s["FN"] + s["TN"] for s in channel_stats.values())
            tot_correct = sum(s["TP"] + s["TN"] for s in channel_stats.values())
            ci1_low, ci1_high = wilson_score_interval(tot_correct, tot_events)
        d1_passed = macro_f1 >= 0.88

        dimensions.append(
            DimensionScore(
                dimension_number=1,
                dimension_name="Task Extraction",
                primary_metric="Macro-F1 across active channels",
                target_bar="≥ 92.0%",
                hard_gate="F1 ≥ 88.0%",
                measured_value=round(macro_f1, 4),
                display_value=f"{macro_f1 * 100:.1f}%",
                ci=ConfidenceInterval(lower=ci1_low, upper=ci1_high),
                passed=d1_passed,
                details=channel_details,
            )
        )

        # -------------------------------------------------------------
        # Dimension 2: Noise & Negation Rejection (Rejection Rate)
        # -------------------------------------------------------------
        tot_neg = sum(s["TN"] + s["FP"] for s in channel_stats.values())
        tot_tn = sum(s["TN"] for s in channel_stats.values())
        rejection_rate = tot_tn / tot_neg if tot_neg > 0 else 1.0
        fpr = (tot_neg - tot_tn) / tot_neg if tot_neg > 0 else 0.0
        ci2_low, ci2_high = wilson_score_interval(tot_tn, tot_neg)
        d2_passed = fpr <= 0.10 and rejection_rate >= 0.90

        dimensions.append(
            DimensionScore(
                dimension_number=2,
                dimension_name="Noise & Negation Rejection",
                primary_metric="Noise Rejection Rate [TN / (TN + FP)]",
                target_bar="≥ 85.0%",
                hard_gate="FPR ≤ 10.0% (Rejection ≥ 90.0%)",
                measured_value=round(rejection_rate, 4),
                display_value=f"{rejection_rate * 100:.1f}% (FPR {fpr * 100:.1f}%)",
                ci=ConfidenceInterval(lower=ci2_low, upper=ci2_high),
                passed=d2_passed,
                details={"total_noise_cases": tot_neg, "rejected_TN": tot_tn, "leaked_FP": tot_neg - tot_tn},
            )
        )

        # -------------------------------------------------------------
        # Dimension 3: Attribution Accuracy (Owner & Requester exact IDs)
        # -------------------------------------------------------------
        owner_cases = 0
        owner_matches = 0
        req_cases = 0
        req_matches = 0
        for sample in eval_samples:
            if not sample.ground_truth.expected_is_task:
                continue
            snap = clean_trace.get_snapshot(sample.sample_id)
            if snap and snap.is_pending:
                continue
            if not snap or not snap.resulting_task:
                if sample.ground_truth.expected_owner_id:
                    owner_cases += 1
                if sample.ground_truth.expected_requester_id:
                    req_cases += 1
                continue

            t = snap.resulting_task
            if sample.ground_truth.expected_owner_id is not None:
                owner_cases += 1
                if t.owner_canonical_id == sample.ground_truth.expected_owner_id:
                    owner_matches += 1

            if sample.ground_truth.expected_requester_id is not None:
                req_cases += 1
                if t.requester_canonical_id == sample.ground_truth.expected_requester_id:
                    req_matches += 1

        tot_attr_cases = owner_cases + req_cases
        tot_attr_matches = owner_matches + req_matches
        attr_acc = tot_attr_matches / tot_attr_cases if tot_attr_cases > 0 else 1.0
        owner_acc = owner_matches / owner_cases if owner_cases > 0 else 1.0
        ci3_low, ci3_high = wilson_score_interval(tot_attr_matches, tot_attr_cases)
        d3_passed = owner_acc >= 0.85

        dimensions.append(
            DimensionScore(
                dimension_number=3,
                dimension_name="Attribution (Owner & Requester)",
                primary_metric="Exact Canonical Persona ID Accuracy",
                target_bar="≥ 90.0%",
                hard_gate="Owner ≥ 85.0%",
                measured_value=round(attr_acc, 4),
                display_value=f"{attr_acc * 100:.1f}% (Owner: {owner_acc * 100:.1f}%)",
                ci=ConfidenceInterval(lower=ci3_low, upper=ci3_high),
                passed=d3_passed,
                details={
                    "owner_cases": owner_cases,
                    "owner_matches": owner_matches,
                    "requester_cases": req_cases,
                    "requester_matches": req_matches,
                },
            )
        )

        # -------------------------------------------------------------
        # Dimension 4: Deadline Extraction (Exact UTC Timestamp Match)
        # -------------------------------------------------------------
        deadline_cases = 0
        deadline_matches = 0
        for sample in eval_samples:
            if sample.ground_truth.expected_due_date is not None:
                snap = clean_trace.get_snapshot(sample.sample_id)
                if snap and snap.is_pending:
                    continue
                deadline_cases += 1
                if snap and snap.resulting_task and snap.resulting_task.due_date is not None:
                    from benchmarks.evaluator.comparators import normalize_utc_timestamp
                    act = normalize_utc_timestamp(snap.resulting_task.due_date)
                    exp = normalize_utc_timestamp(sample.ground_truth.expected_due_date)
                    if act == exp and bool(snap.resulting_task.explicit_deadline) == bool(sample.ground_truth.expected_explicit_deadline):
                        deadline_matches += 1

        deadline_acc = deadline_matches / deadline_cases if deadline_cases > 0 else 1.0
        ci4_low, ci4_high = wilson_score_interval(deadline_matches, deadline_cases)
        d4_passed = deadline_acc >= 0.85

        dimensions.append(
            DimensionScore(
                dimension_number=4,
                dimension_name="Deadline Extraction",
                primary_metric="Exact UTC Timestamp Match (±0s)",
                target_bar="≥ 85.0%",
                hard_gate="Exact ISO Match (±0s)",
                measured_value=round(deadline_acc, 4),
                display_value=f"{deadline_acc * 100:.1f}% ({deadline_matches}/{deadline_cases})",
                ci=ConfidenceInterval(lower=ci4_low, upper=ci4_high),
                passed=d4_passed,
                details={"deadline_cases": deadline_cases, "matches": deadline_matches},
            )
        )

        # -------------------------------------------------------------
        # Dimension 5: Status Inference (State Machine strict enum match)
        # -------------------------------------------------------------
        status_cases = 0
        status_matches = 0
        for sample in eval_samples:
            if sample.ground_truth.expected_status is not None:
                snap = clean_trace.get_snapshot(sample.sample_id)
                if snap and snap.is_pending:
                    continue
                status_cases += 1
                if snap and snap.resulting_task:
                    act_status = snap.resulting_task.status.value if hasattr(snap.resulting_task.status, "value") else str(snap.resulting_task.status)
                    exp_status = sample.ground_truth.expected_status.value if hasattr(sample.ground_truth.expected_status, "value") else str(sample.ground_truth.expected_status)
                    if act_status == exp_status:
                        status_matches += 1

        status_acc = status_matches / status_cases if status_cases > 0 else 1.0
        ci5_low, ci5_high = wilson_score_interval(status_matches, status_cases)
        d5_passed = status_acc >= 0.90

        dimensions.append(
            DimensionScore(
                dimension_number=5,
                dimension_name="Status Inference",
                primary_metric="State Machine Match (Strict Enum)",
                target_bar="≥ 90.0%",
                hard_gate="Strict Enum (No TODO=IN_PROGRESS)",
                measured_value=round(status_acc, 4),
                display_value=f"{status_acc * 100:.1f}% ({status_matches}/{status_cases})",
                ci=ConfidenceInterval(lower=ci5_low, upper=ci5_high),
                passed=d5_passed,
                details={"status_cases": status_cases, "matches": status_matches},
            )
        )

        # -------------------------------------------------------------
        # Dimension 6: Ranking & Prioritization (Kendall's tau)
        # -------------------------------------------------------------
        tasks = clean_trace.final_tasks
        k_tau = compute_kendall_tau_for_tasks(tasks)
        has_in_prog = any(
            (t.status == TaskStatus.IN_PROGRESS or str(t.status) == "IN_PROGRESS")
            for t in tasks
        )
        in_prog_top_ok = True
        if has_in_prog and tasks:
            sorted_tasks = sorted(tasks, key=canonical_task_sort_key)
            first_status = sorted_tasks[0].status.value if hasattr(sorted_tasks[0].status, "value") else str(sorted_tasks[0].status)
            in_prog_top_ok = first_status == "IN_PROGRESS"

        d6_passed = k_tau >= 0.85 and in_prog_top_ok

        dimensions.append(
            DimensionScore(
                dimension_number=6,
                dimension_name="Ranking & Prioritization",
                primary_metric="Kendall's tau rank correlation",
                target_bar="≥ 0.85",
                hard_gate="In-Progress #1",
                measured_value=round(k_tau, 4),
                display_value=f"tau = {k_tau:.3f} (InProg #1: {'PASS' if in_prog_top_ok else 'FAIL'})",
                ci=None,
                passed=d6_passed,
                details={"total_tasks": len(tasks), "in_prog_top_ok": in_prog_top_ok},
            )
        )

        # -------------------------------------------------------------
        # Dimension 7: Correlation Merging (FMR & MMR)
        # -------------------------------------------------------------
        false_merges = 0
        expected_merges = 0
        missed_merges = 0
        for sample in eval_samples:
            snap = clean_trace.get_snapshot(sample.sample_id)
            if snap and snap.is_pending:
                continue
            exp_merge = sample.ground_truth.correlation.expected_merge
            if exp_merge:
                expected_merges += 1
                if not (snap and snap.merged):
                    missed_merges += 1
            else:
                if snap and snap.merged:
                    false_merges += 1

        fmr = false_merges / len(eval_samples) if eval_samples else 0.0
        mmr = missed_merges / expected_merges if expected_merges > 0 else 0.0
        d7_passed = (false_merges == 0) and (mmr <= 0.08)

        dimensions.append(
            DimensionScore(
                dimension_number=7,
                dimension_name="Correlation Merging",
                primary_metric="False Merge Rate (FMR) & Missed Merge Rate (MMR)",
                target_bar="FMR = 0.0%, MMR ≤ 5.0%",
                hard_gate="FMR == 0.0%, MMR ≤ 8.0%",
                measured_value=round(fmr, 4),
                display_value=f"FMR: {fmr * 100:.1f}%, MMR: {mmr * 100:.1f}%",
                ci=None,
                passed=d7_passed,
                details={
                    "false_merges": false_merges,
                    "expected_merges": expected_merges,
                    "missed_merges": missed_merges,
                },
            )
        )

        # -------------------------------------------------------------
        # Dimension 8: Lineage & Provenance (Orphan Tasks Count)
        # -------------------------------------------------------------
        orphan_tasks = 0
        for t in clean_trace.final_tasks:
            if not t.evidences or any(not getattr(e, "source_event_id", None) and not getattr(e, "raw_event_id", None) for e in t.evidences):
                orphan_tasks += 1

        tot_final_tasks = len(clean_trace.final_tasks)
        orphan_rate = orphan_tasks / tot_final_tasks if tot_final_tasks > 0 else 0.0
        d8_passed = orphan_tasks == 0

        dimensions.append(
            DimensionScore(
                dimension_number=8,
                dimension_name="Lineage & Provenance",
                primary_metric="Orphan Tasks Count [tasks without evidence / total]",
                target_bar="0 (ZERO orphans)",
                hard_gate="Lineage 100% (0 orphans)",
                measured_value=round(orphan_rate, 4),
                display_value=f"{orphan_tasks} orphans / {tot_final_tasks} tasks",
                ci=None,
                passed=d8_passed,
                details={"orphan_tasks": orphan_tasks, "total_tasks": tot_final_tasks},
            )
        )

        # -------------------------------------------------------------
        # Dimension 9: Idempotency (Replay Duplicate Count)
        # -------------------------------------------------------------
        replay_duplicates = 0
        if replay_trace is not None:
            replay_duplicates = replay_trace.replay_duplicates

        d9_passed = replay_duplicates == 0

        dimensions.append(
            DimensionScore(
                dimension_number=9,
                dimension_name="Idempotency",
                primary_metric="Replay Duplicate Count [tasks_after_replay - tasks_clean]",
                target_bar="0 (ZERO duplicates)",
                hard_gate="Replay duplicates == 0",
                measured_value=float(replay_duplicates),
                display_value=f"{replay_duplicates} duplicates created",
                ci=None,
                passed=d9_passed,
                details={"replay_duplicates": replay_duplicates},
            )
        )

        # -------------------------------------------------------------
        # Dimension 10: System Reliability (p95 Latency)
        # -------------------------------------------------------------
        latencies = [s.latency_ms for s in clean_trace.snapshots if s.latency_ms > 0]
        p95_lat = calculate_percentile(latencies, 95.0)
        crashes = sum(1 for s in clean_trace.snapshots if s.error is not None)
        probes = getattr(clean_trace, "probe_results", [])
        probe_fails = sum(1 for p in probes if p.verdict == EvalVerdict.FAIL)
        probe_passes = sum(1 for p in probes if p.verdict == EvalVerdict.PASS)
        d10_passed = p95_lat <= 1500.0 and crashes == 0 and probe_fails == 0

        probe_str = f", Probes: {probe_passes}/{len(probes)} passed" if probes else ""
        dimensions.append(
            DimensionScore(
                dimension_number=10,
                dimension_name="System Reliability",
                primary_metric="Ingestion & Pipeline p95 Latency & Probe Resilience",
                target_bar="≤ 1500ms, 100% Probes PASS",
                hard_gate="No Unhandled Crash / Latency ≤ 1500ms / 0 Probe Fails",
                measured_value=p95_lat,
                display_value=f"p95: {p95_lat:.1f}ms (crashes: {crashes}{probe_str})",
                ci=None,
                passed=d10_passed,
                details={
                    "p95_ms": p95_lat,
                    "crashes": crashes,
                    "latencies_count": len(latencies),
                    "probes_total": len(probes),
                    "probes_passed": probe_passes,
                    "probes_failed": probe_fails,
                },
            )
        )

        # Overall verdict calculation
        all_dims_passed = all(d.passed for d in dimensions)
        if failed_cases > 0 or not all_dims_passed:
            overall_verdict = EvalVerdict.FAIL
        elif pending_cases > 0:
            overall_verdict = EvalVerdict.PENDING
        elif skipped_cases > 0:
            overall_verdict = EvalVerdict.PASS if passed_cases > 0 else EvalVerdict.SKIPPED
        else:
            overall_verdict = EvalVerdict.PASS

        return BenchmarkScorecard(
            dataset_name=dataset.manifest.dataset_name,
            dataset_version=dataset.manifest.version,
            execution_mode=clean_trace.mode,
            generated_at=datetime.now(timezone.utc),
            total_cases=total_cases,
            passed_cases=passed_cases,
            failed_cases=failed_cases,
            pending_cases=pending_cases,
            skipped_cases=skipped_cases,
            pass_rate=pass_rate,
            overall_verdict=overall_verdict,
            dimensions=dimensions,
            case_results=case_results,
        )
