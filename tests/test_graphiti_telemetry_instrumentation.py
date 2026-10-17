"""Tests for detailed Graphiti telemetry, sub-calls breakdown, and per-model cost audit."""

import pytest
from ptb_contracts.telemetry import CostAuditTracker, ESTIMATED_RATES_PER_1M, get_cost_audit_tracker
from ptb_graph_memory.adapter import GraphitiQwen3Embedder, GraphitiKevCrossEncoder


@pytest.mark.unit
def test_detailed_graphiti_subcall_tracking():
    tracker = get_cost_audit_tracker()
    tracker.reset()

    tracker.record_graphiti_sync(episodes=2, calls=8)
    tracker.record_graphiti_subcall("node_extraction", tokens=120)
    tracker.record_graphiti_subcall("edge_extraction", tokens=85)
    tracker.record_graphiti_subcall("embedding", tokens=40)

    metrics = tracker.get_metrics()
    assert metrics["graphiti"]["episodes_total"] == 2
    # 8 calls from sync + 3 subcalls = 11 calls total
    assert metrics["graphiti"]["calls_total"] == 11
    assert metrics["graphiti"]["subcalls_by_type"]["node_extraction"] == 1
    assert metrics["graphiti"]["subcalls_by_type"]["edge_extraction"] == 1
    assert metrics["graphiti"]["subcalls_by_type"]["embedding"] == 1
    assert metrics["graphiti"]["tokens_total"] == 245


@pytest.mark.unit
def test_per_model_cost_audit_calculation():
    tracker = get_cost_audit_tracker()
    tracker.reset()

    # Model 1: gpt-4o-mini (1M in=$0.15, 1M out=$0.60)
    tracker.record_llm_call(
        provider="openai",
        model="gpt-4o-mini",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    # Model 2: gemini-2.5-flash (1M in=$0.075, 1M out=$0.30)
    tracker.record_llm_call(
        provider="google",
        model="gemini-2.5-flash",
        input_tokens=2_000_000,
        output_tokens=1_000_000,
    )

    metrics = tracker.get_metrics()
    assert metrics["llm"]["requests_total"] == 2
    assert "gpt-4o-mini" in metrics["llm"]["tokens_by_model"]
    assert "gemini-2.5-flash" in metrics["llm"]["tokens_by_model"]

    # gpt-4o-mini cost: 0.15 + 0.60 = 0.75
    # gemini-2.5-flash cost: 2*0.075 + 1*0.30 = 0.45
    # total expected cost: 0.75 + 0.45 = 1.20
    assert metrics["llm"]["estimated_cost_usd"] == pytest.approx(1.20, abs=1e-4)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_graphiti_adapters_telemetry_recording(monkeypatch):
    tracker = get_cost_audit_tracker()
    tracker.reset()

    class FakeQwenService:
        def embed_text(self, text):
            return [0.1, 0.2]
        def embed_batch(self, texts):
            return [[0.1, 0.2] for _ in texts]

    embedder = GraphitiQwen3Embedder(embedding_service=FakeQwenService())
    await embedder.create("Task review meeting scheduled for tomorrow")
    await embedder.create_batch(["Document 1", "Document 2"])

    metrics = tracker.get_metrics()
    assert metrics["embedding"]["inputs_total"] == 3
    assert metrics["graphiti"]["subcalls_by_type"]["embedding"] == 1
    assert metrics["graphiti"]["subcalls_by_type"]["embedding_batch"] == 1
    assert metrics["graphiti"]["tokens_total"] > 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_graphiti_adapters_fallback_isolation():
    tracker = get_cost_audit_tracker()
    tracker.reset()

    class FallbackQwenService:
        def __init__(self):
            self.last_call_status = "fallback"
            self.last_usage_tokens = None
        def embed_text(self, text):
            return [0.0] * 1024
        def embed_batch(self, texts):
            return [[0.0] * 1024 for _ in texts]

    class FallbackKevService:
        def __init__(self):
            self.last_call_status = "fallback"
            self.last_usage_tokens = None
        def rerank(self, query, documents, top_k=None):
            return [{"index": i, "document": d, "score": 0.5} for i, d in enumerate(documents)]

    embedder = GraphitiQwen3Embedder(embedding_service=FallbackQwenService())
    await embedder.create("Offline query")
    await embedder.create_batch(["Offline 1", "Offline 2"])

    reranker = GraphitiKevCrossEncoder(reranker_service=FallbackKevService())
    await reranker.rank("Offline query", ["Passage 1", "Passage 2"])

    metrics = tracker.get_metrics()
    # Fallback calls must be tracked in fallback_calls counters
    assert metrics["embedding"]["fallback_calls"] == 3
    assert metrics["kev"]["fallback_calls"] == 1
    # Normal upstream request / token counters must NOT be polluted
    assert metrics["embedding"]["inputs_total"] == 0
    assert metrics["embedding"]["requests_total"] == 0
    assert metrics["kev"]["requests_total"] == 0
    assert metrics["graphiti"]["tokens_total"] == 0
