"""Unit tests for ApplicationService (Layer 5)."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4
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
    DecisionRecord,
    ForgottenCommitmentItem,
    LessonRecord,
    TaskWithContext,
    TodayBoardView,
    WaitingOnItem,
)
from ptb_contracts.l5_experience import (
    CoverageStatusResponse,
    KnowledgeSearchResponse,
    TaskActionResponse,
)
from ptb_application.service import ApplicationService


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
            raise ValueError(f"None of {evidence_ids_to_detach} found in task")
        orig.evidences = [e for e in orig.evidences if e.id not in evidence_ids_to_detach]
        new_task = UnifiedTaskCandidate(
            id=str(uuid4()),
            title=new_task_title or f"Split: {orig.title}",
            status=orig.status,
            evidences=detached,
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
            if "project_key" in filters and filters["project_key"]:
                if (t.project_key or "").lower() != str(filters["project_key"]).lower():
                    continue

            if "customer" in filters and filters["customer"]:
                if (t.customer_id or "").lower() != str(filters["customer"]).lower():
                    continue
            if "customer_id" in filters and filters["customer_id"]:
                if (t.customer_id or "").lower() != str(filters["customer_id"]).lower():
                    continue

            if "source" in filters and filters["source"]:
                req_source = str(filters["source"]).lower()
                if not any((ev.source_type or "").lower() == req_source for ev in t.evidences):
                    continue
            if "source_type" in filters and filters["source_type"]:
                req_source = str(filters["source_type"]).lower()
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
                elif isinstance(p_filter, (int, float)):
                    if t.priority_score < float(p_filter):
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
                elif isinstance(due_range, (list, tuple)) and len(due_range) == 2:
                    start_dt, end_dt = due_range
                    if start_dt:
                        start_utc = start_dt if start_dt.tzinfo else start_dt.replace(tzinfo=timezone.utc)
                        if t_due < start_utc:
                            continue
                    if end_dt:
                        end_utc = end_dt if end_dt.tzinfo else end_dt.replace(tzinfo=timezone.utc)
                        if t_due > end_utc:
                            continue

            if "has_deadline" in filters and filters["has_deadline"] is not None:
                has_deadline_flag = bool(t.due_date is not None or t.explicit_deadline)
                if has_deadline_flag != bool(filters["has_deadline"]):
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
        has_comp = any(
            ev.evidence_type == EvidenceType.COMPLETION_SIGNAL
            for ev in task.evidences
        )
        return TaskWithContext(
            task=task,
            last_status_change_at=last_change,
            days_in_current_status=days_in_status,
            has_completion_evidence=has_comp,
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
def sample_tasks() -> List[UnifiedTaskCandidate]:
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
    ev3 = EvidenceRecord(
        id="ev-3",
        raw_event_id="raw-3",
        evidence_type=EvidenceType.COMPLETION_SIGNAL,
        source_type="ms_outlook",
        timestamp=now - timedelta(days=1),
        snippet="Đã hoàn thành báo cáo Q3",
        confidence=0.90,
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
        updated_at=now - timedelta(days=4),  # stale (> 3 days)
        evidences=[ev2],
    )

    t3 = UnifiedTaskCandidate(
        id="task-3",
        title="Báo cáo tài chính quý 3",
        status=TaskStatus.DONE,
        priority_score=60.0,
        owner_canonical_id="person-me",
        owner_name="Me",
        project_key="FINANCE",
        customer_id=None,
        due_date=now - timedelta(days=1),
        explicit_deadline=True,
        extraction_confidence=0.90,
        review_status="auto_approved",
        created_at=now - timedelta(days=2),
        updated_at=now - timedelta(hours=2),
        evidences=[ev3],
    )

    return [t1, t2, t3]


@pytest.fixture
def sample_episodes() -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    return [
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
    ]


@pytest.fixture
def app_service(
    sample_tasks: List[UnifiedTaskCandidate],
    sample_episodes: List[Dict[str, Any]],
) -> ApplicationService:
    task_repo = MockTaskDomainRepository(sample_tasks)
    checkpoint_repo = MockCheckpointRepository([
        IngestionCheckpointRecord(
            id="cp-1",
            tenant_id="tenant-ms",
            source_type=SourceType.MS_TEAMS,
            stream_id="stream-1",
            last_external_id="msg-100",
            last_event_timestamp=datetime.now(timezone.utc) - timedelta(minutes=15),
            updated_at=datetime.now(timezone.utc),
        ),
    ])
    graph_memory = MockGraphitiMemoryClient(sample_episodes)
    return ApplicationService(
        task_repo=task_repo,
        checkpoint_repo=checkpoint_repo,
        graph_memory=graph_memory,
    )


# ==============================================================================
# TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_get_today_plan(app_service: ApplicationService):
    """Kiểm tra use-case get_today_plan."""
    plan = await app_service.get_today_plan(user_id="test-user")
    assert isinstance(plan, TodayBoardView)
    assert plan.user_id == "test-user"
    assert len(plan.top_tasks) > 0
    # Task-1 (TODO, priority 85) phải nằm trong top_tasks
    top_ids = [t.task_id for t in plan.top_tasks]
    assert "task-1" in top_ids
    # Task-3 đã DONE nên không nằm trong active top_tasks
    assert "task-3" not in top_ids
    assert plan.summary_headline != ""


@pytest.mark.asyncio
async def test_list_tasks_no_filter(app_service: ApplicationService):
    """Kiểm tra list_tasks không truyền filters trả về toàn bộ tasks."""
    tasks = await app_service.list_tasks()
    assert len(tasks) == 3


@pytest.mark.asyncio
async def test_list_tasks_filters(app_service: ApplicationService):
    """Kiểm tra list_tasks với các bộ lọc phong phú."""
    # 1. Filter theo status
    todo_tasks = await app_service.list_tasks(filters={"status": TaskStatus.TODO})
    assert len(todo_tasks) == 1
    assert todo_tasks[0].id == "task-1"

    # 2. Filter theo status list
    active_tasks = await app_service.list_tasks(filters={"status": ["TODO", "BLOCKED"]})
    assert len(active_tasks) == 2

    # 3. Filter theo project_key
    auth_tasks = await app_service.list_tasks(filters={"project_key": "AUTH"})
    assert len(auth_tasks) == 1
    assert auth_tasks[0].id == "task-1"

    # 4. Filter theo customer_id
    cust_b_tasks = await app_service.list_tasks(filters={"customer_id": "CUST-B"})
    assert len(cust_b_tasks) == 1
    assert cust_b_tasks[0].id == "task-2"

    # 5. Filter theo source
    teams_tasks = await app_service.list_tasks(filters={"source": "ms_teams"})
    assert len(teams_tasks) == 1
    assert teams_tasks[0].id == "task-1"

    # 6. Filter theo owner
    huy_tasks = await app_service.list_tasks(filters={"owner": "Huy Nguyen"})
    assert len(huy_tasks) == 1
    assert huy_tasks[0].id == "task-2"

    # 7. Filter theo priority range
    high_priority = await app_service.list_tasks(filters={"priority": {"min": 80.0, "max": 100.0}})
    assert len(high_priority) == 1
    assert high_priority[0].id == "task-1"

    # 8. Filter theo stale
    stale_tasks = await app_service.list_tasks(filters={"stale": True})
    assert len(stale_tasks) == 1
    assert stale_tasks[0].id == "task-2"

    # 9. Filter theo waiting
    waiting_tasks = await app_service.list_tasks(filters={"waiting": True})
    assert len(waiting_tasks) == 1
    assert waiting_tasks[0].id == "task-2"

    # 10. Filter theo review_status
    pending_tasks = await app_service.list_tasks(filters={"review_status": "pending_review"})
    assert len(pending_tasks) == 1
    assert pending_tasks[0].id == "task-2"

    # 11. Filter limit
    limited = await app_service.list_tasks(filters={"limit": 2})
    assert len(limited) == 2


@pytest.mark.asyncio
async def test_get_task_detail_found(app_service: ApplicationService):
    """Kiểm tra get_task_detail khi task tồn tại."""
    detail = await app_service.get_task_detail("task-2")
    assert detail is not None
    assert isinstance(detail, TaskWithContext)
    assert detail.task.id == "task-2"
    assert "task-blocker-1" in detail.blocking_tasks
    assert "Alex" in detail.dependent_people
    # Kiểm tra related decisions / lessons tích hợp từ Graphiti
    assert len(detail.past_lessons_learned) > 0 or len(detail.related_decisions) > 0


@pytest.mark.asyncio
async def test_get_task_detail_not_found(app_service: ApplicationService):
    """Kiểm tra get_task_detail khi task không tồn tại."""
    detail = await app_service.get_task_detail("task-non-existent")
    assert detail is None


@pytest.mark.asyncio
async def test_get_review_inbox(app_service: ApplicationService):
    """Kiểm tra get_review_inbox lấy các task cần review."""
    inbox = await app_service.get_review_inbox(limit=10)
    assert len(inbox) == 1
    assert inbox[0].candidate_task.id == "task-2"
    assert "Confidence" in inbox[0].reason


@pytest.mark.asyncio
async def test_search_knowledge(app_service: ApplicationService):
    """Kiểm tra search_knowledge lấy decisions và lessons."""
    res = await app_service.search_knowledge(query="AUTH")
    assert isinstance(res, KnowledgeSearchResponse)
    assert len(res.decisions) == 1
    assert res.decisions[0].decision_id == "dec-1"
    assert "Redis" in res.decisions[0].summary
    assert res.synthesis_summary is not None


@pytest.mark.asyncio
async def test_get_sources_health(app_service: ApplicationService):
    """Kiểm tra get_sources_health tổng hợp tenants status."""
    health = await app_service.get_sources_health()
    assert isinstance(health, CoverageStatusResponse)
    assert len(health.tenants) >= 1
    assert health.overall_health == "healthy"


@pytest.mark.asyncio
async def test_execute_task_action_update_status(app_service: ApplicationService):
    """Kiểm tra execute_task_action action UPDATE_STATUS."""
    res = await app_service.execute_task_action(
        task_id="task-1",
        action="UPDATE_STATUS",
        new_status="IN_PROGRESS",
        actor="TEST_USER",
    )
    assert res.success is True
    assert "IN_PROGRESS" in res.message

    updated = await app_service.task_repo.get_task_by_id("task-1")
    assert updated.status == TaskStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_execute_task_action_approve(app_service: ApplicationService):
    """Kiểm tra execute_task_action action APPROVE."""
    res = await app_service.execute_task_action(
        task_id="task-2",
        action="APPROVE",
        actor="ADMIN",
    )
    assert res.success is True
    assert "approved" in res.message.lower()

    updated = await app_service.task_repo.get_task_by_id("task-2")
    assert updated.review_status == "auto_approved"


@pytest.mark.asyncio
async def test_execute_task_action_reject(app_service: ApplicationService):
    """Kiểm tra execute_task_action action REJECT."""
    res = await app_service.execute_task_action(
        task_id="task-2",
        action="REJECT",
        actor="ADMIN",
    )
    assert res.success is True
    assert "rejected" in res.message.lower()

    updated = await app_service.task_repo.get_task_by_id("task-2")
    assert updated.review_status == "rejected"
    assert updated.status == TaskStatus.DISMISSED


@pytest.mark.asyncio
async def test_execute_task_action_mark_done(app_service: ApplicationService):
    """Kiểm tra execute_task_action action MARK_DONE."""
    res = await app_service.execute_task_action(
        task_id="task-1",
        action="MARK_DONE",
        actor="USER",
    )
    assert res.success is True
    assert "DONE" in res.message

    updated = await app_service.task_repo.get_task_by_id("task-1")
    assert updated.status == TaskStatus.DONE


@pytest.mark.asyncio
async def test_execute_task_action_not_found(app_service: ApplicationService):
    """Kiểm tra execute_task_action khi task_id không tồn tại."""
    res = await app_service.execute_task_action(
        task_id="non-existent",
        action="MARK_DONE",
    )
    assert res.success is False
    assert "not found" in res.message.lower()


@pytest.mark.asyncio
async def test_execute_task_action_invalid_action(app_service: ApplicationService):
    """Kiểm tra execute_task_action với action không hợp lệ."""
    res = await app_service.execute_task_action(
        task_id="task-1",
        action="UNKNOWN_ACTION",
    )
    assert res.success is False
    assert "Unsupported action" in res.message


@pytest.mark.asyncio
async def test_application_service_split_task(app_service: ApplicationService):
    """Kiểm tra ApplicationService.split_task tách đúng evidence và cập nhật domain."""
    orig = await app_service.task_repo.get_task_by_id("task-1")
    assert len(orig.evidences) == 1
    assert orig.evidences[0].id == "ev-1"

    new_task = await app_service.split_task(
        task_id="task-1",
        evidence_ids=["ev-1"],
        new_title="Split: Migrated token work",
    )

    assert new_task.title == "Split: Migrated token work"
    assert len(new_task.evidences) == 1
    assert new_task.evidences[0].id == "ev-1"
    assert new_task.evidences[0].raw_event_id == "raw-1"

    # Original task now has 0 evidences
    orig_updated = await app_service.task_repo.get_task_by_id("task-1")
    assert len(orig_updated.evidences) == 0

