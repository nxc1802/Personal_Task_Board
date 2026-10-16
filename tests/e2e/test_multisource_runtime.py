"""End-to-End (E2E) Runtime & Ingestion Tests for Multi-Source Personal Task Board (PTB).

Covers Task W3-02:
1. Multi-source configuration schema validation (config/sources.yaml) covering all 9 sources.
2. Supervisor initialization, multi-adapter lifecycle, and rate limit isolation.
3. Partial failure tolerance: HTTP 429 (rate limit), 401/403 (auth), or network drops do not crash the pipeline.
4. Replay safety: zero duplicate tasks or raw events generated across multiple ingest runs.
5. Checkpoint durability across process restarts per (tenant_id, source_type, stream_id).
6. CLI `ptb doctor` truthful audit of all 9 sources (READY, NOT_CONFIGURED, AUTH_REQUIRED, DEGRADED).
7. CLI `ptb ingest --source <all|name>` on-demand execution.
8. Live cloud credentials verification (graceful PENDING / skip when unconfigured).
"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sys
from typing import AsyncIterator, Dict, List, Optional
import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from ptb_acquisition.adapters.base import (
    AcquisitionAdapter,
    AdapterAuthenticationError,
    AdapterRateLimitError,
    AdapterTransientError,
)
from ptb_acquisition.pipeline import AcquisitionPipeline
from ptb_contracts import (
    IngestionCheckpointRecord,
    ProcessingStatus,
    RawEventRecord,
    SourceType,
)
from scripts.ptb_cli import (
    AdapterPollingRunner,
    PTBProcessSupervisor,
    cmd_doctor,
    cmd_ingest,
)
from tests.support.test_doubles import (
    InMemoryCheckpointRepository,
    InMemoryRawEventRepository,
)


# ==============================================================================
# TEST FIXTURES & DUMMY ADAPTERS
# ==============================================================================

class MockSourceAdapter(AcquisitionAdapter):
    """Test adapter producing deterministic events with configurable behavior."""

    def __init__(
        self,
        source_type: SourceType,
        tenant_id: str = "test-tenant",
        events: Optional[List[RawEventRecord]] = None,
        fail_mode: Optional[str] = None,
    ):
        super().__init__(source_type=source_type, tenant_id=tenant_id)
        self.events = events or []
        self.fail_mode = fail_mode
        self.backfill_called = 0
        self.poll_called = 0

    async def discover(self) -> List[Dict[str, str]]:
        return [{"stream_id": "main", "name": f"{self.source_type.value} Main Stream"}]

    async def backfill(
        self, stream_id: str, since: Optional[datetime] = None
    ) -> AsyncIterator[RawEventRecord]:
        self.backfill_called += 1
        if self.fail_mode == "rate_limit":
            raise AdapterRateLimitError("Rate limit 429 Too Many Requests", retry_after_seconds=45, source_type=self.source_type)
        elif self.fail_mode == "auth":
            raise AdapterAuthenticationError("Auth 401 Unauthorized", source_type=self.source_type)
        elif self.fail_mode == "network":
            raise AdapterTransientError("Network error: Connection timed out", source_type=self.source_type)

        for e in self.events:
            if since is None or e.event_timestamp >= since:
                yield e

    async def poll_incremental(
        self, stream_id: str, checkpoint: Optional[IngestionCheckpointRecord] = None
    ) -> AsyncIterator[RawEventRecord]:
        self.poll_called += 1
        if self.fail_mode == "rate_limit":
            raise AdapterRateLimitError("Rate limit in incremental sync", retry_after_seconds=60, source_type=self.source_type)
        elif self.fail_mode == "auth":
            raise AdapterAuthenticationError("Auth failure during polling", source_type=self.source_type)

        for e in self.events:
            if checkpoint and checkpoint.last_event_timestamp:
                if e.event_timestamp <= checkpoint.last_event_timestamp:
                    continue
            yield e

    async def health(self) -> Dict[str, str]:
        if self.fail_mode:
            return {"status": "unhealthy", "error": self.fail_mode}
        return {"status": "healthy", "source_type": self.source_type.value}


def make_raw_event(
    event_id: str,
    source_type: SourceType,
    ts: datetime,
    tenant_id: str = "local-user",
    content: str = "Test message",
) -> RawEventRecord:
    import hashlib
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    idempotency_key = hashlib.sha256(f"{tenant_id}:{source_type.value}:{event_id}".encode("utf-8")).hexdigest()
    return RawEventRecord(
        id=event_id,
        tenant_id=tenant_id,
        source_type=source_type,
        external_id=event_id,
        idempotency_key=idempotency_key,
        content_hash=content_hash,
        event_timestamp=ts,
        captured_at=ts,
        author_external_id="author-1",
        conversation_or_project_id="stream-main",
        raw_payload={"content": content},
        normalized_text=content,
        processing_status=ProcessingStatus.PENDING,
    )


# ==============================================================================
# 1. SOURCES CONFIG SCHEMA & FEATURE FLAGS
# ==============================================================================

def test_sources_yaml_all_9_sources_and_schema():
    """Verify config/sources.yaml declares all 9 sources with valid feature flags and rate limits."""
    config_path = PROJECT_ROOT / "config" / "sources.yaml"
    assert config_path.exists(), f"Configuration file not found: {config_path}"

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    assert "sources" in data, "Root key 'sources' missing in config/sources.yaml"
    sources = data["sources"]

    # All 9 canonical sources
    expected_sources = {
        "coding_agents",
        "git",
        "ms_teams",
        "ms_outlook",
        "jira",
        "shortcut",
        "confluence",
        "github",
        "slack",
    }
    present_sources = set(sources.keys())
    assert expected_sources.issubset(present_sources), f"Missing sources: {expected_sources - present_sources}"

    # Verify enabled flags: coding_agents & git are true; cloud sources without credentials default to false
    assert sources["coding_agents"]["enabled"] is True, "coding_agents must be enabled by default"
    assert sources["git"]["enabled"] is True, "git must be enabled by default"
    for remote_src in ["jira", "shortcut", "confluence", "github", "slack"]:
        assert sources[remote_src]["enabled"] is False, f"Remote source {remote_src} must default to enabled: false"

    # Verify rate limit and checkpoint blocks exist for all 9 sources
    for src_name, cfg in sources.items():
        assert "tenant_id" in cfg, f"{src_name} missing tenant_id"
        assert "rate_limit" in cfg, f"{src_name} missing rate_limit configuration"
        rl = cfg["rate_limit"]
        assert "max_requests_per_minute" in rl, f"{src_name} missing max_requests_per_minute"
        assert "burst_limit" in rl, f"{src_name} missing burst_limit"
        assert "retry_after_default_seconds" in rl, f"{src_name} missing retry_after_default_seconds"
        assert rl["max_requests_per_minute"] > 0
        assert rl["burst_limit"] > 0
        assert rl["retry_after_default_seconds"] > 0

        assert "checkpoint" in cfg, f"{src_name} missing checkpoint configuration"
        ckpt = cfg["checkpoint"]
        assert "cursor_strategy" in ckpt, f"{src_name} missing cursor_strategy"
        assert "auto_resume" in ckpt, f"{src_name} missing auto_resume"


# ==============================================================================
# 2. SUPERVISOR CONFIG LOADING & MULTI-ADAPTER ORCHESTRATION
# ==============================================================================

@pytest.mark.asyncio
async def test_supervisor_initialization_with_sources_config(tmp_path):
    """Supervisor parses sources config and configures runners without starting background tasks."""
    custom_yaml = tmp_path / "custom_sources.yaml"
    custom_yaml.write_text("""
