"""Unit and integration tests for Task 5: Exporting Agent & BE as MCP Server for Coding Agents.

Tests:
1. FastMCP full tool suite (19 tools: 10 queries, 6 mutations, 3 agent/knowledge).
2. End-to-end task lifecycle driven by a Coding Agent via MCP.
3. Review queue actions (approve, dismiss).
4. Episodic knowledge recording (record_decision, record_lesson_learned).
5. Direct agent reasoning & context synthesis (ask_ptb_agent).
6. MCP Standard Resources (ptb://today-plan, ptb://tasks, ptb://system-health).
7. MCP Prompts (morning_briefing, task_execution_context).
8. CLI management: ptb mcp config & ptb mcp install.
"""

from datetime import datetime, timezone
import json
from typing import Any, Dict, List, Optional
from uuid import uuid4
import pytest

from ptb_application.service import ApplicationService
from ptb_contracts.l2_processing import (
    CommitmentRecord,
    ReviewQueueItem,
    StatusTransitionAuditRecord,
    TaskStatus,
    UnifiedTaskCandidate,
)
from ptb_contracts.l4_intelligence import TaskWithContext
from ptb_mcp.server import create_mcp_server, get_mcp_health
from scripts.ptb_cli import cmd_mcp


class MockTaskRepo:
    def __init__(self, initial_tasks: Optional[List[UnifiedTaskCandidate]] = None):
        self.tasks: Dict[str, UnifiedTaskCandidate] = {
            t.id: t for t in (initial_tasks or [])
        }
        self.audits: List[StatusTransitionAuditRecord] = []

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
            return sorted(tasks, key=lambda x: (x.status != TaskStatus.IN_PROGRESS, -x.priority_score))
        filtered = []
        for t in tasks:
            if "status" in filters and filters["status"]:
                req = filters["status"]
                val = req.value if hasattr(req, "value") else str(req)
                if t.status.value != val:
                    continue
            filtered.append(t)
        return sorted(filtered, key=lambda x: (x.status != TaskStatus.IN_PROGRESS, -x.priority_score))

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
                        raw_event_id="raw-t5",
                        candidate_task=t,
                        reason="Candidate requiring approval",
                        created_at=datetime.now(timezone.utc),
                    )
                )
        return items[:limit]

    async def get_active_commitments(self, user_id: Optional[str] = None) -> List[CommitmentRecord]:
        return []


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


class MockNeo4jClient:
    async def verify_connectivity(self) -> bool:
        return True


@pytest.fixture
def mcp_setup():
    now = datetime.now(timezone.utc)
    t1 = UnifiedTaskCandidate(
        id="task-init-1",
        title="Develop Authentication Service",
        status=TaskStatus.TODO,
        priority_score=75.0,
        project_key="AUTH",
        created_at=now,
        updated_at=now,
    )
    t2 = UnifiedTaskCandidate(
        id="task-cand-2",
        title="Candidate Task From Slack",
        status=TaskStatus.BLOCKED,
        priority_score=40.0,
        project_key="GLOBAL",
        review_status="pending_review",
        created_at=now,
        updated_at=now,
    )
    repo = MockTaskRepo([t1, t2])
    graph = MockGraphMemory()
    client = MockNeo4jClient()

    svc = ApplicationService(
        task_repo=repo,
        graph_memory=graph,
        neo4j_client=client,
        processing_worker_status="healthy",
        llm_status="healthy",
        playwright_status="healthy",
    )

    server = create_mcp_server(application_service=svc, read_only=False)
    return server, svc, repo, graph


def _extract(res: Any) -> Any:
    if hasattr(res, "structured_content") and res.structured_content:
        if isinstance(res.structured_content, dict) and "result" in res.structured_content:
            return res.structured_content["result"]
        return res.structured_content
    if hasattr(res, "content") and res.content:
        text = res.content[0].text
        try:
            return json.loads(text)
        except Exception:
            return text
    return None


# ==============================================================================
# TEST CASES
# ==============================================================================

