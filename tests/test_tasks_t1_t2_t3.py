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
