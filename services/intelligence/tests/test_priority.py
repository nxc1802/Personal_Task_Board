"""Unit tests for DeterministicPriorityEngine."""

from datetime import datetime, timedelta, timezone
import pytest

from ptb_contracts.l2_processing import (
    EvidenceRecord,
    EvidenceType,
    TaskStatus,
    UnifiedTaskCandidate,
)
from ptb_contracts.l4_intelligence import GraphRelationInfo, TaskWithContext
from ptb_intelligence.priority import DeterministicPriorityEngine


@pytest.fixture
def priority_engine():
    return DeterministicPriorityEngine()


def test_deadline_score_overdue(priority_engine):
    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
    due_date = now - timedelta(days=2)  # Quá hạn 2 ngày

    task = UnifiedTaskCandidate(
        id="task-1",
        title="Fix database migration",
        due_date=due_date,
    )

    breakdown = priority_engine.calculate_priority(task, now=now)
    assert breakdown.deadline_score == 35.0
    assert "quá hạn" in breakdown.llm_explanation.lower()
    assert breakdown.total_score >= 35.0


def test_deadline_score_within_24h(priority_engine):
    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
    due_date = now + timedelta(hours=6)

    task = UnifiedTaskCandidate(
        id="task-2",
        title="Submit sprint review",
        due_date=due_date,
    )

    breakdown = priority_engine.calculate_priority(task, now=now)
    assert 30.0 <= breakdown.deadline_score <= 35.0
    assert "sát deadline" in breakdown.llm_explanation.lower()


def test_customer_impact_with_id(priority_engine):
    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
    task = UnifiedTaskCandidate(
        id="task-3",
        title="Export audit report",
        customer_id="cust-acme-corp",
    )

    breakdown = priority_engine.calculate_priority(task, now=now)
    assert breakdown.customer_impact_score == 25.0
    assert "cust-acme-corp" in breakdown.llm_explanation


def test_production_impact_high_keyword(priority_engine):
    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
    task = UnifiedTaskCandidate(
        id="task-4",
        title="Production API outage hotfix",
    )

    breakdown = priority_engine.calculate_priority(task, now=now)
    assert breakdown.production_impact_score == 20.0
    assert "Production" in breakdown.llm_explanation


def test_production_impact_bug_evidence(priority_engine):
    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
    ev = EvidenceRecord(
        id="ev-1",
        raw_event_id="raw-1",
        evidence_type=EvidenceType.AGENT_BUG_FIX,
        source_type="git",
        timestamp=now,
        snippet="Fixed null pointer exception in payment flow",
    )
    task = UnifiedTaskCandidate(
        id="task-5",
        title="Payment flow patch",
        evidences=[ev],
    )

    breakdown = priority_engine.calculate_priority(task, now=now)
    assert breakdown.production_impact_score == 12.0


def test_commitment_weight(priority_engine):
    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
    ev = EvidenceRecord(
        id="ev-commit",
        raw_event_id="raw-2",
        evidence_type=EvidenceType.CHAT_COMMITMENT,
        source_type="teams",
        timestamp=now,
        snippet="Tôi hứa sẽ gửi bản demo trước 5h chiều",
    )
    task = UnifiedTaskCandidate(
        id="task-6",
        title="Gửi bản demo cho team",
        evidences=[ev],
    )

    breakdown = priority_engine.calculate_priority(task, now=now)
    assert breakdown.commitment_weight == 10.0
    assert "cam kết" in breakdown.llm_explanation.lower()


def test_stale_age_and_waiting_penalty(priority_engine):
    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
    task = UnifiedTaskCandidate(
        id="task-7",
        title="Refactor legacy parser",
        status=TaskStatus.BLOCKED,
    )
    ctx = TaskWithContext(
        task=task,
        last_status_change_at=now - timedelta(days=5),
        days_in_current_status=5,
        blocking_tasks=["task-blocker-1"],
    )

    breakdown = priority_engine.calculate_priority(ctx, now=now)
    assert breakdown.stale_age_score == 6.5
    assert breakdown.waiting_penalty == 5.0
    assert "tồn đọng" in breakdown.llm_explanation.lower()
    assert "chờ người khác" in breakdown.llm_explanation.lower()


def test_uncertainty_deduction(priority_engine):
    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
    task = UnifiedTaskCandidate(
        id="task-8",
        title="Unclear vague task",
        extraction_confidence=0.55,
    )

    breakdown = priority_engine.calculate_priority(task, now=now)
    assert breakdown.uncertainty_deduction > 0.0
    assert "độ tin cậy" in breakdown.llm_explanation.lower()


