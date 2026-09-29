"""Tests for Checkpoint Isolation."""

import pytest
from datetime import datetime, timezone

from ptb_contracts import IngestionCheckpointRecord, SourceType
from ptb_acquisition.pipeline import AcquisitionPipeline
from tests.support.test_doubles import InMemoryCheckpointRepository

@pytest.mark.asyncio
async def test_two_tenant_checkpoint_isolation():
    repo = InMemoryCheckpointRepository()
    
    # Save checkpoint for tenant-A
    ckpt_a = IngestionCheckpointRecord(
        source_type=SourceType.GIT,
        stream_id="all",
        tenant_id="tenant-A",
        last_external_id="ext-a",
        last_event_timestamp=datetime.now(timezone.utc)
    )
    await repo.save_checkpoint(ckpt_a)
    
    # Save checkpoint for tenant-B
    ckpt_b = IngestionCheckpointRecord(
        source_type=SourceType.GIT,
        stream_id="all",
        tenant_id="tenant-B",
        last_external_id="ext-b",
        last_event_timestamp=datetime.now(timezone.utc)
    )
    await repo.save_checkpoint(ckpt_b)
    
    res_a = await repo.get_checkpoint(SourceType.GIT, "all", tenant_id="tenant-A")
    assert res_a is not None
    assert res_a.tenant_id == "tenant-A"
    assert res_a.last_external_id == "ext-a"
    
    res_b = await repo.get_checkpoint(SourceType.GIT, "all", tenant_id="tenant-B")
    assert res_b is not None
    assert res_b.tenant_id == "tenant-B"
    assert res_b.last_external_id == "ext-b"

@pytest.mark.asyncio
async def test_restart_resumes_correct_checkpoint():
    repo = InMemoryCheckpointRepository()
    
    # Save checkpoint for tenant-A
    ckpt_a = IngestionCheckpointRecord(
        source_type=SourceType.JIRA,
        stream_id="tickets",
        tenant_id="tenant-A",
        last_external_id="jira-123",
        last_event_timestamp=datetime.now(timezone.utc)
    )
    await repo.save_checkpoint(ckpt_a)
    
    res_a = await repo.get_checkpoint(SourceType.JIRA, "tickets", tenant_id="tenant-A")
    assert res_a is not None
    
    res_b = await repo.get_checkpoint(SourceType.JIRA, "tickets", tenant_id="tenant-B")
    assert res_b is None

@pytest.mark.asyncio
async def test_acquisition_pipeline_passes_tenant_id():
    repo = InMemoryCheckpointRepository()
    pipeline = AcquisitionPipeline(checkpoint_repo=repo, tenant_id="work")
    
    # Save checkpoint using pipeline
    ckpt = IngestionCheckpointRecord(
        source_type=SourceType.GIT,
        stream_id="commits",
        tenant_id="work",
        last_external_id="commit-abc",
        last_event_timestamp=datetime.now(timezone.utc)
    )
    await pipeline.save_checkpoint(ckpt)
    
    # Get checkpoint using pipeline
    res = await pipeline.get_checkpoint(SourceType.GIT, "commits")
    assert res is not None
    assert res.tenant_id == "work"
    assert res.last_external_id == "commit-abc"
    
    # Verify directly via repo that it was saved with 'work'
    res_repo = await repo.get_checkpoint(SourceType.GIT, "commits", tenant_id="work")
    assert res_repo is not None

@pytest.mark.asyncio
async def test_checkpoint_deterministic_id():
    repo = InMemoryCheckpointRepository()
    
    ckpt_1 = IngestionCheckpointRecord(
        source_type=SourceType.GIT,
        stream_id="commits",
        tenant_id="tenant-C",
        last_external_id="x",
        last_event_timestamp=datetime.now(timezone.utc)
    )
    await repo.save_checkpoint(ckpt_1)
    
    # Check id
    res_1 = await repo.get_checkpoint(SourceType.GIT, "commits", tenant_id="tenant-C")
    id_1 = res_1.id
    
    ckpt_2 = IngestionCheckpointRecord(
        source_type=SourceType.GIT,
        stream_id="commits",
        tenant_id="tenant-C",
        last_external_id="y",
        last_event_timestamp=datetime.now(timezone.utc)
    )
    await repo.save_checkpoint(ckpt_2)
    
    res_2 = await repo.get_checkpoint(SourceType.GIT, "commits", tenant_id="tenant-C")
    id_2 = res_2.id
    
    assert id_1 == id_2
    # Ensure id is somewhat hash-like
    assert len(id_1) > 10
