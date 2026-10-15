"""Smoke tests executing ReplayRunner and BenchmarkMetricEvaluator on mini_smoke dataset."""

from pathlib import Path
from typing import Optional
import pytest

from ptb_contracts.l2_processing import EvidenceRecord, TaskStatus, UnifiedTaskCandidate

from benchmarks.evaluator.scorecard import BenchmarkMetricEvaluator
from benchmarks.manifest import load_dataset
from benchmarks.runner.environment import EnvironmentManager
from benchmarks.runner.runner import ReplayRunner


class _FastTestExtractor:
    """Fast test double for unit test execution to avoid external LLM network latency."""

    async def extract_async(
        self,
        parsed,
        raw_event_id: str,
        source_type: str,
        external_url: str = None,
    ) -> Optional[UnifiedTaskCandidate]:
        from datetime import datetime, timezone
        from ptb_contracts.l2_processing import EvidenceType

        text = parsed.actual_content_text or ""
        if "cân nhắc" in text or "sau này" in text:
            return None

        return UnifiedTaskCandidate(
            id=f"cand-{raw_event_id}",
            title=text or "Test Title",
            status=TaskStatus.TODO,
            owner_canonical_id="cuong.dam",
            evidences=[
                EvidenceRecord(
                    source_event_id=raw_event_id,
                    raw_event_id=raw_event_id,
                    evidence_type=EvidenceType.CHAT_COMMITMENT,
                    snippet="Test snippet",
                    source_type=source_type,
                    timestamp=datetime.now(timezone.utc),
                )
            ],
        )


@pytest.mark.asyncio
async def test_replay_runner_mini_smoke_idempotency_and_snapshotting():
    """Verify that ReplayRunner executes clean pass, replay pass, and computes 10 dimensions."""
    ds_dir = Path("benchmarks/dataset/mini_smoke")
    dataset = load_dataset(ds_dir)

    env = EnvironmentManager.create_environment(dataset=dataset, mode="fixture")
    # Inject test extractor for fast deterministic test execution without network I/O
    test_extractor = _FastTestExtractor()
    env.llm_extractor = test_extractor
    env.processing_pipeline.llm_extractor = test_extractor

    runner = ReplayRunner(env)

    # 1. Clean run pass
    clean_trace = await runner.run_clean(dataset)
    assert len(clean_trace.snapshots) == 12
    assert all(s.latency_ms >= 0.0 for s in clean_trace.snapshots)
    assert clean_trace.raw_event_count > 0

    # 2. Replay idempotency pass
    replay_trace = await runner.run_replay(dataset, clean_trace)
    assert replay_trace.replay_duplicates == 0, "Idempotency replay created duplicate tasks!"

    # 3. Scorecard evaluation
    scorecard = BenchmarkMetricEvaluator.evaluate(dataset, clean_trace, replay_trace)
    assert scorecard.total_cases == 12
    assert len(scorecard.dimensions) == 10

    # Dimension 9: Idempotency must PASS
    dim_idempotency = next(d for d in scorecard.dimensions if d.dimension_number == 9)
    assert dim_idempotency.passed is True
    assert dim_idempotency.measured_value == 0.0

    # Dimension 2: Noise rejection must PASS
    dim_noise = next(d for d in scorecard.dimensions if d.dimension_number == 2)
    assert dim_noise.passed is True
    assert dim_noise.measured_value >= 0.85


@pytest.mark.asyncio
async def test_replay_runner_filter_single_case():
    """Verify that ReplayRunner executes single-case filtering accurately."""
    ds_dir = Path("benchmarks/dataset/mini_smoke")
    dataset = load_dataset(ds_dir)

    env = EnvironmentManager.create_environment(dataset=dataset, mode="fixture")
    runner = ReplayRunner(env)

    clean_trace = await runner.run_clean(dataset, filter_case_id="smoke-07-noise-chitchat")
    assert len(clean_trace.snapshots) == 1
    snap = clean_trace.snapshots[0]
    assert snap.sample_id == "smoke-07-noise-chitchat"
    assert snap.should_extract is False
    assert snap.resulting_task is None

    scorecard = BenchmarkMetricEvaluator.evaluate(
        dataset,
        clean_trace,
        filter_case_id="smoke-07-noise-chitchat",
    )
    assert scorecard.total_cases == 1
    assert scorecard.passed_cases == 1
    assert scorecard.overall_verdict.value == "PASS"
