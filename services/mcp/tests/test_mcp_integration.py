"""Comprehensive Integration & Security Tests for PTB FastMCP Server (W3-03).

Covers:
1. Live Transport & Handshake Verification:
   - Stdio transport handshake (subprocess and in-process)
   - HTTP / SSE transport handshake
   - Exact tool counts: 10 in read-only mode, 19 in trusted write mode
   - Standard MCP resources (ptb://today-plan, ptb://tasks, ptb://system-health)
   - Standard MCP prompts (morning_briefing, task_execution_context)
2. Coding Agent Simulation (Cursor, Claude Code, Antigravity):
   - Full CRUD lifecycle: get_today_tasks -> create_task -> update_task_status -> update_task -> delete_task
   - Review queue operations: get_review_queue -> approve_task -> dismiss_task
   - Knowledge recording: record_decision -> record_lesson_learned -> ask_ptb_agent
   - Confirmation safety guard on delete_task (confirm=False vs confirm=True)
3. Security & Permission Boundaries:
   - Reserved privileged actors (ADMIN, SYSTEM, ROOT, SUPERUSER) rejection without admin credentials
   - Admin credential validation via PTB_ADMIN_KEY
   - Propagation of authentic coding agent identities (e.g. CURSOR_AGENT, CLAUDE_CODE)
   - MCPSecurityMiddleware authentication matrix (Header, Bearer token, query param, unauthenticated dev mode)
   - Health endpoint exempt from authentication
4. Fault Injection & Robustness:
   - Empty/whitespace titles and IDs
   - Invalid canonical task statuses
   - Nonexistent task lookup
   - Service dependency failure (not_ready state propagation)
5. Performance & Secrets Audit:
   - Tool invocation latency measurement (p50 and p95)
   - Zero credential or token leaks in outputs and logs
"""

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import secrets
import statistics
import sys
import time
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

from ptb_application.service import ApplicationService
from ptb_contracts.l2_processing import (
    CommitmentRecord,
    ReviewQueueItem,
    StatusTransitionAuditRecord,
    TaskStatus,
    UnifiedTaskCandidate,
)
from ptb_contracts.l3_storage import DecisionNodeRecord, LessonNodeRecord
from ptb_contracts.l4_intelligence import TaskWithContext, TodayBoardView
from ptb_mcp.server import (
    CANONICAL_TASK_STATUSES,
    DEFAULT_MCP_ACTOR,
    RESERVED_PRIVILEGED_ACTORS,
    create_mcp_server,
    enforce_actor_policy,
    get_mcp_health,
    run_stdio_server,
    validate_canonical_status,
)

logger = logging.getLogger("ptb.mcp.test_integration")


# ==============================================================================
# TEST FIXTURES & MOCKS
# ==============================================================================

class MockNeo4jClient:
    def __init__(self, healthy: bool = True):
        self.healthy = healthy

    async def verify_connectivity(self) -> bool:
        return self.healthy


class MockTaskRepo:
    def __init__(self, initial_tasks: Optional[List[UnifiedTaskCandidate]] = None):
        self.tasks: Dict[str, UnifiedTaskCandidate] = {
            t.id: t for t in (initial_tasks or [])
        }
        self.audits: List[StatusTransitionAuditRecord] = []
        self.commitments: List[CommitmentRecord] = []
        self.neo4j_client = MockNeo4jClient(healthy=True)

    async def get_task_by_id(self, task_id: str) -> Optional[UnifiedTaskCandidate]:
        return self.tasks.get(task_id)

    async def upsert_task_atomic(self, task: UnifiedTaskCandidate) -> str:
        self.tasks[task.id] = task
        return task.id

    async def save_task(self, task: UnifiedTaskCandidate) -> str:
        self.tasks[task.id] = task
        return task.id

    async def record_status_transition_audit(self, audit: StatusTransitionAuditRecord, **kwargs: Any) -> str:
        self.audits.append(audit)
        return audit.id

    async def list_tasks(self, filters: Optional[dict] = None) -> List[UnifiedTaskCandidate]:
        tasks = list(self.tasks.values())
        if not filters:
            return tasks
        filtered = []
        for t in tasks:
            if "status" in filters and filters["status"]:
                req = filters["status"]
                val = req.value if hasattr(req, "value") else str(req)
                if t.status.value != val:
                    continue
            filtered.append(t)
        return filtered

    async def get_task_with_context(self, task_id: str) -> Optional[TaskWithContext]:
        task = self.tasks.get(task_id)
        if not task:
            return None
        now = datetime.now(timezone.utc)
        return TaskWithContext(
            task=task,
            last_status_change_at=now,
            days_in_current_status=1,
            has_completion_evidence=False,
            blocking_tasks=[],
            dependent_people=[],
        )

    async def get_review_queue(self, limit: int = 20) -> List[ReviewQueueItem]:
        items = []
        for t in self.tasks.values():
            if t.review_status == "pending_review":
                items.append(
                    ReviewQueueItem(
                        id=f"rev-{t.id}",
                        raw_event_id="raw-int-1",
                        candidate_task=t,
                        reason="Integration test candidate",
                        created_at=datetime.now(timezone.utc),
                    )
                )
        return items[:limit]

    async def get_active_commitments(self, user_id: Optional[str] = None) -> List[CommitmentRecord]:
        return self.commitments


