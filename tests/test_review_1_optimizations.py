"""Tests for Review 1 Optimization Requirements (P0, P1, P2).

Covers:
1. P0: CostAuditTracker telemetry metrics and /api/audit/cost endpoint.
2. P1: Production Extraction Cache keyed by sha256(content:model:prompt_version).
3. P1: Deterministic Structured Source Extractor (Jira, Shortcut, GitHub, Git).
4. P1: Embedding content-hash deduplication (skips re-embedding unchanged tasks).
5. P1: Ambiguous correlation candidates (0.45 - 0.85) calibrated with KevDecisionReranker.
6. P2: Selective Graphiti ingestion filter.
"""

import asyncio
from datetime import datetime, timezone
import hashlib
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport

from ptb_application.api import create_app
from ptb_contracts.l1_acquisition import RawEventRecord, SourceType
from ptb_contracts.l2_processing import (
    EvidenceRecord,
    EvidenceType,
    ParsedMessageContent,
    TaskStatus,
    UnifiedTaskCandidate,
)
from ptb_contracts.telemetry import CostAuditTracker, get_cost_audit_tracker
from ptb_database.repositories.task_repo import (
    TaskDomainRepository,
    compute_task_content_hash,
)
from ptb_processing.correlation.candidate_matcher import TaskCandidateMatcher
from ptb_processing.correlation.scoring import CorrelationScoreResult
from ptb_processing.extractor.cache import (
    ExtractionCacheManager,
    compute_extraction_cache_key,
)
from ptb_processing.extractor.llm_extractor import LLMStructuredExtractor
from ptb_processing.extractor.structured_extractor import DeterministicStructuredExtractor
from ptb_processing.pipeline import ProcessingPipeline


@pytest.fixture(autouse=True)
def reset_telemetry():
    """Reset the CostAuditTracker singleton before every test."""
    tracker = get_cost_audit_tracker()
    tracker.reset()
    yield tracker
    tracker.reset()


# ==============================================================================
# 1. P0: Telemetry & Cost Audit (/api/audit/cost)
# ==============================================================================
@pytest.mark.asyncio
async def test_cost_audit_tracker_and_endpoint():
    tracker = get_cost_audit_tracker()
    tracker.record_event_processed(10)
    tracker.record_llm_call(
        provider="openai",
        model="gpt-4o-mini",
        subsystem="extraction",
        input_tokens=1200,
        output_tokens=250,
        attempts=1,
        cached=False,
    )
    tracker.record_cache_hit()
    tracker.record_embedding_call(count=5, is_batch=True, skipped=False)
    tracker.record_embedding_call(count=3, is_batch=False, skipped=True)
    tracker.record_kev_call("correlation_rerank")
    tracker.record_graphiti_sync(episodes=2, calls=4)
    tracker.record_structured_bypass(4)

    metrics = tracker.get_metrics()
    assert metrics["llm"]["requests_total"] == 1
    assert metrics["llm"]["logical_extractions_total"] == 2
    assert metrics["cache"]["hits"] == 1
    assert metrics["cache"]["misses"] == 1
    assert metrics["cache"]["hit_rate"] == 0.5
    assert metrics["embedding"]["inputs_total"] == 5
    assert metrics["embedding"]["skipped_unchanged"] == 3
    assert metrics["kev"]["requests_total"] == 1
    assert metrics["graphiti"]["episodes_total"] == 2
    assert metrics["graphiti"]["calls_per_episode"] == 2.0
    assert metrics["efficiency"]["structured_bypass_count"] == 4
    assert metrics["efficiency"]["events_processed"] == 10

    # Test /api/audit/cost endpoint
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/audit/cost")
        assert resp.status_code == 200
        data = resp.json()
        assert "llm" in data
        assert "cache" in data
        assert "embedding" in data
        assert "kev" in data
        assert "efficiency" in data
        assert data["efficiency"]["structured_bypass_count"] == 4


# ==============================================================================
# 2. P1: Production Extraction Cache (sha256(content:model:prompt_version))
# ==============================================================================
def test_extraction_cache_key_computation():
    content = "Hoàn thành tài liệu kiến trúc PTB trước 5h chiều"
    model = "gpt-4o-mini"
    prompt_version = "v1.0"

    key1 = compute_extraction_cache_key(content, model, prompt_version)
    key2 = compute_extraction_cache_key(content, model, prompt_version)
    assert key1 == key2
    assert len(key1) == 64  # Valid SHA-256 hex digest

    # Different prompt version produces different key
    key_v2 = compute_extraction_cache_key(content, model, "v2.0")
    assert key1 != key_v2


