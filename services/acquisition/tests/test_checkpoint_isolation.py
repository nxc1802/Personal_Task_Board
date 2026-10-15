"""Tests for Checkpoint Isolation and Multi-Source Acquisition Contracts (Task W1-03).

Validates:
1. Checkpoint create/read/update per source stream
2. Checkpoint isolation between tenants (no leakage, independent progression)
3. Restart resumes correct checkpoint for designated tenant and stream
4. Deterministic checkpoint ID calculation (SHA-256)
5. Checkpoint DOES NOT advance on failed ingestion (Persist-First durability)
6. Re-fetch / replay produces 0 duplicate events when data is unchanged
7. Adapter health reports NOT_CONFIGURED / unconfigured when credentials are missing
8. Base AcquisitionAdapter contract, resilience (exponential backoff retry), and rate limit handling
9. CheckpointRepository (Neo4j Cypher) query contracts and tenant-scoped list/delete
"""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any, AsyncIterator, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from ptb_contracts.l1_acquisition import (
    AdapterHealthReport,
    AdapterHealthStatus,
    IngestionCheckpointRecord,
    ProcessingStatus,
    RateLimitConfig,
    RawEventRecord,
    RetryPolicyConfig,
    SourceSyncState,
    SourceType,
    compute_checkpoint_id,
    compute_content_hash,
    compute_idempotency_key,
    ensure_utc,
)
from ptb_acquisition.adapters.base import (
    AcquisitionAdapter,
    AcquisitionError,
    AdapterAuthenticationError,
    AdapterConfigurationError,
    AdapterRateLimitError,
    AdapterStreamNotFoundError,
    AdapterTransientError,
)
from ptb_acquisition.adapters.confluence_adapter import ConfluenceAdapter
from ptb_acquisition.adapters.github_adapter import GitHubAdapter
from ptb_acquisition.adapters.jira_adapter import JiraAdapter
from ptb_acquisition.adapters.outlook_adapter import OutlookMailAdapter
from ptb_acquisition.adapters.slack_adapter import SlackAdapter
from ptb_acquisition.pipeline import AcquisitionPipeline
from ptb_database.repositories.checkpoint_repo import CheckpointRepository
from tests.support.test_doubles import (
    InMemoryCheckpointRepository,
    InMemoryRawEventRepository,
)


# =============================================================================
# DUMMY ADAPTERS FOR CONTRACT TESTING
# =============================================================================

class MockContractAdapter(AcquisitionAdapter):
    """Test adapter for validating poll, backfill, and idempotency guarantees."""

    def __init__(
        self,
        source_type: SourceType = SourceType.JIRA,
        tenant_id: str = "tenant-test",
        events: Optional[List[RawEventRecord]] = None,
    ):
        super().__init__(source_type=source_type, tenant_id=tenant_id)
        self.events: List[RawEventRecord] = events or []

    async def discover(self) -> List[Dict[str, Any]]:
        return [{"stream_id": "stream-1", "name": "Stream 1"}]

    async def backfill(
        self, stream_id: str, since: Optional[datetime] = None
    ) -> AsyncIterator[RawEventRecord]:
        for e in self.events:
            if since is None or e.event_timestamp >= since:
                yield e

    async def poll_incremental(
        self, stream_id: str, checkpoint: Optional[IngestionCheckpointRecord] = None
    ) -> AsyncIterator[RawEventRecord]:
        for e in self.events:
            if checkpoint and checkpoint.last_event_timestamp:
                if e.event_timestamp <= checkpoint.last_event_timestamp:
                    continue
            yield e

    async def health(self) -> Dict[str, Any]:
        return {
            "source_type": self.source_type.value,
            "tenant_id": self.tenant_id,
            "configured": True,
            "status": "healthy",
        }


# =============================================================================
# 1. CHECKPOINT CREATE / READ / UPDATE PER SOURCE
# =============================================================================

