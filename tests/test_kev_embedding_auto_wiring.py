"""Tests for Kev decision reranker and Qwen3 embedding auto-wiring and container readiness (DEV-P1-03)."""

import os
import pytest
from packages.ai_service import KevDecisionReranker, Qwen3EmbeddingService
from ptb_application.service import ApplicationService
from ptb_intelligence.priority import DeterministicPriorityEngine
from ptb_contracts.l4_intelligence import PriorityBreakdown
from ptb_contracts import UnifiedTaskCandidate, TaskStatus


@pytest.mark.unit
def test_embedding_service_auto_activated_via_env_url(monkeypatch):
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://127.0.0.1:8082/v1")
    monkeypatch.delenv("PTB_ENABLE_LOCAL_AI", raising=False)
    monkeypatch.delenv("PTB_REQUIRE_LOCAL_AI", raising=False)

    app_service = ApplicationService()
    assert app_service.task_repo is not None
    assert getattr(app_service.task_repo, "embedding_service", None) is not None
    assert isinstance(app_service.task_repo.embedding_service, Qwen3EmbeddingService)


@pytest.mark.unit
def test_kev_decision_reranker_honest_degraded_behavior_when_offline():
    reranker = KevDecisionReranker(base_url="http://127.0.0.1:59999/v1")
    # Port 59999 is closed, should return neutral scores without crashing
    res = reranker.rerank(
        query="Fix crash in pipeline",
        documents=["Doc 1: Pipeline fix", "Doc 2: Frontend style"],
        top_k=2,
    )
    assert len(res) == 2
    assert res[0]["score"] == 0.5
    assert res[1]["score"] == 0.5

    urgency = reranker.classify_task_urgency("Fix crash in pipeline")
    assert urgency["priority_level"] == "Medium"
    assert urgency["is_task_probability"] == 0.5


@pytest.mark.unit
def test_kev_priority_urgency_calibration_ambiguous_range():
    engine = DeterministicPriorityEngine()

    class MockKevUrgency:
        def classify_task_urgency(self, text):
            return {"priority_level": "High", "urgency_score": 2.5}

    task = UnifiedTaskCandidate(
        id="task-ambig-1",
        title="Check system logs for memory spikes",
        description="Routine check on server logs",
        status=TaskStatus.TODO,
    )

    base = PriorityBreakdown(
        total_score=50.0,
        deadline_score=0.0,
        customer_impact_score=0.0,
        production_impact_score=0.0,
        commitment_weight=0.0,
        stale_age_score=0.0,
        waiting_penalty=0.0,
        uncertainty_deduction=0.0,
    )

    calibrated = engine.resolve_ambiguous_priority_with_kev(
        task=task,
        base_breakdown=base,
        reranker=MockKevUrgency(),
    )

    # 50.0 + 15.0 delta = 65.0
    assert calibrated.total_score == 65.0
    assert "Kev Urgency Calibrated (High: +15pts)" in calibrated.llm_explanation
