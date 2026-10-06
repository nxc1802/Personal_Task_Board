"""External integration tests requiring live Docker/Neo4j/OpenWebUI infrastructure.

These tests are automatically skipped during local no-Docker test runs unless
Neo4j Bolt is listening on 127.0.0.1:7687 or `--run-external` is explicitly passed.
"""

from __future__ import annotations

import os
import urllib.request
import pytest

from ptb_database.neo4j_client import Neo4jClient


@pytest.mark.external_integration
@pytest.mark.asyncio
async def test_live_neo4j_bolt_connectivity() -> None:
    """Verify live Bolt connectivity to a running Neo4j instance."""
    client = Neo4jClient()
    try:
        is_connected = await client.verify_connectivity()
        assert is_connected is True
    finally:
        await client.close()


@pytest.mark.external_integration
def test_live_openwebui_reachability() -> None:
    """Verify HTTP reachability of a running OpenWebUI container on 127.0.0.1:3000."""
    import socket
    from urllib.parse import urlparse

    url = os.getenv("OPENWEBUI_URL", "http://127.0.0.1:3000")
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 3000
    try:
        with socket.create_connection((host, port), timeout=0.5):
            pass
    except OSError:
        pytest.skip(f"OpenWebUI container is not running on {host}:{port}")

    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=2.0) as response:
        assert 200 <= response.status < 400


@pytest.mark.unit
@pytest.mark.asyncio
async def test_simulated_jira_webhook_integration() -> None:
    """Deep integration test: Ingest simulated Jira webhook event through acquisition and processing."""
    from datetime import datetime, timezone
    import hashlib
    from unittest.mock import MagicMock
    from ptb_contracts import RawEventRecord, SourceType, ProcessingStatus, UnifiedTaskCandidate, TaskStatus
    from ptb_processing.pipeline import ProcessingPipeline
    from tests.support.test_doubles import (
        InMemoryRawEventRepository,
        InMemoryTaskDomainRepository,
        FakeDeterministicLLMExtractor,
    )

    raw_repo = InMemoryRawEventRepository()
    task_repo = InMemoryTaskDomainRepository()

    jira_payload = {
        "issue": {
            "key": "PROJ-1024",
            "fields": {
                "summary": "I will fix connection pool exhaustion in database driver before deadline.",
                "description": "Reported by ops team under high load. Needs resolution before Friday.",
                "assignee": {"displayName": "Hoàng Nam", "accountId": "acc-jira-101"},
                "creator": {"displayName": "Minh Tuấn", "accountId": "acc-jira-102"},
                "status": {"name": "In Progress"},
                "priority": {"name": "High"},
            },
        }
    }

    from uuid import uuid4

    event = RawEventRecord(
        id=str(uuid4()),
        external_id="PROJ-1024",
        tenant_id="tenant-jira-sim",
        source_type=SourceType.JIRA,
        idempotency_key=hashlib.sha256(b"PROJ-1024-update-1").hexdigest(),
        event_timestamp=datetime.now(timezone.utc),
        author_external_id="acc-jira-101",
        author_display_name="Hoàng Nam",
        conversation_or_project_id="PROJ-1024",
        normalized_text="I will fix connection pool exhaustion in database driver before deadline.",
        raw_payload=jira_payload,
    )

    raw_id = await raw_repo.persist_raw_event(event)
    assert raw_id == event.id

    llm = FakeDeterministicLLMExtractor()
    pipeline = ProcessingPipeline(task_repo=task_repo, llm_extractor=llm)
    result = await pipeline.process(event)

    assert result.status == ProcessingStatus.PROCESSED
    assert result.candidate is not None
    assert "connection pool" in result.candidate.title.lower()

    tasks = await task_repo.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].id == result.candidate.id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_simulated_teams_webhook_integration() -> None:
    """Deep integration test: Ingest simulated Teams quote/reply message through full pipeline."""
    from datetime import datetime, timezone
    import hashlib
    from uuid import uuid4
    from ptb_contracts import RawEventRecord, SourceType, ProcessingStatus
    from ptb_processing.pipeline import ProcessingPipeline
    from tests.support.test_doubles import (
        InMemoryRawEventRepository,
        InMemoryTaskDomainRepository,
        FakeDeterministicLLMExtractor,
    )

    raw_repo = InMemoryRawEventRepository()
    task_repo = InMemoryTaskDomainRepository()

    teams_message = (
        '<blockquote>Thứ 6 này anh xong tài liệu kiến trúc nhé</blockquote>'
        'Vâng anh, em đang xử lý tài liệu kiến trúc và sẽ gửi trước 17h thứ 6.'
    )

    event = RawEventRecord(
        id=str(uuid4()),
        external_id="msg-teams-9912",
        tenant_id="tenant-teams-sim",
        source_type=SourceType.MS_TEAMS,
        idempotency_key=hashlib.sha256(teams_message.encode("utf-8")).hexdigest(),
        event_timestamp=datetime.now(timezone.utc),
        author_external_id="user-teams-202",
        author_display_name="Trần Văn B",
        conversation_or_project_id="19:channel-arch@thread.tacv2",
        normalized_text=teams_message,
        raw_payload={"body": {"content": teams_message}},
    )

    await raw_repo.persist_raw_event(event)
    llm = FakeDeterministicLLMExtractor()
    pipeline = ProcessingPipeline(task_repo=task_repo, llm_extractor=llm)
    result = await pipeline.process(event)

    assert result.status == ProcessingStatus.PROCESSED
    assert result.candidate is not None
    assert len(result.candidate.evidences) >= 1
    assert result.candidate.explicit_deadline is True