@pytest.mark.asyncio
async def test_checkpoint_create_read_update_per_source():
    """Verify checkpoint CRUD lifecycle is scoped per (tenant_id, source_type, stream_id)."""
    repo = InMemoryCheckpointRepository()
    now = datetime.now(timezone.utc)

    # 1. Create Jira checkpoint
    ckpt_jira = IngestionCheckpointRecord(
        source_type=SourceType.JIRA,
        stream_id="PROJ-ALPHA",
        tenant_id="tenant-acme",
        last_external_id="JIRA-101",
        last_event_timestamp=now,
    )
    await repo.save_checkpoint(ckpt_jira)

    # Read back and verify
    read_jira = await repo.get_checkpoint(SourceType.JIRA, "PROJ-ALPHA", tenant_id="tenant-acme")
    assert read_jira is not None
    assert read_jira.source_type == SourceType.JIRA
    assert read_jira.stream_id == "PROJ-ALPHA"
    assert read_jira.tenant_id == "tenant-acme"
    assert read_jira.last_external_id == "JIRA-101"
    assert read_jira.id == compute_checkpoint_id("tenant-acme", SourceType.JIRA, "PROJ-ALPHA")

    # 2. Update with newer external_id and timestamp
    updated_ts = now + timedelta(hours=2)
    ckpt_jira_updated = IngestionCheckpointRecord(
        source_type=SourceType.JIRA,
        stream_id="PROJ-ALPHA",
        tenant_id="tenant-acme",
        last_external_id="JIRA-105",
        last_event_timestamp=updated_ts,
    )
    await repo.save_checkpoint(ckpt_jira_updated)

    read_updated = await repo.get_checkpoint(SourceType.JIRA, "PROJ-ALPHA", tenant_id="tenant-acme")
    assert read_updated is not None
    assert read_updated.last_external_id == "JIRA-105"
    assert read_updated.last_event_timestamp == updated_ts
    assert read_updated.id == read_jira.id  # Deterministic ID remains identical

    # 3. Verify distinct source on same stream_id remains independent
    read_slack = await repo.get_checkpoint(SourceType.SLACK, "PROJ-ALPHA", tenant_id="tenant-acme")
    assert read_slack is None

    # Save distinct Slack checkpoint on same stream_id
    ckpt_slack = IngestionCheckpointRecord(
        source_type=SourceType.SLACK,
        stream_id="PROJ-ALPHA",
        tenant_id="tenant-acme",
        last_external_id="msg-999",
        last_event_timestamp=now,
    )
    await repo.save_checkpoint(ckpt_slack)

    read_slack = await repo.get_checkpoint(SourceType.SLACK, "PROJ-ALPHA", tenant_id="tenant-acme")
    assert read_slack is not None
    assert read_slack.last_external_id == "msg-999"
    # Ensure Jira checkpoint was NOT overwritten
    read_jira_again = await repo.get_checkpoint(SourceType.JIRA, "PROJ-ALPHA", tenant_id="tenant-acme")
    assert read_jira_again.last_external_id == "JIRA-105"


# =============================================================================
# 2. CHECKPOINT ISOLATION BETWEEN TENANTS
# =============================================================================

@pytest.mark.asyncio
async def test_two_tenant_checkpoint_isolation():
    """Verify tenant A and tenant B have completely isolated checkpoints on identical source/stream."""
    repo = InMemoryCheckpointRepository()
    now = datetime.now(timezone.utc)

    ckpt_a = IngestionCheckpointRecord(
        source_type=SourceType.GIT,
        stream_id="main",
        tenant_id="tenant-A",
        last_external_id="commit-aaa",
        last_event_timestamp=now,
    )
    await repo.save_checkpoint(ckpt_a)

    ckpt_b = IngestionCheckpointRecord(
        source_type=SourceType.GIT,
        stream_id="main",
        tenant_id="tenant-B",
        last_external_id="commit-bbb",
        last_event_timestamp=now,
    )
    await repo.save_checkpoint(ckpt_b)

    res_a = await repo.get_checkpoint(SourceType.GIT, "main", tenant_id="tenant-A")
    assert res_a is not None
    assert res_a.tenant_id == "tenant-A"
    assert res_a.last_external_id == "commit-aaa"

    res_b = await repo.get_checkpoint(SourceType.GIT, "main", tenant_id="tenant-B")
    assert res_b is not None
    assert res_b.tenant_id == "tenant-B"
    assert res_b.last_external_id == "commit-bbb"

    # Advance tenant A only
    ckpt_a_next = IngestionCheckpointRecord(
        source_type=SourceType.GIT,
        stream_id="main",
        tenant_id="tenant-A",
        last_external_id="commit-aaa-v2",
        last_event_timestamp=now + timedelta(minutes=10),
    )
    await repo.save_checkpoint(ckpt_a_next)

    # Tenant B must remain strictly unchanged
    res_b_after = await repo.get_checkpoint(SourceType.GIT, "main", tenant_id="tenant-B")
    assert res_b_after.last_external_id == "commit-bbb"


