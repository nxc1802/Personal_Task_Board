"""ApplicationService: Central business use-cases for Personal Task Board (Layer 5)."""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Union
from uuid import uuid4

from ptb_contracts.l1_acquisition import IngestionCheckpointRecord
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
    PriorityBreakdown,
    TaskWithContext,
    TodayBoardView,
    TodayTaskItem,
    WaitingOnItem,
)
from ptb_contracts.l5_experience import (
    CoverageStatusResponse,
    CoverageTenantStatus,
    KnowledgeSearchResponse,
    TaskActionResponse,
)
from ptb_database.neo4j_client import Neo4jClient
from ptb_database.repositories import (
    CheckpointRepository,
    RawEventRepository,
    TaskDomainRepository,
)
from ptb_graph_memory.client import GraphitiMemoryClient
from ptb_intelligence.detectors import ForgottenCommitmentDetector, WaitingOnDetector
from ptb_intelligence.planner import TodayBoardPlanner
from ptb_intelligence.priority import DeterministicPriorityEngine
from ptb_intelligence.status_machine import StatusInferenceMachine

logger = logging.getLogger("ptb.application.service")


class ApplicationService:
    """Điểm tập trung toàn bộ use-case nghiệp vụ của Personal Task Board.

    Kết hợp:
    - TaskDomainRepository (lưu trữ và truy vấn UnifiedTask, Evidences)
    - RawEventRepository (lưu trữ và tra cứu RawEvent, ProcessingAttempt)
    - CheckpointRepository (theo dõi con trỏ đồng bộ nguồn)
    - DeterministicPriorityEngine (tính điểm ưu tiên 0-100)
    - TodayBoardPlanner (lập kế hoạch Today Board)
    - GraphitiMemoryClient (bộ nhớ ngữ cảnh tri thức decisions & lessons)
    - StatusInferenceMachine (quản lý chuyển đổi trạng thái & audit)
    """

    def __init__(
        self,
        task_repo: Optional[TaskDomainRepository] = None,
        raw_event_repo: Optional[RawEventRepository] = None,
        checkpoint_repo: Optional[CheckpointRepository] = None,
        priority_engine: Optional[DeterministicPriorityEngine] = None,
        planner: Optional[TodayBoardPlanner] = None,
        graph_memory: Optional[GraphitiMemoryClient] = None,
        status_machine: Optional[StatusInferenceMachine] = None,
        neo4j_client: Optional[Neo4jClient] = None,
    ) -> None:
        client = neo4j_client or Neo4jClient()
        self.task_repo = task_repo or TaskDomainRepository(client)
        self.raw_event_repo = raw_event_repo or RawEventRepository(client)
        self.checkpoint_repo = checkpoint_repo or CheckpointRepository(client)
        self.priority_engine = priority_engine or DeterministicPriorityEngine()
        self.planner = planner or TodayBoardPlanner(priority_engine=self.priority_engine)
        self.graph_memory = graph_memory or GraphitiMemoryClient(neo4j_client=client)
        self.status_machine = status_machine or StatusInferenceMachine()

    async def get_today_plan(self, user_id: str = "default") -> TodayBoardView:
        """Lấy today tasks, waiting on others, forgotten commitments, headline."""
        # 1. Lấy active tasks (TODO, IN_PROGRESS, BLOCKED)
        active_tasks = await self.list_tasks(filters={
            "status": [TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED]
        })

        # 2. Xây dựng TaskWithContext cho từng task
        tasks_with_context: list[TaskWithContext] = []
        now = datetime.now(timezone.utc)
        for t in active_tasks:
            last_change = t.updated_at or t.created_at or now
            if last_change.tzinfo is None:
                last_change = last_change.replace(tzinfo=timezone.utc)
            days_in_status = max(0, int((now - last_change).total_seconds() // 86400))
            has_comp = any(
                ev.evidence_type == EvidenceType.COMPLETION_SIGNAL
                or "done" in (ev.snippet or "").lower()
                for ev in t.evidences
            )
            ctx = TaskWithContext(
                task=t,
                last_status_change_at=last_change,
                days_in_current_status=days_in_status,
                has_completion_evidence=has_comp,
                blocking_tasks=[],
                dependent_people=[],
            )
            tasks_with_context.append(ctx)

        # 3. Lấy commitments
        commitments = await self.task_repo.get_active_commitments(user_id=user_id)

        # 4. Sử dụng TodayBoardPlanner để lập kế hoạch tổng hợp
        board_view = self.planner.plan_today(
            user_id=user_id,
            tasks=tasks_with_context,
            commitments=commitments,
        )
        return board_view

    async def list_tasks(self, filters: Optional[dict] = None) -> list[UnifiedTaskCandidate]:
        """Hỗ trợ lọc theo source, project, customer, status, due_range, priority, owner, stale, waiting, review_status."""
        return await self.task_repo.list_tasks(filters=filters)

    async def get_task_detail(self, task_id: str) -> Optional[TaskWithContext]:
        """Đầy đủ task, blockers, dependents, evidences, related decisions/lessons."""
        task_ctx = await self.task_repo.get_task_with_context(task_id)
        if not task_ctx:
            task = await self.task_repo.get_task_by_id(task_id)
            if not task:
                return None
            now = datetime.now(timezone.utc)
            last_change = task.updated_at or task.created_at or now
            if last_change.tzinfo is None:
                last_change = last_change.replace(tzinfo=timezone.utc)
            days_in_status = max(0, int((now - last_change).total_seconds() // 86400))
            task_ctx = TaskWithContext(
                task=task,
                last_status_change_at=last_change,
                days_in_current_status=days_in_status,
                has_completion_evidence=False,
                blocking_tasks=[],
                dependent_people=[],
            )

        # Trích xuất related decisions và lessons thông qua Graphiti Episodic Memory
        try:
            query = f"{task_ctx.task.title} {task_ctx.task.project_key or ''}".strip()
            if query:
                episodes = await self.graph_memory.search_context(query=query, limit=5)
                for ep in episodes:
                    ep_type = str(ep.get("type", "")).upper()
                    summary = ep.get("summary") or ep.get("topic") or ""
                    if "DECISION" in ep_type and summary:
                        if summary not in task_ctx.related_decisions:
                            task_ctx.related_decisions.append(summary)
                    elif "LESSON" in ep_type and summary:
                        if summary not in task_ctx.past_lessons_learned:
                            task_ctx.past_lessons_learned.append(summary)
        except Exception as ex:
            logger.warning("Error querying related knowledge for task %s: %s", task_id, ex)

        return task_ctx

    async def get_review_inbox(self, limit: int = 20) -> list[ReviewQueueItem]:
        """Danh sách task pending review (confidence 0.40 - 0.64 hoặc pending_review)."""
        return await self.task_repo.get_review_queue(limit=limit)

    async def search_knowledge(
        self,
        query: str,
        project_key: Optional[str] = None,
        limit: int = 5,
    ) -> KnowledgeSearchResponse:
        """Tìm kiếm decisions và lessons qua GraphitiMemoryClient."""
        search_query = query
        if project_key:
            search_query = f"{query} {project_key}".strip()

        episodes = await self.graph_memory.search_context(query=search_query, limit=limit * 2)

        decisions: list[DecisionRecord] = []
        lessons: list[LessonRecord] = []
        now = datetime.now(timezone.utc)

        for ep in episodes:
            ep_type = str(ep.get("type", "")).upper()
            ep_id = ep.get("id", str(uuid4()))
            props = ep.get("properties", {})

            if "DECISION" in ep_type:
                dec_at = props.get("decided_at") or ep.get("valid_at") or now
                if isinstance(dec_at, str):
                    try:
                        dec_at = datetime.fromisoformat(dec_at)
                    except Exception:
                        dec_at = now
                decisions.append(DecisionRecord(
                    decision_id=ep_id,
                    project_key=project_key or props.get("project_key"),
                    summary=ep.get("summary") or props.get("summary", ""),
                    rationale=ep.get("content") or props.get("rationale", ""),
                    decided_by=props.get("decided_by", "SYSTEM"),
                    decided_at=dec_at,
                ))
            elif "LESSON" in ep_type:
                rec_at = props.get("recorded_at") or ep.get("valid_at") or now
                if isinstance(rec_at, str):
                    try:
                        rec_at = datetime.fromisoformat(rec_at)
                    except Exception:
                        rec_at = now
                lessons.append(LessonRecord(
                    lesson_id=ep_id,
                    topic=ep.get("topic") or ep.get("summary") or props.get("topic", ""),
                    description=ep.get("content") or props.get("description", ""),
                    solution=props.get("solution") or ep.get("content", ""),
                    related_incident_id=props.get("related_incident_id"),
                    recorded_at=rec_at,
                ))

        decisions = decisions[:limit]
        lessons = lessons[:limit]

        synthesis_parts = []
        if decisions:
            synthesis_parts.append(f"{len(decisions)} quyết định kiến trúc")
        if lessons:
            synthesis_parts.append(f"{len(lessons)} bài học kinh nghiệm")

        if synthesis_parts:
            summary = f"Tìm thấy {' và '.join(synthesis_parts)} phù hợp với '{query}'."
        else:
            summary = f"Không tìm thấy tri thức phù hợp với '{query}'."

        return KnowledgeSearchResponse(
            decisions=decisions,
            lessons=lessons,
            synthesis_summary=summary,
        )

    async def get_sources_health(self) -> CoverageStatusResponse:
        """Tình trạng các tenant và checkpoints."""
        try:
            checkpoints = await self.checkpoint_repo.list_checkpoints()
        except Exception as ex:
            logger.warning("Could not query checkpoints: %s", ex)
            checkpoints = []
        now = datetime.now(timezone.utc)

        tenants_status: list[CoverageTenantStatus] = []
        has_error = False
        has_warning = False

        if checkpoints:
            for cp in checkpoints:
                st = cp.source_type.value if hasattr(cp.source_type, "value") else str(cp.source_type)
                status = "healthy"
                error_msg = None
                last_sync = cp.last_event_timestamp or cp.updated_at

                if last_sync:
                    last_sync_utc = last_sync if last_sync.tzinfo else last_sync.replace(tzinfo=timezone.utc)
                    age_seconds = (now - last_sync_utc).total_seconds()
                    if age_seconds > (7 * 86400):
                        status = "warning"
                        has_warning = True

                tenants_status.append(CoverageTenantStatus(
                    tenant_id=cp.tenant_id,
                    tenant_name=f"{cp.tenant_id} ({st})",
                    source_type=st,
                    status=status,
                    last_successful_sync=last_sync,
                    items_synced_total=1 if cp.last_external_id else 0,
                    error_message=error_msg,
                ))
        else:
            for src, name in [
                ("ms_teams", "Microsoft Teams"),
                ("ms_outlook", "Microsoft Outlook"),
                ("jira", "Jira Cloud"),
                ("shortcut", "Shortcut Stories"),
            ]:
                tenants_status.append(CoverageTenantStatus(
                    tenant_id=f"tenant-{src}",
                    tenant_name=name,
                    source_type=src,
                    status="healthy",
                    last_successful_sync=now,
                    items_synced_total=0,
                    error_message=None,
                ))

        if has_error:
            overall = "critical"
        elif has_warning:
            overall = "degraded"
        else:
            overall = "healthy"

        return CoverageStatusResponse(
            tenants=tenants_status,
            overall_health=overall,
            last_checked_at=now,
        )

    async def execute_task_action(
        self,
        task_id: str,
        action: str,
        new_status: Optional[str] = None,
        actor: str = "USER",
    ) -> TaskActionResponse:
        """Cập nhật trạng thái task hoặc phê duyệt review task (chỉ gọi nội bộ từ application service hoặc OpenWebUI action)."""
        task = await self.task_repo.get_task_by_id(task_id)
        if not task:
            return TaskActionResponse(
                success=False,
                task_id=task_id,
                message=f"Task {task_id} not found",
            )

        action_norm = (action or "").strip().upper()
        now = datetime.now(timezone.utc)
        old_status = task.status

        if action_norm == "UPDATE_STATUS":
            if not new_status:
                return TaskActionResponse(
                    success=False,
                    task_id=task_id,
                    message="Missing 'new_status' for UPDATE_STATUS action",
                )
            try:
                target_status = TaskStatus(new_status)
            except Exception:
                return TaskActionResponse(
                    success=False,
                    task_id=task_id,
                    message=f"Invalid status value: {new_status}",
                )

            audit = StatusTransitionAuditRecord(
                id=str(uuid4()),
                task_id=task_id,
                old_status=old_status,
                new_status=target_status,
                reason=f"Status updated to {target_status.value} by {actor}",
                confidence=1.0,
                changed_at=now,
                change_actor=actor,
            )
            task.status = target_status
            task.updated_at = now
            await self.task_repo.upsert_task_atomic(task)
            await self.task_repo.record_status_transition_audit(audit)
            return TaskActionResponse(
                success=True,
                task_id=task_id,
                message=f"Successfully updated task status to {target_status.value}",
                updated_at=now,
            )

        elif action_norm == "APPROVE":
            task.review_status = "auto_approved"
            if new_status:
                try:
                    task.status = TaskStatus(new_status)
                except Exception:
                    pass
            task.updated_at = now
            audit = StatusTransitionAuditRecord(
                id=str(uuid4()),
                task_id=task_id,
                old_status=old_status,
                new_status=task.status,
                reason=f"Task review approved by {actor}",
                confidence=1.0,
                changed_at=now,
                change_actor=actor,
            )
            await self.task_repo.upsert_task_atomic(task)
            await self.task_repo.record_status_transition_audit(audit)
            return TaskActionResponse(
                success=True,
                task_id=task_id,
                message="Task candidate approved successfully",
                updated_at=now,
            )

        elif action_norm == "REJECT":
            task.review_status = "rejected"
            task.status = TaskStatus.DISMISSED
            task.updated_at = now
            audit = StatusTransitionAuditRecord(
                id=str(uuid4()),
                task_id=task_id,
                old_status=old_status,
                new_status=TaskStatus.DISMISSED,
                reason=f"Task rejected by {actor}",
                confidence=1.0,
                changed_at=now,
                change_actor=actor,
            )
            await self.task_repo.upsert_task_atomic(task)
            await self.task_repo.record_status_transition_audit(audit)
            return TaskActionResponse(
                success=True,
                task_id=task_id,
                message="Task candidate rejected and dismissed",
                updated_at=now,
            )

        elif action_norm == "DISMISS":
            task.status = TaskStatus.DISMISSED
            task.updated_at = now
            audit = StatusTransitionAuditRecord(
                id=str(uuid4()),
                task_id=task_id,
                old_status=old_status,
                new_status=TaskStatus.DISMISSED,
                reason=f"Task dismissed by {actor}",
                confidence=1.0,
                changed_at=now,
                change_actor=actor,
            )
            await self.task_repo.upsert_task_atomic(task)
            await self.task_repo.record_status_transition_audit(audit)
            return TaskActionResponse(
                success=True,
                task_id=task_id,
                message="Task dismissed successfully",
                updated_at=now,
            )

        elif action_norm == "MARK_DONE":
            task.status = TaskStatus.DONE
            task.updated_at = now
            audit = StatusTransitionAuditRecord(
                id=str(uuid4()),
                task_id=task_id,
                old_status=old_status,
                new_status=TaskStatus.DONE,
                reason=f"Task marked as DONE by {actor}",
                confidence=1.0,
                changed_at=now,
                change_actor=actor,
            )
            await self.task_repo.upsert_task_atomic(task)
            await self.task_repo.record_status_transition_audit(audit)
            return TaskActionResponse(
                success=True,
                task_id=task_id,
                message="Task marked as DONE successfully",
                updated_at=now,
            )

        else:
            return TaskActionResponse(
                success=False,
                task_id=task_id,
                message=f"Unsupported action: {action}",
            )
