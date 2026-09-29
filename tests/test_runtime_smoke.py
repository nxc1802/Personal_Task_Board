"""Process Smoke Tests Without Docker (Wave 6 — Sub-Agent 6C).

Verifies full PTBProcessSupervisor runtime lifecycle on ephemeral ports using
test repositories from tests.support.test_doubles without requiring Neo4j or Docker,
and verifies fail-fast behavior in production mode when Neo4j is unavailable.
"""

import asyncio
from datetime import datetime, timezone
import json
import socket
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, patch
import urllib.request

import pytest

from ptb_application.api import get_application_service as get_app_service
from ptb_contracts import BugCode, ProcessingStatus, RawEventRecord, SourceType
from ptb_contracts.l2_processing import UnifiedTaskCandidate
from ptb_mcp.server import get_application_service as get_mcp_service
from scripts.ptb_cli import PTBProcessSupervisor
from tests.support import test_doubles
from tests.support.test_doubles import (
    InMemoryCheckpointRepository,
    InMemoryRawEventRepository,
)


class _SmokeTaskDomainRepository:
    """Lightweight in-memory TaskDomainRepository test double for runtime smoke testing."""

    def __init__(self) -> None:
        self._tasks: Dict[str, UnifiedTaskCandidate] = {}

    async def upsert_task_atomic(self, task: UnifiedTaskCandidate) -> str:
        task_id = task.id or "smoke-task-1"
        self._tasks[task_id] = task
        return task_id

    async def get_task_by_id(self, task_id: str) -> Optional[UnifiedTaskCandidate]:
        return self._tasks.get(task_id)

    async def get_task_with_evidences(self, task_id: str) -> Optional[UnifiedTaskCandidate]:
        return self._tasks.get(task_id)

    async def list_tasks(self, **kwargs: Any) -> List[UnifiedTaskCandidate]:
        return list(self._tasks.values())

    async def get_all_tasks(self, **kwargs: Any) -> List[UnifiedTaskCandidate]:
        return list(self._tasks.values())

    async def get_active_tasks_with_evidence(self, **kwargs: Any) -> List[UnifiedTaskCandidate]:
        return list(self._tasks.values())


def _create_task_repo_double() -> Any:
    """Return InMemoryTaskDomainRepository from tests.support.test_doubles if present, else local double."""
    repo_cls = getattr(test_doubles, "InMemoryTaskDomainRepository", None)
    if repo_cls is not None:
        return repo_cls()
    return _SmokeTaskDomainRepository()


