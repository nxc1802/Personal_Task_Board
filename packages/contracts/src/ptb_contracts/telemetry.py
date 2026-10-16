"""Cost and Call Audit Telemetry Contract for Personal Task Board.

Tracks AI model invocations, token consumption, caching efficiency,
retry amplification, and estimated costs across all subsystems.
Enforces privacy: Never logs raw prompts, tokens, passwords, or personal content.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import threading
from typing import Any, Dict, Optional


# Standard cost rates per 1M tokens (USD)
ESTIMATED_RATES_PER_1M = {
    # LLM pricing (Input, Output)
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gemini-2.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30},
    "default_llm": {"input": 0.15, "output": 0.60},
    # Embedding pricing per 1M tokens (input only)
    "text-embedding-3-small": {"input": 0.02},
    "qwen3-embedding": {"input": 0.00},  # Local AI free
    "default_embedding": {"input": 0.02},
}


class CostAuditTracker:
    """Thread-safe singleton tracker for AI call telemetry and cost audit."""

    _instance: Optional[CostAuditTracker] = None
    _lock = threading.Lock()

    def __new__(cls) -> CostAuditTracker:
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init_tracker()
            return cls._instance

    def _init_tracker(self) -> None:
        self.lock = threading.Lock()
        self.started_at = datetime.now(timezone.utc)
        
        # LLM Metrics
        self.llm_requests_total: int = 0
        self.logical_extractions_total: int = 0
        self.total_http_attempts: int = 0
        self.llm_input_tokens: int = 0
        self.llm_output_tokens: int = 0
        self.llm_by_provider: Dict[str, int] = {}
        self.llm_by_model: Dict[str, int] = {}
        self.llm_by_subsystem: Dict[str, int] = {}

        # Cache Metrics
        self.cache_hits: int = 0
        self.cache_misses: int = 0

        # Embedding Metrics
        self.embedding_requests_total: int = 0
        self.embedding_inputs_total: int = 0
        self.embedding_skipped_unchanged: int = 0

        # Kev Metrics
        self.kev_requests_total: int = 0
        self.kev_by_type: Dict[str, int] = {}

        # Graphiti Metrics
        self.graphiti_episodes_total: int = 0
        self.graphiti_calls_total: int = 0

        # General Event Counter
        self.events_processed_total: int = 0

        # Structured Bypass Counter
        self.structured_bypass_total: int = 0

    def reset(self) -> None:
        """Reset all counters to zero (useful for testing and benchmark suites)."""
        with self.lock:
            self._init_tracker()

    def record_llm_call(
        self,
        provider: str = "openai",
        model: str = "gpt-4o-mini",
        subsystem: str = "extraction",
        input_tokens: int = 0,
        output_tokens: int = 0,
        attempts: int = 1,
        cached: bool = False,
    ) -> None:
        """Record an LLM logical call and its actual HTTP attempts and tokens."""
        with self.lock:
            self.logical_extractions_total += 1
            if cached:
                self.cache_hits += 1
                return

            self.cache_misses += 1
            self.llm_requests_total += 1
            self.total_http_attempts += max(1, attempts)
            self.llm_input_tokens += max(0, input_tokens)
            self.llm_output_tokens += max(0, output_tokens)

            p_key = provider.lower().strip() or "unknown"
            m_key = model.lower().strip() or "unknown"
            s_key = subsystem.lower().strip() or "general"

            self.llm_by_provider[p_key] = self.llm_by_provider.get(p_key, 0) + 1
            self.llm_by_model[m_key] = self.llm_by_model.get(m_key, 0) + 1
            self.llm_by_subsystem[s_key] = self.llm_by_subsystem.get(s_key, 0) + 1

    def record_cache_hit(self) -> None:
        """Record an extraction cache hit."""
        with self.lock:
            self.cache_hits += 1
            self.logical_extractions_total += 1

    def record_embedding_call(
        self,
        count: int = 1,
        is_batch: bool = False,
        skipped: bool = False,
        provider: str = "local_qwen3",
    ) -> None:
        """Record embedding generation or deduplication skip."""
        with self.lock:
            if skipped:
                self.embedding_skipped_unchanged += count
                return
            self.embedding_requests_total += 1
            self.embedding_inputs_total += count

    def record_kev_call(self, query_type: str = "rerank") -> None:
        """Record a Kev System 1 / Reranking request."""
        with self.lock:
            self.kev_requests_total += 1
            q_key = query_type.lower().strip()
            self.kev_by_type[q_key] = self.kev_by_type.get(q_key, 0) + 1

    def record_graphiti_sync(self, episodes: int = 1, calls: int = 1) -> None:
        """Record Graphiti episodic memory synchronization."""
        with self.lock:
            self.graphiti_episodes_total += episodes
            self.graphiti_calls_total += calls

    def record_event_processed(self, count: int = 1) -> None:
        """Record raw events processed."""
        with self.lock:
            self.events_processed_total += count

    def record_structured_bypass(self, count: int = 1) -> None:
        """Record structured source tasks created deterministically without LLM."""
        with self.lock:
            self.structured_bypass_total += count

    def get_metrics(self) -> Dict[str, Any]:
        """Compute and return unified telemetry snapshot."""
        with self.lock:
            total_cache_queries = self.cache_hits + self.cache_misses
            cache_hit_rate = (
                round(self.cache_hits / total_cache_queries, 4)
                if total_cache_queries > 0
                else 0.0
            )

            # Retry amplification: actual HTTP attempts / logical calls
            retry_amplification = (
                round(self.total_http_attempts / max(1, self.logical_extractions_total - self.cache_hits), 3)
                if (self.logical_extractions_total - self.cache_hits) > 0
                else 1.0
            )

            # Graphiti calls per episode
            graphiti_calls_per_episode = (
                round(self.graphiti_calls_total / max(1, self.graphiti_episodes_total), 2)
                if self.graphiti_episodes_total > 0
                else 0.0
            )

            # Estimated cost calculation
            in_cost = (self.llm_input_tokens / 1_000_000.0) * ESTIMATED_RATES_PER_1M["default_llm"]["input"]
            out_cost = (self.llm_output_tokens / 1_000_000.0) * ESTIMATED_RATES_PER_1M["default_llm"]["output"]
            total_llm_cost_usd = round(in_cost + out_cost, 6)

            cost_per_1000 = (
                round((total_llm_cost_usd / max(1, self.events_processed_total)) * 1000.0, 4)
                if self.events_processed_total > 0
                else 0.0
            )

            return {
                "started_at": self.started_at.isoformat(),
                "snapshot_at": datetime.now(timezone.utc).isoformat(),
                "llm": {
                    "requests_total": self.llm_requests_total,
                    "logical_extractions_total": self.logical_extractions_total,
                    "total_http_attempts": self.total_http_attempts,
                    "input_tokens": self.llm_input_tokens,
                    "output_tokens": self.llm_output_tokens,
                    "estimated_cost_usd": total_llm_cost_usd,
                    "by_provider": dict(self.llm_by_provider),
                    "by_model": dict(self.llm_by_model),
                    "by_subsystem": dict(self.llm_by_subsystem),
                },
                "cache": {
                    "hits": self.cache_hits,
                    "misses": self.cache_misses,
                    "hit_rate": cache_hit_rate,
                },
                "embedding": {
                    "requests_total": self.embedding_requests_total,
                    "inputs_total": self.embedding_inputs_total,
                    "skipped_unchanged": self.embedding_skipped_unchanged,
                    "avoided_ratio": round(
                        self.embedding_skipped_unchanged / max(1, self.embedding_skipped_unchanged + self.embedding_inputs_total),
                        4
                    ),
                },
                "kev": {
                    "requests_total": self.kev_requests_total,
                    "by_type": dict(self.kev_by_type),
                },
                "graphiti": {
                    "episodes_total": self.graphiti_episodes_total,
                    "calls_total": self.graphiti_calls_total,
                    "calls_per_episode": graphiti_calls_per_episode,
                },
                "efficiency": {
                    "events_processed": self.events_processed_total,
                    "structured_bypass_count": self.structured_bypass_total,
                    "retry_amplification": retry_amplification,
                    "cost_per_1000_events_usd": cost_per_1000,
                },
            }


def get_cost_audit_tracker() -> CostAuditTracker:
    """Return the global CostAuditTracker singleton."""
    return CostAuditTracker()