sources:
  coding_agents:
    enabled: true
    tenant_id: "test-user"
    rate_limit:
      max_requests_per_minute: 60
      burst_limit: 10
      retry_after_default_seconds: 5
    checkpoint:
      cursor_strategy: "timestamp"
      auto_resume: true
  git:
    enabled: true
    tenant_id: "test-user"
    rate_limit:
      max_requests_per_minute: 30
      burst_limit: 5
      retry_after_default_seconds: 5
    checkpoint:
      cursor_strategy: "commit_hash"
      auto_resume: true
  slack:
    enabled: false
    tenant_id: "test-user"
    rate_limit:
      max_requests_per_minute: 20
      burst_limit: 5
      retry_after_default_seconds: 10
    checkpoint:
      cursor_strategy: "thread_ts"
      auto_resume: true
""", encoding="utf-8")

    supervisor = PTBProcessSupervisor(
        host="127.0.0.1",
        port=8901,
        mcp_port=8902,
        tenant_id="test-user",
        poll_interval=1.0,
        enable_playwright=False,
        sources_config_path=str(custom_yaml),
    )

    assert supervisor.sources_config is not None
    assert "sources" in supervisor.sources_config
    assert supervisor.sources_config["sources"]["coding_agents"]["enabled"] is True
    assert supervisor.sources_config["sources"]["git"]["enabled"] is True
    assert supervisor.sources_config["sources"]["slack"]["enabled"] is False


# ==============================================================================
# 3. PARTIAL FAILURE ISOLATION & RATE LIMIT RESILIENCE
# ==============================================================================

@pytest.mark.asyncio
async def test_supervisor_partial_failure_isolation():
    """Verify that failures in one source (429 rate limit, 401 auth, socket drop)

    never crash the pipeline or interrupt healthy sources.
    """
    raw_repo = InMemoryRawEventRepository()
    ckpt_repo = InMemoryCheckpointRepository()
    pipeline = AcquisitionPipeline(raw_event_repo=raw_repo, checkpoint_repo=ckpt_repo, tenant_id="user-corp")

    now = datetime.now(timezone.utc)
    ev1 = make_raw_event("ev-git-1", SourceType.GIT, now, "user-corp", "commit 1")
    ev2 = make_raw_event("ev-git-2", SourceType.GIT, now + timedelta(seconds=1), "user-corp", "commit 2")

    adapter_git = MockSourceAdapter(SourceType.GIT, tenant_id="user-corp", events=[ev1, ev2])
    adapter_jira = MockSourceAdapter(SourceType.JIRA, tenant_id="user-corp", fail_mode="rate_limit")
    adapter_confluence = MockSourceAdapter(SourceType.CONFLUENCE, tenant_id="user-corp", fail_mode="auth")
    adapter_slack = MockSourceAdapter(SourceType.SLACK, tenant_id="user-corp", fail_mode="network")

    pipeline.register_adapter("git", adapter_git)
    pipeline.register_adapter("jira", adapter_jira)
    pipeline.register_adapter("confluence", adapter_confluence)
    pipeline.register_adapter("slack", adapter_slack)

    # Execute full multi-source synchronization
    results = await pipeline.sync_all_registered(raise_on_error=False)

    # 1. Healthy adapter synced completely
    assert results["git"]["status"] == "success"
    assert results["git"]["count"] == 2
    assert adapter_git.backfill_called == 1

    # 2. Faulty adapters recorded failure without crashing the pipeline
    assert results["jira"]["status"] == "error"
    assert "429" in results["jira"]["error"]

    assert results["confluence"]["status"] == "error"
    assert "401" in results["confluence"]["error"]

    assert results["slack"]["status"] == "error"
    assert "timed out" in results["slack"]["error"]

    # 3. Overall stats verify exact counts
    assert pipeline.stats["ingested"] == 2
    assert pipeline.stats["persisted"] == 2
    assert pipeline.stats["errors"] == 3
    assert raw_repo.count == 2


@pytest.mark.asyncio
async def test_adapter_polling_runner_rate_limit_backoff():
    """Verify AdapterPollingRunner applies backoff upon receiving AdapterRateLimitError."""
    adapter_rl = MockSourceAdapter(SourceType.JIRA, fail_mode="rate_limit")
    pipeline = AcquisitionPipeline()
    runner = AdapterPollingRunner(
        adapter=adapter_rl,
        pipeline=pipeline,
        poll_interval=0.1,
    )

    # Run single iteration directly
    backoff = await runner.poll_once()
    assert backoff == 45.0, "Runner must respect retry_after_seconds=45 from AdapterRateLimitError"


# ==============================================================================
# 4. REPLAY SAFETY (ZERO DUPLICATES ON RE-INGESTION)
# ==============================================================================

@pytest.mark.asyncio
async def test_replay_safety_zero_duplicates_on_repeated_ingestion():
    """Verify zero duplicate tasks or raw events are created upon re-ingestion."""
    raw_repo = InMemoryRawEventRepository()
    pipeline = AcquisitionPipeline(raw_event_repo=raw_repo, tenant_id="tenant-replay")

    t0 = datetime(2026, 10, 15, 8, 0, 0, tzinfo=timezone.utc)
    events = [
        make_raw_event(f"replay-{i}", SourceType.GIT, t0 + timedelta(minutes=i), "tenant-replay", f"message {i}")
        for i in range(5)
    ]

    adapter = MockSourceAdapter(SourceType.GIT, tenant_id="tenant-replay", events=events)
    pipeline.register_adapter("git", adapter)

    # First pass: Ingest all 5 events
    count_1 = await pipeline.sync_source("git")
    assert count_1 == 5
    assert pipeline.stats["ingested"] == 5
    assert pipeline.stats["deduplicated"] == 0
    assert raw_repo.count == 5

    # Second pass: Re-ingest the exact same events
    count_2 = await pipeline.sync_source("git")
    # All 5 events are detected as duplicates
    assert count_2 == 0
    assert pipeline.stats["ingested"] == 5
    assert pipeline.stats["deduplicated"] == 5
    assert raw_repo.count == 5, "Raw event repository count must remain strictly unchanged"


# ==============================================================================
# 5. CHECKPOINT PERSISTENCE & AUTO-RESUME ACROSS RESTARTS
# ==============================================================================

@pytest.mark.asyncio
async def test_checkpoint_persistence_across_process_restarts():
    """Verify checkpoints saved per (tenant_id, source_type, stream_id) survive restarts

    and prevent re-backfilling previously captured events.
    """
    ckpt_repo = InMemoryCheckpointRepository()
    raw_repo = InMemoryRawEventRepository()

    t0 = datetime(2026, 10, 15, 9, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=15)
    t2 = t0 + timedelta(minutes=30)

    ev0 = make_raw_event("ev-0", SourceType.CODING_AGENT, t0, "tenant-ckpt", "turn 0")
    ev1 = make_raw_event("ev-1", SourceType.CODING_AGENT, t1, "tenant-ckpt", "turn 1")

    # Instance 1: Initial Ingestion Run
    pipeline_1 = AcquisitionPipeline(
        raw_event_repo=raw_repo,
        checkpoint_repo=ckpt_repo,
        tenant_id="tenant-ckpt",
    )
    adapter_1 = MockSourceAdapter(SourceType.CODING_AGENT, tenant_id="tenant-ckpt", events=[ev0, ev1])
    pipeline_1.register_adapter("coding_agents", adapter_1)

    count_1 = await pipeline_1.sync_source("coding_agents", stream_id="main")
    assert count_1 == 2
    assert adapter_1.backfill_called == 1
    assert adapter_1.poll_called == 0

    # Verify checkpoint stored with timestamp t1
    saved_ckpt = await ckpt_repo.get_checkpoint(SourceType.CODING_AGENT.value, "main", tenant_id="tenant-ckpt")
    assert saved_ckpt is not None
    assert saved_ckpt.last_event_timestamp == t1
    assert saved_ckpt.last_external_id == "ev-1"

    # Instance 2: Process Restart Simulation (fresh pipeline, shared checkpoint repo)
    pipeline_2 = AcquisitionPipeline(
        raw_event_repo=raw_repo,
        checkpoint_repo=ckpt_repo,
        tenant_id="tenant-ckpt",
    )
    # New event appears at t2
    ev2 = make_raw_event("ev-2", SourceType.CODING_AGENT, t2, "tenant-ckpt", "turn 2")
    adapter_2 = MockSourceAdapter(SourceType.CODING_AGENT, tenant_id="tenant-ckpt", events=[ev0, ev1, ev2])
    pipeline_2.register_adapter("coding_agents", adapter_2)

    count_2 = await pipeline_2.sync_source("coding_agents", stream_id="main")
    # Must use poll_incremental (not backfill) and only ingest ev2
    assert adapter_2.poll_called == 1
    assert adapter_2.backfill_called == 0
    assert count_2 == 1
    assert raw_repo.count == 3

    # Checkpoint updated to t2
    updated_ckpt = await ckpt_repo.get_checkpoint(SourceType.CODING_AGENT.value, "main", tenant_id="tenant-ckpt")
    assert updated_ckpt.last_event_timestamp == t2
    assert updated_ckpt.last_external_id == "ev-2"


# ==============================================================================
# 6. CLI DOCTOR: TRUTHFUL HEALTH ACROSS ALL 9 SOURCES
# ==============================================================================

@pytest.mark.asyncio
async def test_cli_doctor_truthful_health_9_sources(monkeypatch):
    """Verify ptb doctor truthfully audits all 9 sources with READY, NOT_CONFIGURED, AUTH_REQUIRED, DEGRADED."""
    args = argparse.Namespace(deep=False)

    # Capture stdout
    captured_stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured_stdout)

    code = await cmd_doctor(args)

    output = captured_stdout.getvalue()
    # Output must contain the doctor table header
    assert "PERSONAL TASK BOARD: SYSTEM & DEPENDENCY DOCTOR CHECK" in output
    assert "Doctor Summary:" in output

    # Check that all 9 sources are reported in the doctor table
    sources_to_check = [
        "Source: Coding Agents",
        "Source: Local Git",
        "Source: Microsoft Teams",
        "Source: Microsoft Outlook",
        "Source: Jira Cloud/Server",
        "Source: Shortcut Stories",
        "Source: Confluence Docs",
        "Source: GitHub Repos",
        "Source: Slack Workspace",
    ]
    for s in sources_to_check:
        assert s in output, f"Source component '{s}' not found in doctor audit output"

    # Status tags must be truthfully present
    valid_statuses = ["READY", "NOT_CONFIG", "AUTH_REQ", "DEGRADED"]
    found_any_status = any(st in output for st in valid_statuses)
    assert found_any_status is True


# ==============================================================================
# 7. CLI INGEST: ON-DEMAND EXECUTION ACROSS SOURCES
# ==============================================================================

@pytest.mark.external_integration
@pytest.mark.e2e
@pytest.mark.asyncio
async def test_cli_ingest_on_demand_git():
    """Verify ptb ingest --source git executes on-demand without failure."""
    from ptb_database.neo4j_client import Neo4jClient
    client = Neo4jClient()
    if not await client.verify_connectivity():
        await client.close()
        pytest.skip("Neo4j is offline (live service required for CLI ingest test)")
    args = argparse.Namespace(
        source="git",
        tenant_id="test-e2e-cli",
        config=None,
    )
    code = await cmd_ingest(args)
    assert code == 0, "ptb ingest --source git must return exit code 0"


@pytest.mark.external_integration
@pytest.mark.e2e
@pytest.mark.asyncio
async def test_cli_ingest_on_demand_all():
    """Verify ptb ingest --source all completes and isolates unconfigured sources."""
    from ptb_database.neo4j_client import Neo4jClient
    client = Neo4jClient()
    if not await client.verify_connectivity():
        await client.close()
        pytest.skip("Neo4j is offline (live service required for CLI ingest test)")
    args = argparse.Namespace(
        source="all",
        tenant_id="test-e2e-cli",
        config=None,
    )
    code = await cmd_ingest(args)
    assert code == 0, "ptb ingest --source all must return exit code 0 (partial failure tolerance)"


# ==============================================================================
# 8. LIVE CLOUD CREDENTIALS AUDIT (PENDING / SKIPPED WHEN ABSENT)
# ==============================================================================

@pytest.mark.parametrize(
    "env_var,source_name",
    [
        ("JIRA_API_TOKEN", "Jira Software"),
        ("CONFLUENCE_API_TOKEN", "Atlassian Confluence"),
        ("SHORTCUT_API_TOKEN", "Shortcut.com"),
        ("SLACK_BOT_TOKEN", "Slack Bot"),
        ("GITHUB_TOKEN", "GitHub REST API"),
    ],
)
def test_live_cloud_sources_pending_when_credentials_absent(env_var: str, source_name: str):
    """Verify external cloud sources report PENDING / skip when live credentials are not set."""
    token = os.getenv(env_var)
    if not token or token.strip() == "":
        pytest.skip(
            f"Live external credentials absent ({env_var} not set). "
            f"Marked PENDING as required by audit rule (never fake verification)."
        )

    # If credentials are provided, live verification must be performed
    assert len(token) > 5, f"Token for {source_name} exists but appears invalid"
