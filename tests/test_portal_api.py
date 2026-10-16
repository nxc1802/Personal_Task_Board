"""Tests for Portal REST endpoints (Wave 2 & Issue #18).

Verifies:
1. POST /api/ingest/message uses canonical RawEvent persistence and ProcessingPipeline.
2. POST /api/ingest/message returns 400 for empty messages.
3. POST /api/ingest/layer1 runs via shared ApplicationService repos without separate Neo4jClient.
4. POST /api/database/reset is disabled by default (403 Forbidden) and requires PTB_ENABLE_DESTRUCTIVE_API=true.
"""

from datetime import datetime, timezone
import os
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from ptb_application.api import create_app
from ptb_application.service import ApplicationService
from ptb_contracts.l1_acquisition import ProcessingStatus, RawEventRecord
from ptb_contracts.l2_processing import TaskStatus, UnifiedTaskCandidate
from ptb_processing.pipeline import PipelineResult
from tests.support.test_doubles import (
    FakeGraphitiAdapter,
    InMemoryCheckpointRepository,
    InMemoryRawEventRepository,
    InMemoryTaskDomainRepository,
)


@pytest.fixture
def mock_app_client():
    task_repo = InMemoryTaskDomainRepository()
    cp_repo = InMemoryCheckpointRepository()
    raw_repo = InMemoryRawEventRepository()
    fake_graph = FakeGraphitiAdapter()

    svc = ApplicationService(
        task_repo=task_repo,
        checkpoint_repo=cp_repo,
        raw_event_repo=raw_repo,
        graph_memory=fake_graph,
        processing_worker_status="healthy",
    )

    app = create_app(application_service=svc)
    client = TestClient(app)
    return client, svc