def _allocate_ephemeral_ports(host: str = "127.0.0.1") -> Tuple[int, int]:
    """Allocate two distinct free ephemeral TCP ports on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s1, socket.socket(
        socket.AF_INET, socket.SOCK_STREAM
    ) as s2:
        s1.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s1.bind((host, 0))
        s2.bind((host, 0))
        return int(s1.getsockname()[1]), int(s2.getsockname()[1])


async def _fetch_json(url: str, timeout: float = 2.0) -> Tuple[int, Dict[str, Any]]:
    """Perform an HTTP GET request in a worker thread and return (status_code, json_payload)."""

    def _get() -> Tuple[int, Dict[str, Any]]:
        req = urllib.request.Request(url, headers={"User-Agent": "PTB-Runtime-Smoke-Test"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status_code = int(getattr(resp, "status", 200))
            body = resp.read().decode("utf-8")
            return status_code, json.loads(body)

    return await asyncio.to_thread(_get)


@pytest.mark.runtime_smoke
@pytest.mark.asyncio
async def test_supervisor_runtime_smoke_binds_rest_and_mcp_and_runs_worker():
    """Verify PTBProcessSupervisor binds REST and MCP servers on ephemeral ports, runs ProcessingWorker,
    shares the exact same ApplicationService / ProcessingPipeline / TaskIntelligenceLifecycle graph,
    and performs a clean graceful shutdown returning exit code 0.
    """
    host = "127.0.0.1"
    app_port, mcp_port = _allocate_ephemeral_ports(host)

    raw_event_repo = InMemoryRawEventRepository()
    checkpoint_repo = InMemoryCheckpointRepository()
    task_repo = _create_task_repo_double()

    # Seed one non-actionable RawEventRecord so ProcessingWorker actively processes it in its loop
    smoke_event = RawEventRecord(
        id="evt-smoke-001",
        tenant_id="smoke-tenant",
        source_type=SourceType.MS_TEAMS,
        external_id="msg-smoke-001",
        idempotency_key="idem-smoke-001",
        event_timestamp=datetime.now(timezone.utc),
        author_external_id="user@example.com",
        author_display_name="Smoke Tester",
        conversation_or_project_id="conv-smoke-1",
        raw_payload={"body": {"content": "Chào buổi sáng cả nhóm!"}},
        normalized_text="Chào buổi sáng cả nhóm!",
        processing_status=ProcessingStatus.PENDING,
    )
    await raw_event_repo.persist_raw_event(smoke_event)

    supervisor = PTBProcessSupervisor(
        host=host,
        app_port=app_port,
        mcp_port=mcp_port,
        tenant_id="smoke-tenant",
        enable_playwright=False,
        poll_interval=0.1,
        raw_event_repo=raw_event_repo,
        checkpoint_repo=checkpoint_repo,
        task_repo=task_repo,
        sources_config={"sources": {"git": {"enabled": False}, "jira": {"enabled": False}, "shortcut": {"enabled": False}}},
    )

    run_task = asyncio.create_task(supervisor.run())

    try:
        # Wait until supervisor reaches READY state
        deadline = asyncio.get_running_loop().time() + 10.0
        while supervisor.state == "INITIALIZING" and not run_task.done():
            if asyncio.get_running_loop().time() > deadline:
                pytest.fail(f"Supervisor did not reach READY within timeout (state={supervisor.state})")
            await asyncio.sleep(0.05)

        assert not run_task.done(), "Supervisor task exited prematurely before inspection"
        assert supervisor.state == "READY"

        # 1. Confirm REST server (:app_port) binds and responds GET /health HTTP 200
        rest_status, rest_payload = await _fetch_json(f"http://{host}:{app_port}/health")
        assert rest_status == 200
        assert rest_payload.get("status") in ("healthy", "degraded")

        # 2. Confirm MCP server (:mcp_port) binds and responds GET /health HTTP 200
        mcp_status, mcp_payload = await _fetch_json(f"http://{host}:{mcp_port}/health")
        assert mcp_status == 200
        assert mcp_payload.get("status") == "healthy"
        assert mcp_payload.get("service") == "ptb-mcp"
        assert mcp_payload.get("application_service_bound") is True

        # 3. Confirm ProcessingWorker loop is running and processes queued event
        assert supervisor.processing_worker is not None
        assert supervisor.processing_worker._running is True
        worker_tasks = [t for t in supervisor.tasks if t.get_name() == "ptb-processing-worker"]
        assert len(worker_tasks) == 1
        assert not worker_tasks[0].done()

        proc_deadline = asyncio.get_running_loop().time() + 5.0
        while True:
            stored_evt = await raw_event_repo.get_by_id("evt-smoke-001")
            if stored_evt is not None and stored_evt.processing_status == ProcessingStatus.PROCESSED:
                break
            if asyncio.get_running_loop().time() > proc_deadline:
                pytest.fail("ProcessingWorker did not process pending RawEvent within timeout")
            await asyncio.sleep(0.05)

        # 4. Confirm single shared ApplicationService, ProcessingPipeline, and TaskIntelligenceLifecycle
        assert supervisor.application_service is not None
        assert supervisor.application_service is get_app_service() is get_mcp_service()
        assert (
            supervisor.processing_worker.pipeline
            is supervisor.application_service.processing_pipeline
        )
        assert (
            supervisor.processing_worker.pipeline.intelligence_lifecycle
            is supervisor.application_service.lifecycle
        )

        # 5. Trigger graceful shutdown and verify clean exit with code 0
        supervisor.trigger_shutdown()
        exit_code = await asyncio.wait_for(run_task, timeout=10.0)
        assert exit_code == 0
        assert supervisor._running is False
        assert all(t.done() for t in supervisor.tasks)
    finally:
        if not run_task.done():
            supervisor.trigger_shutdown()
            await asyncio.wait([run_task], timeout=5.0)


@pytest.mark.runtime_smoke
@pytest.mark.asyncio
async def test_supervisor_production_mode_neo4j_unavailable_exits_without_ram_fallback(capsys):
    """Verify PTBProcessSupervisor in production mode (no injected test repositories) fails fast
    when Neo4jClient.verify_connectivity() returns False:
    - Emits BugCode.PTB_STORAGE_001
    - Transitions state to NOT_READY
    - Exits immediately with exit code 1
    - Never initializes any InMemory* RAM fallback repositories
    """
    host = "127.0.0.1"
    app_port, mcp_port = _allocate_ephemeral_ports(host)

    with patch(
        "ptb_database.neo4j_client.Neo4jClient.verify_connectivity",
        new_callable=AsyncMock,
        return_value=False,
    ), patch(
        "ptb_database.neo4j_client.Neo4jClient.close",
        new_callable=AsyncMock,
    ) as mock_close, patch(
        "scripts.ptb_cli.log_bug"
    ) as mock_log_bug:
        supervisor = PTBProcessSupervisor(
            host=host,
            app_port=app_port,
            mcp_port=mcp_port,
            enable_playwright=False,
        )

        exit_code = await supervisor.run(max_runtime=1.0)

        assert exit_code == 1
        assert supervisor.state == "NOT_READY"
        assert mock_close.await_count == 1

        # Verify BugCode.PTB_STORAGE_001 was logged
        assert any(
            call.kwargs.get("code") == BugCode.PTB_STORAGE_001
            or (call.args and call.args[0] == BugCode.PTB_STORAGE_001)
            for call in mock_log_bug.call_args_list
        )

        # Verify no InMemory* fallback or runtime services were initialized
        assert supervisor.raw_event_repo is None
        assert supervisor.checkpoint_repo is None
        assert supervisor.task_repo is None
        assert supervisor.application_service is None
        assert supervisor.processing_worker is None
        assert supervisor.tasks == []

        captured = capsys.readouterr().out
        assert "CRITICAL ERROR: Neo4j authoritative store unavailable" in captured