@pytest.mark.asyncio
async def test_restart_resumes_correct_checkpoint():
    """Verify adapter restart resumes the exact checkpoint for designated tenant only."""
    repo = InMemoryCheckpointRepository()
    now = datetime.now(timezone.utc)

    ckpt_a = IngestionCheckpointRecord(
        source_type=SourceType.JIRA,
        stream_id="tickets",
        tenant_id="tenant-A",
        last_external_id="jira-123",
        last_event_timestamp=now,
    )
    await repo.save_checkpoint(ckpt_a)

    res_a = await repo.get_checkpoint(SourceType.JIRA, "tickets", tenant_id="tenant-A")
    assert res_a is not None
    assert res_a.last_external_id == "jira-123"

    res_b = await repo.get_checkpoint(SourceType.JIRA, "tickets", tenant_id="tenant-B")
    assert res_b is None


@pytest.mark.asyncio
async def test_acquisition_pipeline_passes_tenant_id():
    """Verify AcquisitionPipeline propagates tenant_id into CheckpointRepository."""
    repo = InMemoryCheckpointRepository()
    pipeline = AcquisitionPipeline(checkpoint_repo=repo, tenant_id="work-tenant")

    ckpt = IngestionCheckpointRecord(
        source_type=SourceType.GITHUB,
        stream_id="issues",
        tenant_id="work-tenant",
        last_external_id="pr-42",
        last_event_timestamp=datetime.now(timezone.utc),
    )
    await pipeline.save_checkpoint(ckpt)

    res = await pipeline.get_checkpoint(SourceType.GITHUB, "issues")
    assert res is not None
    assert res.tenant_id == "work-tenant"
    assert res.last_external_id == "pr-42"

    # Querying repo directly with different tenant must return None
    res_other = await repo.get_checkpoint(SourceType.GITHUB, "issues", tenant_id="personal-tenant")
    assert res_other is None


@pytest.mark.asyncio
async def test_checkpoint_deterministic_id():
    """Verify checkpoint ID is deterministic across repeated saves."""
    repo = InMemoryCheckpointRepository()
    now = datetime.now(timezone.utc)

    ckpt_1 = IngestionCheckpointRecord(
        source_type=SourceType.GIT,
        stream_id="commits",
        tenant_id="tenant-C",
        last_external_id="hash-1",
        last_event_timestamp=now,
    )
    await repo.save_checkpoint(ckpt_1)

    res_1 = await repo.get_checkpoint(SourceType.GIT, "commits", tenant_id="tenant-C")
    id_1 = res_1.id

    ckpt_2 = IngestionCheckpointRecord(
        source_type=SourceType.GIT,
        stream_id="commits",
        tenant_id="tenant-C",
        last_external_id="hash-2",
        last_event_timestamp=now + timedelta(seconds=30),
    )
    await repo.save_checkpoint(ckpt_2)

    res_2 = await repo.get_checkpoint(SourceType.GIT, "commits", tenant_id="tenant-C")
    id_2 = res_2.id

    assert id_1 == id_2
    assert len(id_1) == 64  # Valid SHA-256 hex string


# =============================================================================
# 3. CHECKPOINT DOES NOT ADVANCE ON FAILED INGESTION
# =============================================================================