def test_ingest_message_empty_validation(mock_app_client):
    client, _ = mock_app_client
    resp = client.post("/api/ingest/message", json={"message": "   "})
    assert resp.status_code == 400
    assert "không được để trống" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_ingest_message_canonical_pipeline_execution(mock_app_client):
    client, svc = mock_app_client

    sample_candidate = UnifiedTaskCandidate(
        id="task-portal-test-123",
        title="Nghiên cứu tài liệu kiến trúc PTB",
        description="Đọc kỹ v1_1.md",
        status=TaskStatus.TODO,
        owner_name="Minh",
        requester_name="Truong",
        priority_score=85.0,
        review_status="auto_approved",
    )

    mock_pipeline = MagicMock()
    mock_pipeline.process = AsyncMock(
        return_value=PipelineResult(
            raw_event_id="raw-123",
            status=ProcessingStatus.PROCESSED,
            should_extract=True,
            candidate=sample_candidate,
            task_id=sample_candidate.id,
        )
    )
    svc.processing_pipeline = mock_pipeline

    resp = client.post(
        "/api/ingest/message",
        json={
            "message": "Minh nhớ nghiên cứu tài liệu kiến trúc PTB nhé",
            "sender": "Truong",
            "source": "Direct Input",
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["task"]["id"] == "task-portal-test-123"
    assert data["task"]["title"] == "Nghiên cứu tài liệu kiến trúc PTB"
    assert data["task"]["owner_name"] == "Minh"
    assert data["task"]["requester_name"] == "Truong"
    assert data["task"]["priority_level"] == "High"
    assert data["task"]["status"] == "TODO"

    # Verify RawEvent was persisted to raw_event_repo
    raw_events = svc.raw_event_repo.events
    assert len(raw_events) == 1
    ev = next(iter(raw_events.values()))
    assert ev.source_type.value == "coding_agent" or ev.source_type == "coding_agent"
    assert "nghiên cứu tài liệu" in ev.normalized_text.lower()

    # Verify pipeline was called
    mock_pipeline.process.assert_awaited_once()


def test_database_reset_forbidden_by_default(mock_app_client):
    client, _ = mock_app_client
    with patch.dict(os.environ, {"PTB_ENABLE_DESTRUCTIVE_API": "false"}, clear=False):
        resp = client.post("/api/database/reset")
        assert resp.status_code == 403
        assert "Destructive API disabled" in resp.json()["detail"]


def test_database_reset_allowed_when_explicitly_enabled(mock_app_client):
    client, svc = mock_app_client
    mock_neo4j = MagicMock()
    mock_neo4j.execute_write = AsyncMock(return_value=[])
    svc.neo4j_client = mock_neo4j

    with patch.dict(os.environ, {"PTB_ENABLE_DESTRUCTIVE_API": "true"}, clear=False):
        with patch("ptb_database.setup_all.setup_neo4j", new=AsyncMock(return_value=True)):
            resp = client.post("/api/database/reset")
            assert resp.status_code == 200
            assert resp.json()["status"] == "success"
            assert resp.json()["constraints_restored"] is True
            mock_neo4j.execute_write.assert_awaited_once_with("MATCH (n) DETACH DELETE n")


def test_ingest_layer1_uses_shared_service(mock_app_client):
    client, svc = mock_app_client
    with patch("ptb_acquisition.pipeline.AcquisitionPipeline.sync_adapter", new=AsyncMock(return_value=5)):
        with patch("ptb_processing.worker.ProcessingWorker.process_batch", new=AsyncMock(return_value=["PROCESSED", "PROCESSED"])):
            resp = client.post("/api/ingest/layer1", json={"limit": 2})
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "success"
            assert data["ingested"] == 10  # 2 adapters * 5 events
            assert data["processed"] == 2
            assert data["failed"] == 0


def test_portal_html_disabled_by_default(mock_app_client):
    client, _ = mock_app_client
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("PTB_DEV_PORTAL", None)
        resp = client.get("/portal")
        assert resp.status_code == 404
        assert "disabled" in resp.json()["detail"].lower()


def test_portal_html_enabled_when_explicit(mock_app_client):
    client, _ = mock_app_client
    with patch.dict(os.environ, {"PTB_DEV_PORTAL": "true"}, clear=False):
        resp = client.get("/portal")
        assert resp.status_code == 200
        assert "<!DOCTYPE html>" in resp.text


def test_ingest_message_missing_llm_key_returns_422(mock_app_client):
    """P0.1: Verify graceful 422 validation when LLM_API_KEY is not configured."""
    client, svc = mock_app_client
    with patch.dict(os.environ, {"LLM_API_KEY": "", "OPENAI_API_KEY": ""}, clear=False):
        resp = client.post(
            "/api/ingest/message",
            json={
                "message": "Minh hãy hoàn thành báo cáo trước 5h chiều nay",
                "sender": "Truong",
            },
        )
        assert resp.status_code == 422
        data = resp.json()["detail"]
        assert data["error"] == "LLM_API_KEY_REQUIRED"
        assert data["code"] == "PTB_LLM_001"
        assert "LLM_API_KEY" in data["message"]


def test_connectors_config_get_and_post(mock_app_client, tmp_path):
    """P1.1: Verify GET and POST /api/connectors/config."""
    client, _ = mock_app_client
    # Test GET
    resp = client.get("/api/connectors/config")
    assert resp.status_code == 200
    connectors = resp.json().get("connectors", [])
    assert len(connectors) == 9
    connector_ids = [c["id"] for c in connectors]
    assert "github" in connector_ids
    assert "jira" in connector_ids

    # Test POST update connector token
    test_env = tmp_path / ".env.test"
    test_env.write_text("GITHUB_TOKEN=\n", encoding="utf-8")
    with patch.dict(os.environ, {"GITHUB_TOKEN": ""}, clear=False):
        post_resp = client.post(
            "/api/connectors/config",
            json={
                "connector_id": "github",
                "token": "ghp_testnewtoken1234567890abcdef",
            },
        )
        assert post_resp.status_code == 200
        result = post_resp.json()
        assert result["status"] == "success"
        assert result["configured"] is True
        assert "ghp_te" in result["masked_token"]
        assert os.environ.get("GITHUB_TOKEN") == "ghp_testnewtoken1234567890abcdef"



