"""Production LLM extraction cache.

Stores task extraction results only under production runtime data; never bootstraps
from benchmark fixture files. Uses content/model/prompt-version SHA-256 keys.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Dict, Optional

from ptb_contracts.l2_processing import UnifiedTaskCandidate

logger = logging.getLogger("ptb.processing.extractor.cache")

# File: <repo>/services/processing/src/ptb_processing/extractor/cache.py
# parents[5] is <repo>; parents[4] is services/ and MUST NOT hold runtime data.
# /app/data is persisted by docker-compose.prod.yml (./data:/app/data).
DEFAULT_PROD_CACHE_FILE = (
    Path(__file__).resolve().parents[5] / "data" / "cache" / "extraction_cache.json"
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
        configured_path = os.getenv("PTB_EXTRACTION_CACHE_FILE", "").strip()
        self.cache_file = Path(cache_file) if cache_file is not None else (
            Path(configured_path).expanduser() if configured_path else DEFAULT_PROD_CACHE_FILE
        )
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
        """Load only production cache; never reuse committed benchmark fixtures."""
        with self._lock:
            if not self.cache_file.is_file():
                return
            try:
                data = json.loads(self.cache_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._entries = data
                    logger.info(
                        "Loaded %d production extraction cache entries from %s",
                        len(self._entries), self.cache_file,
                    )
            except Exception as exc:
                logger.warning(
                    "Failed reading extraction cache from %s: %s",
                    self.cache_file, exc,
                )

    def _save_unlocked(self) -> None:
        """Write atomically to avoid partial cache JSON on an interrupted save."""
        temp_path: Optional[Path] = None
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.cache_file.parent,
                prefix=".ptb-extraction-", suffix=".tmp", delete=False,
            ) as tmp:
                temp_path = Path(tmp.name)
                json.dump(self._entries, tmp, indent=2, ensure_ascii=False)
                tmp.write("\n")
            try:
                os.chmod(temp_path, 0o600)
            except OSError as perm_error:
                logger.warning(
                    "Unable to restrict extraction cache file permissions: %s", perm_error
                )
            os.replace(temp_path, self.cache_file)
            temp_path = None
        except Exception as exc:
            logger.warning(
                "Failed to save extraction cache to %s: %s",
                self.cache_file, exc,
            )
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def save(self) -> None:
        with self._lock:
            self._save_unlocked()

    def get(
        self,
        cache_key: str,
        alias_key: Optional[str] = None,
    ) -> Optional[UnifiedTaskCandidate]:
        """Look up cached candidate by content hash key or secondary alias (raw_event_id).
        Guarantees correctness by invalidating alias lookups when content hash differs (P1.3).
        """
        with self._lock:
            raw_data = self._entries.get(cache_key)
            if raw_data is None and alias_key:
                candidate_alias_data = self._entries.get(alias_key)
                if candidate_alias_data is not None:
                    expected_hash = candidate_alias_data.get("_cached_content_hash")
                    is_sha256 = len(cache_key) == 64 and all(c in "0123456789abcdefABCDEF" for c in cache_key)
                    # An unbound legacy alias is not safe for a content-hash lookup.
                    if is_sha256 and expected_hash != cache_key:
                        logger.info(
                            "Cache entry for alias %s invalidated: content hash mismatch (%s != %s)",
                            alias_key, str(expected_hash)[:8], cache_key[:8]
                        )
                        raw_data = None
                    else:
                        raw_data = candidate_alias_data

            if raw_data is not None:
                try:
                    clean_data = {k: v for k, v in raw_data.items() if not k.startswith("_cached_")}
                    return UnifiedTaskCandidate.model_validate(clean_data)
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
        """Store candidate in cache under content hash key and optional alias with content hash binding."""
        dumped = candidate.model_dump(mode="json")
        with self._lock:
            self._entries[cache_key] = dumped
            if alias_key:
                alias_entry = dict(dumped)
                alias_entry["_cached_content_hash"] = cache_key
                self._entries[alias_key] = alias_entry
            self._save_unlocked()

    def count(self) -> int:
        with self._lock:
            return len(self._entries)

    def contains(self, key: str) -> bool:
        with self._lock:
            return key in self._entries