@pytest.mark.asyncio
async def test_checkpoint_does_not_advance_on_failed_ingestion():
    """Verify that when raw event persistence fails, the checkpoint is NOT updated or advanced."""
    ckpt_repo = InMemoryCheckpointRepository()

    # Pre-populate an initial checkpoint
    initial_ts = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
    initial_ckpt = IngestionCheckpointRecord(
        source_type=SourceType.JIRA,
        stream_id="PROJ-SEC",
        tenant_id="tenant-fail-test",
        last_external_id="JIRA-INIT",
        last_event_timestamp=initial_ts,
    )
    await ckpt_repo.save_checkpoint(initial_ckpt)

    # Create a failing raw event repository
    class FailingRawEventRepository:
        async def persist_raw_event(self, record: RawEventRecord) -> bool:
            return False  # Persistence rejected

        async def save_raw_event(self, record: RawEventRecord) -> bool:
            return False

    failing_event_repo = FailingRawEventRepository()

    # Create adapter with a newer event
    newer_ts = initial_ts + timedelta(hours=1)
    new_event = RawEventRecord(
        id="raw-evt-fail-001",
        tenant_id="tenant-fail-test",
        source_type=SourceType.JIRA,
        external_id="JIRA-NEW-99",
        idempotency_key="idemp-key-fail-001",
        event_timestamp=newer_ts,
        author_external_id="dev@acme.com",
        conversation_or_project_id="PROJ-SEC",
        normalized_text="Issue that fails DB write",
        content_hash="hash-fail-001",
    )
    adapter = MockContractAdapter(
        source_type=SourceType.JIRA,
        tenant_id="tenant-fail-test",
        events=[new_event],
    )

    pipeline = AcquisitionPipeline(
        raw_event_repo=failing_event_repo,
        checkpoint_repo=ckpt_repo,
        tenant_id="tenant-fail-test",
    )

    # Run synchronization
    ingested_count = await pipeline.sync_adapter(adapter, stream_id="PROJ-SEC")
    assert ingested_count == 0
    assert pipeline.stats["errors"] > 0

    # Verify checkpoint remained at initial_ts and was NOT advanced to newer_ts
    current_ckpt = await ckpt_repo.get_checkpoint(SourceType.JIRA, "PROJ-SEC", tenant_id="tenant-fail-test")
    assert current_ckpt is not None
    assert current_ckpt.last_external_id == "JIRA-INIT"
    assert current_ckpt.last_event_timestamp == initial_ts


@pytest.mark.asyncio
async def test_checkpoint_advances_after_successful_ingestion():
    """Verify checkpoint advances once durable persistence succeeds."""
    ckpt_repo = InMemoryCheckpointRepository()
    raw_repo = InMemoryRawEventRepository()

    initial_ts = datetime(2026, 10, 1, 10, 0, 0, tzinfo=timezone.utc)
    target_ts = datetime(2026, 10, 1, 11, 0, 0, tzinfo=timezone.utc)

    event = RawEventRecord(
        id="raw-evt-succ-001",
        tenant_id="tenant-succ-test",
        source_type=SourceType.SLACK,
        external_id="slack-msg-123",
        idempotency_key="idemp-succ-001",
        event_timestamp=target_ts,
        author_external_id="user1",
        conversation_or_project_id="general",
        normalized_text="Fix urgent bug",
        content_hash="hash-succ-001",
    )
    adapter = MockContractAdapter(
        source_type=SourceType.SLACK,
        tenant_id="tenant-succ-test",
        events=[event],
    )

    pipeline = AcquisitionPipeline(
        raw_event_repo=raw_repo,
        checkpoint_repo=ckpt_repo,
        tenant_id="tenant-succ-test",
    )

    ingested = await pipeline.sync_adapter(adapter, stream_id="general")
    assert ingested == 1

    ckpt = await ckpt_repo.get_checkpoint(SourceType.SLACK, "general", tenant_id="tenant-succ-test")
    assert ckpt is not None
    assert ckpt.last_external_id == "slack-msg-123"
    assert ckpt.last_event_timestamp == target_ts


# =============================================================================
# 4. REPLAY PRODUCES 0 DUPLICATES WHEN DATA IS UNCHANGED
# =============================================================================

@pytest.mark.asyncio
async def test_replay_produces_zero_duplicates_when_data_unchanged():
    """Verify that re-fetching/replaying identical external items yields 0 duplicates."""
    raw_repo = InMemoryRawEventRepository()
    ckpt_repo = InMemoryCheckpointRepository()

    adapter = MockContractAdapter(
        source_type=SourceType.GITHUB,
        tenant_id="tenant-replay-test",
    )
    # Generate 3 events via adapter builder
    events = [
        adapter.build_raw_event(
            external_id=f"gh-issue-{i}",
            content=f"Issue title and body content {i}",
            author_id=f"dev_{i}",
            stream_id="repo-main",
            event_timestamp=datetime(2026, 10, 1, 9, i, 0, tzinfo=timezone.utc),
        )
        for i in range(1, 4)
    ]
    adapter.events = events

    pipeline = AcquisitionPipeline(
        raw_event_repo=raw_repo,
        checkpoint_repo=ckpt_repo,
        tenant_id="tenant-replay-test",
    )

    # First run (Ingestion)
    first_count = await pipeline.sync_adapter(adapter, stream_id="repo-main")
    assert first_count == 3
    assert raw_repo.count == 3
    assert pipeline.stats["deduplicated"] == 0

    # Second run (Replay / Re-fetch with unchanged data)
    second_count = await pipeline.sync_adapter(adapter, stream_id="repo-main")
    # Because checkpoint was updated, poll_incremental filters them out OR pipeline deduplicates
    assert second_count == 0
    assert raw_repo.count == 3  # Exactly 0 new records in database!

    # Even if adapter re-emits them in backfill mode, pipeline deduplicates by idempotency_key
    replay_ingested = 0
    for e in events:
        ingested = await pipeline.ingest_event(e)
        if ingested:
            replay_ingested += 1

    assert replay_ingested == 0
    assert raw_repo.count == 3
    assert pipeline.stats["deduplicated"] == 3


