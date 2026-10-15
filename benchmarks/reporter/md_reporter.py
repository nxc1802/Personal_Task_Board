"""Markdown Report Generator for Benchmark Framework."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from benchmarks.models import BenchmarkScorecard, EvalVerdict


class MarkdownReporter:
    """Generates comprehensive human-readable Markdown reports."""

    @classmethod
    def render(
        cls,
        scorecard: BenchmarkScorecard,
        dataset_dir: Optional[str] = None,
    ) -> str:
        verdict_badge = {
            EvalVerdict.PASS: "🟢 **PASS**",
            EvalVerdict.FAIL: "🔴 **FAIL**",
            EvalVerdict.PENDING: "🟡 **PENDING**",
            EvalVerdict.SKIPPED: "⚪ **SKIPPED**",
        }.get(scorecard.overall_verdict, str(scorecard.overall_verdict))

        ds_path = dataset_dir or "benchmarks/dataset/mini_smoke"

        lines = [
            f"# Benchmark Evaluation Report: {scorecard.dataset_name} ({scorecard.dataset_version})",
            "",
            f"- **Execution Mode:** `{scorecard.execution_mode}`",
            f"- **Timestamp:** `{scorecard.generated_at.isoformat()}`",
            f"- **Overall Verdict:** {verdict_badge}",
            f"- **Case Pass Rate:** `{scorecard.passed_cases}/{scorecard.total_cases}` ({scorecard.pass_rate * 100:.1f}%)",
            f"- **Breakdown:** `PASS={scorecard.passed_cases}`, `FAIL={scorecard.failed_cases}`, `PENDING={scorecard.pending_cases}`, `SKIPPED={scorecard.skipped_cases}`",
            "",
            "> [!NOTE]",
            "> In accordance with anti-inflation audit rules, PENDING and SKIPPED cases explicitly reduce",
            "> the effective score denominator and NEVER contribute to PASS counts.",
            "",
            "## 1. Multi-Dimensional Scorecard (10 Dimensions)",
            "",
            "| # | Dimension | Primary Metric | Target Bar | Hard Gate | Measured Value | 95% Confidence Interval | Verdict |",
            "|---|---|---|---|---|---|---|:---:|",
        ]

        for d in scorecard.dimensions:
            status_symbol = "✅ PASS" if d.passed else "❌ FAIL"
            ci_str = f"[{d.ci.lower:.4f}, {d.ci.upper:.4f}]" if d.ci else "N/A"
            lines.append(
                f"| {d.dimension_number} | **{d.dimension_name}** | {d.primary_metric} | {d.target_bar} | {d.hard_gate} | `{d.display_value}` | {ci_str} | {status_symbol} |"
            )

        # Dimension 1 channel breakdown if available
        d1 = next((d for d in scorecard.dimensions if d.dimension_number == 1), None)
        if d1 and d1.details:
            lines.extend([
                "",
                "### Task Extraction Channel Breakdown",
                "",
                "| Channel | TP | FP | FN | TN | Precision | Recall | F1 Score |",
                "|---|---|---|---|---|---|---|---|",
            ])
            for ch, s in d1.details.items():
                lines.append(
                    f"| `{ch}` | {s['TP']} | {s['FP']} | {s['FN']} | {s['TN']} | {s['precision'] * 100:.1f}% | {s['recall'] * 100:.1f}% | **{s['f1'] * 100:.1f}%** |"
                )

        # Per-case failure diagnostics
        lines.extend([
            "",
            "## 2. Per-Case Evaluation Diagnostics",
            "",
            "| Case ID | Category | Source | Verdict | Failure Category | Diagnostic Details | Reproducible Command |",
            "|---|---|---|:---:|---|---|---|",
        ])

        for c in scorecard.case_results:
            v_badge = {
                EvalVerdict.PASS: "✅ PASS",
                EvalVerdict.FAIL: "❌ FAIL",
                EvalVerdict.PENDING: "⏳ PENDING",
                EvalVerdict.SKIPPED: "⏭️ SKIP",
            }.get(c.verdict, str(c.verdict))

            f_cat = c.failure_category.value if c.failure_category else "-"
            details = c.failure_message or ("All assertions passed." if c.verdict == EvalVerdict.PASS else "-")
            # Clean pipes and newlines for markdown table
            details_clean = details.replace("|", "\\|").replace("\n", " ")

            cmd = f"`python -m benchmarks.cli run --dataset-dir {ds_path} --case-id {c.case_id}`"
            lines.append(
                f"| `{c.case_id}` | `{c.case_category}` | `{c.source_type}` | {v_badge} | `{f_cat}` | {details_clean} | {cmd} |"
            )

        lines.append("")
        return "\n".join(lines)

    @classmethod
    def write(
        cls,
        scorecard: BenchmarkScorecard,
        output_path: Union[str, Path],
        dataset_dir: Optional[str] = None,
    ) -> Path:
        out_file = Path(output_path)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(cls.render(scorecard, dataset_dir=dataset_dir), encoding="utf-8")
        return out_file
