import pytest
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from fastapi.testclient import TestClient

from ptb_application.api import app, get_application_service
from ptb_application.service import ApplicationService
from ptb_contracts.l1_acquisition import IngestionCheckpointRecord, SourceType, SourceSyncState

class MockCheckpointRepo:
    def __init__(self, checkpoints=None):
        self.checkpoints = checkpoints or []
    async def list_checkpoints(self):
        return self.checkpoints

class MockTaskRepo:
    pass

class MockGraphiti:
    pass

class MockNeo4j:
    async def verify_connectivity(self):
        return True

@pytest.fixture
def clean_service():
    service = ApplicationService(
        task_repo=MockTaskRepo(),
        checkpoint_repo=MockCheckpointRepo([]),
        graph_memory=MockGraphiti(),
        neo4j_client=MockNeo4j()
    )
    # Give it explicit statuses to pass deep health check if needed, or leave to test not_ready
    return service

def test_clean_install_sources_not_healthy(clean_service):
    """1. test_clean_install_sources_not_healthy: Không có checkpoint -> KHÔNG có source nào healthy"""
    import asyncio
    res = asyncio.run(clean_service.get_sources_health())
    assert len(res.tenants) >= 6
    for t in res.tenants:
        assert t.status in [
            SourceSyncState.NEVER_SYNCED.value.lower(), 
            SourceSyncState.AUTH_REQUIRED.value.lower(), 
            SourceSyncState.UNCONFIGURED.value.lower(),
            SourceSyncState.DISABLED.value.lower(),
        ]
        assert t.status != "healthy"

def test_neo4j_down_returns_not_ready():
    """2. test_neo4j_down_returns_not_ready: Neo4j unreachable -> status: not_ready"""
    class BadNeo4j:
        def verify_connectivity(self):
            return False
    
    service = ApplicationService(
        neo4j_client=BadNeo4j(),
        processing_worker_status="healthy",
        llm_status="healthy",
        playwright_status="healthy"
    )
    service._explicit_neo4j_client = True
    
    import asyncio
    health = asyncio.run(service.get_system_health())
    assert health["status"] == "not_ready"
    assert health["neo4j"] == "not_ready"

def test_graphiti_down_returns_degraded():
    """3. test_graphiti_down_returns_degraded: Graphiti unavailable -> status: degraded"""
    class BadGraphiti:
        def is_healthy(self):
            return False
            
    service = ApplicationService(
        graph_memory=BadGraphiti(),
        neo4j_client=MockNeo4j(),
        processing_worker_status="healthy",
        llm_status="healthy",
        playwright_status="healthy"
    )
    
    import asyncio
    health = asyncio.run(service.get_system_health())
    assert health["status"] == "degraded"
    assert health["graphiti"] == "degraded"

def test_no_checkpoint_source_never_synced():
    """4. test_no_checkpoint_source_never_synced: Source chưa sync -> NEVER_SYNCED"""
    service = ApplicationService(
        checkpoint_repo=MockCheckpointRepo([]),
        playwright_status="healthy",  # so it doesn't return auth_required
        sources_config={"sources": {k: {"enabled": True, "api_token": "mock-token"} for k in ("ms_teams", "ms_outlook", "coding_agent", "git", "jira", "shortcut")}}
    )
    import asyncio
    res = asyncio.run(service.get_sources_health())
    assert len(res.tenants) >= 6
    for t in res.tenants:
        assert t.status == SourceSyncState.NEVER_SYNCED.value.lower()

def test_stale_checkpoint_degraded():
    """5. test_stale_checkpoint_degraded: Checkpoint > 7 days -> source DEGRADED, all canonical sources present"""
    now = datetime.now(timezone.utc)
    stale_cp = IngestionCheckpointRecord(
        id="cp-1",
        tenant_id="tenant-ms",
        source_type=SourceType.MS_TEAMS,
        stream_id="stream-1",
        last_event_timestamp=now - timedelta(days=8),
        updated_at=now - timedelta(days=8),
    )
    service = ApplicationService(
        checkpoint_repo=MockCheckpointRepo([stale_cp])
    )
    import asyncio
    res = asyncio.run(service.get_sources_health())
    assert len(res.tenants) >= 6
    teams_tenant = next((t for t in res.tenants if t.source_type == "ms_teams"), None)
    assert teams_tenant is not None
    assert teams_tenant.status == SourceSyncState.DEGRADED.value.lower()

