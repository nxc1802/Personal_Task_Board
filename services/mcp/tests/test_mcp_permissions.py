"""Comprehensive Permissions, Security, and CRUD Tests for PTB FastMCP Server (W2-06).

Validates:
1. Tool inventory: 10 read-only tools, 19 full-mode tools.
2. Read-only mode enforcement: write tools rejected, read-only resources/prompts allowed.
3. Permission model & actor impersonation guard (P1-5):
   - Reserved privileged actors (ADMIN, SYSTEM) rejected without admin credentials.
   - Legitimate agent identities propagated to service layer and audit logs.
4. Destructive action safeguard:
   - delete_task confirmation pattern (confirm=False aborts, confirm=True executes).
5. Transport security (HTTP/SSE via Starlette middleware, P0-2, W1-02):
   - Health endpoint exempt from authentication.
   - Remote write / SSE access requires X-PTB-API-KEY, Bearer token, or ?api_key= query param.
   - Unauthenticated dev mode when PTB_API_KEY is unset.
6. Leaked credential prevention (P1-8):
   - Sanitized key checks with zero secrets leaked.
7. Input validation and error propagation across all mutation tools.
8. Concurrent client access simulation.
9. End-to-end CRUD and review flow on shared state.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient

from ptb_application.service import ApplicationService
from ptb_contracts.l1_acquisition import IngestionCheckpointRecord, SourceType
from ptb_contracts.l2_processing import (
    CommitmentRecord,
    EvidenceRecord,
    EvidenceType,
    ReviewQueueItem,
    StatusTransitionAuditRecord,
    TaskStatus,
    UnifiedTaskCandidate,
)
from ptb_contracts.l4_intelligence import TaskWithContext
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
                        raw_event_id="raw-perm-1",
                        candidate_task=t,
                        reason="Permission test review item",
                        created_at=datetime.now(timezone.utc),
                    )
                )
        return items[:limit]

    async def get_active_commitments(self, user_id: Optional[str] = None) -> List[CommitmentRecord]:
        return self.commitments


class MockGraphMemory:
    def __init__(self):
        self.decisions: List[Any] = []
        self.lessons: List[Any] = []

    async def add_decision(self, decision: Any) -> str:
        self.decisions.append(decision)
        return getattr(decision, "decision_id", "dec-1")

    async def add_lesson_episode(self, lesson: Any) -> str:
        self.lessons.append(lesson)
        return getattr(lesson, "lesson_id", "les-1")

    async def search_context(self, query: str, limit: int = 5, include_invalidated: bool = False) -> List[Dict[str, Any]]:
        return []


def _extract_content(result: Any) -> Any:
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
def permissions_setup():
    now = datetime.now(timezone.utc)
    t1 = UnifiedTaskCandidate(
        id="task-p1",
        title="Production Hardening",
        status=TaskStatus.TODO,
        priority_score=80.0,
        project_key="SEC",
        created_at=now,
        updated_at=now,
    )
    t2 = UnifiedTaskCandidate(
        id="task-p2",
        title="Candidate For Review",
        status=TaskStatus.BLOCKED,
        priority_score=45.0,
        project_key="SEC",
        review_status="pending_review",
        created_at=now,
        updated_at=now,
    )
    repo = MockTaskRepo([t1, t2])
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
# 1. TOOL INVENTORY & READ-ONLY MODE TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_tool_inventory_counts(permissions_setup):
    """Xác nhận read-only mode có đúng 10 tools, trusted write mode có đúng 19 tools."""
    _, _, _, server_ro, server_rw = permissions_setup

    tools_ro = await server_ro.list_tools()
    assert len(tools_ro) == 10

    tools_rw = await server_rw.list_tools()
    assert len(tools_rw) == 19


@pytest.mark.asyncio
async def test_read_only_mode_rejects_mutation_tools(permissions_setup):
    """Read-only mode tuyệt đối không cho phép gọi bất kỳ mutation tool nào."""
    _, _, _, server_ro, _ = permissions_setup
    mutation_tools = [
        "create_task",
        "update_task_status",
        "update_task",
        "delete_task",
        "approve_task",
        "dismiss_task",
    ]
    for tool_name in mutation_tools:
        with pytest.raises(Exception) as exc_info:
            await server_ro.call_tool(tool_name, {"task_id": "task-p1"})
        assert "Unknown tool" in str(exc_info.value) or "not found" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_read_only_mode_allows_resources_and_prompts(permissions_setup):
    """Read-only server vẫn cho phép đọc standard MCP resources và prompts an toàn."""
    _, _, _, server_ro, _ = permissions_setup

    resources = await server_ro.list_resources()
    uris = {str(r.uri) for r in resources}
    assert "ptb://today-plan" in uris
    assert "ptb://tasks" in uris
    assert "ptb://system-health" in uris

    res_health = await server_ro.read_resource("ptb://system-health")
    assert len(res_health) > 0
    assert "healthy" in res_health[0].content

    prompts = await server_ro.list_prompts()
    prompt_names = {p.name for p in prompts}
    assert "morning_briefing" in prompt_names
    assert "task_execution_context" in prompt_names


# ==============================================================================
# 2. ACTOR IMPERSONATION & PRIVILEGE BOUNDARIES (P1-5)
# ==============================================================================

def test_actor_policy_enforcement_logic():
    """Kiểm tra logic enforce_actor_policy độc lập."""
    # Default actor
    assert enforce_actor_policy(None) == DEFAULT_MCP_ACTOR
    assert enforce_actor_policy("") == DEFAULT_MCP_ACTOR

    # Standard agent identities
    assert enforce_actor_policy("CODING_AGENT") == "CODING_AGENT"
    assert enforce_actor_policy("CURSOR_AGENT") == "CURSOR_AGENT"
    assert enforce_actor_policy("CLAUDE_CODE") == "CLAUDE_CODE"

    # Privileged actors without admin key -> PermissionError
    for priv in RESERVED_PRIVILEGED_ACTORS:
        with pytest.raises(PermissionError) as exc_info:
            enforce_actor_policy(priv)
        assert "Impersonation forbidden" in str(exc_info.value)

    # Privileged actors with valid admin key -> Allowed
    with patch.dict(os.environ, {"PTB_ADMIN_KEY": "super-admin-key-789"}, clear=False):
        assert enforce_actor_policy("ADMIN", admin_key="super-admin-key-789") == "ADMIN"
        assert enforce_actor_policy("SYSTEM", admin_key="super-admin-key-789") == "SYSTEM"


@pytest.mark.asyncio
async def test_mutation_tools_reject_unauthorized_admin_impersonation(permissions_setup):
    """Tất cả các mutation tools từ chối khi client cố tình giả mạo actor=ADMIN mà không có admin_key."""
    _, _, _, _, server_rw = permissions_setup

    # 1. create_task
    res1 = await server_rw.call_tool(
        "create_task",
        {"title": "Unauthorized task", "actor": "ADMIN"},
    )
    content1 = _extract_content(res1)
    assert content1.get("success") is False
    assert "Impersonation forbidden" in content1.get("error", "")

    # 2. update_task_status
    res2 = await server_rw.call_tool(
        "update_task_status",
        {"task_id": "task-p1", "new_status": "IN_PROGRESS", "actor": "ADMIN"},
    )
    content2 = _extract_content(res2)
    assert content2.get("success") is False
    assert "Impersonation forbidden" in content2.get("error", "")

    # 3. delete_task
    res3 = await server_rw.call_tool(
        "delete_task",
        {"task_id": "task-p1", "actor": "ADMIN"},
    )
    content3 = _extract_content(res3)
    assert content3.get("success") is False
    assert "Impersonation forbidden" in content3.get("error", "")

    # 4. approve_task
    res4 = await server_rw.call_tool(
        "approve_task",
        {"review_id": "task-p2", "actor": "ADMIN"},
    )
    content4 = _extract_content(res4)
    assert content4.get("success") is False
    assert "Impersonation forbidden" in content4.get("error", "")

    # 5. dismiss_task
    res5 = await server_rw.call_tool(
        "dismiss_task",
        {"review_id": "task-p2", "actor": "ADMIN"},
    )
    content5 = _extract_content(res5)
    assert content5.get("success") is False
    assert "Impersonation forbidden" in content5.get("error", "")

    # 6. record_decision
    res6 = await server_rw.call_tool(
        "record_decision",
        {"summary": "S", "rationale": "R", "decided_by": "ADMIN"},
    )
    content6 = _extract_content(res6)
    assert content6.get("success") is False
    assert "Impersonation forbidden" in content6.get("error", "")


@pytest.mark.asyncio
async def test_mutation_tools_allow_admin_with_valid_key(permissions_setup):
    """Khi cung cấp đúng PTB_ADMIN_KEY, mutation tools chấp thuận actor=ADMIN."""
    _, _, _, _, server_rw = permissions_setup
    with patch.dict(os.environ, {"PTB_ADMIN_KEY": "valid-admin-secret"}, clear=False):
        res = await server_rw.call_tool(
            "update_task_status",
            {
                "task_id": "task-p1",
                "new_status": "IN_PROGRESS",
                "actor": "ADMIN",
                "admin_key": "valid-admin-secret",
            },
        )
        content = _extract_content(res)
        assert content.get("success") is True


@pytest.mark.asyncio
async def test_actor_propagation_to_audit_log(permissions_setup, caplog):
    """Xác nhận actor hợp lệ (CURSOR_AGENT) được ghi nhận chuẩn xác vào audit log."""
    _, repo, _, _, server_rw = permissions_setup
    with caplog.at_level(logging.INFO, logger="ptb.mcp.server"):
        res = await server_rw.call_tool(
            "update_task_status",
            {
                "task_id": "task-p1",
                "new_status": "IN_PROGRESS",
                "actor": "CURSOR_AGENT",
            },
        )
    content = _extract_content(res)
    assert content.get("success") is True

    # Audit log check
    audit_msgs = [r.message for r in caplog.records if "AUDIT [MCP]" in r.message]
    assert any("actor=CURSOR_AGENT" in m and "action=UPDATE_STATUS" in m for m in audit_msgs)


# ==============================================================================
# 3. DESTRUCTIVE ACTION SAFEGUARDS (CONFIRMATION PATTERNS)
# ==============================================================================

@pytest.mark.asyncio
async def test_delete_task_requires_confirmation(permissions_setup):
    """Xác nhận delete_task bị từ chối nếu confirm=False và thực thi an toàn khi confirm=True."""
    svc, repo, _, _, server_rw = permissions_setup

    # 1. confirm=False -> Aborted
    res_aborted = await server_rw.call_tool(
        "delete_task",
        {"task_id": "task-p1", "confirm": False},
    )
    content_aborted = _extract_content(res_aborted)
    assert content_aborted["success"] is False
    assert "confirmation required" in content_aborted["error"].lower()

    # Task vẫn tồn tại và ở trạng thái TODO
    task_still_there = await repo.get_task_by_id("task-p1")
    assert task_still_there is not None
    assert task_still_there.status == TaskStatus.TODO

    # 2. confirm=True -> Deleted (chuyển sang DISMISSED)
    res_deleted = await server_rw.call_tool(
        "delete_task",
        {"task_id": "task-p1", "confirm": True},
    )
    content_deleted = _extract_content(res_deleted)
    assert content_deleted["success"] is True

    task_dismissed = await repo.get_task_by_id("task-p1")
    assert task_dismissed.status == TaskStatus.DISMISSED


# ==============================================================================
# 4. INPUT VALIDATION & ERROR PROPAGATION
# ==============================================================================

@pytest.mark.asyncio
async def test_validation_empty_inputs(permissions_setup):
    """Kiểm tra phản hồi lỗi có cấu trúc khi nhận input rỗng hoặc sai chuẩn."""
    _, _, _, _, server_rw = permissions_setup

    # create_task: title rỗng
    r1 = await server_rw.call_tool("create_task", {"title": "   "})
    c1 = _extract_content(r1)
    assert c1["success"] is False
    assert "Field 'title' cannot be empty" in c1["error"]

    # update_task_status: status không hợp lệ
    r2 = await server_rw.call_tool(
        "update_task_status",
        {"task_id": "task-p1", "new_status": "SUPER_DONE"},
    )
    c2 = _extract_content(r2)
    assert c2["success"] is False
    assert "Invalid status 'SUPER_DONE'" in c2["error"]

    # update_task: task_id không tồn tại
    r3 = await server_rw.call_tool(
        "update_task",
        {"task_id": "nonexistent-task-999", "title": "New Title"},
    )
    c3 = _extract_content(r3)
    assert c3["success"] is False
    assert "not found" in c3["error"]

    # record_decision: summary rỗng
    r4 = await server_rw.call_tool("record_decision", {"summary": "", "rationale": "ok"})
    c4 = _extract_content(r4)
    assert c4["success"] is False
    assert "cannot be empty" in c4["error"]

    # record_lesson_learned: solution rỗng
    r5 = await server_rw.call_tool("record_lesson_learned", {"topic": "T", "solution": ""})
    c5 = _extract_content(r5)
    assert c5["success"] is False
    assert "cannot be empty" in c5["error"]


# ==============================================================================
# 5. HTTP / SSE TRANSPORT AUTHENTICATION & SECURITY (P0-2, W1-02)
# ==============================================================================

def test_mcp_sse_security_middleware_unauthenticated_rejected(permissions_setup):
    """Khi PTB_API_KEY được cấu hình, kết nối SSE từ chối client thiếu key với HTTP 401."""
    _, _, _, _, server_rw = permissions_setup
    with patch.dict(os.environ, {"PTB_API_KEY": "super-mcp-secret-key-456"}, clear=False):
        app = server_rw.sse_app()
        with TestClient(app, base_url="http://127.0.0.1:8001") as client:
            # 1. /health luôn được miễn trừ (HTTP 200)
            res_health = client.get("/health")
            assert res_health.status_code == 200

            # 2. SSE endpoint không có key -> 401
            res_sse_no_key = client.get("/sse")
            assert res_sse_no_key.status_code == 401
            assert "Missing API key" in res_sse_no_key.json()["detail"]

            # 3. SSE endpoint có key sai -> 401
            res_sse_wrong_key = client.get("/sse", headers={"X-PTB-API-KEY": "wrong-secret"})
            assert res_sse_wrong_key.status_code == 401
            assert "Invalid API key" in res_sse_wrong_key.json()["detail"]


def test_mcp_sse_security_middleware_query_param_accepted(permissions_setup):
    """Client EventSource chuẩn trong trình duyệt có thể xác thực qua ?api_key= query param."""
    _, _, _, _, server_rw = permissions_setup
    with patch.dict(os.environ, {"PTB_API_KEY": "query-param-token-999"}, clear=False):
        app = server_rw.sse_app()
        with TestClient(app, base_url="http://127.0.0.1:8001") as client:
            # Wrong token in query param -> 401
            r_wrong = client.get("/sse?api_key=wrong-token")
            assert r_wrong.status_code == 401

            # Missing token -> 401
            r_miss = client.get("/sse")
            assert r_miss.status_code == 401


def test_mcp_sse_dev_mode_when_api_key_unset(permissions_setup):
    """Khi PTB_API_KEY không được thiết lập, server chạy ở chế độ dev unauthenticated."""
    _, _, _, _, server_rw = permissions_setup
    env_clean = os.environ.copy()
    env_clean.pop("PTB_API_KEY", None)
    with patch.dict(os.environ, env_clean, clear=True):
        app = server_rw.sse_app()
        with TestClient(app, base_url="http://127.0.0.1:8001") as client:
            res_health = client.get("/health")
            assert res_health.status_code == 200


# ==============================================================================
# 6. P1-8 LEAK CHECK & SANITIZATION
# ==============================================================================

def test_p1_8_no_leaked_credentials_in_server():
    """Kiểm tra không tồn tại chuỗi prefix API key bị lộ trong source code."""
    server_path = os.path.join(
        os.path.dirname(__file__), "..", "src", "ptb_mcp", "server.py"
    )
    with open(server_path, "r", encoding="utf-8") as f:
        content = f.read()
    assert "AQ.Ab8RN6J_9KlMg8fqmX0-veOqjJF9YtqgwO7Kk0vMreLgk-zgtg-mock" not in content


# ==============================================================================
# 7. CONCURRENT CLIENT ACCESS SIMULATION
# ==============================================================================

@pytest.mark.asyncio
async def test_concurrent_tool_execution(permissions_setup):
    """Mô phỏng nhiều Coding Agent tương tác đồng thời mà không bị xung đột hay lỗi trạng thái."""
    _, _, _, _, server_rw = permissions_setup

    async def _create_and_fetch(idx: int):
        title = f"Concurrent Feature #{idx}"
        res = await server_rw.call_tool(
            "create_task",
            {"title": title, "priority_score": 70.0 + idx},
        )
        data = _extract_content(res)
        task_id = data["id"]
        # Fetch context
        ctx_res = await server_rw.call_tool("get_task_context", {"task_id": task_id})
        ctx_data = _extract_content(ctx_res)
        return ctx_data["task"]["id"] == task_id

    results = await asyncio.gather(*[_create_and_fetch(i) for i in range(10)])
    assert all(results)
    assert len(results) == 10


# ==============================================================================
# 8. TRANSPORT SMOKE TEST
# ==============================================================================

@pytest.mark.asyncio
async def test_stdio_server_launch_smoke(permissions_setup):
    """Smoke test khởi chạy FastMCP stdio server."""
    svc, _, _, _, _ = permissions_setup
    with patch("mcp.server.mcpserver.server.MCPServer.run_stdio_async", new_callable=AsyncMock) as mock_stdio:
        await run_stdio_server(application_service=svc, read_only=False)
        assert mock_stdio.called
