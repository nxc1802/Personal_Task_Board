"""Unit tests for FastAPI REST Server (Layer 5).

Tests all 14 REST endpoints, CORS middleware, and error handling (404, 400).
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4
import json
import pytest
from fastapi.testclient import TestClient

from ptb_application.api import app, create_app, get_application_service, set_application_service
from ptb_application.service import ApplicationService
from ptb_mcp.server import create_mcp_server
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
    assert data["neo4j"] == "healthy"
    assert data["processing_worker"] == "healthy"
    assert data["graphiti"] == "healthy"
    assert data["llm"] == "healthy"
    assert "playwright" in data


def test_deep_health_healthy(client: TestClient):
    """Kiểm tra deep health khi các dependencies khỏe mạnh."""
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    for key in ("neo4j", "processing_worker", "graphiti", "llm", "playwright"):
        assert key in data
    assert data["neo4j"] == "healthy"
    assert data["processing_worker"] == "healthy"
    assert data["graphiti"] == "healthy"
    assert data["llm"] == "healthy"


def test_deep_health_neo4j_down_returns_not_ready(
    client: TestClient,
    mock_service: ApplicationService,
    caplog: pytest.LogCaptureFixture,
):
    """Khi neo4j_client.verify_connectivity() trả về False -> status == 'not_ready', neo4j == 'not_ready', phát sinh PTB-APP-001."""
    import logging
    from unittest.mock import AsyncMock

    mock_service._explicit_neo4j_client = True
    mock_service.neo4j_client.verify_connectivity = AsyncMock(return_value=False)

    with caplog.at_level(logging.ERROR, logger="ptb.bugs.application"):
        resp = client.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "not_ready"
    assert data["neo4j"] == "not_ready"
    assert "PTB-APP-001" in caplog.text


def test_deep_health_graphiti_down_returns_degraded(
    client: TestClient,
    mock_service: ApplicationService,
    caplog: pytest.LogCaptureFixture,
):
    """Khi Graphiti lỗi (is_healthy = False) -> status == 'degraded', graphiti == 'degraded', nhưng neo4j == 'healthy'."""
    import logging

    mock_service.graph_memory.is_healthy = False

    with caplog.at_level(logging.WARNING, logger="ptb.bugs.graph_memory"):
        resp = client.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["graphiti"] == "degraded"
    assert data["neo4j"] == "healthy"
    assert "PTB-GRAPH-001" in caplog.text


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


def test_endpoint_6_review_approve(client: TestClient, mock_service: ApplicationService):
    """6. POST /api/review/{task_id}/approve: duyệt task sang TODO, lưu thật qua repository, ghi audit và 404/400."""
    initial_audit_count = len(mock_service.task_repo.audits)

    # Thành công với payload mặc định (chuyển từ BLOCKED/pending_review sang TODO/auto_approved)
    resp = client.post("/api/review/task-2/approve", json={"actor": "ADMIN"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["task_id"] == "task-2"

    # Kiểm tra task trong repository đã đổi sang trạng thái active TODO và auto_approved
    persisted_task = mock_service.task_repo.tasks["task-2"]
    assert persisted_task.status == TaskStatus.TODO
    assert persisted_task.review_status == "auto_approved"
    assert persisted_task.status_authoritative is True

    # Kiểm tra StatusTransitionAuditRecord đã được ghi
    assert len(mock_service.task_repo.audits) == initial_audit_count + 1
    latest_audit = mock_service.task_repo.audits[-1]
    assert latest_audit.task_id == "task-2"
    assert latest_audit.old_status == TaskStatus.BLOCKED
    assert latest_audit.new_status == TaskStatus.TODO
    assert latest_audit.change_actor == "ADMIN"

    # Kiểm tra task không còn nằm trong review queue
    rev_resp = client.get("/api/review")
    assert rev_resp.status_code == 200
    assert all(item["candidate_task"]["id"] != "task-2" for item in rev_resp.json())

    # Hỗ trợ cả review_id có tiền tố rev- (tương thích ptb_tools.py)
    resp_prefix = client.post("/api/review/rev-task-2/approve", json={"actor": "USER", "new_status": "IN_PROGRESS"})
    assert resp_prefix.status_code == 200
    assert mock_service.task_repo.tasks["task-2"].status == TaskStatus.IN_PROGRESS

    # Trạng thái không hợp lệ -> 400
    resp_bad_status = client.post("/api/review/task-2/approve", json={"new_status": "INVALID_STATE"})
    assert resp_bad_status.status_code == 400

    # Task không tồn tại -> 404
    resp_404 = client.post("/api/review/task-unknown/approve")
    assert resp_404.status_code == 404


def test_endpoint_7_review_dismiss(client: TestClient, mock_service: ApplicationService):
    """7. POST /api/review/{task_id}/dismiss: loại bỏ task review sang DISMISSED, lưu thật qua repository."""
    initial_audit_count = len(mock_service.task_repo.audits)

    # Thành công
    resp = client.post("/api/review/task-2/dismiss", json={"actor": "USER"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["task_id"] == "task-2"

    # Kiểm tra task trong repository đã thành DISMISSED và review_status == rejected
    persisted_task = mock_service.task_repo.tasks["task-2"]
    assert persisted_task.status == TaskStatus.DISMISSED
    assert persisted_task.review_status == "rejected"
    assert persisted_task.status_authoritative is True

    # Kiểm tra StatusTransitionAuditRecord đã được ghi
    assert len(mock_service.task_repo.audits) == initial_audit_count + 1
    latest_audit = mock_service.task_repo.audits[-1]
    assert latest_audit.task_id == "task-2"
    assert latest_audit.new_status == TaskStatus.DISMISSED
    assert latest_audit.change_actor == "USER"

    # Kiểm tra task detail đã thành DISMISSED và không còn trong review queue
    task_resp = client.get("/api/tasks/task-2")
    assert task_resp.json()["task"]["status"] == "DISMISSED"
    rev_resp = client.get("/api/review")
    assert all(item["candidate_task"]["id"] != "task-2" for item in rev_resp.json())

    # Task không tồn tại -> 404
    resp_404 = client.post("/api/review/task-unknown/dismiss")
    assert resp_404.status_code == 404


def test_endpoint_8_patch_task(client: TestClient, mock_service: ApplicationService):
    """8. PATCH /api/tasks/{task_id}: cập nhật thông tin task (title, project_key, owner_name, due_date, notes, ...)."""
    initial_audit_count = len(mock_service.task_repo.audits)

    # Cập nhật đầy đủ các trường: title, priority_score, project_key, owner_name, due_date, notes, status
    patch_body = {
        "title": "Tiêu đề đã sửa qua REST API",
        "priority_score": 92.5,
        "project_key": "AUTH_V2",
        "owner_name": "Dam Quang Cuong",
        "due_date": "2026-10-15",
        "notes": "Ghi chú chi tiết cập nhật từ modal edit",
        "status": "IN_PROGRESS",
        "actor": "EDITOR",
    }
    resp = client.patch("/api/tasks/task-1", json=patch_body)
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["id"] == "task-1"
    assert updated["title"] == "Tiêu đề đã sửa qua REST API"
    assert updated["priority_score"] == 92.5
    assert updated["project_key"] == "AUTH_V2"
    assert updated["owner_name"] == "Dam Quang Cuong"
    assert updated["description"] == "Ghi chú chi tiết cập nhật từ modal edit"
    assert updated["status"] == "IN_PROGRESS"
    assert updated["due_date"].startswith("2026-10-15")

    # Kiểm tra lưu thật trong repository và ghi StatusTransitionAuditRecord
    persisted = mock_service.task_repo.tasks["task-1"]
    assert persisted.title == "Tiêu đề đã sửa qua REST API"
    assert persisted.owner_name == "Dam Quang Cuong"
    assert persisted.description == "Ghi chú chi tiết cập nhật từ modal edit"
    assert persisted.status == TaskStatus.IN_PROGRESS
    assert len(mock_service.task_repo.audits) == initial_audit_count + 1
    assert mock_service.task_repo.audits[-1].new_status == TaskStatus.IN_PROGRESS

    # Sai status -> 400
    resp_bad = client.patch("/api/tasks/task-1", json={"status": "INVALID_STATUS"})
    assert resp_bad.status_code == 400
    resp_non_canonical = client.patch("/api/tasks/task-1", json={"status": "OPEN"})
    assert resp_non_canonical.status_code == 400

    # Title rỗng -> 400
    resp_empty_title = client.patch("/api/tasks/task-1", json={"title": "   "})
    assert resp_empty_title.status_code == 400

    # due_date sai định dạng -> 400
    resp_bad_due = client.patch("/api/tasks/task-1", json={"due_date": "not-a-valid-date"})
    assert resp_bad_due.status_code == 400

    # Task không tồn tại -> 404
    resp_404 = client.patch("/api/tasks/task-unknown", json={"title": "Test"})
    assert resp_404.status_code == 404


def test_endpoint_9_update_task_status(client: TestClient, mock_service: ApplicationService):
    """9. POST /api/tasks/{task_id}/status: hỗ trợ cả status và new_status, kiểm tra 5 trạng thái canonical và ghi audit."""
    initial_audit_count = len(mock_service.task_repo.audits)

    # 1. Cập nhật hợp lệ với trường 'status' (tương thích ptb_board.html)
    resp = client.post("/api/tasks/task-1/status", json={"status": "IN_PROGRESS", "actor": "DEV"})
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert mock_service.task_repo.tasks["task-1"].status == TaskStatus.IN_PROGRESS
    assert len(mock_service.task_repo.audits) == initial_audit_count + 1
    assert mock_service.task_repo.audits[-1].old_status == TaskStatus.TODO
    assert mock_service.task_repo.audits[-1].new_status == TaskStatus.IN_PROGRESS
    assert mock_service.task_repo.audits[-1].change_actor == "DEV"

    # 2. Cập nhật hợp lệ với trường 'new_status' (tương thích ptb_tools.py)
    resp_new = client.post("/api/tasks/task-1/status", json={"new_status": "BLOCKED", "actor": "TOOL"})
    assert resp_new.status_code == 200
    assert resp_new.json()["success"] is True
    assert mock_service.task_repo.tasks["task-1"].status == TaskStatus.BLOCKED
    assert len(mock_service.task_repo.audits) == initial_audit_count + 2
    assert mock_service.task_repo.audits[-1].old_status == TaskStatus.IN_PROGRESS
    assert mock_service.task_repo.audits[-1].new_status == TaskStatus.BLOCKED
    assert mock_service.task_repo.audits[-1].change_actor == "TOOL"

    # 3. Kiểm tra đầy đủ các trạng thái canonical còn lại (DONE, DISMISSED, TODO)
    for canonical_st in ("DONE", "DISMISSED", "TODO"):
        r = client.post("/api/tasks/task-1/status", json={"new_status": canonical_st})
        assert r.status_code == 200
        assert mock_service.task_repo.tasks["task-1"].status == TaskStatus(canonical_st)

    # 4. Trạng thái không thuộc 5 trạng thái canonical -> 400
    for invalid_st in ("WRONG_STATUS", "OPEN", "PENDING", "ARCHIVED"):
        resp_invalid = client.post("/api/tasks/task-1/status", json={"status": invalid_st})
        assert resp_invalid.status_code == 400
        resp_invalid_new = client.post("/api/tasks/task-1/status", json={"new_status": invalid_st})
        assert resp_invalid_new.status_code == 400

    # 5. Truyền cả status và new_status nhưng xung đột nhau -> 400
    resp_conflict = client.post(
        "/api/tasks/task-1/status",
        json={"status": "TODO", "new_status": "DONE"},
    )
    assert resp_conflict.status_code == 400

    # 6. Thiếu cả status và new_status -> 400
    resp_missing = client.post("/api/tasks/task-1/status", json={})
    assert resp_missing.status_code == 400

    # 7. Task không tồn tại -> 404
    resp_404 = client.post("/api/tasks/task-unknown/status", json={"status": "DONE"})
    assert resp_404.status_code == 404
    resp_404_new = client.post("/api/tasks/task-unknown/status", json={"new_status": "DONE"})
    assert resp_404_new.status_code == 404


def test_endpoint_10_split_task(client: TestClient, mock_service: ApplicationService):
    """10. POST /api/tasks/{task_id}/split: tách task thành task mới thật trong repository."""
    initial_task_count = len(mock_service.task_repo.tasks)

    # Tách thành công
    resp = client.post(
        "/api/tasks/task-1/split",
        json={"evidence_ids": ["ev-1"], "new_title": "Tách bug xác thực token"},
    )
    assert resp.status_code == 200
    new_task = resp.json()
    new_task_id = new_task["id"]
    assert new_task["title"] == "Tách bug xác thực token"
    assert len(new_task["evidences"]) == 1
    assert new_task["evidences"][0]["id"] == "ev-1"

    # Kiểm tra task mới đã được lưu thật trong repository và evidence đã tách khỏi task gốc
    assert len(mock_service.task_repo.tasks) == initial_task_count + 1
    assert new_task_id in mock_service.task_repo.tasks
    assert len(mock_service.task_repo.tasks["task-1"].evidences) == 0

    # evidence_ids rỗng hoặc chỉ chứa khoảng trắng -> 400
    resp_empty = client.post("/api/tasks/task-1/split", json={"evidence_ids": []})
    assert resp_empty.status_code == 400
    resp_blank = client.post("/api/tasks/task-1/split", json={"evidence_ids": ["   "]})
    assert resp_blank.status_code == 400

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


def test_set_application_service_and_create_app(mock_service: ApplicationService):
    """Kiểm tra set_application_service và create_app(application_service=...)."""
    # 1. create_app với explicit application_service
    custom_app = create_app(application_service=mock_service)
    with TestClient(custom_app) as custom_client:
        resp = custom_client.get("/api/tasks")
        assert resp.status_code == 200
        tasks = resp.json()
        assert len(tasks) == 2

    # 2. set_application_service trực tiếp
    set_application_service(mock_service)
    assert get_application_service() is mock_service

    # Reset
    set_application_service(None)


@pytest.mark.asyncio
async def test_fastapi_and_mcp_shared_application_service_state():
    """Kiểm tra FastAPI và FastMCP cùng chia sẻ trạng thái và repository từ chung một ApplicationService."""
    now = datetime.now(timezone.utc)
    ev = EvidenceRecord(
        id="ev-shared-1",
        raw_event_id="raw-1",
        evidence_type=EvidenceType.CHAT_COMMITMENT,
        source_type="ms_teams",
        timestamp=now,
        snippet="Shared service test evidence",
        confidence=0.9,
    )
    t1 = UnifiedTaskCandidate(
        id="task-shared-1",
        title="Nhiệm vụ chung FastAPI và FastMCP",
        status=TaskStatus.TODO,
        priority_score=75.0,
        owner_name="Dev",
        project_key="SHARED",
        due_date=now + timedelta(days=2),
        explicit_deadline=True,
        extraction_confidence=0.9,
        review_status="auto_approved",
        evidences=[ev],
    )
    task_repo = MockTaskDomainRepository([t1])
    checkpoint_repo = MockCheckpointRepository()
    graph_memory = MockGraphitiMemoryClient()

    # Khởi tạo MỘT instance ApplicationService duy nhất
    shared_service = ApplicationService(
        task_repo=task_repo,
        checkpoint_repo=checkpoint_repo,
        graph_memory=graph_memory,
    )

    # Inject vào cả FastAPI và FastMCP
    api_app = create_app(application_service=shared_service)
    mcp_srv = create_mcp_server(application_service=shared_service)

    # 1. FastMCP đọc trạng thái ban đầu của task
    mcp_res = await mcp_srv.call_tool("get_task_context", {"task_id": "task-shared-1"})
    assert mcp_res.is_error is False
    mcp_data = mcp_res.structured_content if hasattr(mcp_res, "structured_content") and mcp_res.structured_content else json.loads(mcp_res.content[0].text)
    if isinstance(mcp_data, dict) and "result" in mcp_data:
        mcp_data = mcp_data["result"]
    assert mcp_data["task"]["status"] == "TODO"
    assert mcp_data["task"]["priority_score"] == 75.0

    # 2. FastAPI REST cập nhật trạng thái sang IN_PROGRESS và priority_score lên 95.0
    with TestClient(api_app) as api_client:
        status_resp = api_client.post(
            "/api/tasks/task-shared-1/status",
            json={"status": "IN_PROGRESS", "actor": "TEST_RUNNER"},
        )
        assert status_resp.status_code == 200
        assert status_resp.json()["success"] is True

        patch_resp = api_client.patch(
            "/api/tasks/task-shared-1",
            json={"priority_score": 95.0, "title": "Nhiệm vụ đã cập nhật qua REST"},
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["priority_score"] == 95.0

    # 3. FastMCP đọc lại qua tool và ngay lập tức thấy cập nhật từ FastAPI
    mcp_res2 = await mcp_srv.call_tool("get_task_context", {"task_id": "task-shared-1"})
    assert mcp_res2.is_error is False
    mcp_data2 = mcp_res2.structured_content if hasattr(mcp_res2, "structured_content") and mcp_res2.structured_content else json.loads(mcp_res2.content[0].text)
    if isinstance(mcp_data2, dict) and "result" in mcp_data2:
        mcp_data2 = mcp_data2["result"]
    assert mcp_data2["task"]["status"] == "IN_PROGRESS"
    assert mcp_data2["task"]["priority_score"] == 95.0
    assert mcp_data2["task"]["title"] == "Nhiệm vụ đã cập nhật qua REST"

    # 4. FastMCP query tool get_tasks lọc theo status IN_PROGRESS
    mcp_tasks_res = await mcp_srv.call_tool("get_tasks", {"filters": {"status": "IN_PROGRESS"}})
    assert mcp_tasks_res.is_error is False
    mcp_tasks = mcp_tasks_res.structured_content if hasattr(mcp_tasks_res, "structured_content") and mcp_tasks_res.structured_content else json.loads(mcp_tasks_res.content[0].text)
    if isinstance(mcp_tasks, dict) and "result" in mcp_tasks:
        mcp_tasks = mcp_tasks["result"]
    assert len(mcp_tasks) == 1
    assert mcp_tasks[0]["id"] == "task-shared-1"

    # Reset
    set_application_service(None)
