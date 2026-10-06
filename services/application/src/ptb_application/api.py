"""FastAPI REST API Server for Personal Task Board (Layer 5).

Port canonical: 127.0.0.1:8000
Provides exactly 14 REST endpoints for OpenWebUI Board, integrations, and administration.
"""

from datetime import datetime, timezone
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from dotenv import load_dotenv

import sys
ROOT_DIR = Path(__file__).resolve().parents[4]
load_dotenv(ROOT_DIR / ".env")
load_dotenv()
sys.path.insert(0, str(ROOT_DIR / "packages"))
sys.path.insert(0, str(ROOT_DIR))

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ptb_application.portal_html import PORTAL_HTML
from ptb_application.service import ApplicationService
from ptb_contracts.l2_processing import (
    ReviewQueueItem,
    TaskStatus,
    UnifiedTaskCandidate,
)
from ptb_contracts.l4_intelligence import (
    ForgottenCommitmentItem,
    TaskWithContext,
    TodayBoardView,
    WaitingOnItem,
)
from ptb_contracts.l5_experience import (
    CoverageStatusResponse,
    KnowledgeSearchResponse,
    TaskActionResponse,
)

logger = logging.getLogger("ptb.application.api")

# ==============================================================================
# Dependency Injection for ApplicationService
# ==============================================================================
_app_service: Optional[ApplicationService] = None


def get_application_service() -> ApplicationService:
    """Dependency injection provider for ApplicationService.

    Ưu tiên trả về service được inject qua set_application_service() hoặc create_app().
    Nếu chưa được thiết lập, khởi tạo instance mặc định.
    """
    global _app_service
    if _app_service is None:
        _app_service = ApplicationService()
    return _app_service


def set_application_service(service: Optional[ApplicationService]) -> None:
    """Setter to override or reset ApplicationService instance (for tests / shared dependency graph)."""
    global _app_service
    _app_service = service


# ==============================================================================
# Canonical Constants & Helpers
# ==============================================================================
CANONICAL_TASK_STATUSES = (
    TaskStatus.TODO.value,
    TaskStatus.IN_PROGRESS.value,
    TaskStatus.BLOCKED.value,
    TaskStatus.DONE.value,
    TaskStatus.DISMISSED.value,
)


def _validate_canonical_status(status_val: str) -> str:
    """Kiểm tra hợp lệ với 5 trạng thái canonical (TODO, IN_PROGRESS, BLOCKED, DONE, DISMISSED)."""
    norm = str(status_val).strip().upper()
    if norm not in CANONICAL_TASK_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status '{status_val}'. Must be one of {list(CANONICAL_TASK_STATUSES)}",
        )
    return norm


# ==============================================================================
# Request Models
# ==============================================================================
class ApproveReviewRequest(BaseModel):
    new_status: Optional[str] = None
    status: Optional[str] = None
    actor: str = "USER"


class DismissReviewRequest(BaseModel):
    actor: str = "USER"


class TaskPatchRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None
    due_date: Optional[Union[datetime, str]] = None
    explicit_deadline: Optional[bool] = None
    priority_score: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    project_key: Optional[str] = None
    customer_id: Optional[str] = None
    owner_canonical_id: Optional[str] = None
    owner_name: Optional[str] = None
    requester_canonical_id: Optional[str] = None
    requester_name: Optional[str] = None
    review_status: Optional[str] = None
    actor: str = "USER"


class TaskStatusChangeRequest(BaseModel):
    status: Optional[str] = None
    new_status: Optional[str] = None
    actor: str = "USER"


class TaskSplitRequest(BaseModel):
    evidence_ids: List[str]
    new_title: Optional[str] = None


class IngestMessageRequest(BaseModel):
    message: str
    sender: Optional[str] = "User"
    source: Optional[str] = "Direct Input"