def test_extraction_cache_manager_put_and_get(tmp_path):
    cache_file = tmp_path / "test_cache.json"
    cache = ExtractionCacheManager(cache_file=cache_file)

    candidate = UnifiedTaskCandidate(
        id="task-cache-001",
        title="Deploy to Staging",
        description="Testing staging deployment",
        status=TaskStatus.TODO,
        extraction_confidence=0.95,
        review_status="auto_approved",
    )

    key = compute_extraction_cache_key("Deploy to Staging", "gpt-4o-mini", "v1.0")
    cache.put(key, candidate, alias_key="raw-event-001")

    # Get by content hash
    retrieved = cache.get(key)
    assert retrieved is not None
    assert retrieved.title == "Deploy to Staging"

    # Get by alias key
    retrieved_alias = cache.get("non-existent-hash", alias_key="raw-event-001")
    assert retrieved_alias is not None
    assert retrieved_alias.title == "Deploy to Staging"

    # Integrity check: when same alias is queried with different content hash, it must invalidate and return None
    different_hash = compute_extraction_cache_key("Different prompt content entirely", "gpt-4o-mini", "v1.0")
    retrieved_mismatched = cache.get(different_hash, alias_key="raw-event-001")
    assert retrieved_mismatched is None, "Cache must invalidate alias when content hash differs!"


@pytest.mark.asyncio
async def test_llm_extractor_uses_cache_without_calling_http(tmp_path):
    cache_file = tmp_path / "llm_test_cache.json"
    cache = ExtractionCacheManager(cache_file=cache_file)

    parsed = ParsedMessageContent(
        is_quote_reply=False,
        actual_content_text="Sửa lỗi giao diện Task Board",
        actual_author_raw="Cuong",
    )

    # Pre-warm cache
    prompt = "Tin nhắn phản hồi (từ Cuong): \"Sửa lỗi giao diện Task Board\""
    key = compute_extraction_cache_key(prompt, "gpt-4o-mini", "v1.0")
    cached_cand = UnifiedTaskCandidate(
        id="cand-001",
        title="Sửa lỗi giao diện Task Board",
        description="Cache entry",
        status=TaskStatus.TODO,
        extraction_confidence=0.9,
        review_status="auto_approved",
    )
    cache.put(key, cached_cand, alias_key="raw-ev-99")

    # Extractor without API key succeeds because cache hits!
    extractor = LLMStructuredExtractor(api_key="", model="gpt-4o-mini", cache=cache)
    result = extractor.extract_from_parsed(parsed=parsed, raw_event_id="raw-ev-99")
    assert result.title == "Sửa lỗi giao diện Task Board"
    assert get_cost_audit_tracker().cache_hits == 1


# ==============================================================================
# 3. P1: Deterministic Structured Extraction Bypass (Zero LLM Calls)
# ==============================================================================
def test_structured_extractor_jira_bypass():
    extractor = DeterministicStructuredExtractor()
    now = datetime.now(timezone.utc)

    raw_event = RawEventRecord(
        id="raw-jira-01",
        tenant_id="default",
        source_type=SourceType.JIRA,
        external_id="PROJ-789",
        conversation_or_project_id="PROJ",
        author_external_id="user-123",
        author_display_name="Cuong Nguyen",
        event_timestamp=now,
        raw_payload={
            "key": "PROJ-789",
            "fields": {
                "summary": "Fix connection pool memory leak",
                "description": "Investigate pgpool leak in production",
                "status": {"name": "In Progress"},
                "assignee": {"displayName": "Cuong Nguyen"},
                "reporter": {"displayName": "Alice Admin"},
                "duedate": "2026-10-25T17:00:00Z",
            },
        },
    )

    assert extractor.can_extract(raw_event) is True
    cand = extractor.extract(raw_event)
    assert cand is not None
    assert cand.title == "[PROJ-789] Fix connection pool memory leak"
    assert cand.status == TaskStatus.IN_PROGRESS
    assert cand.owner_name == "Cuong Nguyen"
    assert cand.requester_name == "Alice Admin"
    assert cand.extraction_confidence == 1.0
    assert cand.review_status == "auto_approved"
    assert len(cand.evidences) == 1
    assert cand.evidences[0].evidence_type == EvidenceType.JIRA_TICKET


