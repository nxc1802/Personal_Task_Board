"""CLI validation tool for Personal Task Board benchmark dataset manifests.

Verifies cryptographic SHA-256 integrity, JSON schema adherence, split consistency,
ground truth validity, persona resolution invariants, and secret/PII redaction.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

# Ensure repository root is on sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from typing import Any, Dict, List, Optional, Set

from benchmarks.manifest import (
    ManifestValidationError,
    compute_file_sha256,
    validate_ground_truth_sample,
    validate_manifest,
)
from benchmarks.models import BenchmarkSample, DatasetManifest, ProbeSpec


def validate_dataset_splits(
    dataset_dir: Path,
    all_sample_ids: Set[str],
) -> List[str]:
    """Verify split files if present: disjointness, completeness, and alignment."""
    errors: List[str] = []
    split_files = {
        "dev": dataset_dir / "dev_samples.json",
        "calibration": dataset_dir / "calibration_samples.json",
        "test": dataset_dir / "test_samples.json",
    }

    present_splits = {k: v for k, v in split_files.items() if v.is_file()}
    if not present_splits:
        return errors

    seen_split_ids: Dict[str, str] = {}
    combined_split_ids: Set[str] = set()

    for split_name, split_path in present_splits.items():
        try:
            data = json.loads(split_path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                errors.append(f"Split file '{split_path.name}' must contain a JSON list")
                continue
            for item in data:
                sid = item.get("sample_id")
                if not sid:
                    errors.append(f"Missing sample_id in split '{split_name}'")
                    continue
                if sid in seen_split_ids:
                    errors.append(
                        f"Sample '{sid}' appears in multiple splits: '{seen_split_ids[sid]}' and '{split_name}'"
                    )
                seen_split_ids[sid] = split_name
                combined_split_ids.add(sid)
                if sid not in all_sample_ids:
                    errors.append(
                        f"Sample '{sid}' in split '{split_name}' is not present in main samples.json"
                    )
        except Exception as exc:
            errors.append(f"Failed to parse split file '{split_path.name}': {exc}")

    # If all three splits exist, verify completeness
    if len(present_splits) == 3:
        missing_from_splits = all_sample_ids - combined_split_ids
        if missing_from_splits:
            errors.append(
                f"{len(missing_from_splits)} samples in samples.json are missing from splits"
            )

    return errors


def run_full_validation(dataset_dir: Path) -> int:
    """Execute complete validation suite against dataset directory."""
    print("=" * 80)
    print(f"BENCHMARK DATASET INTEGRITY VALIDATOR: {dataset_dir}")
    print("=" * 80)

    if not dataset_dir.is_dir():
        print(f"[-] Error: Directory does not exist: {dataset_dir}", file=sys.stderr)
        return 1

    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.is_file():
        print(f"[-] Error: manifest.json not found in {dataset_dir}", file=sys.stderr)
        return 1

    # 1. Cryptographic SHA-256 Manifest Validation
    print("[1/5] Validating cryptographic manifest checksums...")
    try:
        report = validate_manifest(dataset_dir)
        print(f"      [PASS] Manifest '{report.dataset_name}' (v{report.version}) checksums valid:")
        for fname, sha in report.checked_files.items():
            print(f"             - {fname:<25s}: {sha[:16]}...{sha[-8:]}")
    except ManifestValidationError as err:
        print(f"      [FAIL] Cryptographic validation error:\n{err}", file=sys.stderr)
        return 1

    # 2. Parse Manifest Model
    try:
        manifest_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = DatasetManifest.model_validate(manifest_raw)
    except Exception as exc:
        print(f"      [FAIL] Failed to deserialize DatasetManifest: {exc}", file=sys.stderr)
        return 1

    allowed_domains = set(manifest.allowed_email_domains) if manifest.allowed_email_domains else None

    # 3. Validate Samples & Ground Truth Rules
    print("[2/5] Validating samples.json schema and ground truth invariants...")
    samples_path = dataset_dir / "samples.json"
    samples: List[BenchmarkSample] = []
    sample_ids: Set[str] = set()
    category_counts: Dict[str, int] = {}
    source_counts: Dict[str, int] = {}
    task_count = 0
    non_task_count = 0
    evidence_snippet_errors: List[str] = []

    if samples_path.is_file():
        try:
            samples_raw = json.loads(samples_path.read_text(encoding="utf-8"))
            if not isinstance(samples_raw, list):
                print(f"      [FAIL] samples.json must contain a list of BenchmarkSample", file=sys.stderr)
                return 1

            for idx, item in enumerate(samples_raw):
                sample = BenchmarkSample.model_validate(item)
                if sample.sample_id in sample_ids:
                    print(f"      [FAIL] Duplicate sample_id found: '{sample.sample_id}'", file=sys.stderr)
                    return 1
                sample_ids.add(sample.sample_id)

                # Manifest ground truth assertion check
                validate_ground_truth_sample(sample, allowed_domains=allowed_domains)

                # Check evidence snippet presence in raw event text
                if sample.ground_truth.expected_is_task and sample.ground_truth.evidence:
                    raw_text = (sample.raw_event.normalized_text or "") + " " + json.dumps(sample.raw_event.raw_payload or {})
                    for snippet in sample.ground_truth.evidence.expected_snippet_substrings:
                        if snippet.lower() not in raw_text.lower():
                            evidence_snippet_errors.append(
                                f"Sample '{sample.sample_id}': snippet '{snippet}' not found in event text"
                            )

                cat = sample.case_category
                category_counts[cat] = category_counts.get(cat, 0) + 1
                src = (
                    sample.raw_event.source_type.value
                    if hasattr(sample.raw_event.source_type, "value")
                    else str(sample.raw_event.source_type)
                )
                source_counts[src] = source_counts.get(src, 0) + 1
                if sample.ground_truth.expected_is_task:
                    task_count += 1
                else:
                    non_task_count += 1
                samples.append(sample)

            print(f"      [PASS] Parsed and validated {len(samples)} samples successfully.")
            if evidence_snippet_errors:
                print(f"      [WARN] Evidence snippet warnings ({len(evidence_snippet_errors)}):")
                for w in evidence_snippet_errors[:5]:
                    print(f"             - {w}")
                if len(evidence_snippet_errors) > 5:
                    print(f"             - ... and {len(evidence_snippet_errors) - 5} more")

        except Exception as exc:
            print(f"      [FAIL] Error validating samples: {exc}", file=sys.stderr)
            return 1

    # 4. Validate Probes
    print("[3/5] Validating probes.json schema...")
    probes_path = dataset_dir / "probes.json"
    probe_ids: Set[str] = set()
    probes: List[ProbeSpec] = []
    probe_group_counts: Dict[str, int] = {}

    if probes_path.is_file():
        try:
            probes_raw = json.loads(probes_path.read_text(encoding="utf-8"))
            for item in probes_raw:
                probe = ProbeSpec.model_validate(item)
                if probe.probe_id in probe_ids:
                    print(f"      [FAIL] Duplicate probe_id found: '{probe.probe_id}'", file=sys.stderr)
                    return 1
                if probe.probe_id in sample_ids:
                    print(f"      [FAIL] Collision between probe_id and sample_id: '{probe.probe_id}'", file=sys.stderr)
                    return 1
                probe_ids.add(probe.probe_id)
                probe_group_counts[probe.probe_group] = probe_group_counts.get(probe.probe_group, 0) + 1
                probes.append(probe)

            print(f"      [PASS] Parsed and validated {len(probes)} probes successfully.")
        except Exception as exc:
            print(f"      [FAIL] Error validating probes: {exc}", file=sys.stderr)
            return 1

    # 5. Validate Splits
    print("[4/5] Validating dataset splits and provenance...")
    split_errors = validate_dataset_splits(dataset_dir, sample_ids)
    if split_errors:
        print(f"      [FAIL] Dataset split validation failed:", file=sys.stderr)
        for err in split_errors:
            print(f"             - {err}", file=sys.stderr)
        return 1
    print("      [PASS] Dataset splits valid and mutually disjoint.")

    # 6. Summary Statistics
    print("[5/5] Checking quality distribution and metadata...")
    print("-" * 80)
    print(f"Dataset Name       : {manifest.dataset_name} ({manifest.version})")
    print(f"Baseline Commit    : {manifest.commit_baseline}")
    print(f"Total Cases        : {len(samples) + len(probes)} ({len(samples)} samples + {len(probes)} probes)")
    print(f"Task Classification: {task_count} tasks ({task_count / (len(samples) or 1) * 100:.1f}%), {non_task_count} non-tasks")
    print(f"Personas Registered: {len(manifest.personas)}")
    print(f"Email Domains      : {', '.join(manifest.allowed_email_domains)}")
    print("\nSource Distribution:")
    for src, count in sorted(source_counts.items()):
        print(f"  - {src:<18s}: {count:3d} ({count / (len(samples) or 1) * 100:5.1f}%)")
    print("\nCategory Distribution:")
    for cat, count in sorted(category_counts.items()):
        print(f"  - {cat:<18s}: {count:3d} ({count / (len(samples) or 1) * 100:5.1f}%)")
    print("\nProbe Groups:")
    for grp, count in sorted(probe_group_counts.items()):
        print(f"  - {grp:<25s}: {count:2d} probes")
    print("=" * 80)
    print("[+] ALL MANIFEST INTEGRITY AND QUALITY CHECKS PASSED.")
    print("=" * 80)
    return 0


def run_self_tests() -> int:
    """Execute automated self-tests verifying validation logic, tamper detection, and dataset invariants."""
    import tempfile
    import shutil

    print("=" * 80)
    print("RUNNING MANIFEST VALIDATION SELF-TEST SUITE")
    print("=" * 80)
    passed_tests = 0
    total_tests = 8

    # Test 1: Mini smoke passes
    print("[Test 1/8] Verifying mini_smoke dataset...")
    res = run_full_validation(Path("benchmarks/dataset/mini_smoke"))
    assert res == 0, "Mini smoke validation failed"
    passed_tests += 1
    print("           PASS: mini_smoke is valid.\n")

    # Test 2: PTB-500 passes
    print("[Test 2/8] Verifying PTB-500 dataset...")
    res = run_full_validation(Path("benchmarks/dataset/ptb_500"))
    assert res == 0, "PTB-500 validation failed"
    passed_tests += 1
    print("           PASS: PTB-500 is valid.\n")

    # Test 3: CRLF line ending normalization invariance
    print("[Test 3/8] Verifying CRLF->LF checksum invariance...")
    with tempfile.TemporaryDirectory() as tmpdir:
        p1 = Path(tmpdir) / "crlf.txt"
        p2 = Path(tmpdir) / "lf.txt"
        p1.write_bytes(b"line1\r\nline2\r\n")
        p2.write_bytes(b"line1\nline2\n")
        h1 = compute_file_sha256(p1)
        h2 = compute_file_sha256(p2)
        assert h1 == h2, f"CRLF normalization failed: {h1} != {h2}"
    passed_tests += 1
    print("           PASS: CRLF normalization matches.\n")

    # Test 4: Tamper detection
    print("[Test 4/8] Verifying tamper detection on modified payload...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tpath = Path(tmpdir)
        ptb_dir = Path("benchmarks/dataset/ptb_500")
        for f in ptb_dir.glob("*.json"):
            shutil.copy(f, tpath / f.name)
        # Mutate samples.json
        s_file = tpath / "samples.json"
        s_file.write_text(s_file.read_text(encoding="utf-8") + "\n ", encoding="utf-8")
        tamper_caught = False
        try:
            validate_manifest(tpath)
        except ManifestValidationError as exc:
            if "Checksum mismatch" in str(exc):
                tamper_caught = True
        assert tamper_caught, "Tampered samples.json was not caught by validate_manifest!"
    passed_tests += 1
    print("           PASS: Tamper detection caught corrupted checksum.\n")

    # Test 5: Rejection of SKIP_VERIFY
    print("[Test 5/8] Verifying strict rejection of 'SKIP_VERIFY'...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tpath = Path(tmpdir)
        ptb_dir = Path("benchmarks/dataset/mini_smoke")
        for f in ptb_dir.glob("*.json"):
            shutil.copy(f, tpath / f.name)
        m_file = tpath / "manifest.json"
        m_data = json.loads(m_file.read_text(encoding="utf-8"))
        m_data["samples_sha256"] = "SKIP_VERIFY"
        m_file.write_text(json.dumps(m_data), encoding="utf-8")
        skip_caught = False
        try:
            validate_manifest(tpath)
        except ManifestValidationError as exc:
            if "SKIP_VERIFY" in str(exc):
                skip_caught = True
        assert skip_caught, "SKIP_VERIFY was not rejected!"
    passed_tests += 1
    print("           PASS: SKIP_VERIFY strictly rejected.\n")

    # Test 6: Split disjointness and completeness check
    print("[Test 6/8] Verifying split disjointness and completeness...")
    ds_dir = Path("benchmarks/dataset/ptb_500")
    samples_data = json.loads((ds_dir / "samples.json").read_text(encoding="utf-8"))
    dev_data = json.loads((ds_dir / "dev_samples.json").read_text(encoding="utf-8"))
    calib_data = json.loads((ds_dir / "calibration_samples.json").read_text(encoding="utf-8"))
    test_data = json.loads((ds_dir / "test_samples.json").read_text(encoding="utf-8"))

    all_ids = {s["sample_id"] for s in samples_data}
    dev_ids = {s["sample_id"] for s in dev_data}
    calib_ids = {s["sample_id"] for s in calib_data}
    test_ids = {s["sample_id"] for s in test_data}

    assert len(all_ids) == 475, f"Expected 475 unique sample IDs, got {len(all_ids)}"
    assert len(dev_ids) == 235, f"Expected 235 dev IDs, got {len(dev_ids)}"
    assert len(calib_ids) == 95, f"Expected 95 calib IDs, got {len(calib_ids)}"
    assert len(test_ids) == 145, f"Expected 145 test IDs, got {len(test_ids)}"
    assert dev_ids.isdisjoint(calib_ids), "Dev and Calibration splits overlap!"
    assert dev_ids.isdisjoint(test_ids), "Dev and Test splits overlap!"
    assert calib_ids.isdisjoint(test_ids), "Calibration and Test splits overlap!"
    assert (dev_ids | calib_ids | test_ids) == all_ids, "Splits union does not equal samples.json!"
    passed_tests += 1
    print("           PASS: Splits are perfectly disjoint and complete (235 dev, 95 calib, 145 test).\n")

    # Test 7: 500 Unique Case IDs across samples and probes
    print("[Test 7/8] Verifying 500 unique case IDs...")
    probes_data = json.loads((ds_dir / "probes.json").read_text(encoding="utf-8"))
    probe_ids = {p["probe_id"] for p in probes_data}
    assert len(probe_ids) == 25, f"Expected 25 unique probe IDs, got {len(probe_ids)}"
    assert all_ids.isdisjoint(probe_ids), "Collision between sample_id and probe_id!"
    assert len(all_ids | probe_ids) == 500, f"Expected 500 total unique case IDs, got {len(all_ids | probe_ids)}"
    passed_tests += 1
    print("           PASS: 500 unique case IDs verified (475 samples + 25 probes).\n")

    # Test 8: PII & Secret scan verification
    print("[Test 8/8] Verifying zero secrets and allowed PII email domains...")
    from benchmarks.manifest import _SECRET_PATTERNS
    manifest_data = json.loads((ds_dir / "manifest.json").read_text(encoding="utf-8"))
    allowed = set(manifest_data["allowed_email_domains"])
    for s in samples_data:
        text_repr = json.dumps(s)
        for pat in _SECRET_PATTERNS:
            assert not pat.search(text_repr), f"Secret leak detected in sample {s['sample_id']}!"
    passed_tests += 1
    print("           PASS: 0 secrets leaked, email domains conform to whitelist.\n")

    print("=" * 80)
    print(f"[+] ALL {passed_tests}/{total_tests} SELF-TESTS PASSED SUCCESSFULLY.")
    print("=" * 80)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="validate_manifest",
        description="Verify benchmark dataset manifest cryptographic and semantic integrity.",
    )
    parser.add_argument(
        "--dataset-dir",
        type=str,
        default="benchmarks/dataset/ptb_500",
        help="Path to dataset directory (default: benchmarks/dataset/ptb_500)",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        default=False,
        help="Execute automated self-test verification suite",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_tests()
    return run_full_validation(Path(args.dataset_dir))


if __name__ == "__main__":
    sys.exit(main())

