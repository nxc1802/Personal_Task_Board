"""Unit tests for FastMCP Server (Layer 5)."""

from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock
import pytest

from ptb_contracts.l1_acquisition import (
    IngestionCheckpointRecord,
    SourceType,
)
from ptb_contracts.l2_processing import (
    CommitmentRecord,
    EvidenceRecord,
    EvidenceType,
    ReviewQueueItem,
    StatusTransitionAuditRecord,
    TaskStatus,
    UnifiedTaskCandidate,
)
from ptb_contracts.l4_intelligence import (
    TaskWithContext,
    TodayBoardView,
    TodayTaskItem,
    WaitingOnItem,
)
from fastapi.testclient import TestClient
from ptb_application.api import (
    create_app,
    get_application_service as get_api_app_service,
    set_application_service as set_api_app_service,
)
from ptb_application.service import ApplicationService
from ptb_mcp.server import (
    create_mcp_server,
    get_application_service as get_mcp_app_service,
    get_mcp_health,
    set_application_service as set_mcp_app_service,
)


# ==============================================================================
# MOCKS
# ==============================================================================

class MockNeo4jClient:
    def __init__(self, healthy: bool = True):
        self.healthy = healthy

    async def verify_connectivity(self) -> bool:
        return self.healthy


class MockTaskDomainRepository:
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

    async def record_status_transition_audit(self, audit: StatusTransitionAuditRecord) -> str:
        self.audits.append(audit)
        return audit.id

    async def list_tasks(self, filters: Optional[dict] = None) -> list[UnifiedTaskCandidate]:
        tasks = list(self.tasks.values())
        if not filters:
            return tasks
        filtered = []
        for t in tasks:
            if "status" in filters and filters["status"]:
                req_status = filters["status"]
                if isinstance(req_status, (list, set, tuple)):
                    status_vals = [s.value if hasattr(s, "value") else str(s) for s in req_status]
                    if t.status.value not in status_vals:
                        continue
                else:
                    status_val = req_status.value if hasattr(req_status, "value") else str(req_status)
                    if t.status.value != status_val:
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
            blocking_tasks=["task-blocker-1"] if task.status == TaskStatus.BLOCKED else [],
            dependent_people=["Alex"] if task.status == TaskStatus.BLOCKED else [],
        )

    async def get_review_queue(self, limit: int = 20) -> list[ReviewQueueItem]:
        items = []
        for t in self.tasks.values():
            if t.review_status == "pending_review":
                items.append(ReviewQueueItem(
                    id=f"rev-{t.id}",
                    raw_event_id="raw-1",
                    candidate_task=t,
                    reason="Low confidence review",
                    created_at=datetime.now(timezone.utc),
                ))
        return items[:limit]

    async def get_active_commitments(self, user_id: Optional[str] = None) -> list[CommitmentRecord]:
        return self.commitments


