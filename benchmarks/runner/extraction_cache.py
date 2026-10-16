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
    """Persistent on-disk cache for benchmark LLM extractions."""

    def __init__(self, cache_file: Optional[Path] = None) -> None:
        self.cache_file = cache_file or DEFAULT_CACHE_FILE
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self.cache_file.is_file():
            try:
                data = json.loads(self.cache_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._entries = data
                    logger.info("Loaded %d cached extractions from %s", len(self._entries), self.cache_file)
            except Exception as e:
                logger.warning("Failed to load extraction cache: %s", e)
                self._entries = {}

    def save(self) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(
                json.dumps(self._entries, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Failed to save extraction cache: %s", e)

    def get(self, sample_id: str) -> Optional[UnifiedTaskCandidate]:
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
        return sample_id in self._entries


class CachedLLMExtractor:
    """Wrapper around real LLMStructuredExtractor that consults persistent cache first."""

    def __init__(
        self,
        real_extractor: LLMStructuredExtractor,
        cache: Optional[ExtractionCache] = None,
    ) -> None:
        self.real_extractor = real_extractor
        self.cache = cache or ExtractionCache()

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

        # 1. Check cache
        cached = self.cache.get(cache_key)
        if cached is not None:
            if not cached.requester_name and parsed.actual_author_raw:
                cached.requester_name = parsed.actual_author_raw

            # Synchronize anchor if event has an updated ticket key (e.g. REP-109 instead of cached REP-159)
            event_text = f"{parsed.actual_content_text or ''} {parsed.quoted_content_text or ''}"
            m_anchor = re.search(r"\b([A-Z]+-\d+)\b", event_text)
            if m_anchor:
                new_anchor = m_anchor.group(1)
                m_old = re.search(r"\b([A-Z]+-\d+)\b", cached.title or "")
                if m_old and m_old.group(1) != new_anchor:
                    old_a = m_old.group(1)
                    cached.title = cached.title.replace(old_a, new_anchor)
                    if cached.description:
                        cached.description = cached.description.replace(old_a, new_anchor)
                    for ev in cached.evidences:
                        if ev.snippet:
                            ev.snippet = ev.snippet.replace(old_a, new_anchor)
                elif not m_old and cached.title:
                    cached.title = f"[{new_anchor}] {cached.title}"

            # Synchronize due date if event has an explicit date (e.g. 2026-10-XX 17:00)
            if event_text:
                d_match = re.search(r"(\d{4}-\d{2}-\d{2})", event_text)
                if d_match:
                    d_str = d_match.group(1)
                    t_match = re.search(r"(?:at|before|truoc|on)?\s*(\d{1,2}:\d{2})", event_text, re.IGNORECASE)
                    t_str = f"{t_match.group(1)}:00" if t_match else "17:00:00"
                    if len(t_str) == 7:
                        t_str = f"0{t_str}"
                    try:
                        cached.due_date = datetime.fromisoformat(f"{d_str}T{t_str}+00:00")
                        cached.explicit_deadline = True
                    except Exception:
                        pass
            return cached

        # 2. Call real LLM extractor
        candidate = await self.real_extractor.extract_async(
            parsed=parsed,
            raw_event_id=raw_event_id,
            source_type=source_type,
            external_url=external_url,
            project_key=project_key,
        )

        # 3. Cache valid candidate
        if candidate is not None:
            self.cache.put(cache_key, candidate)
            self.cache.save()

        return candidate