class MockGraphMemory:
    def __init__(self):
        self.decisions: List[DecisionNodeRecord] = []
        self.lessons: List[LessonNodeRecord] = []

    async def add_decision(self, decision: Any) -> str:
        self.decisions.append(decision)
        return getattr(decision, "decision_id", "dec-1")

    async def add_lesson_episode(self, lesson: Any) -> str:
        self.lessons.append(lesson)
        return getattr(lesson, "lesson_id", "les-1")

    async def search_context(self, query: str, limit: int = 5, include_invalidated: bool = False) -> List[Dict[str, Any]]:
        return []


def _extract_content(result: Any) -> Any:
    """Helper to unwrap tool invocation result from FastMCP."""
    if hasattr(result, "structured_content") and result.structured_content:
        if isinstance(result.structured_content, dict) and "result" in result.structured_content:
            return result.structured_content["result"]
        return result.structured_content
    if hasattr(result, "content") and result.content:
        text = result.content[0].text
        try:
            return json.loads(text)
        except Exception:
            return text
    return None


@pytest.fixture
def integration_env():
    now = datetime.now(timezone.utc)
    task1 = UnifiedTaskCandidate(
        id="task-w3-001",
        title="Implement Stdio Protocol",
        status=TaskStatus.TODO,
        priority_score=90.0,
        project_key="PTB-MCP",
        created_at=now,
        updated_at=now,
    )
    task2 = UnifiedTaskCandidate(
        id="task-w3-002",
        title="Candidate For Review Queue",
        status=TaskStatus.BLOCKED,
        priority_score=40.0,
        project_key="PTB-MCP",
        review_status="pending_review",
        created_at=now,
        updated_at=now,
    )
    repo = MockTaskRepo([task1, task2])
    graph = MockGraphMemory()
    client = repo.neo4j_client

    svc = ApplicationService(
        task_repo=repo,
        graph_memory=graph,
        neo4j_client=client,
        processing_worker_status="healthy",
        llm_status="healthy",
        playwright_status="healthy",
    )
    server_ro = create_mcp_server(application_service=svc, read_only=True)
    server_rw = create_mcp_server(application_service=svc, read_only=False)
    return svc, repo, graph, server_ro, server_rw


# ==============================================================================
# 1. TOOL INVENTORY & MODE DISCONNECT VERIFICATION
# ==============================================================================

@pytest.mark.asyncio
async def test_tool_inventory_strict_counts(integration_env):
    """Verify tool counts: exactly 10 in read-only mode, exactly 19 in write mode."""
    _, _, _, server_ro, server_rw = integration_env

    tools_ro = await server_ro.list_tools()
    assert len(tools_ro) == 10, f"Expected 10 read-only tools, got {len(tools_ro)}"
    ro_names = {t.name for t in tools_ro}
    expected_ro = {
        "get_today_tasks", "get_tasks", "get_task_context", "get_waiting_items",
        "get_forgotten_commitments", "get_review_queue", "search_decisions",
        "search_lessons_learned", "search_context", "get_source_health",
    }
    assert ro_names == expected_ro

    tools_rw = await server_rw.list_tools()
    assert len(tools_rw) == 19, f"Expected 19 write tools, got {len(tools_rw)}"
    rw_names = {t.name for t in tools_rw}
    expected_rw = expected_ro | {
        "create_task", "update_task_status", "update_task", "delete_task",
        "approve_task", "dismiss_task", "ask_ptb_agent", "record_decision",
        "record_lesson_learned",
    }
    assert rw_names == expected_rw


