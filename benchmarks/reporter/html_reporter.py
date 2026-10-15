"""HTML Report Generator for Benchmark Framework."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Optional, Union

from benchmarks.models import BenchmarkScorecard, EvalVerdict


class HtmlReporter:
    """Generates styled standalone HTML reports."""

    @classmethod
    def render(
        cls,
        scorecard: BenchmarkScorecard,
        dataset_dir: Optional[str] = None,
    ) -> str:
        verdict_color = {
            EvalVerdict.PASS: "#10b981",
            EvalVerdict.FAIL: "#ef4444",
            EvalVerdict.PENDING: "#f59e0b",
            EvalVerdict.SKIPPED: "#6b7280",
        }.get(scorecard.overall_verdict, "#3b82f6")

        ds_path = dataset_dir or "benchmarks/dataset/mini_smoke"

        scorecard_rows = ""
        for d in scorecard.dimensions:
            badge_class = "badge-pass" if d.passed else "badge-fail"
            badge_text = "PASS" if d.passed else "FAIL"
            ci_text = f"[{d.ci.lower:.4f}, {d.ci.upper:.4f}]" if d.ci else "N/A"
            scorecard_rows += f"""
            <tr>
                <td style="text-align: center; font-weight: bold;">{d.dimension_number}</td>
                <td><strong>{html.escape(d.dimension_name)}</strong></td>
                <td>{html.escape(d.primary_metric)}</td>
                <td><code>{html.escape(d.target_bar)}</code></td>
                <td><code>{html.escape(d.hard_gate)}</code></td>
                <td><code>{html.escape(d.display_value)}</code></td>
                <td><small>{ci_text}</small></td>
                <td style="text-align: center;"><span class="badge {badge_class}">{badge_text}</span></td>
            </tr>
            """

        case_rows = ""
        for c in scorecard.case_results:
            c_badge_class = {
                EvalVerdict.PASS: "badge-pass",
                EvalVerdict.FAIL: "badge-fail",
                EvalVerdict.PENDING: "badge-pending",
                EvalVerdict.SKIPPED: "badge-skip",
            }.get(c.verdict, "badge-skip")

            f_cat = html.escape(c.failure_category.value if c.failure_category else "-")
            details = html.escape(c.failure_message or ("All assertions passed." if c.verdict == EvalVerdict.PASS else "-"))
            cmd = f"python -m benchmarks.cli run --dataset-dir {ds_path} --case-id {c.case_id}"

            case_rows += f"""
            <tr>
                <td><code>{html.escape(c.case_id)}</code></td>
                <td>{html.escape(c.case_category)}</td>
                <td><code>{html.escape(c.source_type)}</code></td>
                <td style="text-align: center;"><span class="badge {c_badge_class}">{c.verdict.value}</span></td>
                <td><code>{f_cat}</code></td>
                <td>{details}</td>
                <td><pre><code>{html.escape(cmd)}</code></pre></td>
            </tr>
            """

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PTB Benchmark Report — {html.escape(scorecard.dataset_name)}</title>
    <style>
        :root {{
            --bg: #0f172a;
            --surface: #1e293b;
            --text: #f8fafc;
            --muted: #94a3b8;
            --border: #334155;
            --pass: #10b981;
            --fail: #ef4444;
            --pending: #f59e0b;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg);
            color: var(--text);
            margin: 0;
            padding: 24px;
            line-height: 1.5;
        }}
        .container {{
            max-width: 1280px;
            margin: 0 auto;
        }}
        .header {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 24px;
            margin-bottom: 24px;
        }}
        .header h1 {{
            margin: 0 0 12px 0;
            font-size: 24px;
        }}
        .meta-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-top: 16px;
        }}
        .meta-card {{
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 12px;
        }}
        .meta-label {{
            font-size: 12px;
            color: var(--muted);
            text-transform: uppercase;
        }}
        .meta-val {{
            font-size: 18px;
            font-weight: bold;
            margin-top: 4px;
        }}
        .badge {{
            display: inline-block;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: bold;
            text-transform: uppercase;
        }}
        .badge-pass {{ background: rgba(16, 185, 129, 0.2); color: var(--pass); border: 1px solid var(--pass); }}
        .badge-fail {{ background: rgba(239, 68, 68, 0.2); color: var(--fail); border: 1px solid var(--fail); }}
        .badge-pending {{ background: rgba(245, 158, 11, 0.2); color: var(--pending); border: 1px solid var(--pending); }}
        .badge-skip {{ background: rgba(107, 114, 128, 0.2); color: var(--muted); border: 1px solid var(--muted); }}
        table {{
            width: 100%;
            border-collapse: collapse;
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 8px;
            overflow: hidden;
            margin-bottom: 32px;
        }}
        th, td {{
            padding: 12px 16px;
            border-bottom: 1px solid var(--border);
            text-align: left;
            font-size: 14px;
        }}
        th {{
            background: rgba(0, 0, 0, 0.2);
            color: var(--muted);
            font-weight: 600;
        }}
        code {{
            background: rgba(0, 0, 0, 0.3);
            padding: 2px 6px;
            border-radius: 4px;
            font-family: Consolas, monospace;
            font-size: 13px;
        }}
        pre {{
            margin: 0;
            background: rgba(0, 0, 0, 0.4);
            padding: 6px;
            border-radius: 4px;
            overflow-x: auto;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>PTB Benchmark Evaluation Report: {html.escape(scorecard.dataset_name)} ({html.escape(scorecard.dataset_version)})</h1>
            <div class="meta-grid">
                <div class="meta-card">
                    <div class="meta-label">Overall Verdict</div>
                    <div class="meta-val" style="color: {verdict_color};">{scorecard.overall_verdict.value}</div>
                </div>
                <div class="meta-card">
                    <div class="meta-label">Case Pass Rate</div>
                    <div class="meta-val">{scorecard.passed_cases}/{scorecard.total_cases} ({scorecard.pass_rate * 100:.1f}%)</div>
                </div>
                <div class="meta-card">
                    <div class="meta-label">Execution Mode</div>
                    <div class="meta-val"><code>{html.escape(scorecard.execution_mode)}</code></div>
                </div>
                <div class="meta-card">
                    <div class="meta-label">Generated At</div>
                    <div class="meta-val" style="font-size: 14px;">{scorecard.generated_at.strftime('%Y-%m-%d %H:%M:%S UTC')}</div>
                </div>
            </div>
        </div>

        <h2>1. Multi-Dimensional Scorecard (10 Dimensions)</h2>
        <table>
            <thead>
                <tr>
                    <th style="text-align: center;">#</th>
                    <th>Dimension</th>
                    <th>Primary Metric</th>
                    <th>Target Bar</th>
                    <th>Hard Gate</th>
                    <th>Measured Value</th>
                    <th>95% Confidence Interval</th>
                    <th style="text-align: center;">Verdict</th>
                </tr>
            </thead>
            <tbody>
                {scorecard_rows}
            </tbody>
        </table>

        <h2>2. Per-Case Evaluation Diagnostics</h2>
        <table>
            <thead>
                <tr>
                    <th>Case ID</th>
                    <th>Category</th>
                    <th>Source</th>
                    <th style="text-align: center;">Verdict</th>
                    <th>Failure Category</th>
                    <th>Diagnostic Details</th>
                    <th>Reproducible Command</th>
                </tr>
            </thead>
            <tbody>
                {case_rows}
            </tbody>
        </table>
    </div>
</body>
</html>
"""

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
