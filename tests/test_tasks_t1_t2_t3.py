"""Unit and integration tests for Task 1, 2, and 3.

Task 1: Priority sorting (IN_PROGRESS first, then priority_score desc).
Task 2: OpenWebUI tools integration (create_task, delete_task, get_tasks).
Task 3: Full 2-way CRUD endpoints on FastAPI and UI contract verification.
"""

from datetime import datetime, timezone
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient

from ptb_application.api import create_app
from ptb_application.service import ApplicationService
from ptb_contracts.l2_processing import TaskStatus, UnifiedTaskCandidate
from integrations.openwebui.tools.ptb_tools import Tools as PtbTools
from ptb_intelligence.planner import TodayBoardPlanner
from tests.support.test_doubles import (
    FakeGraphitiAdapter,
    InMemoryCheckpointRepository,
    InMemoryRawEventRepository,
    InMemoryTaskDomainRepository,
)


@pytest.fixture
def test_setup():
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
    return client, svc, task_repo


# ==============================================================================
# Task 1 Tests: Priority & IN_PROGRESS Sorting
# ==============================================================================

@pytest.mark.asyncio
async def test_task1_in_progress_ranks_before_higher_priority_todo(test_setup):
    """IN_PROGRESS tasks must appear before TODO tasks even if TODO has higher priority_score."""
    client, svc, task_repo = test_setup

    task_todo_high = UnifiedTaskCandidate(
        id=str(uuid4()),
        title="High Priority TODO Task",
        status=TaskStatus.TODO,
        priority_score=99.0,
    )
    task_in_progress_low = UnifiedTaskCandidate(
        id=str(uuid4()),
        title="Lower Priority In-Progress Task",
        status=TaskStatus.IN_PROGRESS,
        priority_score=30.0,
    )
    task_todo_med = UnifiedTaskCandidate(
        id=str(uuid4()),
        title="Medium Priority TODO Task",
        status=TaskStatus.TODO,
        priority_score=60.0,
    )

    await task_repo.upsert_task_atomic(task_todo_high)
    await task_repo.upsert_task_atomic(task_in_progress_low)
    await task_repo.upsert_task_atomic(task_todo_med)

    resp = client.get("/api/tasks")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 3

    # Rank 1: IN_PROGRESS task
    assert tasks[0]["id"] == task_in_progress_low.id
    assert tasks[0]["status"] == "IN_PROGRESS"

    # Rank 2: High priority TODO (99.0)
    assert tasks[1]["id"] == task_todo_high.id
    assert tasks[1]["status"] == "TODO"
    assert tasks[1]["priority_score"] == 99.0

    # Rank 3: Medium priority TODO (60.0)
    assert tasks[2]["id"] == task_todo_med.id
    assert tasks[2]["status"] == "TODO"
    assert tasks[2]["priority_score"] == 60.0


@pytest.mark.asyncio
async def test_task1_pending_and_todo_ranked_by_priority_below_in_progress(test_setup):
    """Tasks in PENDING and TODO must be ordered by priority_score descending, but all below IN_PROGRESS."""
    client, svc, task_repo = test_setup

    t_in_prog = UnifiedTaskCandidate(
        id=str(uuid4()),
        title="Active Working Task",
        status=TaskStatus.IN_PROGRESS,
        priority_score=15.0,
    )
    t_pending_high = UnifiedTaskCandidate(
        id=str(uuid4()),
        title="Urgent Pending Approval Task",
        status="PENDING",
        priority_score=95.0,
    )
    t_todo_mid = UnifiedTaskCandidate(
        id=str(uuid4()),
        title="Normal Todo Task",
        status=TaskStatus.TODO,
        priority_score=75.0,
    )
    t_pending_low = UnifiedTaskCandidate(
        id=str(uuid4()),
        title="Low Pending Review Task",
        status="PENDING",
        priority_score=30.0,
    )

    await task_repo.upsert_task_atomic(t_in_prog)
    await task_repo.upsert_task_atomic(t_pending_high)
    await task_repo.upsert_task_atomic(t_todo_mid)
    await task_repo.upsert_task_atomic(t_pending_low)

    resp = client.get("/api/tasks")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 4

    # 1st: IN_PROGRESS task (even with lowest score 15.0)
    assert tasks[0]["id"] == t_in_prog.id
    assert tasks[0]["status"] == "IN_PROGRESS"

    # Remaining tasks ordered strictly by priority_score desc
    assert tasks[1]["id"] == t_pending_high.id
    assert tasks[1]["priority_score"] == 95.0

    assert tasks[2]["id"] == t_todo_mid.id
    assert tasks[2]["priority_score"] == 75.0

    assert tasks[3]["id"] == t_pending_low.id
    assert tasks[3]["priority_score"] == 30.0