@pytest.mark.asyncio
async def test_task5_tool_suite_completeness(mcp_setup):
    """Xác nhận MCP Server xuất đúng 19 tools, 3 resources, 2 prompts cho Coding Agents."""
    server, _, _, _ = mcp_setup
    tools = await server.list_tools()
    tool_names = {t.name for t in tools}

    assert len(tool_names) == 19

    # 10 Query tools
    query_tools = [
        "get_today_tasks", "get_tasks", "get_task_context", "get_waiting_items",
        "get_forgotten_commitments", "get_review_queue", "search_decisions",
        "search_lessons_learned", "search_context", "get_source_health"
    ]
    for q in query_tools:
        assert q in tool_names

    # 6 Mutation tools
    mutation_tools = [
        "create_task", "update_task_status", "update_task", "delete_task",
        "approve_task", "dismiss_task"
    ]
    for m in mutation_tools:
        assert m in tool_names

    # 3 Agent tools
    agent_tools = ["ask_ptb_agent", "record_decision", "record_lesson_learned"]
    for a in agent_tools:
        assert a in tool_names

    # Resources
    resources = await server.list_resources()
    uris = {str(r.uri) for r in resources}
    assert "ptb://today-plan" in uris
    assert "ptb://tasks" in uris
    assert "ptb://system-health" in uris

    # Prompts
    prompts = await server.list_prompts()
    prompt_names = {p.name for p in prompts}
    assert "morning_briefing" in prompt_names
    assert "task_execution_context" in prompt_names


@pytest.mark.asyncio
async def test_task5_coding_agent_task_lifecycle(mcp_setup):
    """Kịch bản Coding Agent tạo task, chuyển IN_PROGRESS, sửa nội dung và hoàn thành task."""
    server, _, _, _ = mcp_setup

    # 1. Coding Agent tạo task mới
    create_res = await server.call_tool(
        "create_task",
        {
            "title": "Refactor token cache in auth client",
            "description": "Improve TTL management and prevent thundering herd",
            "project_key": "AUTH",
            "priority_score": 88.0,
        },
    )
    assert create_res.is_error is False
    created = _extract(create_res)
    assert created["title"] == "Refactor token cache in auth client"
    task_id = created["id"]

    # 2. Coding Agent bắt đầu làm việc: Chuyển sang IN_PROGRESS
    status_res = await server.call_tool(
        "update_task_status",
        {"task_id": task_id, "new_status": "IN_PROGRESS"},
    )
    assert status_res.is_error is False
    assert _extract(status_res)["success"] is True

    # 3. Coding Agent cập nhật mô tả ghi nhận tiến độ
    update_res = await server.call_tool(
        "update_task",
        {
            "task_id": task_id,
            "description": "TTL logic refactored, writing unit tests",
            "priority_score": 92.0,
        },
    )
    assert update_res.is_error is False
    updated = _extract(update_res)["task"]
    assert updated["description"] == "TTL logic refactored, writing unit tests"
    assert updated["priority_score"] == 92.0

    # 4. Coding Agent hoàn thành task: Chuyển sang DONE
    done_res = await server.call_tool(
        "update_task_status",
        {"task_id": task_id, "new_status": "DONE"},
    )
    assert done_res.is_error is False
    assert _extract(done_res)["success"] is True

    # 5. Tra cứu lại chi tiết task
    ctx_res = await server.call_tool("get_task_context", {"task_id": task_id})
    ctx = _extract(ctx_res)
    assert ctx["task"]["status"] == "DONE"


@pytest.mark.asyncio
async def test_task5_coding_agent_review_queue(mcp_setup):
    """Kịch bản Coding Agent phê duyệt candidate task vào Task Board."""
    server, _, repo, _ = mcp_setup

    # Kiểm tra candidate đang trong hàng đợi
    rev_res = await server.call_tool("get_review_queue", {})
    rev_items = _extract(rev_res)
    assert len(rev_items) == 1
    candidate_id = rev_items[0]["candidate_task"]["id"]

    # Coding Agent phê duyệt task
    appr_res = await server.call_tool(
        "approve_task",
        {"review_id": candidate_id, "new_status": "TODO"},
    )
    assert appr_res.is_error is False
    assert _extract(appr_res)["success"] is True

    # Review queue giờ trống
    rev_after = await server.call_tool("get_review_queue", {})
    assert len(_extract(rev_after)) == 0


@pytest.mark.asyncio
async def test_task5_coding_agent_knowledge_recording(mcp_setup):
    """Kịch bản Coding Agent ghi nhận ADR và Lesson Learned vào Graph Memory."""
    server, _, _, graph = mcp_setup

    # 1. Ghi nhận Architecture Decision Record (ADR)
    dec_res = await server.call_tool(
        "record_decision",
        {
            "summary": "Adopt FastMCP SSE and stdio duality",
            "rationale": "Stdio for IDE subagents; SSE for network/docker OpenWebUI",
            "project_key": "ARCH",
            "decided_by": "Antigravity Pair",
        },
    )
    assert dec_res.is_error is False
    dec = _extract(dec_res)
    assert dec["success"] is True
    assert dec["decision"]["summary"] == "Adopt FastMCP SSE and stdio duality"
    assert len(graph.decisions) == 1

    # 2. Ghi nhận Lesson Learned sau khi debug
    les_res = await server.call_tool(
        "record_lesson_learned",
        {
            "topic": "Uvicorn Windows Signals",
            "solution": "Handle signal.SIGINT gracefully without NotImplementedError on Windows loop",
            "error_context": "NotImplementedError when registering signal in Windows event loop",
        },
    )
    assert les_res.is_error is False
    les = _extract(les_res)
    assert les["success"] is True
    assert les["lesson"]["topic"] == "Uvicorn Windows Signals"
    assert len(graph.lessons) == 1


