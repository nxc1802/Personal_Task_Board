"""Cryptographic SHA-256 manifest validation and dataset loading for Benchmark Framework.

Enforces strict integrity checks:
- Rejects missing manifest files.
- Rejects checksum mismatches between physical files and manifest entries.
- Rejects 'SKIP_VERIFY' bypass attempts unconditionally.
- Enforces cross-platform line-ending normalization (CRLF -> LF) during hashing.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Union
from pydantic import BaseModel, Field

from benchmarks.models import (
    BenchmarkDataset,
    BenchmarkSample,
    ClarityLabel,
    ControlType,
    DatasetManifest,
    FileChecksum,
    ProbeSpec,
)


class ManifestValidationError(RuntimeError):
    """Raised when manifest validation fails or checksum mismatch is detected."""


class ManifestValidationReport(BaseModel):
    """Summary of manifest validation execution."""
    valid: bool
    dataset_name: str
    version: str
    checked_files: Dict[str, str] = Field(default_factory=dict)
    errors: List[str] = Field(default_factory=list)


_SECRET_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+", re.IGNORECASE),
    re.compile(r"eyJ[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}"),
    re.compile(r"(?:api_key|client_secret|access_token|refresh_token)\s*[\"']?\s*[:=]\s*[\"'][A-Za-z0-9\-_]{12,}", re.IGNORECASE),
]

_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def compute_file_sha256(path: Union[str, Path]) -> str:
    """Compute SHA-256 checksum with CRLF->LF normalization for cross-platform stability."""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"File not found for checksum computation: {file_path}")
    raw_bytes = file_path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(raw_bytes).hexdigest().lower()


def validate_manifest(dataset_dir: Union[str, Path]) -> ManifestValidationReport:
    """Validate manifest.json in the dataset directory against physical files.

    Raises:
        ManifestValidationError if manifest is missing, corrupt, uses SKIP_VERIFY,
        or has mismatched hashes.
    """
    dir_path = Path(dataset_dir)
    if not dir_path.is_dir():
        raise ManifestValidationError(f"Dataset directory does not exist: {dir_path}")

    manifest_file = dir_path / "manifest.json"
    if not manifest_file.is_file():
        raise ManifestValidationError(f"manifest.json not found in dataset directory: {dir_path}")

    try:
        manifest_raw = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest = DatasetManifest.model_validate(manifest_raw)
    except Exception as exc:
        raise ManifestValidationError(f"Failed to parse manifest.json: {exc}") from exc

    # Strictly disallow SKIP_VERIFY anywhere
    if manifest.samples_sha256 == "SKIP_VERIFY" or manifest.probes_sha256 == "SKIP_VERIFY":
        raise ManifestValidationError(
            "Security violation: 'SKIP_VERIFY' is strictly prohibited in benchmark manifests."
        )

    for fname, fcheck in manifest.dataset_files.items():
        if fcheck.sha256 == "SKIP_VERIFY":
            raise ManifestValidationError(
                f"Security violation: 'SKIP_VERIFY' found for file '{fname}' in manifest."
            )

    checked_files: Dict[str, str] = {}
    errors: List[str] = []

    # 1. Check dataset_files dictionary if defined
    if manifest.dataset_files:
        for rel_name, checksum_spec in manifest.dataset_files.items():
            target_path = dir_path / rel_name
            if not target_path.is_file():
                errors.append(f"Referenced dataset file missing: {rel_name}")
                continue
            actual_sha = compute_file_sha256(target_path)
            expected_sha = checksum_spec.sha256.lower().strip()
            if actual_sha != expected_sha:
                errors.append(
                    f"Checksum mismatch for '{rel_name}': expected {expected_sha}, got {actual_sha}"
                )
            checked_files[rel_name] = actual_sha

    # 2. Check top-level samples_sha256 if present
    samples_path = dir_path / "samples.json"
    if manifest.samples_sha256:
        if not samples_path.is_file():
            errors.append("samples.json declared in manifest but file does not exist on disk")
        else:
            actual_sha = compute_file_sha256(samples_path)
            expected_sha = manifest.samples_sha256.lower().strip()
            if actual_sha != expected_sha:
                errors.append(
                    f"Checksum mismatch for 'samples.json': expected {expected_sha}, got {actual_sha}"
                )
            checked_files["samples.json"] = actual_sha

    # 3. Check top-level probes_sha256 if present
    probes_path = dir_path / "probes.json"
    if manifest.probes_sha256:
        if not probes_path.is_file():
            errors.append("probes.json declared in manifest but file does not exist on disk")
        else:
            actual_sha = compute_file_sha256(probes_path)
            expected_sha = manifest.probes_sha256.lower().strip()
            if actual_sha != expected_sha:
                errors.append(
                    f"Checksum mismatch for 'probes.json': expected {expected_sha}, got {actual_sha}"
                )
            checked_files["probes.json"] = actual_sha

    if not checked_files:
        errors.append("Manifest contains no dataset file checksum entries to verify.")

    if errors:
        raise ManifestValidationError(
            f"Manifest validation failed for {manifest.dataset_name} ({manifest.version}):\n- "
            + "\n- ".join(errors)
        )

    return ManifestValidationReport(
        valid=True,
        dataset_name=manifest.dataset_name,
        version=manifest.version,
        checked_files=checked_files,
        errors=[],
    )


def validate_ground_truth_sample(sample: BenchmarkSample, allowed_domains: Optional[Set[str]] = None) -> None:
    """Validate sample consistency, preventing contradictory ground truth definitions and leaks."""
    gt = sample.ground_truth
    ev = sample.raw_event

    # 1. Contradictory ground-truth assertions
    if not gt.expected_is_task:
        if (
            gt.expected_owner_id is not None
            or gt.expected_owner_name is not None
            or gt.expected_requester_id is not None
            or gt.expected_status is not None
            or gt.expected_due_date is not None
            or gt.correlation.expected_merge
            or gt.correlation.expected_cluster_id is not None
        ):
            raise ManifestValidationError(
                f"Sample {sample.sample_id} ground truth invalid: expected_is_task=False but task-only fields are set."
            )

    if gt.control_type in (ControlType.DECISION, ControlType.LESSON) and gt.expected_is_task:
        raise ManifestValidationError(
            f"Sample {sample.sample_id} ground truth invalid: control_type={gt.control_type.value} cannot be task."
        )

    if gt.expected_is_task:
        if gt.evidence is not None and gt.evidence.source_event_id != ev.id:
            raise ManifestValidationError(
                f"Sample {sample.sample_id} evidence source mismatch: {gt.evidence.source_event_id} != {ev.id}"
            )
        if gt.clarity == ClarityLabel.AMBIGUOUS and gt.expected_review_status not in ("pending_review", "ignore", None):
            raise ManifestValidationError(
                f"Sample {sample.sample_id} ambiguous sample cannot have review_status={gt.expected_review_status}"
            )

    # 2. Secret leakage check in event payload
    serialized = json.dumps(ev.model_dump(mode="json"), ensure_ascii=False)
    for pat in _SECRET_PATTERNS:
        if pat.search(serialized):
            raise ManifestValidationError(
                f"Sample {sample.sample_id} contains unredacted credentials or tokens in event payload."
            )

    if allowed_domains:
        norm_domains = {d.lower() for d in allowed_domains}
        for match in _EMAIL_PATTERN.finditer(serialized):
            domain = match.group(1).lower()
            if domain not in norm_domains:
                raise ManifestValidationError(
                    f"Sample {sample.sample_id} contains email domain '{domain}' not in allowed manifest domains."
                )


def load_dataset(dataset_dir: Union[str, Path]) -> BenchmarkDataset:
    """Validate manifest and load dataset samples and probes from disk.

    Raises:
        ManifestValidationError if manifest validation fails or files are corrupt.
    """
    dir_path = Path(dataset_dir)
    validate_manifest(dir_path)

    manifest_file = dir_path / "manifest.json"
    manifest_raw = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest = DatasetManifest.model_validate(manifest_raw)

    samples: List[BenchmarkSample] = []
    samples_file = dir_path / "samples.json"
    if samples_file.is_file():
        raw_samples = json.loads(samples_file.read_text(encoding="utf-8"))
        allowed_domains = set(manifest.allowed_email_domains) if manifest.allowed_email_domains else None
        for item in raw_samples:
            sample = BenchmarkSample.model_validate(item)
            validate_ground_truth_sample(sample, allowed_domains=allowed_domains)
            samples.append(sample)

    probes: List[ProbeSpec] = []
    probes_file = dir_path / "probes.json"
    if probes_file.is_file():
        raw_probes = json.loads(probes_file.read_text(encoding="utf-8"))
        for item in raw_probes:
            probe = ProbeSpec.model_validate(item)
            probes.append(probe)

    return BenchmarkDataset(manifest=manifest, samples=samples, probes=probes)


def generate_manifest_checksums(dataset_dir: Union[str, Path]) -> Dict[str, FileChecksum]:
    """Calculate cryptographic SHA-256 checksums and file sizes for all dataset JSON files."""
    dir_path = Path(dataset_dir)
    result: Dict[str, FileChecksum] = {}
    for p in dir_path.glob("*.json"):
        if p.name == "manifest.json":
            continue
        sha = compute_file_sha256(p)
        size = p.stat().st_size
        item_count = None
        try:
            parsed = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(parsed, list):
                item_count = len(parsed)
        except Exception:
            pass
        result[p.name] = FileChecksum(sha256=sha, size_bytes=size, item_count=item_count)
    return result