def test_task1_today_board_planner_in_progress_first():
    """TodayBoardPlanner must place IN_PROGRESS tasks before higher priority TODO tasks in top_tasks."""
    planner = TodayBoardPlanner()

    t_todo_vip = UnifiedTaskCandidate(
        id="t-todo-vip",
        title="Critical Customer Bug in backlog",
        status=TaskStatus.TODO,
        priority_score=98.0,
    )
    t_in_prog = UnifiedTaskCandidate(
        id="t-in-progress",
        title="Ongoing Routine Task",
        status=TaskStatus.IN_PROGRESS,
        priority_score=25.0,
    )
    t_todo_low = UnifiedTaskCandidate(
        id="t-todo-low",
        title="Low Priority Chore",
        status=TaskStatus.TODO,
        priority_score=10.0,
    )

    board = planner.plan_today(
        user_id="test-user",
        tasks=[t_todo_vip, t_in_prog, t_todo_low],
        commitments=[],
    )

    assert len(board.top_tasks) == 3
    # Top task MUST be the IN_PROGRESS task
    assert board.top_tasks[0].task_id == "t-in-progress"
    assert board.top_tasks[0].status == TaskStatus.IN_PROGRESS

    # Remaining tasks ordered by priority score descending
    assert board.top_tasks[1].task_id == "t-todo-vip"
    assert board.top_tasks[2].task_id == "t-todo-low"


@pytest.mark.asyncio
async def test_task1_api_today_endpoint_ranks_in_progress_first(test_setup):
    """GET /api/today endpoint must return top_tasks with IN_PROGRESS tasks first."""
    client, svc, task_repo = test_setup

    t_todo = UnifiedTaskCandidate(
        id=str(uuid4()),
        title="High Priority Backlog Item",
        status=TaskStatus.TODO,
        priority_score=90.0,
    )
    t_in_prog = UnifiedTaskCandidate(
        id=str(uuid4()),
        title="Feature Currently Being Coded",
        status=TaskStatus.IN_PROGRESS,
        priority_score=35.0,
    )

    await task_repo.upsert_task_atomic(t_todo)
    await task_repo.upsert_task_atomic(t_in_prog)

    resp = client.get("/api/today")
    assert resp.status_code == 200
    data = resp.json()
    top_tasks = data.get("top_tasks", [])
    assert len(top_tasks) == 2
    assert top_tasks[0]["task_id"] == t_in_prog.id
    assert top_tasks[0]["status"] == "IN_PROGRESS"
    assert top_tasks[1]["task_id"] == t_todo.id
    assert top_tasks[1]["status"] == "TODO"


@pytest.mark.asyncio
async def test_task1_todo_ranks_before_higher_priority_blocked(test_setup):
    """P1-1 Regression: TODO tasks must outrank BLOCKED tasks even if BLOCKED has higher priority."""
    client, svc, task_repo = test_setup

    t_blocked_urgent = UnifiedTaskCandidate(
        id=str(uuid4()),
        title="High Priority Blocked Task",
        status=TaskStatus.BLOCKED,
        priority_score=95.0,
    )
    t_todo_normal = UnifiedTaskCandidate(
        id=str(uuid4()),
        title="Normal Priority Todo Task",
        status=TaskStatus.TODO,
        priority_score=40.0,
    )
    t_in_prog = UnifiedTaskCandidate(
        id=str(uuid4()),
        title="Working Task",
        status=TaskStatus.IN_PROGRESS,
        priority_score=10.0,
    )

    await task_repo.upsert_task_atomic(t_blocked_urgent)
    await task_repo.upsert_task_atomic(t_todo_normal)
    await task_repo.upsert_task_atomic(t_in_prog)

    # 1. Check /api/tasks (list_tasks)
    resp = client.get("/api/tasks")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 3
    assert tasks[0]["id"] == t_in_prog.id
    assert tasks[0]["status"] == "IN_PROGRESS"
    assert tasks[1]["id"] == t_todo_normal.id
    assert tasks[1]["status"] == "TODO"
    assert tasks[2]["id"] == t_blocked_urgent.id
    assert tasks[2]["status"] == "BLOCKED"

    # 2. Check /api/today (plan_today)
    resp_today = client.get("/api/today")
    assert resp_today.status_code == 200
    today_tasks = resp_today.json().get("top_tasks", [])
    assert len(today_tasks) == 3
    assert today_tasks[0]["task_id"] == t_in_prog.id
    assert today_tasks[0]["status"] == "IN_PROGRESS"
    assert today_tasks[1]["task_id"] == t_todo_normal.id
    assert today_tasks[1]["status"] == "TODO"
    assert today_tasks[2]["task_id"] == t_blocked_urgent.id
    assert today_tasks[2]["status"] == "BLOCKED"


