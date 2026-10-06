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
    url = os.getenv("OPENWEBUI_URL", "http://127.0.0.1:3000")
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