# =============================================================================
# 5. ADAPTER HEALTH REPORTS NOT_CONFIGURED WHEN CREDENTIALS MISSING
# =============================================================================

@pytest.mark.asyncio
async def test_adapter_health_reports_not_configured_when_credentials_missing():
    """Verify all 5 REST/Cloud adapters report unconfigured/standby when credentials are empty."""
    with patch.dict("os.environ", {}, clear=True):
        # 1. JiraAdapter
        jira = JiraAdapter(base_url="", email="", api_token="", tenant_id="user-t1")
        h_jira = await jira.health()
        assert h_jira["configured"] is False
        assert h_jira["status"] in ("unconfigured", "not_configured", "standby")
        assert h_jira["tenant_id"] == "user-t1"
        assert h_jira["source_type"] == SourceType.JIRA.value

        # 2. ConfluenceAdapter
        conf = ConfluenceAdapter(base_url="", email="", api_token="", tenant_id="user-t2")
        h_conf = await conf.health()
        assert h_conf["configured"] is False
        assert h_conf["status"] in ("unconfigured", "not_configured", "standby")
        assert h_conf["tenant_id"] == "user-t2"
        assert h_conf["source_type"] == SourceType.CONFLUENCE.value

        # 3. GitHubAdapter
        gh = GitHubAdapter(api_token="", repositories=[], tenant_id="user-t3")
        h_gh = await gh.health()
        assert h_gh["configured"] is False
        assert h_gh["status"] in ("unconfigured", "not_configured", "standby")
        assert h_gh["tenant_id"] == "user-t3"
        assert h_gh["source_type"] == SourceType.GITHUB.value

        # 4. SlackAdapter
        slack = SlackAdapter(bot_token="", channels=[], tenant_id="user-t4")
        h_slack = await slack.health()
        assert h_slack["configured"] is False
        assert h_slack["status"] in ("unconfigured", "not_configured", "standby")
        assert h_slack["tenant_id"] == "user-t4"
        assert h_slack["source_type"] == SourceType.SLACK.value

        # 5. OutlookMailAdapter
        outlook = OutlookMailAdapter(graph_token="", mailbox="", tenant_id="user-t5")
        h_outlook = await outlook.health()
        assert h_outlook["configured"] is False
        assert h_outlook["status"] in ("unconfigured", "not_configured", "standby")
        assert h_outlook["tenant_id"] == "user-t5"
        assert h_outlook["source_type"] == SourceType.MS_OUTLOOK.value


# =============================================================================
# 6. BASE ADAPTER RESILIENCE & CANONICAL HELPERS CONTRACT
# =============================================================================