class MockCheckpointRepository:
    def __init__(self):
        self.checkpoints = [
            IngestionCheckpointRecord(
                id="cp-1",
                tenant_id="tenant-ms",
                source_type=SourceType.MS_TEAMS,
                stream_id="stream-1",
                last_external_id="msg-100",
                last_event_timestamp=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        ]

    async def list_checkpoints(self) -> list[IngestionCheckpointRecord]:
        return self.checkpoints


class MockGraphitiMemoryClient:
    async def search_context(self, query: str, limit: int = 5, include_invalidated: bool = False) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat()
        return [
            {
                "id": "dec-1",
                "type": "DECISION",
                "summary": "Dùng Redis cho session auth",
                "content": "Rationale cho kiến trúc auth",
                "valid_at": now,
                "properties": {
                    "decision_id": "dec-1",
                    "project_key": "AUTH",
                    "summary": "Dùng Redis cho session auth",
                    "rationale": "Rationale cho kiến trúc auth",
                    "decided_by": "Architect",
                    "decided_at": now,
                },
            },
            {
                "id": "les-1",
                "type": "LESSON",
                "topic": "Gateway Circuit Breaker",
                "summary": "Gateway circuit breaker",
                "content": "Cần thêm timeout fallback",
                "valid_at": now,
                "properties": {
                    "lesson_id": "les-1",
                    "topic": "Gateway Circuit Breaker",
                    "description": "Gateway circuit breaker",
                    "solution": "Cần thêm timeout fallback",
                    "recorded_at": now,
                },
            },
        ][:limit]


# ==============================================================================
# FIXTURES
# ==============================================================================

@pytest.fixture
def mock_app_service() -> ApplicationService:
    now = datetime.now(timezone.utc)
    ev1 = EvidenceRecord(
        id="ev-1",
        raw_event_id="raw-1",
        evidence_type=EvidenceType.CHAT_COMMITMENT,
        source_type="ms_teams",
        timestamp=now,
        snippet="Tôi sẽ làm task này hôm nay",
        confidence=0.9,
    )
    t1 = UnifiedTaskCandidate(
        id="task-101",
        title="Triển khai FastMCP Server",
        status=TaskStatus.TODO,
        priority_score=90.0,
        owner_name="Me",
        project_key="PTB",
        due_date=now + timedelta(days=1),
        explicit_deadline=True,
        extraction_confidence=0.9,
        review_status="auto_approved",
        evidences=[ev1],
    )
    t2 = UnifiedTaskCandidate(
        id="task-102",
        title="Cấu hình SSE Proxy",
        status=TaskStatus.BLOCKED,
        priority_score=40.0,
        owner_name="Alex",
        project_key="NET",
        extraction_confidence=0.5,
        review_status="pending_review",
        evidences=[ev1],
    )

    task_repo = MockTaskDomainRepository([t1, t2])
    task_repo.commitments = [
        CommitmentRecord(
            id="comm-1",
            title="Gửi tài liệu API cho team",
            owner_id="person-me",
            requester_id="person-alex",
            status="ACTIVE",
            created_at=now - timedelta(days=4),
        )
    ]
    checkpoint_repo = MockCheckpointRepository()
    graph_memory = MockGraphitiMemoryClient()

    return ApplicationService(
        task_repo=task_repo,
        checkpoint_repo=checkpoint_repo,
        graph_memory=graph_memory,
    )


@pytest.fixture
def mcp_server(mock_app_service: ApplicationService):
    return create_mcp_server(app_service=mock_app_service)


def _extract_content(result: Any) -> Any:
    """Helper extract data from CallToolResult."""
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


# ==============================================================================
# TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_tool_count_and_strict_read_only(mcp_server):
    """Kiểm tra server có chính xác 10 read-only query tools và TUYỆT ĐỐI không có write-back tools."""
    tools = await mcp_server.list_tools()
    tool_names = [t.name for t in tools]

    expected_tools = [
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
    ]

    assert len(tool_names) == 10, f"Expected exactly 10 tools, got {len(tool_names)}: {tool_names}"
    for expected in expected_tools:
        assert expected in tool_names, f"Missing tool: {expected}"

    # Strict check: không có write-back tools
    forbidden_prefixes = ["create_", "update_", "delete_", "approve_", "reject_", "dismiss_", "mark_", "modify_"]
    for name in tool_names:
        for prefix in forbidden_prefixes:
            assert not name.startswith(prefix), f"Forbidden write tool detected: {name}"


@pytest.mark.asyncio
async def test_tool_1_get_today_tasks(mcp_server):
    """Kiểm tra tool get_today_tasks."""
    res = await mcp_server.call_tool("get_today_tasks", {"limit": 5})
    assert res.is_error is False
    content = _extract_content(res)
    assert isinstance(content, list)
    assert len(content) > 0
    assert content[0]["task_id"] == "task-101"


@pytest.mark.asyncio
async def test_tool_2_get_tasks(mcp_server):
    """Kiểm tra tool get_tasks với và không với filter."""
    # Không filter
    res = await mcp_server.call_tool("get_tasks", {})
    assert res.is_error is False
    content = _extract_content(res)
    assert isinstance(content, list)
    assert len(content) == 2

    # Có filter
    res_filtered = await mcp_server.call_tool("get_tasks", {"filters": {"status": "TODO"}})
    assert res_filtered.is_error is False
    content_filtered = _extract_content(res_filtered)
    assert isinstance(content_filtered, list)
    assert len(content_filtered) == 1
    assert content_filtered[0]["id"] == "task-101"


@pytest.mark.asyncio
async def test_tool_3_get_task_context(mcp_server):
    """Kiểm tra tool get_task_context."""
    # Task tồn tại
    res = await mcp_server.call_tool("get_task_context", {"task_id": "task-102"})
    assert res.is_error is False
    content = _extract_content(res)
    assert isinstance(content, dict)
    assert content["task"]["id"] == "task-102"
    assert "task-blocker-1" in content["blocking_tasks"]

    # Task không tồn tại
    res_missing = await mcp_server.call_tool("get_task_context", {"task_id": "unknown-task"})
    assert res_missing.is_error is False
    content_missing = _extract_content(res_missing)
    assert "error" in content_missing


@pytest.mark.asyncio
async def test_tool_4_get_waiting_items(mcp_server):
    """Kiểm tra tool get_waiting_items."""
    res = await mcp_server.call_tool("get_waiting_items", {})
    assert res.is_error is False
    content = _extract_content(res)
    assert isinstance(content, list)


@pytest.mark.asyncio
async def test_tool_5_get_forgotten_commitments(mcp_server):
    """Kiểm tra tool get_forgotten_commitments."""
    res = await mcp_server.call_tool("get_forgotten_commitments", {"days_stale": 3})
    assert res.is_error is False
    content = _extract_content(res)
    assert isinstance(content, list)


@pytest.mark.asyncio
async def test_tool_6_get_review_queue(mcp_server):
    """Kiểm tra tool get_review_queue."""
    res = await mcp_server.call_tool("get_review_queue", {"limit": 10})
    assert res.is_error is False
    content = _extract_content(res)
    assert isinstance(content, list)
    assert len(content) == 1
    assert content[0]["candidate_task"]["id"] == "task-102"


@pytest.mark.asyncio
async def test_tool_7_search_decisions(mcp_server):
    """Kiểm tra tool search_decisions."""
    res = await mcp_server.call_tool("search_decisions", {"query": "auth"})
    assert res.is_error is False
    content = _extract_content(res)
    assert isinstance(content, list)
    assert len(content) >= 1
    assert content[0]["decision_id"] == "dec-1"


@pytest.mark.asyncio
async def test_tool_8_search_lessons_learned(mcp_server):
    """Kiểm tra tool search_lessons_learned."""
    res = await mcp_server.call_tool("search_lessons_learned", {"error_or_topic": "circuit breaker"})
    assert res.is_error is False
    content = _extract_content(res)
    assert isinstance(content, list)
    assert len(content) >= 1
    assert content[0]["lesson_id"] == "les-1"


@pytest.mark.asyncio
async def test_tool_9_search_context(mcp_server):
    """Kiểm tra tool search_context."""
    res = await mcp_server.call_tool("search_context", {"query": "gateway"})
    assert res.is_error is False
    content = _extract_content(res)
    assert isinstance(content, dict)
    assert "decisions" in content
    assert "lessons" in content
    assert "synthesis_summary" in content


@pytest.mark.asyncio
async def test_tool_10_get_source_health(mcp_server):
    """Kiểm tra tool get_source_health."""
    res = await mcp_server.call_tool("get_source_health", {})
    assert res.is_error is False
    content = _extract_content(res)
    assert isinstance(content, dict)
    assert "tenants" in content
    assert "overall_health" in content
    assert content["overall_health"] == "healthy"


def test_mcp_server_defaults_and_run_mcp_server():
    """Kiểm tra cấu hình mặc định host 127.0.0.1, port 8001 và hàm run_mcp_server."""
    import inspect
    from unittest.mock import patch
    from ptb_mcp.server import (
        DEFAULT_MCP_HOST,
        DEFAULT_MCP_PORT,
        create_mcp_server,
        run_mcp_server,
        run_sse_server,
    )

    # 1. Kiểm tra hằng số host và port mặc định
    assert DEFAULT_MCP_HOST == "127.0.0.1"
    assert DEFAULT_MCP_PORT == 8001

    # 2. Kiểm tra server name
    srv = create_mcp_server()
    assert getattr(srv, "name", None) == "ptb-mcp"

    # 3. Kiểm tra signature của run_mcp_server
    sig = inspect.signature(run_mcp_server)
    assert sig.parameters["host"].default == "127.0.0.1"
    assert sig.parameters["port"].default == 8001

    # 4. Kiểm tra signature của run_sse_server
    sse_sig = inspect.signature(run_sse_server)
    assert sse_sig.parameters["host"].default == "127.0.0.1"
    assert sse_sig.parameters["port"].default == 8001

    # 5. Kiểm tra run_mcp_server gọi run_sse_server với đúng tham số
    coro_to_close = None

    def fake_run(coro):
        nonlocal coro_to_close
        coro_to_close = coro

    with patch("ptb_mcp.server.asyncio.run", side_effect=fake_run):
        run_mcp_server(host="127.0.0.1", port=8001)

    assert coro_to_close is not None
    coro_to_close.close()


@pytest.mark.asyncio
async def test_mcp_set_application_service_and_create_mcp_server(mock_app_service: ApplicationService):
    """Kiểm tra create_mcp_server(application_service=...) và set_application_service cho FastMCP."""
    # 1. set_application_service trực tiếp
    set_mcp_app_service(mock_app_service)
    assert get_mcp_app_service() is mock_app_service

    # 2. create_mcp_server với explicit application_service
    srv = create_mcp_server(application_service=mock_app_service)
    res = await srv.call_tool("get_tasks", {})
    assert res.is_error is False
    content = _extract_content(res)
    assert len(content) == 2

    # Reset
    set_mcp_app_service(None)


@pytest.mark.asyncio
async def test_mcp_and_fastapi_shared_application_service():
    """Kiểm tra FastAPI và FastMCP cùng chia sẻ trạng thái và repository từ chung một ApplicationService."""
    now = datetime.now(timezone.utc)
    ev = EvidenceRecord(
        id="ev-mcp-shared",
        raw_event_id="raw-mcp-1",
        evidence_type=EvidenceType.CHAT_COMMITMENT,
        source_type="ms_teams",
        timestamp=now,
        snippet="MCP & FastAPI shared commitment",
        confidence=0.88,
    )
    t1 = UnifiedTaskCandidate(
        id="task-mcp-1",
        title="Công việc khởi tạo ban đầu",
        status=TaskStatus.TODO,
        priority_score=60.0,
        owner_name="Me",
        project_key="SHARED",
        due_date=now + timedelta(days=1),
        explicit_deadline=True,
        extraction_confidence=0.9,
        review_status="auto_approved",
        evidences=[ev],
    )
    t2 = UnifiedTaskCandidate(
        id="task-mcp-2",
        title="Candidate chờ review",
        status=TaskStatus.BLOCKED,
        priority_score=35.0,
        owner_name="Alex",
        project_key="SHARED",
        extraction_confidence=0.5,
        review_status="pending_review",
        evidences=[ev],
    )

    task_repo = MockTaskDomainRepository([t1, t2])
    checkpoint_repo = MockCheckpointRepository()
    graph_memory = MockGraphitiMemoryClient()

    # MỘT instance ApplicationService duy nhất (Shared Runtime Dependency Graph)
    shared_service = ApplicationService(
        task_repo=task_repo,
        checkpoint_repo=checkpoint_repo,
        graph_memory=graph_memory,
    )

    # Inject shared_service vào cả FastAPI và FastMCP
    api_app = create_app(application_service=shared_service)
    mcp_server = create_mcp_server(application_service=shared_service)

    # 1. Ban đầu FastMCP đọc task-mcp-1
    res_init = await mcp_server.call_tool("get_task_context", {"task_id": "task-mcp-1"})
    assert res_init.is_error is False
    data_init = _extract_content(res_init)
    assert data_init["task"]["priority_score"] == 60.0
    assert data_init["task"]["title"] == "Công việc khởi tạo ban đầu"

    # Ban đầu hàng đợi review có task-mcp-2
    rev_init = await mcp_server.call_tool("get_review_queue", {"limit": 10})
    rev_data_init = _extract_content(rev_init)
    assert len(rev_data_init) == 1
    assert rev_data_init[0]["candidate_task"]["id"] == "task-mcp-2"

    # 2. Thực hiện mutation qua FastAPI REST API
    with TestClient(api_app) as api_client:
        # Patch task 1 (priority_score -> 99.0, title -> "Cập nhật qua REST API")
        patch_res = api_client.patch(
            "/api/tasks/task-mcp-1",
            json={"priority_score": 99.0, "title": "Cập nhật qua REST API"},
        )
        assert patch_res.status_code == 200
        assert patch_res.json()["priority_score"] == 99.0

        # Approve task 2 trong review queue
        approve_res = api_client.post(
            "/api/review/task-mcp-2/approve",
            json={"actor": "ADMIN", "new_status": "TODO"},
        )
        assert approve_res.status_code == 200
        assert approve_res.json()["success"] is True

    # 3. FastMCP đọc lại qua các MCP tools và lập tức thấy sự thay đổi từ FastAPI
    # A. Tool get_today_tasks thấy task 1 ưu tiên hàng đầu với title mới
    today_res = await mcp_server.call_tool("get_today_tasks", {"limit": 5})
    assert today_res.is_error is False
    today_data = _extract_content(today_res)
    assert len(today_data) > 0
    assert today_data[0]["task_id"] == "task-mcp-1"
    assert today_data[0]["title"] == "Cập nhật qua REST API"

    # B. Tool get_task_context thấy title mới
    ctx_res = await mcp_server.call_tool("get_task_context", {"task_id": "task-mcp-1"})
    ctx_data = _extract_content(ctx_res)
    assert ctx_data["task"]["title"] == "Cập nhật qua REST API"
    assert ctx_data["task"]["priority_score"] == 99.0

    # C. Tool get_tasks thấy task 2 đã đổi status thành TODO sau khi approve
    tasks_todo_res = await mcp_server.call_tool("get_tasks", {"filters": {"status": "TODO"}})
    tasks_todo = _extract_content(tasks_todo_res)
    todo_ids = [t["id"] for t in tasks_todo]
    assert "task-mcp-1" in todo_ids
    assert "task-mcp-2" in todo_ids

    # D. Tool get_review_queue thấy task 2 không còn ở trạng thái pending review
    rev_after = await mcp_server.call_tool("get_review_queue", {"limit": 10})
    rev_data_after = _extract_content(rev_after)
    assert len(rev_data_after) == 0

    # E. Xác nhận REST và MCP trỏ tới cùng một instance ApplicationService duy nhất
    assert get_api_app_service() is shared_service
    assert get_mcp_app_service() is shared_service
    assert get_api_app_service() is get_mcp_app_service()

    # Reset
    set_mcp_app_service(None)
    set_api_app_service(None)


@pytest.mark.asyncio
async def test_mcp_health_healthy(mock_app_service: ApplicationService):
    """Kiểm tra get_mcp_health() và GET /health trả về status 'healthy' (HTTP 200) khi ApplicationService khỏe mạnh."""
    srv = create_mcp_server(application_service=mock_app_service)

    # 1. Kiểm tra trực tiếp hàm get_mcp_health()
    health_data = await get_mcp_health(mock_app_service)
    assert health_data == {
        "status": "healthy",
        "service": "ptb-mcp",
        "application_service_bound": True,
        "neo4j": "healthy",
        "tools_count": 10,
    }

    # 2. Kiểm tra qua HTTP GET /health trên SSE ASGI app
    with TestClient(srv.sse_app()) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {
            "status": "healthy",
            "service": "ptb-mcp",
            "application_service_bound": True,
            "neo4j": "healthy",
            "tools_count": 10,
        }

    set_mcp_app_service(None)


@pytest.mark.asyncio
async def test_mcp_health_not_ready_when_neo4j_unhealthy(mock_app_service: ApplicationService, caplog):
    """Kiểm tra khi neo4j_client.verify_connectivity() trả về False, get_mcp_health() trả về 'not_ready', ghi log PTB-MCP-001 và /health trả về HTTP 503."""
    mock_app_service.neo4j_client.verify_connectivity = AsyncMock(return_value=False)
    srv = create_mcp_server(application_service=mock_app_service)

    with caplog.at_level(logging.ERROR, logger="ptb.bugs.mcp"):
        health_data = await get_mcp_health(mock_app_service)

    assert health_data == {
        "status": "not_ready",
        "service": "ptb-mcp",
        "application_service_bound": True,
        "neo4j": "not_ready",
    }
    assert any("PTB-MCP-001" in rec.message for rec in caplog.records)
    assert any("MCP server dependency unhealthy" in rec.message for rec in caplog.records)

    # Kiểm tra qua HTTP GET /health trả về 503
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="ptb.bugs.mcp"):
        with TestClient(srv.sse_app()) as client:
            resp = client.get("/health")
            assert resp.status_code == 503
            assert resp.json() == {
                "status": "not_ready",
                "service": "ptb-mcp",
                "application_service_bound": True,
                "neo4j": "not_ready",
            }
    assert any("PTB-MCP-001" in rec.message for rec in caplog.records)

    set_mcp_app_service(None)


@pytest.mark.asyncio
async def test_mcp_health_reflects_shared_application_service_state(mock_app_service: ApplicationService):
    """Xác nhận shared ApplicationService instance giữa REST và MCP đồng bộ trạng thái kết nối."""
    api_app = create_app(application_service=mock_app_service)
    mcp_srv = create_mcp_server(application_service=mock_app_service)

    assert get_api_app_service() is mock_app_service
    assert get_mcp_app_service() is mock_app_service
    assert getattr(api_app.state, "application_service", None) is get_mcp_app_service()

    # Ban đầu healthy (200)
    with TestClient(mcp_srv.sse_app()) as mcp_client:
        r1 = mcp_client.get("/health")
        assert r1.status_code == 200
        assert r1.json()["status"] == "healthy"

        # Khi neo4j_client trên shared instance bị ngắt kết nối -> MCP /health chuyển sang 503 not_ready
        mock_app_service.neo4j_client.verify_connectivity = AsyncMock(return_value=False)
        r2 = mcp_client.get("/health")
        assert r2.status_code == 503
        assert r2.json()["status"] == "not_ready"
        assert r2.json()["neo4j"] == "not_ready"

    set_mcp_app_service(None)
    set_api_app_service(None)

