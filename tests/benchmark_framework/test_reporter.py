"""Unit tests for BenchmarkReporter (JSON, Markdown, HTML)."""

import json
from pathlib import Path
import pytest

from benchmarks.evaluator.scorecard import BenchmarkMetricEvaluator
from benchmarks.manifest import load_dataset
from benchmarks.models import BenchmarkScorecard, ConfidenceInterval, DimensionScore, EvalVerdict
from benchmarks.reporter.reporter import BenchmarkReporter
from benchmarks.runner.runner import RunTrace


@pytest.fixture
def mock_scorecard() -> BenchmarkScorecard:
    dims = [
        DimensionScore(
            dimension_number=1,
            dimension_name="Task Extraction",
            primary_metric="Macro-F1",
            target_bar=">= 92.0%",
            hard_gate="F1 >= 88.0%",
            measured_value=0.95,
            display_value="95.0%",
            ci=ConfidenceInterval(lower=0.88, upper=0.98),
            passed=True,
        ),
        DimensionScore(
            dimension_number=2,
            dimension_name="Noise & Negation Rejection",
            primary_metric="Noise Rejection Rate",
            target_bar=">= 85.0%",
            hard_gate="FPR <= 10.0%",
            measured_value=1.0,
            display_value="100.0%",
            ci=ConfidenceInterval(lower=0.85, upper=1.0),
            passed=True,
        ),
    ]
    return BenchmarkScorecard(
        dataset_name="TEST-DATASET",
        dataset_version="1.0.0",
        execution_mode="fixture",
        total_cases=10,
        passed_cases=10,
        failed_cases=0,
        pending_cases=0,
        skipped_cases=0,
        pass_rate=1.0,
        overall_verdict=EvalVerdict.PASS,
        dimensions=dims,
        case_results=[],
    )


def test_json_reporter(mock_scorecard: BenchmarkScorecard, tmp_path: Path):
    rendered = BenchmarkReporter.render(mock_scorecard, report_format="json")
    parsed = json.loads(rendered)
    assert parsed["dataset_name"] == "TEST-DATASET"
    assert parsed["pass_rate"] == 1.0

    out_file = tmp_path / "report.json"
    BenchmarkReporter.write_report(mock_scorecard, out_file)
    assert out_file.is_file()
    assert json.loads(out_file.read_text(encoding="utf-8"))["overall_verdict"] == "PASS"


def test_markdown_reporter(mock_scorecard: BenchmarkScorecard, tmp_path: Path):
    rendered = BenchmarkReporter.render(mock_scorecard, report_format="md")
    assert "# Benchmark Evaluation Report: TEST-DATASET" in rendered
    assert "Task Extraction" in rendered
    assert ">= 92.0%" in rendered
    assert "F1 >= 88.0%" in rendered
    assert "0.8800, 0.9800" in rendered

    out_file = tmp_path / "report.md"
    BenchmarkReporter.write_report(mock_scorecard, out_file)
    assert out_file.is_file()
    assert "Multi-Dimensional Scorecard" in out_file.read_text(encoding="utf-8")


def test_html_reporter(mock_scorecard: BenchmarkScorecard, tmp_path: Path):
    rendered = BenchmarkReporter.render(mock_scorecard, report_format="html")
    assert "<!DOCTYPE html>" in rendered
    assert "PTB Benchmark Report" in rendered
    assert "badge-pass" in rendered

    out_file = tmp_path / "report.html"
    BenchmarkReporter.write_report(mock_scorecard, out_file)
    assert out_file.is_file()
    assert "</html>" in out_file.read_text(encoding="utf-8")


def test_write_all_generates_three_formats(mock_scorecard: BenchmarkScorecard, tmp_path: Path):
    files = BenchmarkReporter.write_all(mock_scorecard, tmp_path, base_name="test_report")
    assert files["json"].is_file()
    assert files["md"].is_file()
    assert files["html"].is_file()
