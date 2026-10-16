"""Production & Benchmark Extraction Cache Manager.

Keyed by sha256(content:model:prompt_version) to prevent duplicate LLM calls
on identical text inputs across production operations, replays, and benchmark suites.
Ensures privacy: Never exposes credentials or personal tokens.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import threading
from typing import Any, Dict, Optional

from ptb_contracts.l2_processing import UnifiedTaskCandidate

logger = logging.getLogger("ptb.processing.extractor.cache")

# Default production cache location
DEFAULT_PROD_CACHE_FILE = (
    Path(__file__).resolve().parents[4] / "data" / "cache" / "extraction_cache.json"
)

# Benchmark pre-warmed cache location
BENCHMARK_CACHE_FILE = (
    Path(__file__).resolve().parents[4]
    / "benchmarks"
    / "runner"
    / ".cache"
    / "extraction_cache.json"
)


def compute_extraction_cache_key(
    content: str,
    model: str,
    prompt_version: str = "v1.0",
) -> str:
    """Compute deterministic SHA-256 cache key based on content, model name, and prompt version."""
    clean_content = content.strip()
    clean_model = model.strip().lower()
    raw = f"{clean_content}:{clean_model}:{prompt_version}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class ExtractionCacheManager:
    """Persistent thread-safe extraction cache manager.

    Keyed by sha256(content:model:prompt_version).
    Supports backward-compatible fallback lookup by raw_event_id / sample_id.
    """

    _instance: Optional[ExtractionCacheManager] = None
    _singleton_lock = threading.Lock()

    def __init__(self, cache_file: Optional[Path] = None) -> None:
        self.cache_file = cache_file or DEFAULT_PROD_CACHE_FILE
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._load()

    @classmethod
    def get_instance(cls, cache_file: Optional[Path] = None) -> ExtractionCacheManager:
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = cls(cache_file)
            return cls._instance

    def _load(self) -> None:
        """Load cache entries from primary file, or bootstrap from benchmark cache if empty."""
        with self._lock:
            if self.cache_file.is_file():
                try:
                    data = json.loads(self.cache_file.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        self._entries = data
                        logger.info("Loaded %d extraction cache entries from %s", len(self._entries), self.cache_file)
                        return
                except Exception as e:
                    logger.warning("Failed reading extraction cache from %s: %s", self.cache_file, e)

            # Bootstrap from benchmark cache if available
            if BENCHMARK_CACHE_FILE.is_file():
                try:
                    bench_data = json.loads(BENCHMARK_CACHE_FILE.read_text(encoding="utf-8"))
                    if isinstance(bench_data, dict):
                        self._entries = bench_data
                        logger.info("Bootstrapped %d extraction cache entries from benchmark cache %s", len(self._entries), BENCHMARK_CACHE_FILE)
                        # Save to production location
                        self._save_unlocked()
                except Exception as e:
                    logger.warning("Failed bootstrapping from benchmark cache: %s", e)

    def _save_unlocked(self) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(
                json.dumps(self._entries, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Failed to save extraction cache to %s: %s", self.cache_file, e)

    def save(self) -> None:
        with self._lock:
            self._save_unlocked()

    def get(
        self,
        cache_key: str,
        alias_key: Optional[str] = None,
    ) -> Optional[UnifiedTaskCandidate]:
        """Look up cached candidate by content hash key or secondary alias (raw_event_id)."""
        with self._lock:
            raw_data = self._entries.get(cache_key)
            if raw_data is None and alias_key:
                raw_data = self._entries.get(alias_key)

            if raw_data is not None:
                try:
                    return UnifiedTaskCandidate.model_validate(raw_data)
                except Exception as e:
                    logger.warning("Corrupted extraction cache entry for key %s: %s", cache_key, e)
                    return None
            return None

    def put(
        self,
        cache_key: str,
        candidate: UnifiedTaskCandidate,
        alias_key: Optional[str] = None,
    ) -> None:
        """Store candidate in cache under content hash key and optional alias."""
        dumped = candidate.model_dump(mode="json")
        with self._lock:
            self._entries[cache_key] = dumped
            if alias_key:
                self._entries[alias_key] = dumped
            self._save_unlocked()

    def count(self) -> int:
        with self._lock:
            return len(self._entries)

    def contains(self, key: str) -> bool:
        with self._lock:
            return key in self._entries