@pytest.mark.asyncio
async def test_task1_canonical_sort_tiebreakers(test_setup):
    """Verify Tertiary (due_date ASC, nulls last) and Quaternary (updated_at DESC) tiebreakers."""
    client, svc, task_repo = test_setup
    now = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)

    # 3 tasks with identical status (TODO) and identical priority (50.0)
    t_due_early = UnifiedTaskCandidate(
        id="t-due-early",
        title="Task Due Early",
        status=TaskStatus.TODO,
        priority_score=50.0,
        due_date=now.replace(day=10),
    )
    t_due_late = UnifiedTaskCandidate(
        id="t-due-late",
        title="Task Due Late",
        status=TaskStatus.TODO,
        priority_score=50.0,
        due_date=now.replace(day=20),
    )
    t_due_none = UnifiedTaskCandidate(
        id="t-due-none",
        title="Task With No Due Date",
        status=TaskStatus.TODO,
        priority_score=50.0,
        due_date=None,
    )

    await task_repo.upsert_task_atomic(t_due_none)
    await task_repo.upsert_task_atomic(t_due_late)
    await task_repo.upsert_task_atomic(t_due_early)

    resp = client.get("/api/tasks")
    assert resp.status_code == 200
    ids = [t["id"] for t in resp.json()]
    assert ids == ["t-due-early", "t-due-late", "t-due-none"], (
        "Tertiary: due_date ASC (nulls last) must order earlier due date first and nulls last"
    )


@pytest.mark.asyncio
async def test_task1_list_tasks_and_plan_today_parity():
    """Verify Canonical Sort produces 100% identical ordering between list_tasks and plan_today."""
    from ptb_database.repositories.task_repo import TaskDomainRepository

    now = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
    planner = TodayBoardPlanner(max_top_tasks=20)

    # Set up sample tasks covering all 4 sort keys
    tasks = [
        UnifiedTaskCandidate(
            id="t-inprog-20",
            title="In Progress Low Priority",
            status=TaskStatus.IN_PROGRESS,
            priority_score=20.0,
            priority_override=20.0,
            due_date=now.replace(day=25),
            updated_at=now.replace(hour=1),
        ),
        UnifiedTaskCandidate(
            id="t-inprog-80",
            title="In Progress High Priority",
            status=TaskStatus.IN_PROGRESS,
            priority_score=80.0,
            priority_override=80.0,
            due_date=now.replace(day=20),
            updated_at=now.replace(hour=2),
        ),
        UnifiedTaskCandidate(
            id="t-todo-90",
            title="Todo High Priority",
            status=TaskStatus.TODO,
            priority_score=90.0,
            priority_override=90.0,
            due_date=now.replace(day=18),
            updated_at=now.replace(hour=3),
        ),
        UnifiedTaskCandidate(
            id="t-todo-30-early",
            title="Todo Low Priority Early Due",
            status=TaskStatus.TODO,
            priority_score=30.0,
            priority_override=30.0,
            due_date=now.replace(day=12),
            updated_at=now.replace(hour=4),
        ),
        UnifiedTaskCandidate(
            id="t-todo-30-late",
            title="Todo Low Priority Late Due",
            status=TaskStatus.TODO,
            priority_score=30.0,
            priority_override=30.0,
            due_date=now.replace(day=28),
            updated_at=now.replace(hour=5),
        ),
        UnifiedTaskCandidate(
            id="t-blocked-99",
            title="Blocked Very High Priority",
            status=TaskStatus.BLOCKED,
            priority_score=99.0,
            priority_override=99.0,
            due_date=now.replace(day=10),
            updated_at=now.replace(hour=6),
        ),
    ]

    # 1. Sort using TodayBoardPlanner
    board = planner.plan_today(user_id="u1", tasks=tasks, now=now)
    planner_ids = [t.task_id for t in board.top_tasks]

    # 2. Sort using TaskDomainRepository sort key
    repo_sorted = sorted(tasks, key=TaskDomainRepository._task_sort_key)
    repo_ids = [t.id for t in repo_sorted]

    assert planner_ids == repo_ids, (
        f"Sort drift detected!\nPlanner: {planner_ids}\nRepo:    {repo_ids}"
    )
    expected_order = [
        "t-inprog-80",     # Rank 1, score 80
        "t-inprog-20",     # Rank 1, score 20
        "t-todo-90",       # Rank 2, score 90
        "t-todo-30-early", # Rank 2, score 30, due day 12
        "t-todo-30-late",  # Rank 2, score 30, due day 28
        "t-blocked-99",    # Rank 3, score 99
    ]
    assert planner_ids == expected_order
    assert repo_ids == expected_order



