"""Tests for manifest validation, cryptographic hashing, and dataset loading."""

import json
from pathlib import Path
import pytest

from benchmarks.manifest import (
    ManifestValidationError,
    compute_file_sha256,
    generate_manifest_checksums,
    load_dataset,
    validate_manifest,
)


def test_compute_file_sha256_normalizes_line_endings(tmp_path: Path):
    crlf_file = tmp_path / "crlf.txt"
    lf_file = tmp_path / "lf.txt"

    crlf_file.write_bytes(b"hello\r\nworld\r\n")
    lf_file.write_bytes(b"hello\nworld\n")

    hash_crlf = compute_file_sha256(crlf_file)
    hash_lf = compute_file_sha256(lf_file)

    assert hash_crlf == hash_lf
    assert len(hash_crlf) == 64


def test_validate_manifest_mini_smoke_passes():
    ds_dir = Path("benchmarks/dataset/mini_smoke")
    report = validate_manifest(ds_dir)
    assert report.valid is True
    assert report.dataset_name == "PTB-MINI-SMOKE"
    assert "samples.json" in report.checked_files
    assert "probes.json" in report.checked_files


def test_validate_manifest_detects_tampered_file(tmp_path: Path):
    ds_dir = Path("benchmarks/dataset/mini_smoke")
    manifest = json.loads((ds_dir / "manifest.json").read_text(encoding="utf-8"))

    # Copy files to tmp_path
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "samples.json").write_text((ds_dir / "samples.json").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "probes.json").write_text((ds_dir / "probes.json").read_text(encoding="utf-8"), encoding="utf-8")

    # Tamper samples.json by adding a whitespace
    samples_text = (tmp_path / "samples.json").read_text(encoding="utf-8")
    (tmp_path / "samples.json").write_text(samples_text + " ", encoding="utf-8")

    with pytest.raises(ManifestValidationError) as exc_info:
        validate_manifest(tmp_path)
    assert "Checksum mismatch for" in str(exc_info.value)


def test_validate_manifest_detects_missing_file(tmp_path: Path):
    ds_dir = Path("benchmarks/dataset/mini_smoke")
    manifest = json.loads((ds_dir / "manifest.json").read_text(encoding="utf-8"))

    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    # Don't copy samples.json

    with pytest.raises(ManifestValidationError) as exc_info:
        validate_manifest(tmp_path)
    assert "missing" in str(exc_info.value).lower() or "not exist" in str(exc_info.value).lower()


def test_validate_manifest_strictly_rejects_skip_verify(tmp_path: Path):
    ds_dir = Path("benchmarks/dataset/mini_smoke")
    manifest = json.loads((ds_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["samples_sha256"] = "SKIP_VERIFY"

    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "samples.json").write_text((ds_dir / "samples.json").read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(ManifestValidationError) as exc_info:
        validate_manifest(tmp_path)
    assert "SKIP_VERIFY" in str(exc_info.value)


def test_load_dataset_loads_all_samples_and_probes():
    ds_dir = Path("benchmarks/dataset/mini_smoke")
    dataset = load_dataset(ds_dir)
    assert dataset.manifest.dataset_name == "PTB-MINI-SMOKE"
    assert len(dataset.samples) == 12
    assert len(dataset.probes) == 2
    assert any(s.sample_id == "smoke-01-teams-clear-deadline" for s in dataset.samples)
    assert any(p.probe_id == "probe-smoke-01-missing-timestamp" for p in dataset.probes)


def test_validate_manifest_independent_heldout_passes():
    ds_dir = Path("benchmarks/dataset/independent_heldout")
    report = validate_manifest(ds_dir)
    assert report.valid is True
    assert report.dataset_name == "PTB-INDEPENDENT-HELDOUT-30"
    assert "samples.json" in report.checked_files
    assert "probes.json" in report.checked_files