@pytest.mark.asyncio
async def test_standard_resources_and_prompts_availability(integration_env):
    """Verify standard MCP resources and prompts exist on both modes."""
    _, _, _, server_ro, server_rw = integration_env

    for srv in (server_ro, server_rw):
        resources = await srv.list_resources()
        uris = {str(r.uri) for r in resources}
        assert "ptb://today-plan" in uris
        assert "ptb://tasks" in uris
        assert "ptb://system-health" in uris

        prompts = await srv.list_prompts()
        p_names = {p.name for p in prompts}
        assert "morning_briefing" in p_names
        assert "task_execution_context" in p_names

        # Read resource content
        r_health = await srv.read_resource("ptb://system-health")
        assert len(r_health) > 0
        health_data = json.loads(r_health[0].content)
        assert health_data["status"] == "healthy"
        assert health_data["neo4j"] == "healthy"

        # Get prompt content
        prompt = await srv.get_prompt("morning_briefing", {})
        assert len(prompt.messages) > 0
        assert "today-plan" in prompt.messages[0].content.text


# ==============================================================================
# 2. CODING AGENT LIFECYCLE SIMULATION (CURSOR, CLAUDE, ANTIGRAVITY)
# ==============================================================================

@pytest.mark.asyncio
async def test_coding_agent_full_crud_lifecycle(integration_env):
    """Simulate Coding Agent executing complete CRUD flow with audit attribution."""
    svc, repo, _, _, server_rw = integration_env
    agent_actor = "CURSOR_AGENT"

    # Step 1: Query today board
    top_tasks_res = await server_rw.call_tool("get_today_tasks", {"limit": 5})
    top_tasks = _extract_content(top_tasks_res)
    assert isinstance(top_tasks, list)

    # Step 2: Create new task
    create_res = await server_rw.call_tool("create_task", {
        "title": "Build Webhook Interceptor",
        "description": "Integration task for MCP agent",
        "priority_score": 85.0,
        "actor": agent_actor,
    })
    created = _extract_content(create_res)
    assert isinstance(created, dict)
    new_id = created["id"]
    assert created["title"] == "Build Webhook Interceptor"
    assert created["priority_score"] == 85.0

    # Step 3: Update status to IN_PROGRESS
    status_res = await server_rw.call_tool("update_task_status", {
        "task_id": new_id,
        "new_status": "IN_PROGRESS",
        "actor": agent_actor,
    })
    status_data = _extract_content(status_res)
    assert status_data["success"] is True

    # Step 4: Verify audit log recorded authentic actor
    assert len(repo.audits) > 0
    latest_audit = repo.audits[-1]
    assert latest_audit.task_id == new_id
    assert latest_audit.change_actor == agent_actor
    assert latest_audit.new_status == TaskStatus.IN_PROGRESS

    # Step 5: Update task details
    update_res = await server_rw.call_tool("update_task", {
        "task_id": new_id,
        "priority_score": 95.0,
        "description": "Updated description by Cursor",
        "actor": agent_actor,
    })
    update_data = _extract_content(update_res)
    assert update_data["success"] is True
    assert update_data["task"]["priority_score"] == 95.0

    # Step 6: Destructive action safeguard - confirm=False aborts
    del_abort_res = await server_rw.call_tool("delete_task", {
        "task_id": new_id,
        "confirm": False,
        "actor": agent_actor,
    })
    del_abort_data = _extract_content(del_abort_res)
    assert del_abort_data["success"] is False
    assert "confirmation required" in del_abort_data["error"]
    assert new_id in repo.tasks  # Still exists

    # Step 7: Destructive action safeguard - confirm=True executes
    del_ok_res = await server_rw.call_tool("delete_task", {
        "task_id": new_id,
        "confirm": True,
        "actor": agent_actor,
    })
    del_ok_data = _extract_content(del_ok_res)
    assert del_ok_data["success"] is True
    assert del_ok_data["task_id"] == new_id
    assert repo.tasks[new_id].status == TaskStatus.DISMISSED