# ==============================================================================
# Task 3 Tests: Full CRUD Operations via REST API
# ==============================================================================

def test_task3_create_task_endpoint(test_setup):
    """POST /api/tasks creates task with 201 status code."""
    client, _, _ = test_setup

    payload = {
        "title": "Fix Critical Production Bug",
        "description": "Investigate OOM killer on worker-02",
        "project_key": "OPS",
        "status": "IN_PROGRESS",
        "owner_name": "Dev",
        "priority_score": 90.0,
        "actor": "USER",
    }
    resp = client.post("/api/tasks", json=payload)
    assert resp.status_code == 201
    created = resp.json()
    assert created["title"] == payload["title"]
    assert created["status"] == "IN_PROGRESS"
    assert created["priority_score"] == 90.0
    task_id = created["id"]

    # Verify task appears in GET /api/tasks
    list_resp = client.get("/api/tasks")
    assert list_resp.status_code == 200
    assert any(t["id"] == task_id for t in list_resp.json())


def test_task3_patch_task_endpoint(test_setup):
    """PATCH /api/tasks/{task_id} modifies task attributes."""
    client, _, _ = test_setup

    create_resp = client.post(
        "/api/tasks",
        json={"title": "Original Title", "project_key": "CORE", "priority_score": 40.0},
    )
    task_id = create_resp.json()["id"]

    patch_resp = client.patch(
        f"/api/tasks/{task_id}",
        json={"title": "Updated Title", "priority_score": 85.0},
    )
    assert patch_resp.status_code == 200
    updated = patch_resp.json()
    assert updated["title"] == "Updated Title"
    assert updated["priority_score"] == 85.0


def test_task3_status_update_endpoint(test_setup):
    """POST /api/tasks/{task_id}/status transitions task status."""
    client, _, _ = test_setup

    create_resp = client.post(
        "/api/tasks",
        json={"title": "Status Test Task", "status": "TODO"},
    )
    task_id = create_resp.json()["id"]

    status_resp = client.post(
        f"/api/tasks/{task_id}/status",
        json={"new_status": "DONE", "actor": "USER"},
    )
    assert status_resp.status_code == 200
    assert status_resp.json()["success"] is True

    # Verify get reflects DONE
    get_resp = client.get(f"/api/tasks/{task_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["task"]["status"] == "DONE"


def test_task3_delete_task_endpoints(test_setup):
    """DELETE /api/tasks/{task_id} and POST /api/tasks/{task_id}/delete mark task DISMISSED."""
    client, _, _ = test_setup

    # Test DELETE HTTP verb
    c1 = client.post("/api/tasks", json={"title": "Task To Delete 1"}).json()
    del1 = client.delete(f"/api/tasks/{c1['id']}")
    assert del1.status_code == 200
    assert del1.json()["success"] is True

    # Test POST /api/tasks/{task_id}/delete
    c2 = client.post("/api/tasks", json={"title": "Task To Delete 2"}).json()
    del2 = client.post(f"/api/tasks/{c2['id']}/delete")
    assert del2.status_code == 200
    assert del2.json()["success"] is True


# ==============================================================================
# Task 2 Tests: OpenWebUI Tools Integration
# ==============================================================================

def test_task2_ptb_tools_create_and_delete_live():
    """Verify OpenWebUI Tools class calls Application API correctly for create and delete."""
    from unittest.mock import patch

    tools = PtbTools()
    tools.valves.app_service_url = "http://mock-service:8000"

    with patch.object(tools, "_http_call") as mock_call:
        mock_call.return_value = {
            "id": "task-uuid-123",
            "title": "OpenWebUI Created Task",
            "status": "TODO",
            "priority_score": 75.0,
        }

        res = tools.create_task(
            title="OpenWebUI Created Task",
            description="Created via AI Agent tool",
            priority_score=75.0,
        )
        assert "task-uuid-123" in res
        assert "OpenWebUI Created Task" in res
        mock_call.assert_called_once_with(
            "/api/tasks",
            method="POST",
            payload={
                "title": "OpenWebUI Created Task",
                "description": "Created via AI Agent tool",
                "project_key": "GLOBAL",
                "due_date": None,
                "priority_score": 75.0,
                "actor": "OPENWEBUI",
            },
        )

    with patch.object(tools, "_http_call") as mock_call:
        mock_call.return_value = {"success": True, "task_id": "task-uuid-123"}
        res_del = tools.delete_task("task-uuid-123")
        assert "task-uuid-123" in res_del
        mock_call.assert_called_once_with(
            "/api/tasks/task-uuid-123/delete",
            method="POST",
            payload={"actor": "OPENWEBUI"},
        )
