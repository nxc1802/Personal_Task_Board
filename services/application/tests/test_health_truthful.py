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
    # All tenants should be NEVER_SYNCED or AUTH_REQUIRED or UNCONFIGURED
    for t in res.tenants:
        assert t.status in [
            SourceSyncState.NEVER_SYNCED.value.lower(), 
            SourceSyncState.AUTH_REQUIRED.value.lower(), 
            SourceSyncState.UNCONFIGURED.value.lower()
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
        playwright_status="healthy"  # so it doesn't return auth_required
    )
    import asyncio
    res = asyncio.run(service.get_sources_health())
    for t in res.tenants:
        assert t.status == SourceSyncState.NEVER_SYNCED.value.lower()

def test_stale_checkpoint_degraded():
    """5. test_stale_checkpoint_degraded: Checkpoint > 7 days -> source DEGRADED"""
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
    assert len(res.tenants) == 1
    assert res.tenants[0].status == SourceSyncState.DEGRADED.value.lower()

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

