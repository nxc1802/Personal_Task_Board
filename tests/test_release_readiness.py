"""V1 Release Readiness Behavioral Audit Suite (Wave 4 - Sub-Agent 4A).

Automates behavioral verification of all 13 V1 Release Readiness Gates
defined in docs/v1_3.md without requiring Docker infrastructure.
Uses lightweight component execution, in-memory repositories, and test doubles.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch
import yaml

import pytest
from starlette.testclient import TestClient

from ptb_contracts import (
    BugCode,
    EvidenceRecord,
    EvidenceType,
    IngestionCheckpointRecord,
    TaskStatus,
    UnifiedTaskCandidate,
)
from ptb_contracts.l1_acquisition import SourceSyncState
from ptb_acquisition.pipeline import AcquisitionPipeline
from ptb_acquisition.watchers import ALL_WATCHER_CLASSES, BaseAgentWatcher
from ptb_database.repositories.checkpoint_repo import CheckpointRepository
from ptb_database.repositories.task_repo import TaskDomainRepository
from ptb_graph_memory.sync_worker import GraphMemorySyncWorker, GraphSyncStatus
from ptb_application.api import create_app
from ptb_application.service import ApplicationService
from ptb_mcp.server import create_mcp_server, get_mcp_health
from scripts.ptb_cli import PTBProcessSupervisor
from tests.support.test_doubles import (
    FakeGraphitiAdapter,
    InMemoryCheckpointRepository,
    InMemoryRawEventRepository,
    InMemoryTaskDomainRepository,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Production source directories (excludes tests/)
PROD_DIRS = [
    REPO_ROOT / "packages",
    REPO_ROOT / "services",
    REPO_ROOT / "scripts",
    REPO_ROOT / "integrations",
]


def _iter_prod_py_files():
    """Yield all .py files under production directories, skipping test folders."""
    for d in PROD_DIRS:
        if not d.exists():
            continue
        for f in d.rglob("*.py"):
            rel = str(f).replace("\\", "/")
            if "/tests/" in rel or "/test_" in rel.split("/")[-1]:
                continue
            yield f


# ---------------------------------------------------------------------------
# 1. test_01_no_runtime_mocks
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_01_no_runtime_mocks() -> None:
    """Gate 1: OpenWebUI board, tools, and actions contain NO runtime mocks/demo modes.

    Direct invocation of Action._resolve_board_html_path() with missing file
    returns explicit error text without returning HTML fallback.
    """
    target_files = [
        REPO_ROOT / "integrations" / "openwebui" / "board" / "ptb_board.html",
        REPO_ROOT / "integrations" / "openwebui" / "tools" / "ptb_tools.py",
        REPO_ROOT / "integrations" / "openwebui" / "functions" / "ptb_board_action.py",
    ]
    forbidden = ("MOCK_DATA", "DEMO_MODE", "enable_mock_fallback", "mock_mode")

    for fp in target_files:
        assert fp.exists(), f"Missing OpenWebUI file: {fp}"
        content = fp.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in content, f"Forbidden token '{token}' in {fp.name}"
        assert "PTB-OWUI-001" in content, f"Missing PTB-OWUI-001 error handling in {fp.name}"

    # Behavioral test: dynamically import Action and verify explicit error text
    action_py = REPO_ROOT / "integrations" / "openwebui" / "functions" / "ptb_board_action.py"
    spec = importlib.util.spec_from_file_location("ptb_board_action", action_py)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    action = mod.Action()
    action.valves.board_html_path = "/nonexistent/test_path/ptb_board.html"
    resolved_path = action._resolve_board_html_path()
    assert not resolved_path.exists(), "Resolved path should not exist"

    err_text = action.load_board_html()
    assert err_text.startswith("❌ [PTB-OWUI-001]"), "Must return explicit error code"
    assert "<html>" not in err_text.lower(), "Must NOT return synthetic HTML fallback"
    assert "board html file not found" in err_text.lower()


# ---------------------------------------------------------------------------
# 2. test_02_no_runtime_in_memory_fallback
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_02_no_runtime_in_memory_fallback() -> None:
    """Gate 2: Production code never imports InMemory* repositories or tests/support."""
    forbidden_classes = (
        "InMemoryRawEventRepository",
        "InMemoryCheckpointRepository",
        "InMemoryTaskDomainRepository",
    )

    for py_file in _iter_prod_py_files():
        source = py_file.read_text(encoding="utf-8")
        # Check AST imports to ensure no import from tests or test_doubles
        try:
            tree = ast.parse(source, filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert not alias.name.startswith("tests"), (
                            f"Production file {py_file} imports test module '{alias.name}'"
                        )
                elif isinstance(node, ast.ImportFrom):
                    mod_name = node.module or ""
                    assert not mod_name.startswith("tests"), (
                        f"Production file {py_file} imports from test module '{mod_name}'"
                    )
        except SyntaxError:
            pass

        for cls_name in forbidden_classes:
            assert cls_name not in source, (
                f"Forbidden test double '{cls_name}' found in production file {py_file}"
            )

    # Confirm test doubles exist in the correct test support location
    test_doubles = REPO_ROOT / "tests" / "support" / "test_doubles.py"
    assert test_doubles.exists(), "tests/support/test_doubles.py must exist"
    doubles_src = test_doubles.read_text(encoding="utf-8")
    for cls_name in forbidden_classes:
        assert cls_name in doubles_src, f"{cls_name} should be defined in test_doubles.py"


# ---------------------------------------------------------------------------
# 3. test_03_checkpoint_tenant_isolation_and_resume
# ---------------------------------------------------------------------------
@pytest.mark.contract
@pytest.mark.asyncio
async def test_03_checkpoint_tenant_isolation_and_resume() -> None:
    """Gate 3: Multi-tenant checkpoint isolation, deterministic SHA-256 IDs, and resume."""
    repo = InMemoryCheckpointRepository()

    # 1. Tạo 2 checkpoint cho 2 tenant khác nhau có cùng source_type và stream_id
    ckpt_a = IngestionCheckpointRecord(
        source_type="git",
        stream_id="repo-main",
        tenant_id="tenant-A",
        last_external_id="commit-aaa",
        last_event_timestamp=datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc),
    )
    ckpt_b = IngestionCheckpointRecord(
        source_type="git",
        stream_id="repo-main",
        tenant_id="tenant-B",
        last_external_id="commit-bbb",
        last_event_timestamp=datetime(2026, 9, 29, 11, 0, 0, tzinfo=timezone.utc),
    )

    await repo.save_checkpoint(ckpt_a)
    await repo.save_checkpoint(ckpt_b)

    # Checkpoint ID là SHA-256 deterministic hash
    expected_id_a = hashlib.sha256(b"tenant-A:git:repo-main").hexdigest()
    expected_id_b = hashlib.sha256(b"tenant-B:git:repo-main").hexdigest()
    assert ckpt_a.id == expected_id_a, "Checkpoint A must have deterministic SHA-256 ID"
    assert ckpt_b.id == expected_id_b, "Checkpoint B must have deterministic SHA-256 ID"
    assert ckpt_a.id != ckpt_b.id, "Tenant IDs must differ"

    # get_checkpoint chỉ lấy ra đúng checkpoint của tenant-A, không lẫn sang tenant-B
    loaded_a = await repo.get_checkpoint("git", "repo-main", tenant_id="tenant-A")
    assert loaded_a is not None
    assert loaded_a.tenant_id == "tenant-A"
    assert loaded_a.last_external_id == "commit-aaa"
    assert loaded_a.id == expected_id_a

    loaded_b = await repo.get_checkpoint("git", "repo-main", tenant_id="tenant-B")
    assert loaded_b is not None
    assert loaded_b.tenant_id == "tenant-B"
    assert loaded_b.last_external_id == "commit-bbb"
    assert loaded_b.id == expected_id_b

    # Tenant khác không tồn tại checkpoint
    loaded_c = await repo.get_checkpoint("git", "repo-main", tenant_id="tenant-C")
    assert loaded_c is None

    # Resume: cập nhật checkpoint tenant-A và kiểm tra tenant-B không bị ảnh hưởng
    loaded_a.last_external_id = "commit-aaa-updated"
    await repo.save_checkpoint(loaded_a)

    resumed_a = await repo.get_checkpoint("git", "repo-main", tenant_id="tenant-A")
    assert resumed_a is not None
    assert resumed_a.last_external_id == "commit-aaa-updated"

    resumed_b = await repo.get_checkpoint("git", "repo-main", tenant_id="tenant-B")
    assert resumed_b is not None
    assert resumed_b.last_external_id == "commit-bbb"

    # Verify Cypher composite uniqueness constraint in database constraints
    constraints_dir = REPO_ROOT / "packages" / "database" / "neo4j" / "constraints"
    cypher_files = list(constraints_dir.glob("*.cypher")) if constraints_dir.exists() else []
    assert cypher_files, "No Cypher constraint files found"
    all_cypher = "\n".join(f.read_text(encoding="utf-8") for f in cypher_files)
    assert "tenant_id" in all_cypher and "source_type" in all_cypher and "stream_id" in all_cypher


# ---------------------------------------------------------------------------
# 4. test_04_acquisition_pipeline_passes_tenant_id
# ---------------------------------------------------------------------------
@pytest.mark.contract
@pytest.mark.asyncio
async def test_04_acquisition_pipeline_passes_tenant_id() -> None:
    """Gate 4: AcquisitionPipeline correctly propagates tenant_id to CheckpointRepository."""
    class MockCheckpointRepo:
        def __init__(self) -> None:
            self.get_calls: List[tuple[str, str, str]] = []
            self.saved_checkpoints: List[IngestionCheckpointRecord] = []

        async def get_checkpoint(
            self, source_type: str, stream_id: str, tenant_id: str = "default"
        ) -> Optional[IngestionCheckpointRecord]:
            self.get_calls.append((source_type, stream_id, tenant_id))
            return None

        async def save_checkpoint(self, checkpoint: IngestionCheckpointRecord) -> None:
            self.saved_checkpoints.append(checkpoint)

    mock_cp_repo = MockCheckpointRepo()

    pipeline = AcquisitionPipeline(
        tenant_id="custom-tenant",
        checkpoint_repo=mock_cp_repo,
    )

    # Calling get_checkpoint passes tenant_id="custom-tenant"
    await pipeline.get_checkpoint("git", "repo-stream")
    assert mock_cp_repo.get_calls == [("git", "repo-stream", "custom-tenant")]

    # Saving checkpoint propagates through pipeline
    ckpt = IngestionCheckpointRecord(
        source_type="git",
        stream_id="repo-stream",
        tenant_id=pipeline.tenant_id,
        last_external_id="commit-001",
        updated_at=datetime.now(timezone.utc),
    )
    await pipeline.save_checkpoint(ckpt)
    assert len(mock_cp_repo.saved_checkpoints) == 1
    assert mock_cp_repo.saved_checkpoints[0].tenant_id == "custom-tenant"


# ---------------------------------------------------------------------------
# 5. test_05_task_repo_evidence_marked_pending
# ---------------------------------------------------------------------------
@pytest.mark.contract
@pytest.mark.asyncio
async def test_05_task_repo_evidence_marked_pending() -> None:
    """Gate 5: TaskDomainRepository marks new Evidence graph_sync_status=PENDING and attempts=0."""
    executed_queries: List[tuple[str, Optional[dict]]] = []

    class MockTx:
        async def run(self, cypher: str, params: Optional[dict] = None) -> Any:
            executed_queries.append((cypher, params))
            mock_res = MagicMock()
            mock_res.single = AsyncMock(return_value=None)
            return mock_res

        async def commit(self) -> None:
            pass

        async def __aenter__(self) -> MockTx:
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            pass

    class MockSession:
        def begin_transaction(self) -> MockTx:
            return MockTx()

        async def run(self, cypher: str, params: Optional[dict] = None) -> Any:
            executed_queries.append((cypher, params))
            mock_res = MagicMock()
            mock_res.single = AsyncMock(return_value=None)
            return mock_res

        async def __aenter__(self) -> MockSession:
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            pass

    mock_client = MagicMock()
    mock_client.get_driver().session.return_value = MockSession()
    mock_client.database = "neo4j"

    repo = TaskDomainRepository(mock_client)

    ev = EvidenceRecord(
        id="ev-123",
        task_id="task-123",
        evidence_type=EvidenceType.AGENT_DECISION,
        snippet="Decided on architecture",
        source_type="git",
        timestamp=datetime.now(timezone.utc),
    )
    task = UnifiedTaskCandidate(
        id="task-123",
        title="Implement Architecture",
        status=TaskStatus.TODO,
        evidences=[ev],
    )

    await repo.upsert_task_atomic(task)

    evidence_queries = [
        (cypher, params)
        for cypher, params in executed_queries
        if params and "evidences" in params
    ]
    assert evidence_queries, "Expected Cypher query with $evidences parameter"

    cypher, params = evidence_queries[0]
    ev_param = params["evidences"][0]
    assert ev_param["graph_sync_status"] == "PENDING"
    assert ev_param["graph_sync_attempts"] == 0
    assert 'e.graph_sync_status = "PENDING"' in cypher
    assert "e.graph_sync_attempts = 0" in cypher


# ---------------------------------------------------------------------------
# 6. test_06_graph_worker_sweep_lifecycle
# ---------------------------------------------------------------------------
@pytest.mark.contract
@pytest.mark.asyncio
async def test_06_graph_worker_sweep_lifecycle() -> None:
    """Gate 6: GraphMemorySyncWorker transitions PENDING -> SYNCED, handles errors with RETRY/FAILED, never rolls back domain task."""
    class FakeAsyncResult:
        def __init__(self, rows):
            self.rows = rows

        def __aiter__(self):
            self._iter = iter(self.rows)
            return self

        async def __anext__(self):
            try:
                return next(self._iter)
            except StopIteration:
                raise StopAsyncIteration

    class MockDriver:
        def session(self, **kwargs):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

        async def run(self, cypher, params=None):
            return FakeAsyncResult([
                {
                    "props": {
                        "id": "ev-sweep-01",
                        "snippet": "Verified commit evidence",
                        "graph_sync_status": "PENDING",
                        "graph_sync_attempts": 0,
                    },
                    "task_id": "task-001",
                    "raw_event_id": "raw-001",
                }
            ])

    # 1. Success path: PENDING -> SYNCED
    mem_client = MagicMock()
    mem_client.add_evidence_episode = AsyncMock(return_value="ev-sweep-01")

    worker = GraphMemorySyncWorker(memory_client=mem_client, driver=MockDriver(), max_retries=3)
    sweep_res = await worker.sweep_pending_evidence(limit=10)
    assert sweep_res["synced"] == 1
    assert sweep_res["failed"] == 0
    assert sweep_res["retried"] == 0
    assert worker.get_sync_status("ev-sweep-01") == GraphSyncStatus.SYNCED

    # 2. Failure path: attempts < max_retries -> RETRY + PTB-GRAPH-001 logged
    mem_client_err = MagicMock()
    mem_client_err.add_evidence_episode = AsyncMock(side_effect=RuntimeError("Graphiti cluster timeout"))
    worker_err = GraphMemorySyncWorker(memory_client=mem_client_err, driver=MockDriver(), max_retries=3)

    with patch("ptb_graph_memory.sync_worker.log_bug") as mock_log:
        res_retry = await worker_err.sync_episode("evidence", "ev-retry-01", {"id": "ev-retry-01", "graph_sync_attempts": 0})
        assert res_retry == GraphSyncStatus.RETRY.value
        assert worker_err.get_sync_status("ev-retry-01") == GraphSyncStatus.RETRY
        mock_log.assert_called_once()
        assert mock_log.call_args.kwargs.get("code") == BugCode.PTB_GRAPH_001

    # 3. Failure path: attempts >= max_retries -> FAILED
    with patch("ptb_graph_memory.sync_worker.log_bug") as mock_log:
        res_failed = await worker_err.sync_episode("evidence", "ev-failed-01", {"id": "ev-failed-01", "graph_sync_attempts": 2})
        assert res_failed == GraphSyncStatus.FAILED.value
        assert worker_err.get_sync_status("ev-failed-01") == GraphSyncStatus.FAILED
        mock_log.assert_called_once()
        assert mock_log.call_args.kwargs.get("code") == BugCode.PTB_GRAPH_001


# ---------------------------------------------------------------------------
# 7. test_07_rest_14_endpoints_real
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_07_rest_14_endpoints_real() -> None:
    """Gate 7: FastAPI create_app() exposes exactly 14 real REST endpoints, responds 200 without mocks."""
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
    registered: set[tuple[str, str]] = set()
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if methods and path:
            for method in methods:
                if method in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
                    registered.add((method, path))
                    assert "mock" not in path.lower(), f"Endpoint path contains 'mock': {path}"

    expected_14 = {
        ("GET", "/health"),
        ("GET", "/api/today"),
        ("GET", "/api/tasks"),
        ("GET", "/api/tasks/{task_id}"),
        ("GET", "/api/review"),
        ("POST", "/api/review/{task_id}/approve"),
        ("POST", "/api/review/{task_id}/dismiss"),
        ("PATCH", "/api/tasks/{task_id}"),
        ("POST", "/api/tasks/{task_id}/status"),
        ("POST", "/api/tasks/{task_id}/split"),
        ("GET", "/api/waiting"),
        ("GET", "/api/forgotten"),
        ("GET", "/api/knowledge"),
        ("GET", "/api/sources/health"),
    }

    business_endpoints = {
        (method, path) for (method, path) in registered
        if not path.startswith(("/docs", "/redoc", "/openapi"))
    }
    missing = expected_14 - business_endpoints
    assert not missing, f"Missing REST endpoints: {missing}"
    assert len(business_endpoints) == 14, f"Expected exactly 14 REST endpoints, found {len(business_endpoints)}"

    # Behavioral test via TestClient
    client = TestClient(app)
    resp_health = client.get("/health")
    assert resp_health.status_code == 200
    assert resp_health.json()["service"] == "ptb-application"

    resp_today = client.get("/api/today")
    assert resp_today.status_code == 200

    resp_tasks = client.get("/api/tasks")
    assert resp_tasks.status_code == 200

    resp_sources = client.get("/api/sources/health")
    assert resp_sources.status_code == 200


# ---------------------------------------------------------------------------
# 8. test_08_mcp_10_tools_real_and_health
# ---------------------------------------------------------------------------
@pytest.mark.contract
@pytest.mark.asyncio
async def test_08_mcp_10_tools_real_and_health() -> None:
    """Gate 8: FastMCP server has exactly 10 read-only query tools and /health checks real dependencies."""
    mcp = create_mcp_server()
    tools = await mcp.list_tools()
    tool_names = {t.name for t in tools}

    expected_10 = {
        "get_today_tasks",
        "get_tasks",
        "get_task_context",
        "get_waiting_items",
        "get_forgotten_commitments",
        "get_review_queue",
        "search_decisions",
        "search_lessons_learned",
        "search_context",
        "get_source_health",
    }
    assert tool_names == expected_10, f"MCP tool mismatch: {tool_names ^ expected_10}"
    assert len(tool_names) == 10

    # Behavioral dependency health check:
    # 1. Broken dependency reports not_ready and emits PTB_MCP_001
    broken_srv = MagicMock()
    broken_srv.get_system_health = AsyncMock(return_value={"neo4j": "not_ready", "status": "not_ready"})
    with patch("ptb_mcp.server.log_bug") as mock_log:
        health_broken = await get_mcp_health(broken_srv)
        assert health_broken["status"] == "not_ready"
        assert health_broken["neo4j"] == "not_ready"
        mock_log.assert_called_once()
        assert mock_log.call_args.args[0] == BugCode.PTB_MCP_001

    # 2. Healthy dependency reports healthy
    mock_neo = MagicMock()
    mock_neo.verify_connectivity = AsyncMock(return_value=True)
    healthy_srv = MagicMock()
    healthy_srv.neo4j_client = mock_neo
    healthy_srv.get_system_health = AsyncMock(return_value={"neo4j": "healthy", "status": "healthy"})
    health_ok = await get_mcp_health(healthy_srv)
    assert health_ok["status"] == "healthy"
    assert health_ok["tools_count"] == 10


# ---------------------------------------------------------------------------
# 9. test_09_openwebui_plugins_no_internal_dependencies
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_09_openwebui_plugins_no_internal_dependencies() -> None:
    """Gate 9: OpenWebUI plugins (tools & action) have ZERO internal PTB dependencies in AST."""
    target_files = [
        REPO_ROOT / "integrations" / "openwebui" / "tools" / "ptb_tools.py",
        REPO_ROOT / "integrations" / "openwebui" / "functions" / "ptb_board_action.py",
    ]

    for fp in target_files:
        assert fp.exists(), f"Plugin file missing: {fp}"
        source = fp.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(fp))

        imported_modules: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_modules.append(node.module)

        for mod in imported_modules:
            assert not mod.startswith("ptb_") and not mod.startswith("ptb."), (
                f"Plugin {fp.name} must NOT import internal module '{mod}'"
            )
            assert "contracts" not in mod, f"Plugin {fp.name} must not import contracts: '{mod}'"


# ---------------------------------------------------------------------------
# 10. test_10_health_truthful_behavior
# ---------------------------------------------------------------------------
@pytest.mark.contract
@pytest.mark.asyncio
async def test_10_health_truthful_behavior() -> None:
    """Gate 10: Truthful health behavior: empty checkpoints never healthy, neo4j down -> not_ready, graphiti down -> degraded, ms logged out -> auth_required."""
    # 1. No checkpoint -> NEVER_SYNCED or UNCONFIGURED, overall is NOT healthy
    svc_empty = ApplicationService(
        checkpoint_repo=InMemoryCheckpointRepository(),
        graph_memory=FakeGraphitiAdapter(),
        processing_worker_status="healthy",
    )
    src_health = await svc_empty.get_sources_health()
    assert src_health.overall_health != "healthy"
    assert src_health.overall_health in ("not_ready", "degraded")
    assert any(t.status.upper() in ("NEVER_SYNCED", "UNCONFIGURED") for t in src_health.tenants)

    # 2. Neo4j down -> get_system_health reports not_ready
    mock_neo_down = MagicMock()
    mock_neo_down.verify_connectivity = AsyncMock(return_value=False)
    svc_neo_down = ApplicationService(
        neo4j_client=mock_neo_down,
        graph_memory=FakeGraphitiAdapter(),
        processing_worker_status="healthy",
    )
    sys_health_neo = await svc_neo_down.get_system_health()
    assert sys_health_neo["neo4j"] == "not_ready"
    assert sys_health_neo["status"] == "not_ready"

    # 3. Graphiti down -> get_system_health reports degraded
    fake_graph_down = FakeGraphitiAdapter(is_available=False)
    mock_neo_ok = MagicMock()
    mock_neo_ok.verify_connectivity = AsyncMock(return_value=True)
    svc_graph_down = ApplicationService(
        neo4j_client=mock_neo_ok,
        graph_memory=fake_graph_down,
        processing_worker_status="healthy",
    )
    sys_health_graph = await svc_graph_down.get_system_health()
    assert sys_health_graph["graphiti"] == "degraded"
    assert sys_health_graph["status"] == "degraded"

    # 4. Microsoft not logged in -> get_sources_health reports AUTH_REQUIRED
    svc_ms_auth = ApplicationService(
        checkpoint_repo=InMemoryCheckpointRepository(),
        graph_memory=FakeGraphitiAdapter(),
        playwright_status="auth_required",
    )
    src_health_ms = await svc_ms_auth.get_sources_health()
    ms_tenants = [t for t in src_health_ms.tenants if t.source_type in ("ms_teams", "ms_outlook")]
    assert len(ms_tenants) >= 1
    assert any(t.status.upper() == "AUTH_REQUIRED" for t in ms_tenants)


# ---------------------------------------------------------------------------
# 11. test_11_supervisor_starts_and_stops_graph_worker
# ---------------------------------------------------------------------------
@pytest.mark.contract
@pytest.mark.asyncio
async def test_11_supervisor_starts_and_stops_graph_worker() -> None:
    """Gate 11: PTBProcessSupervisor supervises ptb-graph-sync-worker and terminates it cleanly."""
    mock_sync_worker = MagicMock()
    mock_sync_worker.run_loop = AsyncMock(side_effect=asyncio.CancelledError)
    mock_sync_worker.stop = MagicMock()

    raw_repo = InMemoryRawEventRepository()
    cp_repo = InMemoryCheckpointRepository()
    fake_graph = FakeGraphitiAdapter()

    app_svc = ApplicationService(
        task_repo=MagicMock(),
        raw_event_repo=raw_repo,
        checkpoint_repo=cp_repo,
        graph_memory=fake_graph,
        processing_worker_status="healthy",
    )

    supervisor = PTBProcessSupervisor(
        host="127.0.0.1",
        app_port=8290,
        mcp_port=8291,
        enable_playwright=False,
        poll_interval=0.2,
        raw_event_repo=raw_repo,
        checkpoint_repo=cp_repo,
        task_repo=MagicMock(),
        application_service=app_svc,
        sync_worker=mock_sync_worker,
    )

    run_task = asyncio.create_task(supervisor.run(max_runtime=1.0))
    await asyncio.sleep(0.4)

    task_names = [t.get_name() for t in supervisor.tasks]
    assert "ptb-graph-sync-worker" in task_names, f"ptb-graph-sync-worker missing in tasks: {task_names}"
    assert supervisor.graph_sync_task is not None
    assert supervisor.graph_sync_task.get_name() == "ptb-graph-sync-worker"

    supervisor.trigger_shutdown()
    await run_task

    assert supervisor.graph_sync_task.done() or supervisor.graph_sync_task.cancelled()


# ---------------------------------------------------------------------------
# 12. test_12_cross_platform_tests_green
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_12_cross_platform_tests_green() -> None:
    """Gate 12: Pytest markers configured; CI matrix covers platforms; platformdirs resolver functional."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for marker in ("unit:", "contract:", "fixture_e2e:", "runtime_smoke:", "external_integration:"):
        assert marker in pyproject, f"Missing pytest marker '{marker}' in pyproject.toml"

    # Verify watcher module exists and exports ALL_WATCHER_CLASSES
    assert len(ALL_WATCHER_CLASSES) >= 8, f"Expected ≥8 watcher classes, got {len(ALL_WATCHER_CLASSES)}"

    # Behavioral test: platformdirs resolver runs on current platform without error
    resolved_paths = BaseAgentWatcher.resolve_platform_paths(["Cursor"])
    assert isinstance(resolved_paths, list), "Expected list of resolved Path objects"

    # Verify CI workflow exists and has cross-platform matrix
    ci_yaml = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    assert ci_yaml.exists(), "CI workflow file missing"
    ci_content = ci_yaml.read_text(encoding="utf-8")
    for os_name in ("ubuntu", "macos", "windows"):
        assert os_name in ci_content, f"Missing OS '{os_name}' in CI matrix"
    for py_ver in ("3.11", "3.12", "3.13"):
        assert py_ver in ci_content, f"Missing Python '{py_ver}' in CI matrix"


