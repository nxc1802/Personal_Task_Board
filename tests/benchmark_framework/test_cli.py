"""Unit tests for the Benchmark Framework CLI."""

from pathlib import Path
import pytest

from benchmarks.cli import build_parser, main


def test_cli_parser_subcommands():
    parser = build_parser()

    # validate subcommand
    args = parser.parse_args(["validate", "--dataset-dir", "benchmarks/dataset/mini_smoke"])
    assert args.subcommand == "validate"
    assert args.dataset_dir == "benchmarks/dataset/mini_smoke"

    # run subcommand
    args = parser.parse_args(["run", "--dataset-dir", "benchmarks/dataset/mini_smoke", "--live", "--output-report", "out.md"])
    assert args.subcommand == "run"
    assert args.live is True
    assert args.output_report == "out.md"

    # report subcommand
    args = parser.parse_args(["report", "--results-dir", "reports", "--format", "html"])
    assert args.subcommand == "report"
    assert args.results_dir == "reports"
    assert args.format == "html"


def test_cli_validate_success():
    rc = main(["validate", "--dataset-dir", "benchmarks/dataset/mini_smoke"])
    assert rc == 0


def test_cli_validate_nonexistent_dir():
    rc = main(["validate", "--dataset-dir", "nonexistent/dir"])
    assert rc == 1


def test_cli_report_generation(tmp_path: Path):
    from benchmarks.models import BenchmarkScorecard, EvalVerdict
    from benchmarks.reporter.reporter import BenchmarkReporter

    scorecard = BenchmarkScorecard(
        dataset_name="CLI-TEST",
        dataset_version="1.0.0",
        execution_mode="fixture",
        total_cases=1,
        passed_cases=1,
        failed_cases=0,
        pending_cases=0,
        skipped_cases=0,
        pass_rate=1.0,
        overall_verdict=EvalVerdict.PASS,
        dimensions=[],
        case_results=[],
    )
    json_path = tmp_path / "benchmark_report.json"
    BenchmarkReporter.write_report(scorecard, json_path, report_format="json")

    out_md = tmp_path / "output_cli.md"
    rc = main(["report", "--results-dir", str(tmp_path), "--format", "md", "--output-file", str(out_md)])
    assert rc == 0
    assert out_md.is_file()
    assert "CLI-TEST" in out_md.read_text(encoding="utf-8")