# ==============================================================================
# Application Factory
# ==============================================================================
def create_app(application_service: Optional[ApplicationService] = None) -> FastAPI:
    """Khởi tạo và cấu hình FastAPI application.

    Hỗ trợ inject shared ApplicationService qua tham số application_service hoặc hàm set_application_service().
    """
    if application_service is not None:
        set_application_service(application_service)

    app = FastAPI(
        title="Personal Task Board REST API",
        description="FastAPI REST service for Personal Task Board v1 (Port 8000)",
        version="1.0.0",
    )
    if application_service is not None:
        app.state.application_service = application_service

    # Cấu hình CORS middleware cho phép OpenWebUI gọi từ http://localhost:3000 và http://127.0.0.1:3000
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --------------------------------------------------------------------------
    # 1. GET /health
    # --------------------------------------------------------------------------
    @app.get("/health")
    async def health_check(
        service: ApplicationService = Depends(get_application_service),
    ) -> Dict[str, Any]:
        """Kiểm tra tình trạng sức khỏe sâu (deep health) của service và các dependencies."""
        return await service.get_system_health()

    # --------------------------------------------------------------------------
    # 2. GET /api/today
    # --------------------------------------------------------------------------
    @app.get("/api/today", response_model=TodayBoardView)
    async def get_today_plan(
        user_id: str = Query(default="default"),
        service: ApplicationService = Depends(get_application_service),
    ) -> TodayBoardView:
        """Trả về TodayBoardView gồm top tasks, waiting on others, forgotten commitments."""
        return await service.get_today_plan(user_id=user_id)

    # --------------------------------------------------------------------------
    # 3. GET /api/tasks
    # --------------------------------------------------------------------------
    @app.get("/api/tasks", response_model=List[UnifiedTaskCandidate])
    async def list_tasks(
        source: Optional[str] = Query(default=None),
        project: Optional[str] = Query(default=None),
        project_key: Optional[str] = Query(default=None),
        customer: Optional[str] = Query(default=None),
        status: Optional[str] = Query(default=None),
        due_from: Optional[datetime] = Query(default=None),
        due_to: Optional[datetime] = Query(default=None),
        priority_min: Optional[float] = Query(default=None),
        priority_max: Optional[float] = Query(default=None),
        owner: Optional[str] = Query(default=None),
        stale: Optional[bool] = Query(default=None),
        waiting: Optional[bool] = Query(default=None),
        review_status: Optional[str] = Query(default=None),
        limit: Optional[int] = Query(default=None, ge=1),
        service: ApplicationService = Depends(get_application_service),
    ) -> List[UnifiedTaskCandidate]:
        """Lọc danh sách tasks theo các tiêu chí query parameters."""
        filters: Dict[str, Any] = {}
        if source is not None:
            filters["source"] = source
        if project is not None or project_key is not None:
            filters["project"] = project if project is not None else project_key
        if customer is not None:
            filters["customer"] = customer
        if status is not None:
            if "," in status:
                filters["status"] = [s.strip() for s in status.split(",") if s.strip()]
            else:
                filters["status"] = status
        if due_from is not None or due_to is not None:
            filters["due_range"] = {"start": due_from, "end": due_to}
        if priority_min is not None or priority_max is not None:
            filters["priority"] = {
                "min": priority_min if priority_min is not None else 0.0,
                "max": priority_max if priority_max is not None else 100.0,
            }
        if owner is not None:
            filters["owner"] = owner
        if stale is not None:
            filters["stale"] = stale
        if waiting is not None:
            filters["waiting"] = waiting
        if review_status is not None:
            filters["review_status"] = review_status
        if limit is not None:
            filters["limit"] = limit

        return await service.list_tasks(filters=filters if filters else None)

    # --------------------------------------------------------------------------
    # 4. GET /api/tasks/{task_id}
    # --------------------------------------------------------------------------
    @app.get("/api/tasks/{task_id}", response_model=TaskWithContext)
    async def get_task_by_id(
        task_id: str,
        service: ApplicationService = Depends(get_application_service),
    ) -> TaskWithContext:
        """Trả về chi tiết TaskWithContext bao gồm blockers, dependents, evidences, related knowledge."""
        task_ctx = await service.get_task_detail(task_id=task_id)
        if task_ctx is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Task '{task_id}' not found",
            )
        return task_ctx

    # --------------------------------------------------------------------------
    # 5. GET /api/review
    # --------------------------------------------------------------------------
    @app.get("/api/review", response_model=List[ReviewQueueItem])
    async def get_review_queue(
        limit: int = Query(default=20, ge=1),
        service: ApplicationService = Depends(get_application_service),
    ) -> List[ReviewQueueItem]:
        """Trả về danh sách ReviewQueueItem cần người dùng phê duyệt."""
        return await service.get_review_inbox(limit=limit)

    # --------------------------------------------------------------------------
    # 6. POST /api/review/{task_id}/approve
    # --------------------------------------------------------------------------
    @app.post("/api/review/{task_id}/approve", response_model=TaskActionResponse)
    async def approve_task_review(
        task_id: str,
        body: Optional[ApproveReviewRequest] = None,
        service: ApplicationService = Depends(get_application_service),
    ) -> TaskActionResponse:
        """Duyệt task candidate trong hàng đợi review sang trạng thái active (mặc định TODO)."""
        raw_status = (body.new_status if body and body.new_status is not None else (body.status if body else None))
        target_status = (
            _validate_canonical_status(raw_status)
            if raw_status is not None
            else TaskStatus.TODO.value
        )
        actor = body.actor if body else "USER"
        res = await service.execute_task_action(
            task_id=task_id,
            action="APPROVE",
            new_status=target_status,
            actor=actor,
        )
        if not res.success:
            if "not found" in res.message.lower():
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=res.message,
                )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=res.message,
            )
        return res

    # --------------------------------------------------------------------------
    # 7. POST /api/review/{task_id}/dismiss
    # --------------------------------------------------------------------------
    @app.post("/api/review/{task_id}/dismiss", response_model=TaskActionResponse)
    async def dismiss_task_review(
        task_id: str,
        body: Optional[DismissReviewRequest] = None,
        service: ApplicationService = Depends(get_application_service),
    ) -> TaskActionResponse:
        """Bỏ qua task candidate trong hàng đợi review (chuyển sang DISMISSED)."""
        actor = body.actor if body else "USER"
        res = await service.execute_task_action(
            task_id=task_id,
            action="DISMISS",
            actor=actor,
        )
        if not res.success:
            if "not found" in res.message.lower():
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=res.message,
                )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=res.message,
            )
        return res

    # --------------------------------------------------------------------------
    # 8. PATCH /api/tasks/{task_id}
    # --------------------------------------------------------------------------
    @app.patch("/api/tasks/{task_id}", response_model=UnifiedTaskCandidate)
    async def patch_task(
        task_id: str,
        body: TaskPatchRequest,
        service: ApplicationService = Depends(get_application_service),
    ) -> UnifiedTaskCandidate:
        """Chỉnh sửa các trường thông tin của task (title, project_key, owner_name, due_date, notes, ...)."""
        updates = body.model_dump(exclude_unset=True)
        actor = updates.pop("actor", "USER")

        if "title" in updates:
            if updates["title"] is None or not str(updates["title"]).strip():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Field 'title' cannot be empty",
                )
            updates["title"] = str(updates["title"]).strip()

        if "status" in updates and updates["status"] is not None:
            updates["status"] = _validate_canonical_status(updates["status"])

        if "due_date" in updates and isinstance(updates["due_date"], str):
            due_str = updates["due_date"].strip()
            if not due_str:
                updates["due_date"] = None
            else:
                try:
                    parsed_due = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
                    if parsed_due.tzinfo is None:
                        parsed_due = parsed_due.replace(tzinfo=timezone.utc)
                    updates["due_date"] = parsed_due
                except ValueError:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Invalid due_date '{updates['due_date']}'. Expected ISO-8601 date or datetime.",
                    )

        try:
            updated = await service.update_task(task_id=task_id, actor=actor, **updates)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )

        if updated is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Task '{task_id}' not found",
            )
        return updated

    # --------------------------------------------------------------------------
    # 9. POST /api/tasks/{task_id}/status
    # --------------------------------------------------------------------------
    @app.post("/api/tasks/{task_id}/status", response_model=TaskActionResponse)
    async def update_task_status(
        task_id: str,
        body: TaskStatusChangeRequest,
        service: ApplicationService = Depends(get_application_service),
    ) -> TaskActionResponse:
        """Cập nhật trạng thái task (TODO, IN_PROGRESS, BLOCKED, DONE, DISMISSED)."""
        if (
            body.status is not None
            and body.new_status is not None
            and body.status.strip().upper() != body.new_status.strip().upper()
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Conflicting 'status' and 'new_status' values in request payload",
            )

        raw_status = body.status if body.status is not None else body.new_status
        if not raw_status or not str(raw_status).strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Field 'status' or 'new_status' is required",
            )

        target_status = _validate_canonical_status(raw_status)

        res = await service.execute_task_action(
            task_id=task_id,
            action="UPDATE_STATUS",
            new_status=target_status,
            actor=body.actor,
        )
        if not res.success:
            if "not found" in res.message.lower():
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=res.message,
                )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=res.message,
            )
        return res

    # --------------------------------------------------------------------------
    # 10. POST /api/tasks/{task_id}/split
    # --------------------------------------------------------------------------
    @app.post("/api/tasks/{task_id}/split", response_model=UnifiedTaskCandidate)
    async def split_task(
        task_id: str,
        body: TaskSplitRequest,
        service: ApplicationService = Depends(get_application_service),
    ) -> UnifiedTaskCandidate:
        """Tách các evidence chỉ định khỏi task thành một UnifiedTask mới."""
        cleaned_evidence_ids = [eid.strip() for eid in (body.evidence_ids or []) if eid and str(eid).strip()]
        if not cleaned_evidence_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="evidence_ids list cannot be empty",
            )

        try:
            return await service.split_task(
                task_id=task_id,
                evidence_ids=cleaned_evidence_ids,
                new_title=body.new_title,
            )
        except ValueError as e:
            msg = str(e)
            msg_lower = msg.lower()
            if "not found" in msg_lower and "evidence" not in msg_lower and "none of" not in msg_lower:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=msg,
                )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=msg,
            )

    # --------------------------------------------------------------------------
    # 11. GET /api/waiting
    # --------------------------------------------------------------------------
    @app.get("/api/waiting", response_model=List[WaitingOnItem])
    async def get_waiting_items(
        user_id: str = Query(default="default"),
        service: ApplicationService = Depends(get_application_service),
    ) -> List[WaitingOnItem]:
        """Trả về danh sách WaitingOnItem đang chờ người khác."""
        plan = await service.get_today_plan(user_id=user_id)
        return plan.waiting_on_others

    # --------------------------------------------------------------------------
    # 12. GET /api/forgotten
    # --------------------------------------------------------------------------
    @app.get("/api/forgotten", response_model=List[ForgottenCommitmentItem])
    async def get_forgotten_commitments(
        user_id: str = Query(default="default"),
        days_stale: int = Query(default=3, ge=0),
        service: ApplicationService = Depends(get_application_service),
    ) -> List[ForgottenCommitmentItem]:
        """Trả về danh sách ForgottenCommitmentItem tồn đọng lâu ngày."""
        plan = await service.get_today_plan(user_id=user_id)
        return [
            item for item in plan.forgotten_commitments
            if item.days_stale >= days_stale
        ]

    # --------------------------------------------------------------------------
    # 13. GET /api/knowledge
    # --------------------------------------------------------------------------
    @app.get("/api/knowledge", response_model=KnowledgeSearchResponse)
    async def search_knowledge(
        query: str = Query(..., min_length=1),
        project_key: Optional[str] = Query(default=None),
        limit: int = Query(default=5, ge=1, le=50),
        service: ApplicationService = Depends(get_application_service),
    ) -> KnowledgeSearchResponse:
        """Tìm kiếm decisions và lessons qua Graphiti knowledge base."""
        query_clean = query.strip()
        if not query_clean:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Query parameter 'query' cannot be empty",
            )
        return await service.search_knowledge(
            query=query_clean,
            project_key=project_key,
            limit=limit,
        )

    # --------------------------------------------------------------------------
    # 14. GET /api/sources/health
    # --------------------------------------------------------------------------
    @app.get("/api/sources/health", response_model=CoverageStatusResponse)
    async def get_sources_health(
        service: ApplicationService = Depends(get_application_service),
    ) -> CoverageStatusResponse:
        """Báo cáo tình trạng sức khỏe kết nối và checkpoints của tất cả các sources."""
        return await service.get_sources_health()

    # --------------------------------------------------------------------------
    # 15. Web Control Center & Portal UI (Root & /portal)
    # --------------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    @app.get("/portal", response_class=HTMLResponse)
    async def get_portal():
        """Phục vụ giao diện Web Control Center để quản trị và nạp dữ liệu cá nhân trực quan."""
        if os.getenv("PTB_DEV_PORTAL", "true").lower() not in ("true", "1", "yes"):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Developer control center portal is disabled. Set PTB_DEV_PORTAL=true to enable.",
            )
        return HTMLResponse(content=PORTAL_HTML)

    # --------------------------------------------------------------------------
    # 16. POST /api/ingest/message (Realtime Personal Message Ingestion)
    # --------------------------------------------------------------------------
    @app.post("/api/ingest/message")
    async def ingest_personal_message_api(
        req: IngestMessageRequest,
        service: ApplicationService = Depends(get_application_service),
    ) -> Dict[str, Any]:
        """Nạp tin nhắn qua canonical pipeline: RawEvent → Processing → Task."""
        import hashlib
        from uuid import uuid4
        from ptb_contracts.l1_acquisition import (
            ProcessingStatus,
            RawEventRecord,
            SourceType,
        )
        from ptb_processing.pipeline import ProcessingPipeline

        raw_msg = (req.message or "").strip()
        if not raw_msg:
            raise HTTPException(status_code=400, detail="Nội dung tin nhắn không được để trống")

        tenant_id = os.getenv("TENANT_ID", "local-user")
        event_id = str(uuid4())
        now = datetime.now(timezone.utc)
        ext_id = f"portal-{hashlib.sha256(raw_msg.encode('utf-8')).hexdigest()[:16]}"
        idempotency_key = hashlib.sha256(
            f"{tenant_id}:coding_agent:{ext_id}:{raw_msg}".encode("utf-8")
        ).hexdigest()

        raw_event = RawEventRecord(
            id=event_id,
            tenant_id=tenant_id,
            source_type=SourceType.CODING_AGENT,
            external_id=ext_id,
            idempotency_key=idempotency_key,
            event_timestamp=now,
            captured_at=now,
            author_external_id=req.sender or "user@portal.local",
            author_display_name=req.sender or "User",
            conversation_or_project_id="portal-web",
            raw_payload={"body": {"content": raw_msg}, "from": {"displayName": req.sender or "User"}},
            normalized_text=raw_msg,
            processing_status=ProcessingStatus.PENDING,
        )

        # 1. Persist RawEvent vào repository chuẩn
        raw_repo = service.raw_event_repo
        if raw_repo:
            if hasattr(raw_repo, "persist_raw_event"):
                await raw_repo.persist_raw_event(raw_event)
            elif hasattr(raw_repo, "upsert"):
                await raw_repo.upsert(raw_event)

        # 2. Xử lý qua ProcessingPipeline canonical (Attribution, Identity, TaskDomainRepo, Lifecycle)
        proc_pipe = getattr(service, "processing_pipeline", None) or getattr(service, "_processing_pipeline", None)
        if proc_pipe is None:
            proc_pipe = ProcessingPipeline(task_repo=service.task_repo, intelligence_lifecycle=service.lifecycle)

        result = await proc_pipe.process(raw_event)

        if result.candidate:
            c = result.candidate
            priority_score = float(getattr(c, "priority_score", 0.0) or 0.0)
            priority_level = (
                "High" if priority_score >= 70 else ("Medium" if priority_score >= 40 else "Low")
            )
            return {
                "status": "success",
                "task": {
                    "id": c.id,
                    "title": c.title,
                    "description": c.description or "",
                    "owner_name": c.owner_name or req.sender or "User",
                    "requester_name": c.requester_name or "Self",
                    "due_date": c.due_date.isoformat() if getattr(c, "due_date", None) else None,
                    "priority_level": priority_level,
                    "urgency_score": round(priority_score, 2),
                    "review_status": getattr(c, "review_status", "auto_approved"),
                    "status": c.status.value if hasattr(c.status, "value") else str(c.status),
                },
            }
        else:
            return {
                "status": "processed",
                "message": "Tin nhắn đã được xử lý nhưng không phát hiện task/cam kết.",
                "raw_event_id": event_id,
            }

    class Layer1Request(BaseModel):
        repo_path: Optional[str] = None
        limit: int = 25

    # --------------------------------------------------------------------------
    # 17. POST /api/ingest/layer1 (Automated Layer 1 Adapter Scan & Store)
    # --------------------------------------------------------------------------
    @app.post("/api/ingest/layer1")
    async def ingest_layer1_api(
        req: Optional[Layer1Request] = None,
        service: ApplicationService = Depends(get_application_service),
    ) -> Dict[str, Any]:
        """Kích hoạt quét Layer 1 (Git & Coding Agents) qua shared ApplicationService."""
        from ptb_acquisition.adapters import AgentWatchersAdapter, GitWatcherAdapter
        from ptb_acquisition.pipeline import AcquisitionPipeline
        from ptb_processing.pipeline import ProcessingPipeline
        from ptb_processing.worker import ProcessingWorker

        raw_repo = service.raw_event_repo
        ckpt_repo = service.checkpoint_repo
        task_repo = service.task_repo
        if raw_repo is None or ckpt_repo is None:
            raise HTTPException(status_code=500, detail="ApplicationService chưa được cấu hình đầy đủ repos")

        pipeline = AcquisitionPipeline(raw_event_repo=raw_repo, checkpoint_repo=ckpt_repo, tenant_id="local-user")

        workspace = os.getenv("PTB_WORKSPACE_ROOT", str(ROOT_DIR))
        repo_paths = [workspace] if os.path.isdir(os.path.join(workspace, ".git")) else []
        if req and req.repo_path and os.path.exists(req.repo_path):
            repo_paths = [req.repo_path]

        adapters = [
            AgentWatchersAdapter(tenant_id="local-user"),
            GitWatcherAdapter(repo_paths=repo_paths, tenant_id="local-user"),
        ]

        total_ingested = 0
        adapter_details = {}
        for adp in adapters:
            adp_name = adp.source_type.value
            try:
                cnt = await pipeline.sync_adapter(adp, stream_id="all")
                total_ingested += cnt
                adapter_details[adp_name] = cnt
            except Exception as e:
                logger.warning("Adapter %s sync error: %s", adp_name, e)
                adapter_details[adp_name] = f"Error: {e}"

        batch_limit = min(req.limit if req and req.limit else 3, 5)
        proc_pipe = getattr(service, "processing_pipeline", None) or getattr(service, "_processing_pipeline", None)
        if proc_pipe is None:
            proc_pipe = ProcessingPipeline(task_repo=task_repo, intelligence_lifecycle=service.lifecycle)

        worker = ProcessingWorker(raw_event_repo=raw_repo, pipeline=proc_pipe, max_retries=2)
        processed_statuses = await worker.process_batch(limit=batch_limit)

        failed_count = sum(1 for s in processed_statuses if str(s) in ("FAILED", "RETRY"))
        status_label = "success" if failed_count == 0 else "partial"

        return {
            "status": status_label,
            "ingested": total_ingested,
            "processed": len(processed_statuses),
            "failed": failed_count,
            "details": adapter_details,
        }

    # --------------------------------------------------------------------------
    # 18. POST /api/database/init (Initialize Constraints & Schema)
    # --------------------------------------------------------------------------
    @app.post("/api/database/init")
    async def init_database_api(
        service: ApplicationService = Depends(get_application_service),
    ) -> Dict[str, Any]:
        """Khởi tạo schema constraints và indexes trên Neo4j (không xóa dữ liệu)."""
        from ptb_database.setup_all import setup_neo4j
        ok = await setup_neo4j()
        return {"status": "success", "constraints_restored": ok}

    # --------------------------------------------------------------------------
    # 19. POST /api/database/reset (Clean Database Reset & Re-init)
    # --------------------------------------------------------------------------
    @app.post("/api/database/reset")
    async def reset_database_api(
        service: ApplicationService = Depends(get_application_service),
    ) -> Dict[str, Any]:
        """Xóa sạch database và nạp lại 19 schema constraints ban đầu (yêu cầu env flag)."""
        if os.getenv("PTB_ENABLE_DESTRUCTIVE_API", "false").lower() not in ("true", "1"):
            raise HTTPException(
                status_code=403,
                detail="Destructive API disabled. Set PTB_ENABLE_DESTRUCTIVE_API=true to enable.",
            )

        if service.neo4j_client and hasattr(service.neo4j_client, "execute_write"):
            await service.neo4j_client.execute_write("MATCH (n) DETACH DELETE n")
        else:
            from ptb_database.neo4j_client import Neo4jClient
            client = Neo4jClient()
            await client.execute_write("MATCH (n) DETACH DELETE n")
            await client.close()

        from ptb_database.setup_all import setup_neo4j
        ok = await setup_neo4j()
        return {"status": "success", "constraints_restored": ok}

    return app


# Instance mặc định chạy ở cổng 8000
app = create_app()


def run_api_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Khởi chạy FastAPI REST Server ở cổng 8000."""
    import uvicorn
    logger.info("Starting PTB FastAPI REST Server on http://%s:%d ...", host, port)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run_api_server()