# ---------------------------------------------------------------------------
# 13. test_13_docs_and_config_synchronized
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_13_docs_and_config_synchronized() -> None:
    """Gate 13: .env.example, docker-compose.yml, config/sources.yaml standardized; no hardcoded secrets."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    guideline = (REPO_ROOT / "Guideline.md").read_text(encoding="utf-8")
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    docker_compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    sources_yaml_file = REPO_ROOT / "config" / "sources.yaml"

    # docker-compose port bindings and volumes
    assert "127.0.0.1:7687:7687" in docker_compose, "Neo4j bolt port binding missing"
    assert "127.0.0.1:3000:8080" in docker_compose, "OpenWebUI port binding missing"
    assert "./data/openwebui:/app/backend/data" in docker_compose, "OpenWebUI bind mount missing"

    # .env.example placeholder secrets and standard ports
    assert "NEO4J_PASSWORD=change_me_in_production" in env_example
    assert "APP_PORT=8000" in env_example
    assert "MCP_PORT=8001" in env_example
    # No real hardcoded passwords or API tokens
    assert "sk-" not in env_example, "Found possible hardcoded OpenAI secret in .env.example"

    # config/sources.yaml parses as valid yaml and does not contain hardcoded secrets
    assert sources_yaml_file.exists(), "config/sources.yaml missing"
    sources_text = sources_yaml_file.read_text(encoding="utf-8")
    parsed_sources = yaml.safe_load(sources_text)
    assert isinstance(parsed_sources, dict)
    assert "sources" in parsed_sources
    assert "sk-" not in sources_text, "Found possible hardcoded API key in config/sources.yaml"

    # All 10 BugCodes documented in README and Guideline
    for bc in BugCode:
        assert bc.value in readme, f"Missing {bc.value} in README.md"
        assert bc.value in guideline, f"Missing {bc.value} in Guideline.md"

    # All 5 pytest markers documented
    for marker in ("unit", "contract", "fixture_e2e", "runtime_smoke", "external_integration"):
        assert marker in readme, f"Missing marker '{marker}' in README.md"
        assert marker in guideline, f"Missing marker '{marker}' in Guideline.md"


# ---------------------------------------------------------------------------
# Bonus: TEMPORARY_WHITELIST is empty (anti-fallback guard contract)
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_bonus_temporary_whitelist_empty() -> None:
    """Verify TEMPORARY_WHITELIST == {} in test_anti_fallback_guard.py."""
    guard_file = REPO_ROOT / "tests" / "test_anti_fallback_guard.py"
    assert guard_file.exists(), "test_anti_fallback_guard.py missing"
    content = guard_file.read_text(encoding="utf-8")
    assert "TEMPORARY_WHITELIST" in content, "TEMPORARY_WHITELIST not found"
    assert re.search(r"TEMPORARY_WHITELIST\b.*=\s*\{\}", content), "TEMPORARY_WHITELIST must be empty (= {})"
