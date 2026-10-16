"""Regression guards for extraction cache integrity and uv workspace stability."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tomllib

import pytest

from ptb_contracts import TaskStatus, UnifiedTaskCandidate
from ptb_processing.extractor.cache import (
    DEFAULT_PROD_CACHE_FILE,
    ExtractionCacheManager,
    compute_extraction_cache_key,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.unit
def test_production_cache_path_is_in_persisted_root_data() -> None:
    assert DEFAULT_PROD_CACHE_FILE == REPO_ROOT / "data" / "cache" / "extraction_cache.json"
    assert not DEFAULT_PROD_CACHE_FILE.is_relative_to(REPO_ROOT / "services")


@pytest.mark.unit
def test_uv_workspace_has_only_real_package_members() -> None:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    members = config["tool"]["uv"]["workspace"]["members"]
    assert members
    assert all("*" not in member for member in members)
    assert all((REPO_ROOT / member / "pyproject.toml").is_file() for member in members)
    assert "services/data" not in members


@pytest.mark.unit
def test_missing_production_cache_never_imports_benchmark_fixtures(tmp_path: Path) -> None:
    cache_file = tmp_path / "data" / "cache" / "extractions.json"
    cache = ExtractionCacheManager(cache_file=cache_file)
    assert cache.count() == 0
    assert not cache_file.exists()


@pytest.mark.unit
def test_production_cache_environment_override(monkeypatch, tmp_path: Path) -> None:
    cache_file = tmp_path / "data" / "cache" / "override.json"
    monkeypatch.setenv("PTB_EXTRACTION_CACHE_FILE", str(cache_file))
    assert ExtractionCacheManager().cache_file == cache_file


@pytest.mark.unit
def test_cache_atomic_save_reload_and_file_permissions(tmp_path: Path) -> None:
    cache_file = tmp_path / "data" / "cache" / "extractions.json"
    item = UnifiedTaskCandidate(
        id="cache-task-1", title="Review deployment", description="Release check",
        status=TaskStatus.TODO, extraction_confidence=0.95,
        review_status="auto_approved",
    )
    key = compute_extraction_cache_key("Review deployment", "gpt-4o-mini", "v1.0")
    cache = ExtractionCacheManager(cache_file=cache_file)
    cache.put(key, item, alias_key="raw-1")
    restored = ExtractionCacheManager(cache_file=cache_file)
    assert restored.get(key).title == item.title
    assert restored.get(key, alias_key="raw-1").title == item.title
    assert not list(cache_file.parent.glob(".ptb-extraction-*.tmp"))
    if os.name != "nt":
        assert stat.S_IMODE(cache_file.stat().st_mode) == 0o600


@pytest.mark.unit
def test_unbound_legacy_alias_does_not_return_stale_task_for_new_hash(tmp_path: Path) -> None:
    item = UnifiedTaskCandidate(
        id="old-task", title="Outdated task", description="Old input",
        status=TaskStatus.TODO, extraction_confidence=0.95,
        review_status="auto_approved",
    )
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({"raw-legacy": item.model_dump(mode="json")}), encoding="utf-8")
    cache = ExtractionCacheManager(cache_file=path)
    new_hash = compute_extraction_cache_key("New text", "gpt-4o-mini", "v1.0")
    assert cache.get(new_hash, alias_key="raw-legacy") is None
