"""Tests for cold-cache benchmark harness and isolated live extraction verification (DEV-P1-01)."""

import pytest
from pathlib import Path
from benchmarks.cli import build_parser
from benchmarks.runner.extraction_cache import ExtractionCache, CachedLLMExtractor
from benchmarks.runner.environment import EnvironmentManager
from ptb_contracts import UnifiedTaskCandidate, TaskStatus, ParsedMessageContent


@pytest.mark.unit
def test_cold_cache_initialization_and_isolation(tmp_path: Path):
    fake_cache_file = tmp_path / "warm_cache.json"
    fake_cache_file.write_text('{"sample-1": {"id": "t-1", "title": "Warm task", "status": "TODO"}}', encoding="utf-8")

    # Regular warm cache loads the file
    warm_cache = ExtractionCache(cache_file=fake_cache_file, cold_cache=False)
    assert warm_cache.count() == 1
    assert warm_cache.contains("sample-1")
    assert warm_cache.get("sample-1") is not None

    # Cold cache completely ignores disk content
    cold_cache = ExtractionCache(cache_file=fake_cache_file, cold_cache=True)
    assert cold_cache.count() == 0
    assert not cold_cache.contains("sample-1")
    assert cold_cache.get("sample-1") is None

    # Saving in cold-cache does not modify disk file
    cold_cache.put("new-sample", UnifiedTaskCandidate(id="t-new", title="New task", status=TaskStatus.TODO))
    cold_cache.save()
    # File content remains unchanged
    assert '"Warm task"' in fake_cache_file.read_text(encoding="utf-8")
    assert '"New task"' not in fake_cache_file.read_text(encoding="utf-8")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cached_llm_extractor_stats_tracking():
    class MockRealExtractor:
        def __init__(self):
            self.call_count = 0
        async def extract_async(self, parsed, raw_event_id, source_type, external_url=None, project_key=None):
            self.call_count += 1
            return UnifiedTaskCandidate(id="t-ext", title="Extracted", status=TaskStatus.TODO)

    mock_real = MockRealExtractor()
    cold_cache = ExtractionCache(cold_cache=True)
    extractor = CachedLLMExtractor(real_extractor=mock_real, cache=cold_cache)

    parsed = ParsedMessageContent(actual_content_text="Need help fixing production outage")
    res1 = await extractor.extract_async(parsed=parsed, raw_event_id="ev-1", source_type="slack", sample_id="s-1")
    res2 = await extractor.extract_async(parsed=parsed, raw_event_id="ev-2", source_type="slack", sample_id="s-2")

    stats = extractor.get_stats()
    assert stats["cold_cache"] is True
    assert stats["call_count"] == 2
    assert stats["cache_hits"] == 0
    assert stats["cache_misses"] == 2
    assert stats["live_calls"] == 2
    assert mock_real.call_count == 2


@pytest.mark.unit
def test_environment_manager_cold_cache_parameter():
    env = EnvironmentManager.create_environment(mode="fixture", cold_cache=True)
    assert env.cold_cache is True
    assert hasattr(env.llm_extractor, "cache")
    assert env.llm_extractor.cache.cold_cache is True
    # Dual-cache isolation: underlying real_extractor must also bypass ExtractionCacheManager
    real_extractor = env.llm_extractor.real_extractor
    assert getattr(real_extractor, "bypass_cache", False) is True
    assert getattr(real_extractor, "cache", None) is None


@pytest.mark.unit
def test_cli_parser_accepts_cold_cache_flag():
    parser = build_parser()
    args = parser.parse_args(["run", "--dataset-dir", "benchmarks/dataset/mini_smoke", "--cold-cache"])
    assert args.cold_cache is True
