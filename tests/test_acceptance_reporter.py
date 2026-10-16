"""Tests for release acceptance report generation (DEV-P1-05)."""

import pytest
from pathlib import Path
from scripts.generate_acceptance_report import generate_dev_report, get_git_commit_sha, get_git_author


@pytest.mark.unit
def test_acceptance_report_generation(tmp_path: Path):
    target = tmp_path / "DEV_REPORT.md"
    generated_path = generate_dev_report(
        commit_sha="abcdef1234567890abcdef1234567890abcdef12",
        author="test-engineer",
        verdict="GO",
        task_statuses={"DEV-P0-01": "PASS", "DEV-P1-01": "PASS"},
        output_path=target,
    )
    assert generated_path == target
    assert target.is_file()

    content = target.read_text(encoding="utf-8")
    assert "Commit SHA:** `abcdef1234567890abcdef1234567890abcdef12`" in content
    assert "Author/reviewer:** `test-engineer`" in content
    assert "Overall Release Verdict:** **GO**" in content
    assert "DEV-P0-01" in content
    assert "DEV-P1-01" in content
    assert "**PASS**" in content


@pytest.mark.unit
def test_acceptance_report_with_commands(tmp_path: Path):
    target = tmp_path / "DEV_REPORT_CMDS.md"
    generate_dev_report(
        commit_sha="1111111111111111111111111111111111111111",
        author="test-engineer",
        verdict="PENDING",
        task_statuses={"DEV-P1-01": "PENDING"},
        task_commands={"DEV-P1-01": "pytest tests/test_cold_cache_benchmark.py (Exit 0)"},
        task_evidences={"DEV-P1-01": "Dual cache isolated; live benchmark pending"},
        output_path=target,
    )
    content = target.read_text(encoding="utf-8")
    assert "`pytest tests/test_cold_cache_benchmark.py (Exit 0)`" in content
    assert "Dual cache isolated; live benchmark pending" in content
