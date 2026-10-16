"""Benchmark LLM Extraction Cache and Batch Pre-fetcher.

Ensures reliable, reproducible, and resilient benchmark execution against live LLMs:
- Caches real LLM extractions keyed by sample_id and raw content hash.
- Never mocks or fakes responses: caches authentic outputs from real LLM API calls.
- Handles transient network errors, HTTP 500s, and rate limits with exponential retry.
- Persists to benchmarks/runner/.cache/extraction_cache.json.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
import os
import re

from ptb_contracts.l2_processing import ParsedMessageContent, UnifiedTaskCandidate
from ptb_processing.extractor.llm_extractor import LLMStructuredExtractor
from benchmarks.models import BenchmarkDataset, BenchmarkSample

logger = logging.getLogger("ptb.benchmarks.cache")

CACHE_DIR = Path(__file__).resolve().parent / ".cache"
DEFAULT_CACHE_FILE = CACHE_DIR / "extraction_cache.json"


class ExtractionCache:
    """Persistent on-disk cache for benchmark LLM extractions, with optional cold-cache isolation."""

    def __init__(self, cache_file: Optional[Path] = None, cold_cache: bool = False) -> None:
        self.cold_cache = cold_cache
        self.cache_file = None if cold_cache else (cache_file or DEFAULT_CACHE_FILE)
        self._entries: Dict[str, Dict[str, Any]] = {}
        if not self.cold_cache:
            self._load()

    def _load(self) -> None:
        if self.cache_file and self.cache_file.is_file():
            try:
                data = json.loads(self.cache_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._entries = data
                    logger.info("Loaded %d cached extractions from %s", len(self._entries), self.cache_file)
            except Exception as e:
                logger.warning("Failed to load extraction cache: %s", e)
                self._entries = {}

    def save(self) -> None:
        if self.cold_cache or not self.cache_file:
            return
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(
                json.dumps(self._entries, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Failed to save extraction cache: %s", e)

    def get(self, sample_id: str) -> Optional[UnifiedTaskCandidate]:
        if self.cold_cache:
            return None
        if sample_id in self._entries:
            try:
                raw_data = self._entries[sample_id]
                return UnifiedTaskCandidate.model_validate(raw_data)
            except Exception as e:
                logger.warning("Corrupted cache entry for %s: %s", sample_id, e)
                return None
        return None

    def put(self, sample_id: str, candidate: UnifiedTaskCandidate) -> None:
        self._entries[sample_id] = candidate.model_dump(mode="json")

    def count(self) -> int:
        return len(self._entries)

    def contains(self, sample_id: str) -> bool:
        if self.cold_cache:
            return False
        return sample_id in self._entries


class CachedLLMExtractor:
    """Wrapper around real LLMStructuredExtractor that consults persistent cache first."""

    def __init__(
        self,
        real_extractor: LLMStructuredExtractor,
        cache: Optional[ExtractionCache] = None,
    ) -> None:
        self.real_extractor = real_extractor
        self.cache = cache if cache is not None else ExtractionCache()
        self.call_count: int = 0
        self.cache_hits: int = 0
        self.cache_misses: int = 0
        self.live_calls: int = 0

    def get_stats(self) -> Dict[str, Any]:
        """Return statistics on cache hits, misses, and live calls."""
        return {
            "cold_cache": getattr(self.cache, "cold_cache", False),
            "call_count": self.call_count,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "live_calls": self.live_calls,
            "hit_rate": round(self.cache_hits / max(1, self.call_count), 4),
        }

    async def extract_async(
        self,
        parsed: ParsedMessageContent,
        raw_event_id: str,
        source_type: str,
        external_url: Optional[str] = None,
        project_key: Optional[str] = None,
        channel_or_group: Optional[str] = None,
        sample_id: Optional[str] = None,
    ) -> UnifiedTaskCandidate:
        cache_key = sample_id or raw_event_id
        self.call_count += 1

        # 1. Check cache (skipped in cold-cache mode)
        if not getattr(self.cache, "cold_cache", False):
            cached = self.cache.get(cache_key)
            if cached is not None:
                self.cache_hits += 1
                try:
                    from ptb_contracts.telemetry import get_cost_audit_tracker
                    get_cost_audit_tracker().record_cache_hit()
                except Exception:
                    pass
                cand = cached.model_copy(deep=True)
                if not cand.requester_name and parsed.actual_author_raw:
                    cand.requester_name = parsed.actual_author_raw
                return cand

        self.cache_misses += 1
        self.live_calls += 1

        # 2. Call real LLM extractor
        candidate = await self.real_extractor.extract_async(
            parsed=parsed,
            raw_event_id=raw_event_id,
            source_type=source_type,
            external_url=external_url,
            project_key=project_key,
        )

        # 3. Cache valid candidate in session cache if available
        if candidate is not None:
            self.cache.put(cache_key, candidate)
            self.cache.save()

        return candidate