@pytest.mark.asyncio
async def test_base_adapter_contract_and_resilience_helpers():
    """Verify AcquisitionAdapter builder, validation, retry, and rate limiting helpers."""
    adapter = MockContractAdapter(source_type=SourceType.CODING_AGENT, tenant_id="t-resilience")

    # 1. Content Hash & Idempotency Key
    text = "Review architectural decision for Task W1-03"
    h1 = adapter.compute_content_hash(text)
    h2 = compute_content_hash(text)
    assert h1 == h2
    assert len(h1) == 64

    ikey = adapter.compute_idempotency_key(external_id="item-01", content_hash=h1, stream_id="s1")
    assert len(ikey) == 64

    # 2. Build and Validate Record
    record = adapter.build_raw_event(
        external_id="item-01",
        content=text,
        author_id="dev-42",
        stream_id="s1",
        event_timestamp=datetime.now(timezone.utc),
    )
    assert adapter.validate_record(record) is True
    assert record.tenant_id == "t-resilience"
    assert record.source_type == SourceType.CODING_AGENT

    # Multi-tenant leak detection
    record_leak = record.model_copy()
    object.__setattr__(record_leak, "tenant_id", "malicious-tenant")
    assert adapter.validate_record(record_leak) is False

    # 3. Retry Helper with Exponential Backoff
    attempts = 0

    async def flaky_fetch():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise AdapterTransientError(f"Transient HTTP 503 attempt {attempts}")
        return "success-result"

    policy = RetryPolicyConfig(max_retries=3, initial_delay_seconds=0.01, backoff_multiplier=1.5, jitter=False)
    res = await adapter.execute_with_retry(flaky_fetch, retry_policy=policy)
    assert res == "success-result"
    assert attempts == 3

    # Non-retryable error must fail fast
    async def fatal_error():
        raise AdapterAuthenticationError("401 Invalid Token")

    with pytest.raises(AdapterAuthenticationError):
        await adapter.execute_with_retry(fatal_error, retry_policy=policy)

    # 4. Rate Limiting Helper
    with pytest.raises(AdapterRateLimitError) as exc_info:
        await adapter.handle_rate_limit(retry_after=999.0)
    assert exc_info.value.retry_after_seconds == 999.0

    # 5. Error Hierarchy
    assert issubclass(AdapterAuthenticationError, AcquisitionError)
    assert issubclass(AdapterRateLimitError, AcquisitionError)
    assert issubclass(AdapterTransientError, AcquisitionError)
    assert issubclass(AdapterConfigurationError, AcquisitionError)
    assert issubclass(AdapterStreamNotFoundError, AcquisitionError)


# =============================================================================
# 7. CHECKPOINT REPOSITORY (NEO4J CYPHER) ISOLATION CONTRACT
# =============================================================================

@pytest.mark.asyncio
async def test_checkpoint_repo_neo4j_cypher_tenant_isolation():
    """Verify CheckpointRepository issues tenant-scoped Cypher queries."""
    mock_client = MagicMock()
    mock_driver = MagicMock()
    mock_client.get_driver.return_value = mock_driver
    mock_client.database = "neo4j"

    mock_session = AsyncMock()
    mock_driver.session.return_value.__aenter__.return_value = mock_session

    repo = CheckpointRepository(neo4j_client=mock_client)

    # 1. get_checkpoint passes tenant_id parameter
    mock_result = AsyncMock()
    mock_result.single.return_value = None
    mock_session.run.return_value = mock_result

    await repo.get_checkpoint(SourceType.JIRA, "stream-a", tenant_id="tenant-x")

    run_args = mock_session.run.call_args
    cypher_text, params = run_args[0]
    assert "WHERE cp.tenant_id = $tenant_id" in cypher_text
    assert params["tenant_id"] == "tenant-x"
    assert params["source_type"] == SourceType.JIRA.value
    assert params["stream_id"] == "stream-a"

    # 2. save_checkpoint MERGEs on composite key
    ckpt = IngestionCheckpointRecord(
        source_type=SourceType.JIRA,
        stream_id="stream-a",
        tenant_id="tenant-x",
        last_external_id="ext-1",
        last_event_timestamp=datetime.now(timezone.utc),
    )
    await repo.save_checkpoint(ckpt)

    save_args = mock_session.run.call_args
    save_cypher, save_params = save_args[0]
    assert "MERGE (cp:IngestionCheckpoint {tenant_id: $tenant_id, source_type: $source_type, stream_id: $stream_id})" in save_cypher
    assert save_params["tenant_id"] == "tenant-x"

    # 3. list_checkpoints with tenant_id filter
    mock_session.run.reset_mock()
    await repo.list_checkpoints(tenant_id="tenant-x")
    list_args = mock_session.run.call_args
    list_cypher, list_params = list_args[0]
    assert "WHERE cp.tenant_id = $tenant_id" in list_cypher
    assert list_params["tenant_id"] == "tenant-x"

    # 4. delete_checkpoint
    mock_del_res = AsyncMock()
    mock_del_res.single.return_value = {"deleted_count": 1}
    mock_session.run.return_value = mock_del_res

    deleted = await repo.delete_checkpoint(SourceType.JIRA, "stream-a", tenant_id="tenant-x")
    assert deleted is True
    del_args = mock_session.run.call_args
    del_cypher, del_params = del_args[0]
    assert "MATCH (cp:IngestionCheckpoint {tenant_id: $tenant_id, source_type: $source_type, stream_id: $stream_id})" in del_cypher
    assert "DETACH DELETE cp" in del_cypher
    assert del_params["tenant_id"] == "tenant-x"
