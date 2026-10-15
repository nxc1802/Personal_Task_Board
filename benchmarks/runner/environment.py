"""Isolated Benchmark Execution Environment Manager.

Wires production PTB pipelines (L1-L4) with REAL components:
- Uses real LLMStructuredExtractor (never FakeDeterministicLLMExtractor)
- Uses independent BenchmarkInMemoryTaskRepository in fixture mode (never InMemoryTaskDomainRepository from test_doubles)
- In live mode: verifies live Neo4j and LLM availability; marks PENDING if unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import socket
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse
import urllib.request

from ptb_acquisition.pipeline import AcquisitionPipeline
from ptb_contracts.l1_acquisition import RawEventRecord
from ptb_intelligence.lifecycle import TaskIntelligenceLifecycle
from ptb_intelligence.priority import DeterministicPriorityEngine
from ptb_intelligence.status_machine import StatusInferenceMachine
from ptb_processing.extractor.heuristic_filter import HeuristicCandidateFilter
from ptb_processing.extractor.llm_extractor import LLMStructuredExtractor
from ptb_processing.identity.resolver import IdentityResolver
from ptb_processing.parsers.multi_channel import UnifiedChannelParser
from ptb_processing.pipeline import ProcessingPipeline
from ptb_processing.validator.attribution import AttributionValidator

from benchmarks.models import BenchmarkDataset
from benchmarks.runner.in_memory_domain_repo import (
    BenchmarkGraphMemorySink,
    BenchmarkInMemoryCheckpointRepository,
    BenchmarkInMemoryRawEventRepository,
    BenchmarkInMemoryTaskRepository,
)

logger = logging.getLogger("ptb.benchmarks.environment")


class BenchmarkEnvironmentError(RuntimeError):
    """Raised when an environment operation fails or unsafe target is configured."""


def probe_tcp_port(host: str, port: int, timeout: float = 0.5) -> bool:
    """Check whether a TCP port is open and accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def probe_live_llm_endpoint(base_url: Optional[str] = None, timeout: float = 1.0) -> bool:
    """Probe whether the configured LLM endpoint is reachable."""
    url = base_url or os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    if not url:
        return False
    try:
        clean_url = url.rstrip("/")
        # Try models endpoint
        probe_target = f"{clean_url}/models"
        req = urllib.request.Request(
            probe_target,
            headers={"User-Agent": "PTB-Benchmark-Harness"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in (200, 401, 403)
    except Exception:
        # Fallback probe to host:port if url has netloc
        try:
            parsed = urlparse(url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            return probe_tcp_port(host, port, timeout=timeout)
        except Exception:
            return False


@dataclass
class BenchmarkEnvironment:
    """Isolated runtime container holding repositories, pipelines, and configuration."""

    mode: str  # "fixture" or "live"
    dataset: Optional[BenchmarkDataset]
    raw_event_repo: Any
    checkpoint_repo: Any
    memory_sink: BenchmarkGraphMemorySink
    task_repo: Any
    identity_resolver: IdentityResolver
    llm_extractor: LLMStructuredExtractor
    heuristic_filter: HeuristicCandidateFilter
    priority_engine: DeterministicPriorityEngine
    status_machine: StatusInferenceMachine
    lifecycle: TaskIntelligenceLifecycle
    acquisition_pipeline: AcquisitionPipeline
    processing_pipeline: ProcessingPipeline
    live_infrastructure_available: bool = True
    infrastructure_error: Optional[str] = None
    events_by_id: Dict[str, RawEventRecord] = field(default_factory=dict)

    def reset(self) -> None:
        """Reset state for clean execution."""
        if hasattr(self.raw_event_repo, "clear"):
            self.raw_event_repo.clear()
        if hasattr(self.checkpoint_repo, "clear"):
            self.checkpoint_repo.clear()
        if hasattr(self.task_repo, "clear"):
            self.task_repo.clear()
        self.memory_sink.clear()
        if hasattr(self.acquisition_pipeline, "_internal_seen_keys"):
            self.acquisition_pipeline._internal_seen_keys.clear()


class EnvironmentManager:
    """Factory and manager for Benchmark Environments."""

    SAFE_BENCHMARK_PORTS = {7688, 17687, 27687}

    @classmethod
    def probe_infrastructure(cls) -> Tuple[bool, Optional[str]]:
        """Verify live Neo4j and LLM endpoint availability."""
        neo4j_uri = os.getenv("NEO4J_BENCHMARK_URI") or os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687")
        parsed = urlparse(neo4j_uri)
        neo4j_host = parsed.hostname or "127.0.0.1"
        neo4j_port = parsed.port or 7687

        neo4j_ok = probe_tcp_port(neo4j_host, neo4j_port, timeout=0.5)
        if not neo4j_ok:
            return False, f"Live Neo4j not reachable on {neo4j_host}:{neo4j_port}"

        llm_ok = probe_live_llm_endpoint(timeout=1.0)
        if not llm_ok:
            return False, "Live LLM endpoint not reachable"

        return True, None

    @classmethod
    def create_environment(
        cls,
        dataset: Optional[BenchmarkDataset] = None,
        mode: str = "fixture",
    ) -> BenchmarkEnvironment:
        """Build a fresh BenchmarkEnvironment wired to REAL production extractors and pipelines."""
        events_by_id: Dict[str, RawEventRecord] = {}
        if dataset is not None:
            for s in dataset.samples:
                events_by_id[s.raw_event.id] = s.raw_event

        live_available = True
        infra_error = None

        if mode == "live":
            live_available, infra_error = cls.probe_infrastructure()

        # In fixture mode (or fallback when live unavailable), use independent benchmark repositories
        raw_event_repo = BenchmarkInMemoryRawEventRepository()
        checkpoint_repo = BenchmarkInMemoryCheckpointRepository()
        memory_sink = BenchmarkGraphMemorySink()
        task_repo = BenchmarkInMemoryTaskRepository()

        # Initialize identity resolver with personas
        identity_resolver = IdentityResolver()
        if dataset is not None and dataset.manifest and dataset.manifest.personas:
            for p in dataset.manifest.personas:
                for acc in (p.account_ids or [None]):
                    identity_resolver.register_person(
                        person_id=p.canonical_id,
                        canonical_name=p.canonical_name,
                        primary_email=p.email,
                        account_id=acc,
                        tenant_id=p.tenant_id,
                        aliases=p.aliases,
                    )
                identity_resolver.register_person(
                    person_id=p.canonical_id,
                    canonical_name=p.canonical_name,
                    primary_email=p.email,
                    tenant_id=p.tenant_id,
                    aliases=p.aliases,
                )

        # REAL Production Extractor: NEVER FakeDeterministicLLMExtractor
        real_llm_extractor = LLMStructuredExtractor()
        from benchmarks.runner.extraction_cache import CachedLLMExtractor
        llm_extractor = CachedLLMExtractor(real_llm_extractor)
        heuristic_filter = HeuristicCandidateFilter()

        # Priority & Status engines
        config_path = Path(__file__).resolve().parent.parent.parent / "config" / "priority.yaml"
        priority_engine = (
            DeterministicPriorityEngine(config_path=config_path)
            if config_path.is_file()
            else DeterministicPriorityEngine()
        )
        status_machine = StatusInferenceMachine()
        lifecycle = TaskIntelligenceLifecycle(
            task_repo=task_repo,
            priority_engine=priority_engine,
            status_machine=status_machine,
        )

        tenant_id = (
            dataset.manifest.personas[0].tenant_id
            if dataset and dataset.manifest.personas
            else "tenant-fpt-internal"
        )
        acquisition_pipeline = AcquisitionPipeline(
            raw_event_repo=raw_event_repo,
            checkpoint_repo=checkpoint_repo,
            tenant_id=tenant_id,
        )

        processing_pipeline = ProcessingPipeline(
            task_repo=task_repo,
            identity_resolver=identity_resolver,
            heuristic_filter=heuristic_filter,
            llm_extractor=llm_extractor,
            attribution_validator=AttributionValidator(),
            intelligence_lifecycle=lifecycle,
        )

        return BenchmarkEnvironment(
            mode=mode,
            dataset=dataset,
            raw_event_repo=raw_event_repo,
            checkpoint_repo=checkpoint_repo,
            memory_sink=memory_sink,
            task_repo=task_repo,
            identity_resolver=identity_resolver,
            llm_extractor=llm_extractor,
            heuristic_filter=heuristic_filter,
            priority_engine=priority_engine,
            status_machine=status_machine,
            lifecycle=lifecycle,
            acquisition_pipeline=acquisition_pipeline,
            processing_pipeline=processing_pipeline,
            live_infrastructure_available=live_available,
            infrastructure_error=infra_error,
            events_by_id=events_by_id,
        )