@pytest.mark.asyncio
async def test_task5_ask_ptb_agent(mcp_setup):
    """Kịch bản Coding Agent hỏi đáp trực tiếp với PTB Agent qua MCP."""
    server, _, _, _ = mcp_setup
    res = await server.call_tool(
        "ask_ptb_agent",
        {"prompt": "Tôi cần giải quyết những task nào cho dự án AUTH?"},
    )
    assert res.is_error is False
    ans = _extract(res)
    assert isinstance(ans, dict)
    assert "reply" in ans
    assert ans["context_grounded"] is True


@pytest.mark.asyncio
async def test_task5_resources_and_prompts(mcp_setup):
    """Kịch bản đọc MCP resources và lấy prompts định dạng chuẩn."""
    server, _, _, _ = mcp_setup

    # 1. Đọc resource ptb://today-plan
    r1 = await server.read_resource("ptb://today-plan")
    assert len(r1) > 0
    assert "summary_headline" in r1[0].content

    # 2. Đọc resource ptb://tasks
    r2 = await server.read_resource("ptb://tasks")
    assert len(r2) > 0
    tasks_data = json.loads(r2[0].content)
    assert isinstance(tasks_data, list)

    # 3. Lấy prompt morning_briefing
    p1 = await server.get_prompt("morning_briefing", {})
    assert "get_today_tasks" in p1.messages[0].content.text

    # 4. Lấy prompt task_execution_context
    p2 = await server.get_prompt("task_execution_context", {"task_id": "task-init-1"})
    assert "task-init-1" in p2.messages[0].content.text


@pytest.mark.asyncio
async def test_task5_cli_config_and_install():
    """Kiểm tra lệnh CLI ptb mcp config và ptb mcp install."""
    import argparse

    args_cfg = argparse.Namespace(
        mcp_action="config",
        transport="stdio",
        read_only=False,
        host="127.0.0.1",
        port=8001,
        client="antigravity",
    )
    code = await cmd_mcp(args_cfg)
    assert code == 0

    args_inst = argparse.Namespace(
        mcp_action="install",
        transport="stdio",
        read_only=False,
        host="127.0.0.1",
        port=8001,
        client="antigravity",
    )
    code_inst = await cmd_mcp(args_inst)
    assert code_inst == 0


@pytest.mark.asyncio
async def test_task5_read_only_default_and_guard(mcp_setup):
    """Xác nhận read-only mode mặc định chỉ có 10 query tools và từ chối mọi mutation tool."""
    _, svc, _, _ = mcp_setup
    server_ro = create_mcp_server(application_service=svc, read_only=True)

    tools = await server_ro.list_tools()
    assert len(tools) == 10

    with pytest.raises(Exception):
        await server_ro.call_tool("create_task", {"title": "Forbidden In Read Only"})


@pytest.mark.asyncio
async def test_task5_destructive_safeguard_and_actor_policy(mcp_setup):
    """Xác nhận cơ chế xác nhận an toàn cho delete_task và ngăn chặn mạo danh actor."""
    server, _, repo, _ = mcp_setup

    # 1. confirm=False aborts
    res_abort = await server.call_tool("delete_task", {"task_id": "task-init-1", "confirm": False})
    c_abort = _extract(res_abort)
    assert c_abort["success"] is False
    assert "confirmation required" in c_abort["error"].lower()

    # Task vẫn nguyên vẹn
    t = await repo.get_task_by_id("task-init-1")
    assert t.status == TaskStatus.TODO

    # 2. Impersonation of ADMIN rejected without credentials
    res_impersonate = await server.call_tool("delete_task", {"task_id": "task-init-1", "actor": "ADMIN"})
    c_imp = _extract(res_impersonate)
    assert c_imp["success"] is False
    assert "Impersonation forbidden" in c_imp["error"]

    # 3. Legitimate agent deletion with confirm=True
    res_ok = await server.call_tool("delete_task", {"task_id": "task-init-1", "actor": "CODING_AGENT", "confirm": True})
    c_ok = _extract(res_ok)
    assert c_ok["success"] is True

    t_dismissed = await repo.get_task_by_id("task-init-1")
    assert t_dismissed.status == TaskStatus.DISMISSED