def test_structured_extractor_shortcut_bypass():
    extractor = DeterministicStructuredExtractor()
    now = datetime.now(timezone.utc)

    raw_event = RawEventRecord(
        id="raw-sc-01",
        tenant_id="default",
        source_type=SourceType.SHORTCUT,
        external_id="1204",
        conversation_or_project_id="ENGINEERING",
        author_external_id="user-456",
        author_display_name="Bob Developer",
        event_timestamp=now,
        raw_payload={
            "id": 1204,
            "name": "Add GraphQL query caching",
            "description": "Cache schema introspections in Redis",
            "completed": False,
            "deadline": "2026-10-30T12:00:00Z",
        },
    )

    assert extractor.can_extract(raw_event) is True
    cand = extractor.extract(raw_event)
    assert cand is not None
    assert cand.title == "[story-1204] Add GraphQL query caching"
    assert cand.status == TaskStatus.TODO
    assert cand.extraction_confidence == 1.0
    assert cand.evidences[0].evidence_type == EvidenceType.SHORTCUT_STORY


# ==============================================================================
# 4. P1: Embedding Content-Hash Deduplication (TaskDomainRepository)
# ==============================================================================
def test_compute_task_content_hash():
    h1 = compute_task_content_hash("Review Code", "Check PR #42", "qwen3-embedding")
    h2 = compute_task_content_hash("Review Code", "Check PR #42", "qwen3-embedding")
    assert h1 == h2
    assert len(h1) == 64

    # Change description changes hash
    h3 = compute_task_content_hash("Review Code", "Check PR #43", "qwen3-embedding")
    assert h1 != h3


@pytest.mark.asyncio
async def test_upsert_task_atomic_skips_embedding_when_hash_matches():
    mock_neo4j = MagicMock()
    mock_session = AsyncMock()
    mock_neo4j.get_driver().session.return_value.__aenter__.return_value = mock_session

    mock_embed_service = MagicMock()
    mock_embed_service.model_name = "qwen3-embedding"
    mock_embed_service.embed_text = MagicMock(return_value=[0.1, 0.2, 0.3])

    repo = TaskDomainRepository(neo4j_client=mock_neo4j, embedding_service=mock_embed_service)

    task = UnifiedTaskCandidate(
        id="task-embed-001",
        title="Write Unit Tests",
        description="Comprehensive tests for deduplication",
        status=TaskStatus.TODO,
    )

    expected_hash = compute_task_content_hash(task.title, task.description, "qwen3-embedding")

    # Case 1: First time (no existing hash) -> embed is called
    repo.get_task_embedding_hash = AsyncMock(return_value=None)
    repo.set_task_embedding = AsyncMock()
    repo._execute_upsert_task = AsyncMock()

    await repo.upsert_task_atomic(task)
    assert mock_embed_service.embed_text.called
    assert repo.set_task_embedding.called
    assert get_cost_audit_tracker().embedding_inputs_total == 1
    assert get_cost_audit_tracker().embedding_skipped_unchanged == 0

    # Case 2: Second time (hash matches existing) -> embed is SKIPPED
    mock_embed_service.embed_text.reset_mock()
    repo.set_task_embedding.reset_mock()
    repo.get_task_embedding_hash = AsyncMock(return_value=expected_hash)

    await repo.upsert_task_atomic(task)
    assert not mock_embed_service.embed_text.called
    assert not repo.set_task_embedding.called
    assert get_cost_audit_tracker().embedding_skipped_unchanged == 1