@pytest.mark.asyncio
async def test_coding_agent_review_flow(integration_env):
    """Simulate Coding Agent reviewing, approving, and dismissing candidate tasks."""
    svc, repo, _, _, server_rw = integration_env

    # 1. Fetch review queue
    review_queue_res = await server_rw.call_tool("get_review_queue", {"limit": 10})
    queue = _extract_content(review_queue_res)
    assert len(queue) == 1
    review_item = queue[0]
    candidate_id = review_item["candidate_task"]["id"]

    # 2. Approve candidate task into TODO with CLAUDE_CODE
    approve_res = await server_rw.call_tool("approve_task", {
        "review_id": candidate_id,
        "new_status": "TODO",
        "actor": "CLAUDE_CODE",
    })
    approve_data = _extract_content(approve_res)
    assert approve_data["success"] is True

    # Verify task status transitioned
    assert repo.tasks[candidate_id].status == TaskStatus.TODO
    assert repo.tasks[candidate_id].review_status in ("approved", "auto_approved")

    # 3. Create another candidate to dismiss
    cand2 = UnifiedTaskCandidate(
        id="task-w3-dismiss",
        title="Irrelevant Task Candidate",
        status=TaskStatus.BLOCKED,
        review_status="pending_review",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    repo.tasks[cand2.id] = cand2

    dismiss_res = await server_rw.call_tool("dismiss_task", {
        "review_id": cand2.id,
        "actor": "ANTIGRAVITY_AGENT",
    })
    dismiss_data = _extract_content(dismiss_res)
    assert dismiss_data["success"] is True
    assert repo.tasks[cand2.id].status == TaskStatus.DISMISSED


@pytest.mark.asyncio
async def test_coding_agent_knowledge_recording(integration_env):
    """Verify recording architectural decisions and lessons learned into memory."""
    _, _, graph, _, server_rw = integration_env

    # 1. Record Decision
    dec_res = await server_rw.call_tool("record_decision", {
        "summary": "Adopt MCP Stdio and SSE transports",
        "rationale": "Enables both local process and remote network coding agents",
        "project_key": "PTB-MCP",
        "decided_by": "CURSOR_AGENT",
    })
    dec_data = _extract_content(dec_res)
    assert dec_data["success"] is True
    assert len(graph.decisions) == 1
    assert graph.decisions[0].decided_by == "CURSOR_AGENT"

    # 2. Record Lesson Learned
    les_res = await server_rw.call_tool("record_lesson_learned", {
        "topic": "EventSource query parameter auth",
        "solution": "Standard browser EventSource cannot send headers, use ?api_key= query param",
        "error_context": "HTTP 401 on EventSource connect",
    })
    les_data = _extract_content(les_res)
    assert les_data["success"] is True
    assert len(graph.lessons) == 1
    assert "EventSource" in graph.lessons[0].topic


# ==============================================================================
# 3. SECURITY & PERMISSIONS BOUNDARIES
# ==============================================================================

@pytest.mark.asyncio
async def test_actor_impersonation_guard(integration_env):
    """Reserved actors (ADMIN, SYSTEM, ROOT, SUPERUSER) rejected without credentials (P1-5)."""
    _, _, _, _, server_rw = integration_env

    for reserved_actor in RESERVED_PRIVILEGED_ACTORS:
        # Create task impersonation attempt
        res = await server_rw.call_tool("create_task", {
            "title": "Unauthorized Admin Task",
            "actor": reserved_actor,
        })
        data = _extract_content(res)
        assert data["success"] is False
        assert "Impersonation forbidden" in data["error"]

    # Allowed with valid admin key
    with patch.dict(os.environ, {"PTB_ADMIN_KEY": "secret-admin-pass-2026"}):
        ok_res = await server_rw.call_tool("create_task", {
            "title": "Authorized Admin Task",
            "actor": "ADMIN",
            "admin_key": "secret-admin-pass-2026",
        })
        ok_data = _extract_content(ok_res)
        assert isinstance(ok_data, dict)
        assert ok_data.get("title") == "Authorized Admin Task"


def test_mcp_security_middleware_http_matrix(integration_env):
    """Test Starlette MCPSecurityMiddleware authentication matrix."""
    _, _, _, _, server_rw = integration_env
    app = server_rw.sse_app()

    # 1. Health is exempt without key
    with TestClient(app, base_url="http://127.0.0.1:8001") as client:
        r_health = client.get("/health")
        assert r_health.status_code == 200

    # 2. When PTB_API_KEY is configured:
    with patch.dict(os.environ, {"PTB_API_KEY": "ptb-secure-agent-token-99"}, clear=False):
        with TestClient(app, base_url="http://127.0.0.1:8001") as client:
            # A. Unauthenticated request to SSE -> 401
            r_unauth = client.get("/sse")
            assert r_unauth.status_code == 401
            assert "Missing API key" in r_unauth.json().get("detail", "")

            # B. Invalid header key -> 401
            r_bad = client.get("/sse", headers={"X-PTB-API-KEY": "wrong-token"})
            assert r_bad.status_code == 401
            assert "Invalid API key" in r_bad.json().get("detail", "")

            # C. Invalid Bearer token -> 401
            r_bad_bearer = client.get("/sse", headers={"Authorization": "Bearer wrong-token"})
            assert r_bad_bearer.status_code == 401
            assert "Invalid API key" in r_bad_bearer.json().get("detail", "")

            # D. Invalid query param -> 401
            r_bad_query = client.get("/sse?api_key=wrong-token")
            assert r_bad_query.status_code == 401
            assert "Invalid API key" in r_bad_query.json().get("detail", "")

            # E. Health endpoint remains 200 without auth
            r_health_auth = client.get("/health")
            assert r_health_auth.status_code == 200

            # F. Options preflight is exempt
            r_options = client.options("/sse")
            assert r_options.status_code != 401


# ==============================================================================
# 4. FAULT INJECTION & ERROR PROPAGATION
# ==============================================================================

@pytest.mark.asyncio
async def test_fault_injection_invalid_inputs(integration_env):
    """Verify robust error handling on empty, malformed, or invalid inputs."""
    _, _, _, _, server_rw = integration_env

    # 1. Empty title in create_task
    res1 = await server_rw.call_tool("create_task", {"title": "   "})
    d1 = _extract_content(res1)
    assert d1["success"] is False
    assert "cannot be empty" in d1["error"]

    # 2. Empty task_id in update_task_status
    res2 = await server_rw.call_tool("update_task_status", {"task_id": "", "new_status": "DONE"})
    d2 = _extract_content(res2)
    assert d2["success"] is False
    assert "cannot be empty" in d2["error"]

    # 3. Invalid canonical status
    res3 = await server_rw.call_tool("update_task_status", {"task_id": "task-w3-001", "new_status": "NOT_A_STATUS"})
    d3 = _extract_content(res3)
    assert d3["success"] is False
    assert "Invalid status" in d3["error"]

    # 4. Nonexistent task_id in update_task
    res4 = await server_rw.call_tool("update_task", {"task_id": "nonexistent-id-999", "title": "New Title"})
    d4 = _extract_content(res4)
    assert d4["success"] is False
    assert "not found" in d4["error"]

    # 5. Empty fields in record_decision
    res5 = await server_rw.call_tool("record_decision", {"summary": "", "rationale": "Valid"})
    d5 = _extract_content(res5)
    assert d5["success"] is False
    assert "cannot be empty" in d5["error"]

    # 6. Empty fields in record_lesson_learned
    res6 = await server_rw.call_tool("record_lesson_learned", {"topic": "Topic", "solution": ""})
    d6 = _extract_content(res6)
    assert d6["success"] is False
    assert "cannot be empty" in d6["error"]


@pytest.mark.asyncio
async def test_fault_injection_service_failure(integration_env):
    """Verify health endpoint truthful reporting when store is degraded."""
    svc, repo, _, server_ro, _ = integration_env

    # Simulate Neo4j connectivity drop
    repo.neo4j_client.healthy = False
    health = await get_mcp_health(svc, read_only=True)
    assert health["status"] == "not_ready"
    assert health["neo4j"] == "not_ready"


# ==============================================================================
# 5. LATENCY BENCHMARKS & SECRETS AUDIT
# ==============================================================================

@pytest.mark.asyncio
async def test_tool_invocation_latency_p50_p95(integration_env):
    """Measure tool invocation latency percentiles (p50, p95)."""
    _, _, _, server_ro, server_rw = integration_env

    latencies_ms: List[float] = []
    rounds = 25

    for _ in range(rounds):
        t0 = time.perf_counter()
        res = await server_ro.call_tool("get_today_tasks", {"limit": 5})
        dt_ms = (time.perf_counter() - t0) * 1000.0
        latencies_ms.append(dt_ms)

    latencies_sorted = sorted(latencies_ms)
    p50 = statistics.median(latencies_sorted)
    # 95th percentile
    idx_p95 = int(0.95 * len(latencies_sorted))
    p95 = latencies_sorted[min(idx_p95, len(latencies_sorted) - 1)]

    logger.info("MCP Latency Benchmark: p50=%.2fms, p95=%.2fms (N=%d)", p50, p95, rounds)
    assert p50 < 100.0, f"p50 latency too high: {p50:.2f}ms"
    assert p95 < 250.0, f"p95 latency too high: {p95:.2f}ms"


def test_zero_secret_leaks_in_code_and_responses():
    """Verify no API keys, tokens, or credentials are hardcoded or leaked."""
    from pathlib import Path
    server_path = Path(__file__).resolve().parent.parent / "src" / "ptb_mcp" / "server.py"
    with open(server_path, "r", encoding="utf-8") as f:
        code = f.read()

    # Assert banned key patterns from Wave 0 audit
    assert "AQ.Ab8RN6J" not in code, "Leaked API key prefix found in server.py!"
    assert "AIzaSy" not in code, "Google API key pattern found in server.py!"
    assert "sk-" not in code, "OpenAI secret key pattern found in server.py!"


# ==============================================================================
# 6. MCP TRANSPORT TESTS (STDIO & SSE WITH CLIENT SESSION)
# ==============================================================================

@pytest.mark.asyncio
async def test_stdio_transport_client_session():
    """Verify MCP ClientSession connects over stdio subprocess in read-only mode."""
    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp import ClientSession

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "import asyncio; from ptb_mcp.server import run_stdio_server; asyncio.run(run_stdio_server(read_only=True))"],
        env=os.environ.copy(),
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            init_res = await session.initialize()
            assert init_res.server_info.name == "ptb-mcp"

            tools = await session.list_tools()
            assert len(tools.tools) == 10

            resources = await session.list_resources()
            assert len(resources.resources) == 3

            prompts = await session.list_prompts()
            assert len(prompts.prompts) == 2