@pytest.mark.external_integration
@pytest.mark.asyncio
async def test_live_e2e_full_vertical_slice() -> None:
    """Full Real E2E Test on live infrastructure:
    real Neo4j -> RawEvent persistence & dedup -> ProcessingPipeline -> TaskDomainRepository
    -> Task status transitions -> Checkpoint restart/resume -> Graphiti episode -> Health check.
    """
    from datetime import datetime, timezone
    import hashlib
    from uuid import uuid4
    from ptb_database.repositories import RawEventRepository, TaskDomainRepository, CheckpointRepository
    from ptb_contracts import RawEventRecord, SourceType, ProcessingStatus, TaskStatus, IngestionCheckpointRecord
    from ptb_processing.pipeline import ProcessingPipeline
    from ptb_graph_memory.client import GraphitiMemoryClient
    from ptb_application.service import ApplicationService
    from tests.support.test_doubles import FakeDeterministicLLMExtractor

    client = Neo4jClient()
    try:
        assert await client.verify_connectivity() is True

        raw_repo = RawEventRepository(client)
        task_repo = TaskDomainRepository(client)
        ckpt_repo = CheckpointRepository(client)

        test_run_id = str(uuid4())[:8]
        tenant_id = f"tenant-real-e2e-{test_run_id}"

        # 1. Ingest RawEvent into real Neo4j
        e2e_summary = "I will fix the production release deployment before Friday 17:00 deadline"
        event = RawEventRecord(
            id=str(uuid4()),
            external_id=f"e2e-task-{test_run_id}",
            tenant_id=tenant_id,
            source_type=SourceType.JIRA,
            idempotency_key=hashlib.sha256(f"jira-e2e-{test_run_id}".encode()).hexdigest(),
            event_timestamp=datetime.now(timezone.utc),
            author_external_id="lead-user-1",
            author_display_name="Lead Engineer",
            conversation_or_project_id="PROJ-E2E",
            normalized_text=e2e_summary,
            raw_payload={"summary": e2e_summary},
        )
        saved_id = await raw_repo.persist_raw_event(event)
        assert saved_id == event.id

        # 1b. Test idempotency / dedup in real Neo4j
        second_id = await raw_repo.persist_raw_event(event)
        assert second_id == event.id

        # 2. Process RawEvent into real Neo4j TaskDomainRepository
        llm = FakeDeterministicLLMExtractor()
        pipeline = ProcessingPipeline(task_repo=task_repo, llm_extractor=llm)
        proc_result = await pipeline.process(event)
        assert proc_result.status == ProcessingStatus.PROCESSED
        assert proc_result.candidate is not None
        task_id = proc_result.candidate.id

        # 3. Verify task exists in authoritative Neo4j store
        fetched_task = await task_repo.get_task_by_id(task_id)
        assert fetched_task is not None
        assert fetched_task.id == task_id

        # 4. Status transition audit in real Neo4j
        await task_repo.record_status_transition_audit(
            task_id=task_id,
            old_status=TaskStatus.TODO,
            new_status=TaskStatus.IN_PROGRESS,
            reason="Started by real E2E runner",
            changed_by="tester",
        )

        # 5. Checkpoint isolation & resume in real Neo4j
        cp = IngestionCheckpointRecord(
            id=str(uuid4()),
            tenant_id=tenant_id,
            source_type=SourceType.JIRA,
            stream_id="stream-jira-1",
            cursor_token="cursor-100",
            last_event_timestamp=datetime.now(timezone.utc),
        )
        await ckpt_repo.save_checkpoint(cp)
        resumed_cp = await ckpt_repo.get_checkpoint(
            tenant_id=tenant_id,
            source_type=SourceType.JIRA,
            stream_id="stream-jira-1",
        )
        assert resumed_cp is not None
        assert resumed_cp.cursor_token == "cursor-100"

        # 6. Graphiti integration in real Neo4j
        graph_client = GraphitiMemoryClient(neo4j_client=client)
        ep_id = await graph_client.add_decision(
            summary=f"E2E Decision {test_run_id}",
            decided_by="Architecture Team",
            context="E2E Validation Test",
        )
        assert ep_id is not None

        # 7. ApplicationService deep health check against real Neo4j
        app_service = ApplicationService(
            neo4j_client=client,
            task_repo=task_repo,
            raw_event_repo=raw_repo,
            checkpoint_repo=ckpt_repo,
            graph_memory=graph_client,
            processing_worker_status="healthy",
            llm_status="healthy",
            playwright_status="healthy",
        )
        health = await app_service.get_system_health()
        assert health["neo4j"] == "healthy"
        assert health["status"] in ("healthy", "degraded")

        # Cleanup test tenant data from real Neo4j
        driver = client.get_driver()
        async with driver.session() as session:
            await session.run("MATCH (n {tenant_id: $t}) DETACH DELETE n", {"t": tenant_id})
    finally:
        await client.close()