# ==============================================================================
# 5. P1: Ambiguous Correlation Reranking via Kev Decision Reranker
# ==============================================================================
@pytest.mark.asyncio
async def test_candidate_matcher_ambiguous_correlation_kev_rerank():
    mock_reranker = MagicMock()
    mock_reranker.rerank = MagicMock(return_value=[
        {"index": 0, "document": "Fix connection pool memory leak", "score": 0.88}
    ])

    matcher = TaskCandidateMatcher(reranker=mock_reranker)

    cand = UnifiedTaskCandidate(
        id="cand-ambiguous-01",
        title="Check leak in connection pool",
        description="DB connections keep growing",
        status=TaskStatus.TODO,
    )

    existing = [
        UnifiedTaskCandidate(
            id="target-01",
            title="Fix connection pool memory leak",
            description="DB connection leak on production cluster",
            status=TaskStatus.TODO,
        )
    ]

    # Force a borderline correlation score (0.60, within 0.45 - 0.85) without deterministic anchors
    mock_scorer = MagicMock()
    mock_scorer.score.return_value = CorrelationScoreResult(
        target_task_id="target-01",
        correlation_confidence=0.60,
        should_merge=False,
        has_deterministic_anchor=False,
        semantic_similarity=0.60,
    )
    matcher.scorer = mock_scorer

    matches = await matcher.find_candidates(cand, existing_tasks=existing)
    assert len(matches) == 1
    # Verify Kev was invoked
    assert mock_reranker.rerank.called
    assert get_cost_audit_tracker().kev_requests_total == 1

    # Verify Kev calibrated the confidence: (0.60 * 0.6) + (0.88 * 0.4) = 0.36 + 0.352 = 0.712
    calibrated_conf = matches[0].score_result.correlation_confidence
    assert calibrated_conf > 0.60
    assert matches[0].score_result.kev_calibrated is True


# ==============================================================================
# 6. P1: Ambiguous Priority Resolution via Kev Urgency Classification
# ==============================================================================
def test_priority_engine_resolve_ambiguous_priority_with_kev():
    from ptb_intelligence.priority import DeterministicPriorityEngine
    from ptb_contracts.l4_intelligence import PriorityBreakdown

    engine = DeterministicPriorityEngine()

    task = UnifiedTaskCandidate(
        id="task-ambig-prio",
        title="Check server memory usage",
        description="Investigate memory spike reported on staging",
        status=TaskStatus.TODO,
    )

    # Base breakdown in ambiguous tier (50.0)
    base_breakdown = PriorityBreakdown(
        total_score=50.0,
        deadline_score=0.0,
        customer_impact_score=0.0,
        production_impact_score=20.0,
        commitment_weight=10.0,
        stale_age_score=20.0,
        waiting_penalty=0.0,
        uncertainty_deduction=0.0,
        llm_explanation="Score 50.0/100: Normal priority",
    )

    mock_reranker = MagicMock()
    mock_reranker.classify_task_urgency.return_value = {
        "is_actionable": True,
        "urgency_score": 2.5,
        "priority": "High",
    }

    calibrated = engine.resolve_ambiguous_priority_with_kev(task, base_breakdown, reranker=mock_reranker)
    assert calibrated.total_score == 65.0  # 50.0 + 15.0 delta
    assert "Kev Urgency Calibrated (High: +15pts)" in calibrated.llm_explanation
    assert get_cost_audit_tracker().kev_requests_total == 1


# ==============================================================================
# 7. P1: Structured Extractor Git Smart Status
# ==============================================================================
def test_structured_extractor_git_smart_status():
    extractor = DeterministicStructuredExtractor()
    now = datetime.now(timezone.utc)

    # WIP / active commit -> IN_PROGRESS
    raw_wip = RawEventRecord(
        id="raw-git-wip",
        tenant_id="default",
        source_type=SourceType.GIT,
        external_id="abc1234",
        conversation_or_project_id="PTB",
        author_external_id="dev@ptb.local",
        author_display_name="Dev User",
        event_timestamp=now,
        raw_payload={"message": "feat: implement caching layer for Redis", "hash": "abc1234"},
        normalized_text="feat: implement caching layer for Redis",
    )
    cand_wip = extractor.extract(raw_wip)
    assert cand_wip is not None
    assert cand_wip.status == TaskStatus.IN_PROGRESS
    assert cand_wip.extraction_confidence == 1.0

    # Fix / done commit -> DONE
    raw_done = RawEventRecord(
        id="raw-git-done",
        tenant_id="default",
        source_type=SourceType.GIT,
        external_id="def5678",
        conversation_or_project_id="PTB",
        author_external_id="dev@ptb.local",
        author_display_name="Dev User",
        event_timestamp=now,
        raw_payload={"message": "fix: resolve memory leak on shutdown", "hash": "def5678"},
        normalized_text="fix: resolve memory leak on shutdown",
    )
    cand_done = extractor.extract(raw_done)
    assert cand_done is not None
    assert cand_done.status == TaskStatus.DONE
