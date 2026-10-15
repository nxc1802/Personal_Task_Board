"""Unified Benchmark Reporter orchestrating JSON, Markdown, and HTML rendering."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Union

from benchmarks.models import BenchmarkScorecard
from benchmarks.reporter.html_reporter import HtmlReporter
from benchmarks.reporter.json_reporter import JsonReporter
from benchmarks.reporter.md_reporter import MarkdownReporter


class BenchmarkReporter:
    """Unified report generator supporting json, md, and html formats."""

    @classmethod
    def render(
        cls,
        scorecard: BenchmarkScorecard,
        report_format: str = "md",
        dataset_dir: Optional[str] = None,
    ) -> str:
        fmt = report_format.lower().strip()
        if fmt == "json":
            return JsonReporter.render(scorecard)
        elif fmt in ("md", "markdown"):
            return MarkdownReporter.render(scorecard, dataset_dir=dataset_dir)
        elif fmt == "html":
            return HtmlReporter.render(scorecard, dataset_dir=dataset_dir)
        else:
            raise ValueError(f"Unsupported report format '{report_format}'. Supported: json, md, html")

    @classmethod
    def write_report(
        cls,
        scorecard: BenchmarkScorecard,
        output_path: Union[str, Path],
        report_format: Optional[str] = None,
        dataset_dir: Optional[str] = None,
    ) -> Path:
        out_file = Path(output_path)
        fmt = report_format or out_file.suffix.lstrip(".") or "md"
        fmt = fmt.lower().strip()

        if fmt == "json":
            return JsonReporter.write(scorecard, out_file)
        elif fmt in ("md", "markdown"):
            return MarkdownReporter.write(scorecard, out_file, dataset_dir=dataset_dir)
        elif fmt == "html":
            return HtmlReporter.write(scorecard, out_file, dataset_dir=dataset_dir)
        else:
            raise ValueError(f"Unsupported report format '{fmt}'. Supported: json, md, html")

    @classmethod
    def write_all(
        cls,
        scorecard: BenchmarkScorecard,
        output_dir: Union[str, Path],
        dataset_dir: Optional[str] = None,
        base_name: str = "benchmark_report",
    ) -> Dict[str, Path]:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        return {
            "json": JsonReporter.write(scorecard, out_dir / f"{base_name}.json"),
            "md": MarkdownReporter.write(scorecard, out_dir / f"{base_name}.md", dataset_dir=dataset_dir),
            "html": HtmlReporter.write(scorecard, out_dir / f"{base_name}.html", dataset_dir=dataset_dir),
        }
