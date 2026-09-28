"""Unit tests for FastAPI REST Server (Layer 5).

Tests all 14 REST endpoints, CORS middleware, and error handling (404, 400).
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient

from ptb_application.api import app, get_application_service
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

    async def split_task(
        self,
        original_task_id: str,
        evidence_ids_to_detach: list[str],
        new_task_title: Optional[str] = None,
    ) -> UnifiedTaskCandidate:
        orig = self.tasks.get(original_task_id)
        if not orig:
            raise ValueError(f"Task {original_task_id} not found")
        detached = [e for e in orig.evidences if e.id in evidence_ids_to_detach]
        if not detached:
            raise ValueError(f"None of {evidence_ids_to_detach} found in task {original_task_id}")
        orig.evidences = [e for e in orig.evidences if e.id not in evidence_ids_to_detach]
        new_task = UnifiedTaskCandidate(
            id=str(uuid4()),
            title=new_task_title or f"Split: {orig.title}",
            status=orig.status,
            evidences=detached,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self.tasks[new_task.id] = new_task
        return new_task

    async def list_tasks(self, filters: Optional[dict] = None) -> list[UnifiedTaskCandidate]:
        tasks = list(self.tasks.values())
        if not filters:
            return tasks

        filtered = []
        now = datetime.now(timezone.utc)
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

            if "project" in filters and filters["project"]:
                if (t.project_key or "").lower() != str(filters["project"]).lower():
                    continue

            if "customer" in filters and filters["customer"]:
                if (t.customer_id or "").lower() != str(filters["customer"]).lower():
                    continue

            if "source" in filters and filters["source"]:
                req_source = str(filters["source"]).lower()
                if not any((ev.source_type or "").lower() == req_source for ev in t.evidences):
                    continue

            if "owner" in filters and filters["owner"]:
                req_owner = str(filters["owner"]).lower()
                owner_match = (
                    (t.owner_canonical_id or "").lower() == req_owner
                    or (t.owner_name or "").lower() == req_owner
                )
                if not owner_match:
                    continue

            if "priority" in filters and filters["priority"] is not None:
                p_filter = filters["priority"]
                if isinstance(p_filter, dict):
                    min_p = p_filter.get("min", 0.0)
                    max_p = p_filter.get("max", 100.0)
                    if not (min_p <= t.priority_score <= max_p):
                        continue

            if "due_range" in filters and filters["due_range"]:
                due_range = filters["due_range"]
                if not t.due_date:
                    continue
                t_due = t.due_date if t.due_date.tzinfo else t.due_date.replace(tzinfo=timezone.utc)
                if isinstance(due_range, dict):
                    start_dt = due_range.get("start")
                    end_dt = due_range.get("end")
                    if start_dt:
                        start_utc = start_dt if start_dt.tzinfo else start_dt.replace(tzinfo=timezone.utc)
                        if t_due < start_utc:
                            continue
                    if end_dt:
                        end_utc = end_dt if end_dt.tzinfo else end_dt.replace(tzinfo=timezone.utc)
                        if t_due > end_utc:
                            continue

            if "stale" in filters and filters["stale"] is not None:
                updated = t.updated_at or t.created_at
                is_stale = False
                if updated:
                    upd_utc = updated if updated.tzinfo else updated.replace(tzinfo=timezone.utc)
                    is_stale = (now - upd_utc).total_seconds() >= (3 * 86400) and t.status not in [TaskStatus.DONE, TaskStatus.DISMISSED]
                if is_stale != bool(filters["stale"]):
                    continue

            if "waiting" in filters and filters["waiting"] is not None:
                is_waiting = (t.status == TaskStatus.BLOCKED)
                if is_waiting != bool(filters["waiting"]):
                    continue

            if "review_status" in filters and filters["review_status"]:
                if (t.review_status or "").lower() != str(filters["review_status"]).lower():
                    continue

            filtered.append(t)

        if "limit" in filters and isinstance(filters["limit"], int):
            return filtered[:filters["limit"]]
        return filtered

    async def get_task_with_context(self, task_id: str) -> Optional[TaskWithContext]:
        task = self.tasks.get(task_id)
        if not task:
            return None
        now = datetime.now(timezone.utc)
        last_change = task.updated_at or task.created_at or now
        if last_change.tzinfo is None:
            last_change = last_change.replace(tzinfo=timezone.utc)
        days_in_status = max(0, int((now - last_change).total_seconds() // 86400))
        return TaskWithContext(
            task=task,
            last_status_change_at=last_change,
            days_in_current_status=days_in_status,
            has_completion_evidence=False,
            blocking_tasks=["task-blocker-1"] if task.status == TaskStatus.BLOCKED else [],
            dependent_people=["Alex"] if task.status == TaskStatus.BLOCKED else [],
        )

    async def get_review_queue(self, limit: int = 20) -> list[ReviewQueueItem]:
        items: list[ReviewQueueItem] = []
        for t in self.tasks.values():
            if t.review_status == "pending_review" or (0.40 <= (t.extraction_confidence or 0.0) < 0.65):
                raw_id = t.evidences[0].raw_event_id if t.evidences else "raw-001"
                items.append(ReviewQueueItem(
                    id=f"rev-{t.id}",
                    raw_event_id=raw_id,
                    candidate_task=t,
                    reason=f"Confidence {t.extraction_confidence} requires human review",
                    created_at=t.created_at or datetime.now(timezone.utc),
                ))
                if len(items) >= limit:
                    break
        return items

    async def get_active_commitments(self, user_id: Optional[str] = None) -> list[CommitmentRecord]:
        return self.commitments


class MockCheckpointRepository:
    def __init__(self, checkpoints: Optional[List[IngestionCheckpointRecord]] = None):
        self.checkpoints = checkpoints or []

    async def list_checkpoints(self) -> list[IngestionCheckpointRecord]:
        return self.checkpoints


class MockGraphitiMemoryClient:
    def __init__(self, episodes: Optional[List[Dict[str, Any]]] = None):
        self.episodes = episodes or []

    async def search_context(self, query: str, limit: int = 5, include_invalidated: bool = False) -> List[Dict[str, Any]]:
        results = []
        words = [w.lower() for w in query.split() if len(w) >= 3]
        for ep in self.episodes:
            topic = ep.get("topic", "").lower()
            summary = ep.get("summary", "").lower()
            content = ep.get("content", "").lower()
            if not words or any(w in topic or w in summary or w in content for w in words):
                results.append(ep)
        return results[:limit]


# ==============================================================================
# FIXTURES
# ==============================================================================

@pytest.fixture
def mock_service() -> ApplicationService:
    now = datetime.now(timezone.utc)
    ev1 = EvidenceRecord(
        id="ev-1",
        raw_event_id="raw-1",
        evidence_type=EvidenceType.CHAT_COMMITMENT,
        source_type="ms_teams",
        timestamp=now - timedelta(hours=2),
        snippet="Tôi sẽ sửa bug login vào chiều nay",
        confidence=0.95,
    )
    ev2 = EvidenceRecord(
        id="ev-2",
        raw_event_id="raw-2",
        evidence_type=EvidenceType.JIRA_TICKET,
        source_type="jira",
        timestamp=now - timedelta(days=4),
        snippet="PROJ-101: Triển khai gateway",
        confidence=0.55,
    )

    t1 = UnifiedTaskCandidate(
        id="task-1",
        title="Sửa lỗi login Microsoft Teams",
        status=TaskStatus.TODO,
        priority_score=85.0,
        owner_canonical_id="person-me",
        owner_name="Me",
        project_key="AUTH",
        customer_id="CUST-A",
        due_date=now + timedelta(days=1),
        explicit_deadline=True,
        extraction_confidence=0.95,
        review_status="auto_approved",
        created_at=now - timedelta(days=1),
        updated_at=now - timedelta(hours=1),
        evidences=[ev1],
    )

    t2 = UnifiedTaskCandidate(
        id="task-2",
        title="Review ticket PROJ-101 gateway",
        status=TaskStatus.BLOCKED,
        priority_score=45.0,
        owner_canonical_id="person-huy",
        owner_name="Huy Nguyen",
        project_key="GATEWAY",
        customer_id="CUST-B",
        due_date=now + timedelta(days=3),
        explicit_deadline=False,
        extraction_confidence=0.55,
        review_status="pending_review",
        created_at=now - timedelta(days=5),
        updated_at=now - timedelta(days=4),
        evidences=[ev2],
    )

    task_repo = MockTaskDomainRepository([t1, t2])
    task_repo.commitments = [
        CommitmentRecord(
            id="comm-1",
            title="Gửi tài liệu API",
            owner_id="person-me",
            requester_id="person-huy",
            status="ACTIVE",
            created_at=now - timedelta(days=5),
        )
    ]
    checkpoint_repo = MockCheckpointRepository([
        IngestionCheckpointRecord(
            id="cp-1",
            tenant_id="tenant-ms",
            source_type=SourceType.MS_TEAMS,
            stream_id="stream-1",
            last_external_id="msg-100",
            last_event_timestamp=now - timedelta(minutes=15),
            updated_at=now,
        ),
    ])
    graph_memory = MockGraphitiMemoryClient([
        {
            "id": "dec-1",
            "type": "DECISION",
            "topic": "AUTH",
            "summary": "Sử dụng Redis cache cho Session JWT",
            "content": "Giảm tải DB khi xác thực token",
            "valid_at": now.isoformat(),
            "properties": {
                "decision_id": "dec-1",
                "project_key": "AUTH",
                "summary": "Sử dụng Redis cache cho Session JWT",
                "rationale": "Giảm tải DB khi xác thực token",
                "decided_by": "Architect",
                "decided_at": now.isoformat(),
            },
        },
        {
            "id": "les-1",
            "type": "LESSON",
            "topic": "GATEWAY",
            "summary": "Timeout khi gọi downstream service",
            "content": "Cần thêm circuit breaker pattern",
            "valid_at": now.isoformat(),
            "properties": {
                "lesson_id": "les-1",
                "topic": "GATEWAY Timeout",
                "description": "Timeout khi gọi downstream service",
                "solution": "Cần thêm circuit breaker pattern",
                "recorded_at": now.isoformat(),
            },
        },
    ])

    return ApplicationService(
        task_repo=task_repo,
        checkpoint_repo=checkpoint_repo,
        graph_memory=graph_memory,
    )


@pytest.fixture
def client(mock_service: ApplicationService):
    app.dependency_overrides[get_application_service] = lambda: mock_service
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# ==============================================================================
# TESTS
# ==============================================================================

def test_endpoint_1_health(client: TestClient):
    """1. GET /health: kiểm tra cấu trúc phản hồi health check."""
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["service"] == "ptb-application"
    assert "timestamp" in data


def test_endpoint_2_today(client: TestClient):
    """2. GET /api/today: kiểm tra TodayBoardView tổng hợp."""
    resp = client.get("/api/today?user_id=test-user")
    assert resp.status_code == 200
    data = resp.json()
    assert data["user_id"] == "test-user"
    assert "summary_headline" in data
    assert "top_tasks" in data
    assert len(data["top_tasks"]) > 0
    assert data["top_tasks"][0]["task_id"] == "task-1"
    assert "waiting_on_others" in data
    assert "forgotten_commitments" in data


def test_endpoint_3_tasks_list_and_filters(client: TestClient):
    """3. GET /api/tasks: kiểm tra lọc theo các query parameters."""
    # Lấy toàn bộ
    resp = client.get("/api/tasks")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 2

    # Lọc status đơn
    resp_todo = client.get("/api/tasks?status=TODO")
    assert resp_todo.status_code == 200
    assert len(resp_todo.json()) == 1
    assert resp_todo.json()[0]["id"] == "task-1"

    # Lọc status nhiều giá trị dạng phẩy
    resp_multi = client.get("/api/tasks?status=TODO,BLOCKED")
    assert resp_multi.status_code == 200
    assert len(resp_multi.json()) == 2

    # Lọc project
    resp_proj = client.get("/api/tasks?project=AUTH")
    assert resp_proj.status_code == 200
    assert len(resp_proj.json()) == 1
    assert resp_proj.json()[0]["project_key"] == "AUTH"

    # Lọc customer
    resp_cust = client.get("/api/tasks?customer=CUST-B")
    assert resp_cust.status_code == 200
    assert len(resp_cust.json()) == 1
    assert resp_cust.json()[0]["id"] == "task-2"

    # Lọc source
    resp_src = client.get("/api/tasks?source=ms_teams")
    assert resp_src.status_code == 200
    assert len(resp_src.json()) == 1
    assert resp_src.json()[0]["id"] == "task-1"

    # Lọc owner
    resp_owner = client.get("/api/tasks?owner=Huy%20Nguyen")
    assert resp_owner.status_code == 200
    assert len(resp_owner.json()) == 1

    # Lọc priority range
    resp_prio = client.get("/api/tasks?priority_min=80.0&priority_max=100.0")
    assert resp_prio.status_code == 200
    assert len(resp_prio.json()) == 1
    assert resp_prio.json()[0]["id"] == "task-1"

    # Lọc stale
    resp_stale = client.get("/api/tasks?stale=true")
    assert resp_stale.status_code == 200
    assert len(resp_stale.json()) == 1
    assert resp_stale.json()[0]["id"] == "task-2"

    # Lọc waiting
    resp_waiting = client.get("/api/tasks?waiting=true")
    assert resp_waiting.status_code == 200
    assert len(resp_waiting.json()) == 1
    assert resp_waiting.json()[0]["id"] == "task-2"

    # Lọc review_status
    resp_rev = client.get("/api/tasks?review_status=pending_review")
    assert resp_rev.status_code == 200
    assert len(resp_rev.json()) == 1
    assert resp_rev.json()[0]["id"] == "task-2"

    # Lọc limit
    resp_limit = client.get("/api/tasks?limit=1")
    assert resp_limit.status_code == 200
    assert len(resp_limit.json()) == 1


def test_endpoint_4_task_detail(client: TestClient):
    """4. GET /api/tasks/{task_id}: kiểm tra chi tiết task và 404 khi không tìm thấy."""
    # Tìm thấy
    resp = client.get("/api/tasks/task-1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["task"]["id"] == "task-1"
    assert "blocking_tasks" in data
    assert "dependent_people" in data

    # Không tìm thấy -> 404
    resp_404 = client.get("/api/tasks/task-non-existent")
    assert resp_404.status_code == 404
    assert "not found" in resp_404.json()["detail"].lower()


def test_endpoint_5_review_queue(client: TestClient):
    """5. GET /api/review: kiểm tra hàng đợi review."""
    resp = client.get("/api/review?limit=10")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["candidate_task"]["id"] == "task-2"
    assert "reason" in items[0]


def test_endpoint_6_review_approve(client: TestClient):
    """6. POST /api/review/{task_id}/approve: duyệt task và 404 khi task không tồn tại."""
    # Thành công
    resp = client.post("/api/review/task-2/approve", json={"actor": "ADMIN", "new_status": "TODO"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["task_id"] == "task-2"

    # Kiểm tra task detail đã đổi review_status
    task_resp = client.get("/api/tasks/task-2")
    assert task_resp.json()["task"]["review_status"] == "auto_approved"

    # Task không tồn tại -> 404
    resp_404 = client.post("/api/review/task-unknown/approve")
    assert resp_404.status_code == 404


def test_endpoint_7_review_dismiss(client: TestClient):
    """7. POST /api/review/{task_id}/dismiss: bỏ qua task review."""
    # Thành công
    resp = client.post("/api/review/task-2/dismiss", json={"actor": "USER"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["task_id"] == "task-2"

    # Kiểm tra task status đã thành DISMISSED
    task_resp = client.get("/api/tasks/task-2")
    assert task_resp.json()["task"]["status"] == "DISMISSED"

    # Task không tồn tại -> 404
    resp_404 = client.post("/api/review/task-unknown/dismiss")
    assert resp_404.status_code == 404


def test_endpoint_8_patch_task(client: TestClient):
    """8. PATCH /api/tasks/{task_id}: cập nhật thông tin task."""
    # Cập nhật title, priority_score, project_key
    patch_body = {
        "title": "Tiêu đề đã sửa qua REST API",
        "priority_score": 92.5,
        "project_key": "AUTH_V2",
    }
    resp = client.patch("/api/tasks/task-1", json=patch_body)
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["id"] == "task-1"
    assert updated["title"] == "Tiêu đề đã sửa qua REST API"
    assert updated["priority_score"] == 92.5
    assert updated["project_key"] == "AUTH_V2"

    # Sai status -> 400
    resp_bad = client.patch("/api/tasks/task-1", json={"status": "INVALID_STATUS"})
    assert resp_bad.status_code == 400

    # Task không tồn tại -> 404
    resp_404 = client.patch("/api/tasks/task-unknown", json={"title": "Test"})
    assert resp_404.status_code == 404


def test_endpoint_9_update_task_status(client: TestClient):
    """9. POST /api/tasks/{task_id}/status: cập nhật trạng thái task và validate."""
    # Cập nhật hợp lệ
    resp = client.post("/api/tasks/task-1/status", json={"status": "IN_PROGRESS", "actor": "DEV"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True

    # Kiểm tra trạng thái mới
    task_resp = client.get("/api/tasks/task-1")
    assert task_resp.json()["task"]["status"] == "IN_PROGRESS"

    # Trạng thái không hợp lệ -> 400
    resp_invalid = client.post("/api/tasks/task-1/status", json={"status": "WRONG_STATUS"})
    assert resp_invalid.status_code == 400

    # Thiếu status -> 400
    resp_missing = client.post("/api/tasks/task-1/status", json={})
    assert resp_missing.status_code == 400

    # Task không tồn tại -> 404
    resp_404 = client.post("/api/tasks/task-unknown/status", json={"status": "DONE"})
    assert resp_404.status_code == 404


def test_endpoint_10_split_task(client: TestClient):
    """10. POST /api/tasks/{task_id}/split: tách task thành task mới."""
    # Tách thành công
    resp = client.post(
        "/api/tasks/task-1/split",
        json={"evidence_ids": ["ev-1"], "new_title": "Tách bug xác thực token"},
    )
    assert resp.status_code == 200
    new_task = resp.json()
    assert new_task["title"] == "Tách bug xác thực token"
    assert len(new_task["evidences"]) == 1
    assert new_task["evidences"][0]["id"] == "ev-1"

    # evidence_ids rỗng -> 400
    resp_empty = client.post("/api/tasks/task-1/split", json={"evidence_ids": []})
    assert resp_empty.status_code == 400

    # evidence_ids không tồn tại trong task -> 400
    resp_bad_ev = client.post("/api/tasks/task-1/split", json={"evidence_ids": ["non-existent-ev"]})
    assert resp_bad_ev.status_code == 400

    # task không tồn tại -> 404
    resp_404 = client.post("/api/tasks/task-unknown/split", json={"evidence_ids": ["ev-1"]})
    assert resp_404.status_code == 404


def test_endpoint_11_waiting(client: TestClient):
    """11. GET /api/waiting: danh sách WaitingOnItem."""
    resp = client.get("/api/waiting")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


def test_endpoint_12_forgotten(client: TestClient):
    """12. GET /api/forgotten: danh sách ForgottenCommitmentItem."""
    resp = client.get("/api/forgotten?days_stale=2")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


def test_endpoint_13_knowledge(client: TestClient):
    """13. GET /api/knowledge: tìm kiếm decisions và lessons."""
    # Thành công
    resp = client.get("/api/knowledge?query=AUTH&project_key=AUTH&limit=5")
    assert resp.status_code == 200
    data = resp.json()
    assert "decisions" in data
    assert "lessons" in data
    assert "synthesis_summary" in data

    # Query rỗng -> 400 hoặc 422
    resp_empty = client.get("/api/knowledge?query=%20%20")
    assert resp_empty.status_code == 400


def test_endpoint_14_sources_health(client: TestClient):
    """14. GET /api/sources/health: báo cáo CoverageStatusResponse."""
    resp = client.get("/api/sources/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "tenants" in data
    assert "overall_health" in data
    assert data["overall_health"] == "healthy"
    assert len(data["tenants"]) >= 1


def test_cors_middleware(client: TestClient):
    """Kiểm tra cấu hình CORS middleware cho phép OpenWebUI cổng 3000."""
    # Origin localhost:3000
    resp1 = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert resp1.headers.get("access-control-allow-origin") == "http://localhost:3000"

    # Origin 127.0.0.1:3000
    resp2 = client.get("/health", headers={"Origin": "http://127.0.0.1:3000"})
    assert resp2.headers.get("access-control-allow-origin") == "http://127.0.0.1:3000"

    # OPTIONS preflight
    resp_options = client.options(
        "/api/tasks",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp_options.status_code == 200
    assert resp_options.headers.get("access-control-allow-origin") == "http://localhost:3000"