def test_clamped_total_score(priority_engine):
    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
    # Huge task that could exceed 100
    ev = EvidenceRecord(
        id="ev-max",
        raw_event_id="raw-max",
        evidence_type=EvidenceType.CHAT_COMMITMENT,
        source_type="teams",
        timestamp=now,
        snippet="Cam kết khẩn cấp",
    )
    task = UnifiedTaskCandidate(
        id="task-max",
        title="Production crash fix for customer VIP SLA",
        customer_id="cust-vip",
        due_date=now - timedelta(days=1),
        evidences=[ev],
    )
    ctx = TaskWithContext(
        task=task,
        last_status_change_at=now - timedelta(days=10),
        days_in_current_status=10,
    )

    breakdown = priority_engine.calculate_priority(ctx, now=now)
    assert breakdown.total_score <= 100.0
    assert breakdown.total_score == 100.0  # 35 + 25 + 20 + 10 + 10 = 100.0


def test_default_weights_loaded_from_yaml(priority_engine):
    """Verify that default weights correspond to v1_1.md and config/priority.yaml specs."""
    assert priority_engine.deadline_weight == 35.0
    assert priority_engine.customer_impact_weight == 25.0
    assert priority_engine.production_impact_weight == 20.0
    assert priority_engine.commitment_weight == 10.0
    assert priority_engine.stale_age_weight == 10.0
    assert priority_engine.waiting_penalty == 5.0
    assert priority_engine.uncertainty_penalty_max == 15.0


def test_custom_yaml_config_loading(tmp_path):
    """Verify custom YAML config overrides default weights and influences calculations."""
    custom_yaml = tmp_path / "custom_priority.yaml"
    custom_yaml.write_text(
        """
weights:
  deadline_weight: 45.0
  customer_impact_weight: 30.0
  production_impact_weight: 10.0
  commitment_weight: 5.0
  stale_age_weight: 10.0
  waiting_penalty: 8.0
  uncertainty_penalty_max: 20.0
keywords:
  prod_high:
    - "critical_incident"
"""
    )

    engine = DeterministicPriorityEngine(config_path=custom_yaml)
    assert engine.deadline_weight == 45.0
    assert engine.customer_impact_weight == 30.0
    assert engine.production_impact_weight == 10.0
    assert engine.commitment_weight == 5.0
    assert engine.waiting_penalty == 8.0
    assert engine.uncertainty_penalty_max == 20.0
    assert "critical_incident" in engine.prod_high_keywords

    # Calculation reflects custom weights
    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
    task = UnifiedTaskCandidate(
        id="task-custom",
        title="critical_incident occurred",
        due_date=now - timedelta(days=2),  # Overdue
        status=TaskStatus.BLOCKED,
    )
    breakdown = engine.calculate_priority(task, now=now)
    # Overdue receives deadline_weight (45.0)
    assert breakdown.deadline_score == 45.0
    # Production receives production_impact_weight (10.0)
    assert breakdown.production_impact_score == 10.0
    # Blocked receives waiting_penalty (8.0)
    assert breakdown.waiting_penalty == 8.0


def test_safe_fallback_on_missing_or_corrupt_yaml(tmp_path):
    """Verify safe fallback to default values when YAML file does not exist or has syntax errors."""
    # 1. Non-existent file
    missing_path = tmp_path / "does_not_exist.yaml"
    engine_missing = DeterministicPriorityEngine(config_path=missing_path)
    assert engine_missing.deadline_weight == 35.0
    assert engine_missing.customer_impact_weight == 25.0
    assert engine_missing.waiting_penalty == 5.0

    # 2. Corrupt / invalid syntax file
    corrupt_path = tmp_path / "corrupt.yaml"
    corrupt_path.write_text("weights: [invalid: yaml: ::::")
    engine_corrupt = DeterministicPriorityEngine(config_path=corrupt_path)
    assert engine_corrupt.deadline_weight == 35.0
    assert engine_corrupt.production_impact_weight == 20.0
    assert engine_corrupt.uncertainty_penalty_max == 15.0


def test_from_config_factory_method(tmp_path):
    """Verify from_config factory method instantiates engine correctly."""
    custom_yaml = tmp_path / "factory_priority.yaml"
    custom_yaml.write_text(
        """
weights:
  deadline_weight: 50.0
  waiting_penalty: 12.0
"""
    )
    engine = DeterministicPriorityEngine.from_config(config_path=custom_yaml)
    assert engine.deadline_weight == 50.0
    assert engine.waiting_penalty == 12.0
    # Unspecified weights maintain defaults
    assert engine.customer_impact_weight == 25.0