def test_overall_never_synced_not_healthy():
    """6. test_overall_never_synced_not_healthy: Tất cả sources NEVER_SYNCED -> overall KHÔNG healthy"""
    service = ApplicationService(
        checkpoint_repo=MockCheckpointRepo([]),
        playwright_status="healthy"
    )
    import asyncio
    res = asyncio.run(service.get_sources_health())
    assert res.overall_health == "not_ready"
    assert res.overall_health != "healthy"


# ==============================================================================
# DOCS/V1_4 BEHAVIORAL TESTS (A - F)
# ==============================================================================

def test_a_mixed_state():
    """Test A — Mixed state:
    Git checkpoint recent -> HEALTHY
    Teams no auth -> AUTH_REQUIRED
    Outlook no checkpoint -> NEVER_SYNCED
    Jira disabled -> DISABLED
    Shortcut enabled but no token -> UNCONFIGURED
    Tất cả phải xuất hiện cùng lúc.
    """
    now = datetime.now(timezone.utc)
    git_cp = IngestionCheckpointRecord(
        id="cp-git-1",
        tenant_id="tenant-git",
        source_type=SourceType.GIT,
        stream_id="main",
        last_event_timestamp=now - timedelta(minutes=10),
        updated_at=now - timedelta(minutes=10),
    )
    service = ApplicationService(
        checkpoint_repo=MockCheckpointRepo([git_cp]),
        sources_config={
            "sources": {
                "git": {"enabled": True},
                "ms_teams": {"enabled": True},
                "ms_outlook": {"enabled": True},
                "jira": {"enabled": False},
                "shortcut": {"enabled": True, "api_token": ""},
                "coding_agent": {"enabled": True},
            }
        },
        runtime_statuses={"ms_teams": "auth_required"},
        playwright_status="healthy",
    )
    import asyncio
    res = asyncio.run(service.get_sources_health())
    tenants_by_type = {t.source_type: t for t in res.tenants}

    assert "git" in tenants_by_type
    assert "ms_teams" in tenants_by_type
    assert "ms_outlook" in tenants_by_type
    assert "jira" in tenants_by_type
    assert "shortcut" in tenants_by_type

    assert tenants_by_type["git"].status == SourceSyncState.HEALTHY.value.lower()
    assert tenants_by_type["ms_teams"].status == SourceSyncState.AUTH_REQUIRED.value.lower()
    assert tenants_by_type["ms_outlook"].status == SourceSyncState.NEVER_SYNCED.value.lower()
    assert tenants_by_type["jira"].status == SourceSyncState.DISABLED.value.lower()
    assert tenants_by_type["shortcut"].status == SourceSyncState.UNCONFIGURED.value.lower()


def test_b_no_checkpoint_no_source_healthy():
    """Test B — No checkpoint:
    Không checkpoint nào -> không source nào HEALTHY.
    """
    service = ApplicationService(
        checkpoint_repo=MockCheckpointRepo([]),
    )
    import asyncio
    res = asyncio.run(service.get_sources_health())
    assert len(res.tenants) >= 6
    for t in res.tenants:
        assert t.status != SourceSyncState.HEALTHY.value.lower()


def test_c_disabled_source_wins_over_legacy_checkpoint():
    """Test C — Disabled wins:
    Jira disabled, legacy Jira checkpoint exists -> Jira = DISABLED
    """
    now = datetime.now(timezone.utc)
    legacy_jira_cp = IngestionCheckpointRecord(
        id="cp-jira-legacy",
        tenant_id="tenant-jira",
        source_type=SourceType.JIRA,
        stream_id="stream-jira",
        last_event_timestamp=now - timedelta(hours=1),
        updated_at=now - timedelta(hours=1),
    )
    service = ApplicationService(
        checkpoint_repo=MockCheckpointRepo([legacy_jira_cp]),
        sources_config={"sources": {"jira": {"enabled": False}}},
    )
    import asyncio
    res = asyncio.run(service.get_sources_health())
    jira_tenant = next(t for t in res.tenants if t.source_type == "jira")
    assert jira_tenant.status == SourceSyncState.DISABLED.value.lower()


def test_d_runtime_error_wins_over_recent_checkpoint():
    """Test D — Runtime error wins:
    Git has recent checkpoint, Git runtime adapter ERROR -> Git = ERROR (không HEALTHY).
    """
    now = datetime.now(timezone.utc)
    recent_git_cp = IngestionCheckpointRecord(
        id="cp-git-recent",
        tenant_id="tenant-git",
        source_type=SourceType.GIT,
        stream_id="main",
        last_event_timestamp=now - timedelta(minutes=5),
        updated_at=now - timedelta(minutes=5),
    )
    service = ApplicationService(
        checkpoint_repo=MockCheckpointRepo([recent_git_cp]),
        runtime_statuses={"git": "error"},
    )
    import asyncio
    res = asyncio.run(service.get_sources_health())
    git_tenant = next(t for t in res.tenants if t.source_type == "git")
    assert git_tenant.status == SourceSyncState.ERROR.value.lower()
    assert git_tenant.status != SourceSyncState.HEALTHY.value.lower()


