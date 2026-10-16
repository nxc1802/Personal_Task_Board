"""Command-line interface (CLI) for Personal Task Board Benchmark Framework."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from typing import List, Optional

# Ensure standard output can handle utf-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Load environment configuration from .env if present
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
load_dotenv()

from benchmarks.evaluator.scorecard import BenchmarkMetricEvaluator
from benchmarks.manifest import ManifestValidationError, load_dataset, validate_manifest
from benchmarks.models import BenchmarkScorecard, EvalVerdict
from benchmarks.reporter.reporter import BenchmarkReporter
from benchmarks.runner.environment import EnvironmentManager
from benchmarks.runner.runner import ReplayRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchmarks",
        description="Independent Benchmark Framework and Metric Evaluator for Personal Task Board",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # 1. 'validate' subcommand
    val_parser = subparsers.add_parser("validate", help="Validate dataset manifest and SHA-256 checksums")
    val_parser.add_argument(
        "--dataset-dir",
        required=True,
        type=str,
        help="Path to dataset directory containing manifest.json",
    )

    # 2. 'run' subcommand
    run_parser = subparsers.add_parser("run", help="Execute benchmark dataset against production pipelines")
    run_parser.add_argument(
        "--dataset-dir",
        required=True,
        type=str,
        help="Path to dataset directory containing manifest.json",
    )
    run_parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Execute in live infrastructure mode (real Neo4j and LLM). Marks PENDING if unavailable.",
    )
    run_parser.add_argument(
        "--mode",
        choices=["fixture", "live"],
        default=None,
        help="Explicit execution mode (fixture or live). Overrides --live if specified.",
    )
    run_parser.add_argument(
        "--output-report",
        type=str,
        default=None,
        help="File path to write generated report (e.g. docs/agent_reports/W1_live_benchmark.md)",
    )
    run_parser.add_argument(
        "--format",
        choices=["json", "md", "html"],
        default="md",
        help="Format for generated report (default: md)",
    )
    run_parser.add_argument(
        "--case-id",
        type=str,
        default=None,
        help="Execute single test case by sample_id for targeted debugging",
    )
    run_parser.add_argument(
        "--no-replay",
        action="store_true",
        default=False,
        help="Skip replay idempotency verification step",
    )
    run_parser.add_argument(
        "--cold-cache",
        action="store_true",
        default=False,
        help="Execute benchmark in cold-cache mode with an empty isolated cache to verify live extractions without fixture reuse (DEV-P1-01)",
    )

    # 3. 'report' subcommand
    rep_parser = subparsers.add_parser("report", help="Generate or convert benchmark reports from previous run results")
    rep_parser.add_argument(
        "--results-dir",
        required=True,
        type=str,
        help="Directory containing benchmark_report.json",
    )
    rep_parser.add_argument(
        "--format",
        choices=["json", "md", "html"],
        default="md",
        help="Target report format (default: md)",
    )
    rep_parser.add_argument(
        "--output-file",
        type=str,
        default=None,
        help="Optional path to output file. Defaults to results-dir/benchmark_report.<format>",
    )

    return parser


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate dataset manifest cryptographic integrity."""
    dataset_dir = Path(args.dataset_dir)
    print(f"[*] Validating manifest in: {dataset_dir}")
    try:
        report = validate_manifest(dataset_dir)
        print(f"[+] Manifest VALID: {report.dataset_name} ({report.version})")
        print("    Verified SHA-256 files:")
        for fname, sha in report.checked_files.items():
            print(f"    - {fname}: {sha[:16]}...{sha[-8:]}")
        return 0
    except ManifestValidationError as err:
        print(f"[-] Manifest VALIDATION FAILED:\n{err}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[-] Unexpected validation error: {exc}", file=sys.stderr)
        return 1


