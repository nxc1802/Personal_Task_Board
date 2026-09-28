"""Unit tests for FastMCP Server (Layer 5)."""

from datetime import datetime, timedelta, timezone
import json
from typing import Any, Dict, List, Optional
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
from ptb_application.service import ApplicationService
from ptb_mcp.server import create_mcp_server


# ==============================================================================
# MOCKS
# ==============================================================================

class MockTaskDomainRepository:
    def __init__(self, initial_tasks: Optional[List[UnifiedTaskCandidate]] = None):
        self.tasks: Dict[str, UnifiedTaskCandidate] = {
            t.id: t for t in (initial_tasks or [])
        }
        self.audits: List[StatusTransitionAuditRecord] = []
        self.commitments: List[CommitmentRecord] = []

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