def test_e_multiple_checkpoints_worst_status_wins():
    """Test E — Multiple checkpoints:
    Git stream A recent, Git stream B stale -> Git = DEGRADED.
    """
    now = datetime.now(timezone.utc)
    recent_ts = now - timedelta(minutes=5)
    stale_ts = now - timedelta(days=12)

    cp_a = IngestionCheckpointRecord(
        id="cp-git-a",
        tenant_id="tenant-git",
        source_type=SourceType.GIT,
        stream_id="repo-a",
        last_event_timestamp=recent_ts,
        updated_at=recent_ts,
    )
    cp_b = IngestionCheckpointRecord(
        id="cp-git-b",
        tenant_id="tenant-git",
        source_type=SourceType.GIT,
        stream_id="repo-b",
        last_event_timestamp=stale_ts,
        updated_at=stale_ts,
    )
    service = ApplicationService(
        checkpoint_repo=MockCheckpointRepo([cp_a, cp_b]),
    )
    import asyncio
    res = asyncio.run(service.get_sources_health())
    git_tenant = next(t for t in res.tenants if t.source_type == "git")
    assert git_tenant.status == SourceSyncState.DEGRADED.value.lower()
    assert git_tenant.last_successful_sync == recent_ts


def test_f_microsoft_auth_valid_but_never_synced():
    """Test F — Microsoft auth valid but never synced:
    valid session, no Teams checkpoint -> Teams = NEVER_SYNCED (không HEALTHY).
    """
    service = ApplicationService(
        checkpoint_repo=MockCheckpointRepo([]),
        playwright_status="healthy",
    )
    import asyncio
    res = asyncio.run(service.get_sources_health())
    teams_tenant = next(t for t in res.tenants if t.source_type == "ms_teams")
    assert teams_tenant.status == SourceSyncState.NEVER_SYNCED.value.lower()
    assert teams_tenant.status != SourceSyncState.HEALTHY.value.lower()


def test_local_ai_disabled_reports_disabled_and_healthy(monkeypatch):
    """Test Local AI Disabled invariant: PTB_ENABLE_LOCAL_AI=false -> qwen/kev = disabled, status = healthy."""
    monkeypatch.setenv("PTB_ENABLE_LOCAL_AI", "false")
    monkeypatch.delenv("PTB_REQUIRE_LOCAL_AI", raising=False)

    service = ApplicationService(
        neo4j_client=MockNeo4j(),
        graph_memory=MockGraphiti(),
        processing_worker_status="healthy",
        llm_status="healthy",
        playwright_status="healthy",
    )
    import asyncio
    health = asyncio.run(service.get_system_health())
    assert health["qwen3"] == "disabled"
    assert health["kev"] == "disabled"
    assert health["status"] == "healthy"


def test_local_ai_enabled_unreachable_reports_degraded(monkeypatch):
    """Test Local AI Enabled invariant: PTB_ENABLE_LOCAL_AI=true -> unreachable -> unavailable & status = degraded."""
    monkeypatch.setenv("PTB_ENABLE_LOCAL_AI", "true")

    service = ApplicationService(
        neo4j_client=MockNeo4j(),
        graph_memory=MockGraphiti(),
        processing_worker_status="healthy",
        llm_status="healthy",
        playwright_status="healthy",
    )
    import asyncio
    health = asyncio.run(service.get_system_health())
    assert health["qwen3"] == "unavailable"
    assert health["kev"] == "unavailable"
    assert health["status"] == "degraded"


def test_local_ai_enabled_healthy_reports_healthy(monkeypatch):
    """Test Local AI Enabled invariant: PTB_ENABLE_LOCAL_AI=true with healthy services -> status = healthy."""
    monkeypatch.setenv("PTB_ENABLE_LOCAL_AI", "true")

    service = ApplicationService(
        neo4j_client=MockNeo4j(),
        graph_memory=MockGraphiti(),
        processing_worker_status="healthy",
        llm_status="healthy",
        playwright_status="healthy",
        qwen3_status="healthy",
        kev_status="healthy",
    )
    import asyncio
    health = asyncio.run(service.get_system_health())
    assert health["qwen3"] == "healthy"
    assert health["kev"] == "healthy"
    assert health["status"] == "healthy"



