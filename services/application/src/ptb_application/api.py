"""FastAPI REST API Server for Personal Task Board (Layer 5).

Port canonical: 127.0.0.1:8000
Provides exactly 14 REST endpoints for OpenWebUI Board, integrations, and administration.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Union

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

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
    """Dependency injection provider for ApplicationService."""
    global _app_service
    if _app_service is None:
        _app_service = ApplicationService()
    return _app_service


def set_application_service(service: Optional[ApplicationService]) -> None:
    """Setter to override or reset ApplicationService instance (for tests / lifecycle)."""
    global _app_service
    _app_service = service


# ==============================================================================
# Request Models
# ==============================================================================
class ApproveReviewRequest(BaseModel):
    new_status: Optional[str] = None
    actor: str = "USER"


class DismissReviewRequest(BaseModel):
    actor: str = "USER"


class TaskPatchRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    due_date: Optional[datetime] = None
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


# ==============================================================================
# Application Factory
# ==============================================================================
def create_app() -> FastAPI:
    """Khởi tạo và cấu hình FastAPI application."""
    app = FastAPI(
        title="Personal Task Board REST API",
        description="FastAPI REST service for Personal Task Board v1 (Port 8000)",
        version="1.0.0",
    )

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
    async def health_check() -> Dict[str, Any]:
        """Kiểm tra tình trạng sức khỏe của service."""
        return {
            "status": "healthy",
            "service": "ptb-application",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

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
        if project is not None:
            filters["project"] = project
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
        """Duyệt task candidate trong hàng đợi review."""
        new_status = body.new_status if body else None
        actor = body.actor if body else "USER"
        res = await service.execute_task_action(
            task_id=task_id,
            action="APPROVE",
            new_status=new_status,
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
        """Bỏ qua task candidate trong hàng đợi review."""
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
        """Chỉnh sửa các trường thông tin của task."""
        if body.status is not None:
            try:
                TaskStatus(body.status)
            except ValueError:
                valid_statuses = [s.value for s in TaskStatus]
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid status '{body.status}'. Must be one of {valid_statuses}",
                )

        updates = body.model_dump(exclude_unset=True)
        actor = updates.pop("actor", "USER")

        updated = await service.update_task(task_id=task_id, actor=actor, **updates)
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
        target_status = body.status or body.new_status
        if not target_status:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Field 'status' or 'new_status' is required",
            )

        try:
            TaskStatus(target_status)
        except ValueError:
            valid_statuses = [s.value for s in TaskStatus]
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status '{target_status}'. Must be one of {valid_statuses}",
            )

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
        if not body.evidence_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="evidence_ids list cannot be empty",
            )

        try:
            return await service.split_task(
                task_id=task_id,
                evidence_ids=body.evidence_ids,
                new_title=body.new_title,
            )
        except ValueError as e:
            msg = str(e)
            if "not found" in msg.lower():
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