async def _run_benchmark_async(args: argparse.Namespace) -> int:
    dataset_dir = Path(args.dataset_dir)
    mode = args.mode or ("live" if args.live else "fixture")

    print(f"[*] Starting benchmark execution:")
    print(f"    - Dataset:    {dataset_dir}")
    print(f"    - Mode:       {mode}")
    print(f"    - Cold Cache: {'YES (Isolated, zero fixture reuse)' if args.cold_cache else 'NO (Warm cache enabled)'}")
    if args.case_id:
        print(f"    - Filter:     Case ID '{args.case_id}'")

    # 1. Validate and load dataset
    try:
        dataset = load_dataset(dataset_dir)
        print(f"[+] Dataset loaded: {dataset.manifest.dataset_name} ({len(dataset.samples)} samples, {len(dataset.probes)} probes)")
    except Exception as err:
        print(f"[-] Failed to load dataset: {err}", file=sys.stderr)
        return 1

    # 2. Setup isolated environment
    env = EnvironmentManager.create_environment(dataset=dataset, mode=mode, cold_cache=args.cold_cache)
    if mode == "live" and not env.live_infrastructure_available:
        print(f"[!] Warning: Live infrastructure unavailable: {env.infrastructure_error}. Cases will be marked PENDING.")

    # 3. Execute replay runner
    runner = ReplayRunner(env)
    print("[*] Running clean ingestion pass...")
    clean_trace = await runner.run_clean(dataset, filter_case_id=args.case_id)

    replay_trace = None
    if not args.no_replay and not args.case_id:
        print("[*] Running replay idempotency pass...")
        replay_trace = await runner.run_replay(dataset, clean_trace)

    # 4. Evaluate metrics and scorecard
    print("[*] Evaluating 10-dimension scorecard...")
    scorecard = BenchmarkMetricEvaluator.evaluate(
        dataset=dataset,
        clean_trace=clean_trace,
        replay_trace=replay_trace,
        filter_case_id=args.case_id,
    )

    # 5. Output report if requested
    if args.output_report:
        out_path = Path(args.output_report)
        BenchmarkReporter.write_report(
            scorecard=scorecard,
            output_path=out_path,
            report_format=args.format,
            dataset_dir=str(dataset_dir),
        )
        print(f"[+] Report written to: {out_path}")

    # 6. Print summary scorecard to terminal
    print("\n" + "=" * 80)
    print(f"BENCHMARK SCORECARD: {scorecard.dataset_name} ({scorecard.dataset_version})")
    print("=" * 80)
    print(f"Overall Verdict : {scorecard.overall_verdict.value}")
    print(f"Total Cases     : {scorecard.total_cases}")
    print(f"Passed Cases    : {scorecard.passed_cases} ({scorecard.pass_rate * 100:.1f}%)")
    print(f"Failed Cases    : {scorecard.failed_cases}")
    print(f"Pending Cases   : {scorecard.pending_cases}")
    print(f"Skipped Cases   : {scorecard.skipped_cases}")
    print("-" * 80)
    for d in scorecard.dimensions:
        status_str = "PASS" if d.passed else "FAIL"
        ci_str = f" [{d.ci.lower:.2f}, {d.ci.upper:.2f}]" if d.ci else ""
        print(f"[{status_str:4s}] Dim {d.dimension_number:2d}: {d.dimension_name:<30s} = {d.display_value}{ci_str}")

    if hasattr(env.llm_extractor, "get_stats"):
        c_stats = env.llm_extractor.get_stats()
        print("-" * 80)
        print("EXTRACTION EXECUTION & CACHE AUDIT:")
        print(f"  Cold Cache Mode : {'YES' if c_stats.get('cold_cache') else 'NO'}")
        print(f"  Total Calls     : {c_stats.get('call_count', 0)}")
        print(f"  Cache Hits      : {c_stats.get('cache_hits', 0)}")
        print(f"  Cache Misses    : {c_stats.get('cache_misses', 0)}")
        print(f"  Live Calls      : {c_stats.get('live_calls', 0)}")
        print(f"  Hit Rate        : {c_stats.get('hit_rate', 0.0) * 100:.1f}%")
    print("=" * 80 + "\n")

    if scorecard.failed_cases > 0:
        return 1
    if scorecard.pending_cases > 0:
        return 2
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    return asyncio.run(_run_benchmark_async(args))


def cmd_report(args: argparse.Namespace) -> int:
    """Generate report from an existing benchmark_report.json."""
    results_dir = Path(args.results_dir)
    json_path = results_dir / "benchmark_report.json"
    if not json_path.is_file():
        if results_dir.is_file() and results_dir.suffix == ".json":
            json_path = results_dir
        else:
            print(f"[-] benchmark_report.json not found in {results_dir}", file=sys.stderr)
            return 1

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        scorecard = BenchmarkScorecard.model_validate(data)
    except Exception as exc:
        print(f"[-] Failed to parse benchmark results JSON: {exc}", file=sys.stderr)
        return 1

    fmt = args.format
    if args.output_file:
        out_path = Path(args.output_file)
    else:
        out_path = results_dir / f"benchmark_report.{fmt}"

    BenchmarkReporter.write_report(
        scorecard=scorecard,
        output_path=out_path,
        report_format=fmt,
    )
    print(f"[+] Generated {fmt.upper()} report at: {out_path}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "validate":
        return cmd_validate(args)
    elif args.subcommand == "run":
        return cmd_run(args)
    elif args.subcommand == "report":
        return cmd_report(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