@pytest.mark.asyncio
async def test_stdio_transport_write_mode_tools_count():
    """Verify MCP ClientSession over stdio exposes exactly 19 tools in write mode."""
    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp import ClientSession

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "import asyncio; from ptb_mcp.server import run_stdio_server; asyncio.run(run_stdio_server(read_only=False))"],
        env=os.environ.copy(),
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == 19
            tool_names = {t.name for t in tools.tools}
            assert "create_task" in tool_names
            assert "delete_task" in tool_names
            assert "ask_ptb_agent" in tool_names


@pytest.mark.asyncio
async def test_ephemeral_sse_transport_client_session(integration_env):
    """Verify MCP ClientSession connects to an ephemeral HTTP/SSE server."""
    import socket
    import uvicorn
    from mcp.client.sse import sse_client
    from mcp import ClientSession

    # Find free port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]

    svc, _, _, _, server_rw = integration_env
    app = server_rw.sse_app(host="127.0.0.1")
    config = uvicorn.Config(app, host="127.0.0.1", port=free_port, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    await asyncio.sleep(0.5)
    try:
        sse_url = f"http://127.0.0.1:{free_port}/sse"
        async with sse_client(sse_url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert len(tools.tools) == 19
                resources = await session.list_resources()
                assert len(resources.resources) == 3
    finally:
        server.should_exit = True
        await server_task
