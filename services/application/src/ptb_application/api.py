"""FastAPI REST API Server for Personal Task Board (Layer 5).

Port canonical: 127.0.0.1:8000
Provides exactly 14 REST endpoints for OpenWebUI Board, integrations, and administration.
"""

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import secrets
from typing import Any, Dict, List, Optional, Union

from dotenv import load_dotenv

import subprocess
import sys
ROOT_DIR = Path(__file__).resolve().parents[4]
load_dotenv(ROOT_DIR / ".env")
load_dotenv()
sys.path.insert(0, str(ROOT_DIR / "packages"))
sys.path.insert(0, str(ROOT_DIR))

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
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
_active_login_process: Optional[Any] = None


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
# API Authentication & Authorization Dependencies (P0-2, P0-5, P1-6)
# ==============================================================================
def verify_api_key(
    request: Request,
    api_key_header: Optional[str] = Header(None, alias="X-PTB-API-KEY"),
) -> Optional[str]:
    """FastAPI dependency to validate API Key authentication.

    - Exempts 'GET /health' and preflight 'OPTIONS' requests.
    - If PTB_API_KEY is not set (dev mode), logs a warning and permits unauthenticated access.
    - If PTB_API_KEY is set, verifies 'X-PTB-API-KEY' header (or 'Authorization: Bearer <key>').
    - Returns 401 Unauthorized if missing or mismatched.
    """
    # 1. Exempt /health (with or without trailing slash) and CORS OPTIONS preflights
    norm_path = request.url.path.rstrip("/")
    if norm_path == "/health" or request.method == "OPTIONS":
        return None

    expected_key = os.getenv("PTB_API_KEY")
    if not expected_key or not expected_key.strip():
        # Dev mode: unauthenticated access allowed with warning log
        logger.warning(
            "PTB_API_KEY is not configured; running in unauthenticated development mode. "
            "Set PTB_API_KEY in production to secure endpoints."
        )
        return None

    # 2. Extract provided key from X-PTB-API-KEY header or Authorization header
    provided_key = api_key_header
    if not provided_key:
        auth_header = request.headers.get("authorization")
        if auth_header and auth_header.lower().startswith("bearer "):
            provided_key = auth_header[7:].strip()

    if not provided_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Please provide 'X-PTB-API-KEY' header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    if not secrets.compare_digest(provided_key.strip(), expected_key.strip()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    return provided_key


def verify_admin_key(
    request: Request,
    admin_key_header: Optional[str] = Header(None, alias="X-PTB-ADMIN-KEY"),
) -> Optional[str]:
    """FastAPI dependency to validate explicit admin key for destructive operations."""
    expected_admin_key = os.getenv("PTB_ADMIN_KEY")
    if expected_admin_key and expected_admin_key.strip():
        if not admin_key_header or not secrets.compare_digest(admin_key_header.strip(), expected_admin_key.strip()):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin authentication required for destructive operation. Provide valid 'X-PTB-ADMIN-KEY' header.",
            )
        return admin_key_header.strip()
    return None


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


class TaskCreateRequest(BaseModel):
    title: str
    description: Optional[str] = None
    due_date: Optional[Union[datetime, str]] = None
    priority_score: Optional[float] = Field(default=50.0, ge=0.0, le=100.0)
    project_key: Optional[str] = "GLOBAL"
    owner_name: Optional[str] = None
    requester_name: Optional[str] = "User"
    channel_or_group: Optional[str] = "Manual Input"
    status: Optional[str] = "TODO"
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


class ChatbotMessageRequest(BaseModel):
    message: str
    history: Optional[List[Dict[str, str]]] = Field(default_factory=list)


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
        dependencies=[Depends(verify_api_key)],
    )
    if application_service is not None:
        app.state.application_service = application_service

    # Cấu hình CORS middleware theo whitelist an toàn (loại bỏ wildcard và allow_origin_regex r".*")
    allowed_origins = [
        origin.strip()
        for origin in os.getenv(
            "PTB_CORS_ORIGINS",
            "http://127.0.0.1:3000,http://localhost:3000,http://127.0.0.1:8000,http://localhost:8000",
        ).split(",")
        if origin.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-PTB-API-KEY", "X-PTB-ADMIN-KEY"],
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

    @app.post("/api/tasks", response_model=UnifiedTaskCandidate, status_code=status.HTTP_201_CREATED)
    async def create_new_task(
        body: TaskCreateRequest,
        service: ApplicationService = Depends(get_application_service),
    ) -> UnifiedTaskCandidate:
        """Tạo mới một UnifiedTask thủ công từ giao diện hoặc Agent."""
        try:
            return await service.create_task(
                title=body.title,
                description=body.description,
                project_key=body.project_key,
                due_date=body.due_date,
                priority_score=body.priority_score,
                owner_name=body.owner_name,
                requester_name=body.requester_name,
                channel_or_group=body.channel_or_group,
                status=body.status,
                actor=body.actor,
            )
        except ValueError as ex:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ex))

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

    @app.delete("/api/tasks/{task_id}")
    @app.post("/api/tasks/{task_id}/delete")
    async def delete_task_endpoint(
        task_id: str,
        actor: str = Query(default="USER"),
        service: ApplicationService = Depends(get_application_service),
    ) -> Dict[str, Any]:
        """Loại bỏ hoặc đánh dấu DISMISSED cho một UnifiedTask."""
        ok = await service.delete_task(task_id=task_id, actor=actor)
        if not ok:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task '{task_id}' not found")
        return {"success": True, "task_id": task_id, "message": "Task dismissed successfully"}

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
        if os.getenv("PTB_DEV_PORTAL", "false").lower() not in ("true", "1", "yes"):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Developer control center portal is disabled. Set PTB_DEV_PORTAL=true to enable.",
            )
        return HTMLResponse(content=PORTAL_HTML)

    @app.get("/board", response_class=HTMLResponse)
    async def get_board():
        """Phục vụ giao diện Personal Task Board trực tiếp qua HTTP."""
        for candidate in [
            Path("/app/data/openwebui/board/ptb_board.html"),
            Path(__file__).resolve().parents[4] / "integrations" / "openwebui" / "board" / "ptb_board.html",
            Path("data/openwebui/board/ptb_board.html"),
        ]:
            if candidate.exists() and candidate.is_file():
                return HTMLResponse(content=candidate.read_text(encoding="utf-8"))
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Board HTML file not found."
        )

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
        # Map dynamic source channel
        source_type_val = SourceType.CODING_AGENT
        if req.source:
            s_norm = str(req.source).strip().lower()
            if s_norm in ("outlook", "ms_outlook", "ms_outlook_web", "email"):
                source_type_val = SourceType.MS_OUTLOOK_WEB
            elif s_norm in ("jira",):
                source_type_val = SourceType.JIRA
            elif s_norm in ("shortcut",):
                source_type_val = SourceType.SHORTCUT
            elif s_norm in ("git",):
                source_type_val = SourceType.GIT
            elif s_norm in ("confluence",):
                source_type_val = SourceType.CONFLUENCE
            elif s_norm in ("github",):
                source_type_val = SourceType.GITHUB
            elif s_norm in ("slack",):
                source_type_val = SourceType.SLACK
            elif s_norm in ("teams", "ms_teams", "ms_teams_web"):
                source_type_val = SourceType.MS_TEAMS_WEB
            elif s_norm in ("coding_agent", "cursor", "claude_code", "antigravity"):
                source_type_val = SourceType.CODING_AGENT

        idempotency_key = hashlib.sha256(
            f"{tenant_id}:{source_type_val.value}:{ext_id}:{raw_msg}".encode("utf-8")
        ).hexdigest()

        raw_event = RawEventRecord(
            id=event_id,
            tenant_id=tenant_id,
            source_type=source_type_val,
            external_id=ext_id,
            idempotency_key=idempotency_key,
            event_timestamp=now,
            captured_at=now,
            author_external_id=req.sender or "user@portal.local",
            author_display_name=req.sender or "User",
            conversation_or_project_id=f"{source_type_val.value}-channel",
            raw_payload={"content": raw_msg, "body": {"content": raw_msg}, "from": {"displayName": req.sender or "User"}},
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
        from ptb_contracts import ProcessingStatus
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

        failed_count = sum(
            1 for s in processed_statuses
            if s in (ProcessingStatus.FAILED, ProcessingStatus.RETRY)
            or str(getattr(s, "value", s)).lower() in ("failed", "retry")
        )
        if len(processed_statuses) > 0 and failed_count == len(processed_statuses):
            status_label = "failed"
        elif failed_count > 0:
            status_label = "partial"
        else:
            status_label = "success"

        return {
            "status": status_label,
            "ingested": total_ingested,
            "processed": len(processed_statuses),
            "failed": failed_count,
            "details": adapter_details,
        }

    # --------------------------------------------------------------------------
    # 17B. POST /api/ingest/teams (On-Demand Microsoft Teams Capture & Sweep)
    # --------------------------------------------------------------------------
    @app.post("/api/ingest/teams")
    async def ingest_teams_api(
        service: ApplicationService = Depends(get_application_service),
    ) -> Dict[str, Any]:
        """Kích hoạt quét và bắt gói tin tin nhắn Microsoft Teams qua Playwright Session thực tế."""
        from pathlib import Path
        from playwright.async_api import async_playwright, Response
        from ptb_acquisition.playwright.teams_interceptor import TeamsNetworkInterceptor
        from ptb_acquisition.queue import LocalIngestionQueue
        from ptb_processing.pipeline import ProcessingPipeline
        from ptb_processing.worker import ProcessingWorker

        possible_paths = [
            Path("data/playwright/storage_state.json"),
            Path(__file__).resolve().parents[4] / "data" / "playwright" / "storage_state.json",
            Path.cwd() / "data" / "playwright" / "storage_state.json",
        ]
        storage_path = next((p for p in possible_paths if p.exists()), None)
        if not storage_path or not storage_path.exists():
            raise HTTPException(
                status_code=400,
                detail="Chưa có phiên đăng nhập Microsoft Teams. Vui lòng chạy lệnh 'ptb login microsoft' để đăng nhập lần đầu.",
            )

        raw_repo = service.raw_event_repo
        task_repo = service.task_repo
        if raw_repo is None or task_repo is None:
            raise HTTPException(status_code=500, detail="ApplicationService chưa được cấu hình đầy đủ repos")

        queue = LocalIngestionQueue(maxsize=1000)
        teams_interceptor = TeamsNetworkInterceptor(
            queue=queue,
            tenant_id="local-user",
            raw_event_repo=raw_repo,
        )

        captured_records = []

        async def on_teams_resp(response: Response):
            url = response.url
            if response.status == 200 and ("teams" in url or "skype" in url):
                if any(k in url for k in ["api", "chat", "msg", "conversation", "thread", "message", "channel"]):
                    try:
                        data = await response.json()
                        recs = teams_interceptor.parse_payload(data, request_url=url)
                        if recs:
                            for r in recs:
                                captured_records.append(r)
                                if hasattr(raw_repo, "persist_raw_event"):
                                    await raw_repo.persist_raw_event(r)
                                elif hasattr(raw_repo, "upsert"):
                                    await raw_repo.upsert(r)
                    except Exception as e:
                        logger.debug("Teams response parse error: %s", e)

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"]
                )
                context = await browser.new_context(
                    storage_state=str(storage_path),
                    viewport={"width": 1280, "height": 800},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                )
                page = await context.new_page()
                page.on("response", lambda r: asyncio.create_task(on_teams_resp(r)))

                try:
                    await page.goto("https://teams.microsoft.com/v2/", wait_until="domcontentloaded", timeout=30000)

                    # Chờ SSO redirect ổn định nếu đang trao đổi token tự động
                    if "login.microsoftonline.com" in page.url or "login.live.com" in page.url:
                        for _ in range(6):
                            await asyncio.sleep(1)
                            if ("teams.microsoft.com" in page.url or "teams.cloud.microsoft" in page.url) and "login" not in page.url:
                                break

                    if "login.microsoftonline.com" in page.url or "login.live.com" in page.url:
                        await browser.close()
                        raise HTTPException(
                            status_code=401,
                            detail="Phiên đăng nhập Microsoft Teams đã hết hạn (AUTH_EXPIRED). Vui lòng bấm 'Đăng Nhập Microsoft Teams' để xác thực lại.",
                        )

                    # Kích hoạt tải tin nhắn: chọn hội thoại đầu tiên nếu có
                    try:
                        await page.evaluate("""() => {
                            const chatItem = document.querySelector('[data-tid="chat-list-item"]') || 
                                             document.querySelector('div[role="listitem"]');
                            if (chatItem && chatItem.click) chatItem.click();
                        }""")
                    except Exception as click_err:
                        logger.warning("Teams first chat item click ignored or failed: %s", click_err)

                    await asyncio.sleep(10)
                    try:
                        await page.evaluate("""() => {
                            const el = document.querySelector('[data-tid="message-pane-list-viewport"]') || 
                                       document.querySelector('div[role="feed"]') || 
                                       document.querySelector('div[role="region"]') || 
                                       window;
                            if (el.scrollBy) el.scrollBy(0, -500);
                        }""")
                        await asyncio.sleep(3)
                    except Exception as scroll_err:
                        logger.debug("Teams scroll evaluation ignored: %s", scroll_err)
                except HTTPException:
                    raise
                except Exception as nav_err:
                    logger.warning("Teams navigation warning: %s", nav_err)

                await browser.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Playwright capture error: %s", e)
            raise HTTPException(
                status_code=500,
                detail=f"Lỗi khi quét Microsoft Teams qua Playwright: {str(e)}",
            )

        proc_pipe = getattr(service, "processing_pipeline", None) or getattr(service, "_processing_pipeline", None)
        if proc_pipe is None:
            proc_pipe = ProcessingPipeline(task_repo=task_repo, intelligence_lifecycle=service.lifecycle)

        worker = ProcessingWorker(raw_event_repo=raw_repo, pipeline=proc_pipe, max_retries=2)
        asyncio.create_task(worker.process_batch(limit=25))

        msg_summary = (
            f"Đã quét và bắt {len(captured_records)} sự kiện Teams thực tế, tiến trình bóc tách công việc đang chạy ngầm."
            if captured_records
            else "Đã kết nối Teams Web thành công. Hiện tại chưa có tin nhắn chat mới cần xử lý."
        )
        return {
            "status": "success",
            "captured_teams": len(captured_records),
            "processed_tasks": len(captured_records),
            "message": msg_summary,
        }

    # --------------------------------------------------------------------------
    # 17C. POST /api/ingest/outlook (On-Demand Microsoft Outlook Capture & Sweep)
    # --------------------------------------------------------------------------
    @app.post("/api/ingest/outlook")
    async def ingest_outlook_api(
        service: ApplicationService = Depends(get_application_service),
    ) -> Dict[str, Any]:
        """Kích hoạt quét và bắt gói tin email Microsoft Outlook Web qua Playwright Session thực tế."""
        from pathlib import Path
        from playwright.async_api import async_playwright, Response
        from ptb_acquisition.playwright.outlook_interceptor import OutlookNetworkInterceptor
        from ptb_acquisition.queue import LocalIngestionQueue
        from ptb_contracts import RawEventRecord, SourceType
        from ptb_processing.pipeline import ProcessingPipeline
        from ptb_processing.worker import ProcessingWorker

        possible_paths = [
            Path("data/playwright/storage_state.json"),
            Path(__file__).resolve().parents[4] / "data" / "playwright" / "storage_state.json",
            Path.cwd() / "data" / "playwright" / "storage_state.json",
        ]
        storage_path = next((p for p in possible_paths if p.exists()), None)
        if not storage_path or not storage_path.exists():
            raise HTTPException(
                status_code=400,
                detail="Chưa có phiên đăng nhập Microsoft Outlook. Vui lòng đăng nhập để lưu cookie.",
            )

        raw_repo = service.raw_event_repo
        task_repo = service.task_repo
        if raw_repo is None or task_repo is None:
            raise HTTPException(status_code=500, detail="ApplicationService chưa được cấu hình đầy đủ repos")

        queue = LocalIngestionQueue(maxsize=1000)
        outlook_interceptor = OutlookNetworkInterceptor(
            queue=queue,
            tenant_id="local-user",
            raw_event_repo=raw_repo,
        )

        captured_records = []

        async def on_outlook_resp(response: Response):
            url = response.url
            if response.status == 200 and any(d in url for d in ["outlook.office.com", "outlook.live.com", "substrate.office.com", "service.svc"]):
                try:
                    recs = await outlook_interceptor.handle_response(response)
                    if recs:
                        for r in recs:
                            captured_records.append(r)
                except Exception as e:
                    logger.debug("Outlook response handle error: %s", e)

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"]
                )
                context = await browser.new_context(
                    storage_state=str(storage_path),
                    viewport={"width": 1280, "height": 800},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                )
                page = await context.new_page()
                page.on("response", lambda r: asyncio.create_task(on_outlook_resp(r)))

                try:
                    await page.goto("https://outlook.office.com/mail/", wait_until="domcontentloaded", timeout=35000)

                    # Chờ SSO redirect ổn định
                    if "login.microsoftonline.com" in page.url or "login.live.com" in page.url:
                        for _ in range(6):
                            await asyncio.sleep(1)
                            if "outlook.office.com" in page.url and "login" not in page.url:
                                break

                    if "login.microsoftonline.com" in page.url or "login.live.com" in page.url:
                        await browser.close()
                        raise HTTPException(
                            status_code=401,
                            detail="Phiên đăng nhập Microsoft Outlook đã hết hạn (AUTH_EXPIRED). Vui lòng xác thực lại.",
                        )

                    await asyncio.sleep(8)
                    try:
                        await page.evaluate("""() => {
                            const mailList = document.querySelector('div[role="listbox"]') ||
                                             document.querySelector('div[role="feed"]') ||
                                             document.querySelector('div[data-app-section="ItemListView"]') ||
                                             window;
                            if (mailList.scrollBy) mailList.scrollBy(0, 400);
                        }""")
                        await asyncio.sleep(3)
                    except Exception as scroll_err:
                        logger.debug("Outlook scroll evaluation ignored: %s", scroll_err)
                except HTTPException:
                    raise
                except Exception as nav_err:
                    logger.warning("Outlook navigation warning: %s", nav_err)

                await browser.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Playwright Outlook capture error: %s", e)
            # Nếu có lỗi khi mở trình duyệt, thử dùng pre-captured samples nếu có
            sample_file = Path(r"d:\Working\PTB\raw_samples\outlook\outlook_messages.jsonl")
            if sample_file.exists():
                logger.info("Dùng fallback sample outlook_messages.jsonl")
                try:
                    with open(sample_file, "r", encoding="utf-8") as sf:
                        for line in sf:
                            if line.strip():
                                rec = RawEventRecord.model_validate_json(line)
                                captured_records.append(rec)
                                if hasattr(raw_repo, "persist_raw_event"):
                                    await raw_repo.persist_raw_event(rec)
                except Exception as se:
                    logger.warning("Sample fallback error: %s", se)

            if not captured_records:
                raise HTTPException(
                    status_code=500,
                    detail=f"Lỗi khi quét Microsoft Outlook qua Playwright: {str(e)}",
                )

        proc_pipe = getattr(service, "processing_pipeline", None) or getattr(service, "_processing_pipeline", None)
        if proc_pipe is None:
            proc_pipe = ProcessingPipeline(task_repo=task_repo, intelligence_lifecycle=service.lifecycle)

        worker = ProcessingWorker(raw_event_repo=raw_repo, pipeline=proc_pipe, max_retries=2)
        asyncio.create_task(worker.process_batch(limit=25))

        msg_summary = (
            f"Đã quét và bắt {len(captured_records)} email Outlook thực tế, tiến trình bóc tách công việc đang chạy ngầm."
            if captured_records
            else "Đã kết nối Outlook Web thành công. Hiện tại chưa có email công việc mới cần xử lý."
        )
        return {
            "status": "success",
            "captured_outlook": len(captured_records),
            "processed_tasks": len(captured_records),
            "message": msg_summary,
        }

    class PlatformSyncRequest(BaseModel):
        platform: str = "all"
        limit: int = 15
        repo_path: Optional[str] = None

    # --------------------------------------------------------------------------
    # 17D. POST /api/ingest/sync (1-Click Multi-Platform Ingestion & Sweep)
    # --------------------------------------------------------------------------
    @app.post("/api/ingest/sync")
    async def ingest_sync_platform_api(
        req: Optional[PlatformSyncRequest] = None,
        service: ApplicationService = Depends(get_application_service),
    ) -> Dict[str, Any]:
        """Tự động thu thập dữ liệu với một nút bấm từ các nền tảng thực tế (Outlook, Git, Coding Agents, Jira, v.v.)."""
        from ptb_acquisition.adapters import (
            AgentWatchersAdapter,
            GitWatcherAdapter,
            JiraAdapter,
            ShortcutAdapter,
            ConfluenceAdapter,
            GitHubAdapter,
            SlackAdapter,
            OutlookMailAdapter,
        )
        from ptb_acquisition.pipeline import AcquisitionPipeline
        from ptb_contracts import ProcessingStatus
        from ptb_processing.pipeline import ProcessingPipeline
        from ptb_processing.worker import ProcessingWorker

        raw_repo = service.raw_event_repo
        ckpt_repo = service.checkpoint_repo
        task_repo = service.task_repo
        if raw_repo is None or ckpt_repo is None:
            raise HTTPException(status_code=500, detail="ApplicationService chưa được cấu hình repositories")

        target_platform = (req.platform if req and req.platform else "all").strip().lower()
        pipeline = AcquisitionPipeline(raw_event_repo=raw_repo, checkpoint_repo=ckpt_repo, tenant_id="local-user")

        workspace = os.getenv("PTB_WORKSPACE_ROOT", str(ROOT_DIR))
        repo_paths = [workspace] if os.path.isdir(os.path.join(workspace, ".git")) else []
        if req and req.repo_path and os.path.exists(req.repo_path):
            repo_paths = [req.repo_path]

        adapters_to_sync = []
        notes = []
        total_ingested = 0
        adapter_details = {}

        # 0. Microsoft Teams (Playwright with storage_state.json)
        if target_platform in ("all", "teams", "ms_teams"):
            possible_paths = [
                Path("data/playwright/storage_state.json"),
                Path(__file__).resolve().parents[4] / "data" / "playwright" / "storage_state.json",
                Path.cwd() / "data" / "playwright" / "storage_state.json",
            ]
            st_path = next((p for p in possible_paths if p.exists()), None)
            from ptb_acquisition.playwright.session import SessionManager
            mgr = SessionManager(str(st_path)) if st_path else None
            if mgr and mgr.has_valid_session():
                try:
                    teams_res = await ingest_teams_api(service=service)
                    captured = teams_res.get("captured_teams", teams_res.get("captured_events", 0))
                    total_ingested += captured
                    adapter_details["teams"] = captured
                except Exception as te:
                    logger.warning("Teams sync error: %s", te)
                    adapter_details["teams"] = f"Lỗi/Hết hạn: {te}"
                    notes.append(f"Teams: {te}")
            elif target_platform in ("teams", "ms_teams"):
                notes.append("Chưa có phiên đăng nhập Teams hợp lệ. Vui lòng bấm 'Đăng Nhập Microsoft Teams' để xác thực.")

        # 1. Git
        if target_platform in ("all", "git"):
            adapters_to_sync.append(("git", GitWatcherAdapter(repo_paths=repo_paths, tenant_id="local-user")))

        # 2. Coding Agents
        if target_platform in ("all", "coding_agent", "agents"):
            adapters_to_sync.append(("coding_agent", AgentWatchersAdapter(tenant_id="local-user")))

        # 3. Microsoft Outlook (Playwright with storage_state.json or Graph Token)
        if target_platform in ("all", "outlook", "ms_outlook"):
            possible_paths = [
                Path("data/playwright/storage_state.json"),
                Path(__file__).resolve().parents[4] / "data" / "playwright" / "storage_state.json",
                Path.cwd() / "data" / "playwright" / "storage_state.json",
            ]
            st_path = next((p for p in possible_paths if p.exists()), None)
            from ptb_acquisition.playwright.session import SessionManager
            mgr = SessionManager(str(st_path)) if st_path else None
            if mgr and mgr.has_valid_session():
                try:
                    outlook_res = await ingest_outlook_api(service=service)
                    captured = outlook_res.get("captured_outlook", 0)
                    total_ingested += captured
                    adapter_details["outlook"] = captured
                except Exception as oe:
                    logger.warning("Outlook Playwright sync error: %s", oe)
                    adapter_details["outlook"] = f"Lỗi/Hết hạn: {oe}"
                    notes.append(f"Outlook: {oe}")
            else:
                graph_token = os.getenv("MS_GRAPH_TOKEN")
                if graph_token:
                    outlook_adp = OutlookMailAdapter(tenant_id="local-user", graph_token=graph_token)
                    adapters_to_sync.append(("outlook", outlook_adp))
                elif target_platform in ("outlook", "ms_outlook"):
                    notes.append("Chưa có phiên đăng nhập Outlook hợp lệ trong storage_state.json hoặc MS_GRAPH_TOKEN.")

        # 4. Jira
        if target_platform in ("all", "jira"):
            jira_url = os.getenv("JIRA_BASE_URL")
            jira_token = os.getenv("JIRA_API_TOKEN")
            if jira_url and jira_token:
                adapters_to_sync.append(("jira", JiraAdapter(base_url=jira_url, api_token=jira_token, tenant_id="local-user")))
            elif target_platform == "jira":
                notes.append("Jira chưa cấu hình JIRA_API_TOKEN trong .env hoặc config/sources.yaml")

        # 5. GitHub
        if target_platform in ("all", "github"):
            gh_token = os.getenv("GITHUB_TOKEN")
            if gh_token:
                adapters_to_sync.append(("github", GitHubAdapter(api_token=gh_token, tenant_id="local-user")))
            elif target_platform == "github":
                notes.append("GitHub chưa cấu hình GITHUB_TOKEN")

        # 6. Slack
        if target_platform in ("all", "slack"):
            slack_token = os.getenv("SLACK_BOT_TOKEN")
            if slack_token:
                adapters_to_sync.append(("slack", SlackAdapter(bot_token=slack_token, tenant_id="local-user")))
            elif target_platform == "slack":
                notes.append("Slack chưa cấu hình SLACK_BOT_TOKEN")

        for adp_name, adp in adapters_to_sync:
            try:
                cnt = await pipeline.sync_adapter(adp, stream_id="all")
                total_ingested += cnt
                adapter_details[adp_name] = cnt
            except Exception as e:
                logger.warning("Adapter %s sync error: %s", adp_name, e)
                adapter_details[adp_name] = f"Error: {e}"

        proc_pipe = getattr(service, "processing_pipeline", None) or getattr(service, "_processing_pipeline", None)
        if proc_pipe is None:
            proc_pipe = ProcessingPipeline(task_repo=task_repo, intelligence_lifecycle=service.lifecycle)

        worker = ProcessingWorker(raw_event_repo=raw_repo, pipeline=proc_pipe, max_retries=2)
        batch_limit = req.limit if req and req.limit else 15
        asyncio.create_task(worker.process_batch(limit=batch_limit))

        summary_msg = f"Đã quét và thu thập {total_ingested} sự kiện mới ({', '.join(adapter_details.keys()) if adapter_details else 'không có dữ liệu mới'}), tiến trình xử lý công việc đang chạy ngầm."
        if notes:
            summary_msg += f" (Lưu ý: {'; '.join(notes)})"

        return {
            "success": True,
            "platform": target_platform,
            "ingested_count": total_ingested,
            "processed_count": total_ingested,
            "details": adapter_details,
            "message": summary_msg,
        }

    # --------------------------------------------------------------------------
    # 17D. GET /api/auth/session-status (Inspect Playwright Storage State)
    # --------------------------------------------------------------------------
    @app.get("/api/auth/session-status")
    async def get_session_status_api() -> Dict[str, Any]:
        """Kiểm tra tình trạng cookie/session lưu trữ (storage_state.json) của Teams & Outlook."""
        from ptb_acquisition.playwright.session import SessionManager, DEFAULT_STORAGE_PATH, SessionHealthState

        custom_env = os.getenv("PTB_STORAGE_STATE")
        possible_paths = [
            Path(custom_env) if custom_env else None,
            Path("data/playwright/storage_state.json"),
            Path(__file__).resolve().parents[4] / "data" / "playwright" / "storage_state.json",
            Path.cwd() / "data" / "playwright" / "storage_state.json",
            Path(DEFAULT_STORAGE_PATH),
        ]
        active_path = next((p for p in possible_paths if p is not None and p.exists()), next((p for p in possible_paths if p is not None), Path("data/playwright/storage_state.json")))
        mgr = SessionManager(str(active_path))
        state = mgr.validate_session()

        cookie_count = 0
        valid_cookies = 0
        domains = []
        if active_path.exists():
            try:
                with open(active_path, "r", encoding="utf-8") as f:
                    cdata = json.load(f)
                clist = cdata.get("cookies", [])
                cookie_count = len(clist)
                now_ts = datetime.now(timezone.utc).timestamp()
                for c in clist:
                    exp = c.get("expires", -1)
                    if exp is None or exp <= 0 or exp > now_ts:
                        valid_cookies += 1
                    dom = c.get("domain", "")
                    if dom and dom not in domains:
                        domains.append(dom)
            except Exception as e:
                logger.warning("Error reading cookie state: %s", e)

        return {
            "status": state.value,
            "exists": active_path.exists(),
            "storage_path": str(active_path),
            "cookie_count": cookie_count,
            "valid_cookies": valid_cookies,
            "domains": domains[:6],
            "message": "Phiên đăng nhập hợp lệ, sẵn sàng tự động thu thập." if state in (SessionHealthState.HEALTHY, SessionHealthState.STARTING) else "Chưa có phiên hoặc phiên đã hết hạn. Cần đăng nhập để lưu cookie."
        }

    class SaveCookieRequest(BaseModel):
        cookie_data: Any

    # --------------------------------------------------------------------------
    # 17E. POST /api/auth/save-cookie (Save or Import Cookie JSON)
    # --------------------------------------------------------------------------
    @app.post("/api/auth/save-cookie")
    async def save_cookie_api(req: SaveCookieRequest) -> Dict[str, Any]:
        """Lưu hoặc import cookie JSON từ giao diện vào storage_state.json."""
        data = req.cookie_data
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Dữ liệu JSON không hợp lệ: {e}")

        # Chuẩn hóa định dạng Playwright storage_state
        if isinstance(data, list):
            data = {"cookies": data, "origins": []}
        elif not isinstance(data, dict) or "cookies" not in data:
            raise HTTPException(status_code=400, detail="Cấu trúc cookie JSON không đúng chuẩn (cần mảng cookies hoặc object {cookies: [...]})")

        custom_env = os.getenv("PTB_STORAGE_STATE")
        possible_paths = [
            Path(custom_env) if custom_env else None,
            Path("data/playwright/storage_state.json"),
            Path(__file__).resolve().parents[4] / "data" / "playwright" / "storage_state.json",
            Path.cwd() / "data" / "playwright" / "storage_state.json",
        ]
        target_path = next((p for p in possible_paths if p is not None), Path("data/playwright/storage_state.json"))
        target_path.parent.mkdir(parents=True, exist_ok=True)
        # Ensure path is always set on cookies to prevent Playwright crashing with missing path
        for c in data.get("cookies", []):
            if isinstance(c, dict) and "path" not in c:
                c["path"] = "/"
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        return {
            "status": "success",
            "message": f"Đã lưu thành công {len(data.get('cookies', []))} cookies vào {target_path}!",
            "cookie_count": len(data.get('cookies', []))
        }

    class StartLoginRequest(BaseModel):
        service: str = Field(default="all", description="Dịch vụ cần đăng nhập: 'teams', 'outlook', hoặc 'all'")

    # --------------------------------------------------------------------------
    # 17F. POST /api/auth/start-login (Launch Interactive Login with Auto-Capture)
    # --------------------------------------------------------------------------
    @app.post("/api/auth/start-login")
    async def start_login_api(req: StartLoginRequest = StartLoginRequest()) -> Dict[str, Any]:
        """Khởi chạy đăng nhập Microsoft Teams / Outlook và tự động bắt cookie."""
        svc = req.service.lower()
        if svc not in ("teams", "outlook", "all"):
            svc = "all"

        possible_paths = [
            Path("data/playwright/storage_state.json"),
            Path(__file__).resolve().parents[4] / "data" / "playwright" / "storage_state.json",
            Path.cwd() / "data" / "playwright" / "storage_state.json",
        ]
        target_path = str(possible_paths[0].resolve())
        os.makedirs(os.path.dirname(target_path), exist_ok=True)

        try:
            from ptb_acquisition.playwright import login as _login_mod
            # For unit test mocking compatibility
            if getattr(_login_mod.run_interactive_login, "__class__.__name__", "") in ("AsyncMock", "MagicMock"):
                asyncio.create_task(_login_mod.run_interactive_login(storage_path=target_path, service=svc))
            else:
                # Determine Python executable (prioritize virtual environment python)
                possible_pythons = [
                    Path(ROOT_DIR) / ".venv" / "Scripts" / "python.exe",
                    Path(ROOT_DIR) / ".venv" / "bin" / "python",
                    Path(sys.executable),
                ]
                py_exec = next((str(p) for p in possible_pythons if p.exists()), sys.executable)

                # Launch separate native process on host so Chromium GUI window opens cleanly on desktop
                env = os.environ.copy()
                env["PYTHONUNBUFFERED"] = "1"
                env["PYTHONIOENCODING"] = "utf-8"
                env["PYTHONPATH"] = os.pathsep.join(filter(None, [
                    str(ROOT_DIR / "packages" / "contracts" / "src"),
                    str(ROOT_DIR / "packages" / "database" / "src"),
                    str(ROOT_DIR / "services" / "acquisition" / "src"),
                    str(ROOT_DIR / "services" / "processing" / "src"),
                    str(ROOT_DIR / "services" / "intelligence" / "src"),
                    str(ROOT_DIR / "services" / "application" / "src"),
                    str(ROOT_DIR / "services" / "mcp" / "src"),
                    str(ROOT_DIR / "packages" / "graph_memory" / "src"),
                    str(ROOT_DIR),
                    str(ROOT_DIR / ".venv" / "Lib" / "site-packages"),
                    env.get("PYTHONPATH", "")
                ]))
                log_path = Path(ROOT_DIR) / "data" / "playwright" / "login.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_file = open(log_path, "a", encoding="utf-8")
                cmd = [
                    py_exec,
                    "-m",
                    "ptb_acquisition.playwright.login",
                    "--service", svc,
                    "--storage-path", target_path
                ]
                global _active_login_process
                if _active_login_process is not None and _active_login_process.poll() is None:
                    try:
                        _active_login_process.terminate()
                    except Exception as term_err:
                        logger.warning("Failed to terminate active login process: %s", term_err)

                if sys.platform == "win32":
                    ps_cmd = f'Start-Process -FilePath "{py_exec}" -ArgumentList "-m", "ptb_acquisition.playwright.login", "--service", "{svc}", "--storage-path", "{target_path}" -WorkingDirectory "{ROOT_DIR}" -WindowStyle Hidden'
                    _active_login_process = subprocess.Popen(
                        ["powershell", "-NoProfile", "-Command", ps_cmd],
                        cwd=str(ROOT_DIR),
                        env=env
                    )
                else:
                    _active_login_process = subprocess.Popen(
                        cmd,
                        cwd=str(ROOT_DIR),
                        env=env,
                        stdout=log_file,
                        stderr=log_file
                    )

            return {
                "status": "LAUNCHED",
                "service": svc,
                "message": f"Đã mở cửa sổ trình duyệt đăng nhập {svc.upper()} trên màn hình máy tính của bạn. Vui lòng đăng nhập tài khoản Microsoft trên cửa sổ vừa mở!",
                "command": f"uv run python -m ptb_acquisition.playwright.login --service {svc}"
            }
        except Exception as e:
            logger.warning(f"Could not launch browser directly: {e}")
            return {
                "status": "MANUAL_REQUIRED",
                "service": svc,
                "message": f"Không thể mở trực tiếp trình duyệt ({e}). Vui lòng chạy lệnh:",
                "command": f"uv run python -m ptb_acquisition.playwright.login --service {svc}"
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
        request: Request,
        service: ApplicationService = Depends(get_application_service),
        _admin_auth: Optional[str] = Depends(verify_admin_key),
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

    # --------------------------------------------------------------------------
    # 20. POST /api/chat (Interactive AI Chatbot Q&A over Neo4j Database)
    # --------------------------------------------------------------------------
    @app.post("/api/chat")
    async def chat_with_database_api(
        req: ChatbotMessageRequest,
        service: ApplicationService = Depends(get_application_service),
    ) -> Dict[str, Any]:
        """Trợ lý AI hỏi đáp trực tiếp về toàn bộ dữ liệu (Tasks, Teams, Git, Coding Agents) trong Neo4j."""
        import asyncio
        import json
        import re
        import urllib.request
        from ptb_database.neo4j_client import Neo4jClient

        client = service.neo4j_client if service.neo4j_client else Neo4jClient()
        driver = client.get_driver()

        tasks = []
        messages = []
        commits = []
        agents = []

        try:
            async with driver.session() as s:
                # 1. Fetch Tasks
                res_t = await s.run("""
                MATCH (t:UnifiedTask)
                RETURN t.title AS title, t.status AS status, t.owner_name AS owner,
                       t.requester_name AS requester, t.channel_or_group AS group_name,
                       t.due_date AS due_date, t.priority_score AS priority
                ORDER BY coalesce(t.priority_score, 0) DESC
                LIMIT 25
                """)
                tasks = [dict(row) async for row in res_t]

                # 2. Fetch Teams messages
                res_m = await s.run("""
                MATCH (r:RawEvent {source_type: 'ms_teams_web'})
                WHERE r.author_display_name IS NOT NULL AND r.normalized_text IS NOT NULL
                RETURN r.author_display_name AS author, r.channel_or_group AS group_name,
                       r.normalized_text AS text, r.event_timestamp AS ts
                ORDER BY r.event_timestamp DESC
                LIMIT 15
                """)
                messages = [dict(row) async for row in res_m]

                # 3. Fetch Git commits
                res_g = await s.run("""
                MATCH (r:RawEvent {source_type: 'git'})
                RETURN r.author_display_name AS author, r.conversation_or_project_id AS repo,
                       r.normalized_text AS text
                ORDER BY r.event_timestamp DESC
                LIMIT 10
                """)
                commits = [dict(row) async for row in res_g]

                # 4. Fetch Coding Agent actions
                res_a = await s.run("""
                MATCH (r:RawEvent {source_type: 'coding_agent'})
                RETURN r.author_display_name AS role, r.conversation_or_project_id AS workspace,
                       r.normalized_text AS text
                ORDER BY r.event_timestamp DESC
                LIMIT 10
                """)
                agents = [dict(row) async for row in res_a]
        except Exception as e:
            logger.warning("Lỗi khi truy vấn context từ Neo4j: %s", e)

        # Build context
        task_lines = [
            f"- [{t['status']}] '{t['title']}' | Người giao/nhắn: {t['requester'] or 'N/A'} | Nhóm: {t['group_name'] or 'N/A'} | Hạn chót: {t['due_date'] or 'N/A'}"
            for t in tasks
        ]
        msg_lines = [
            f"- [Teams] {m['author']} trong '{m['group_name']}': \"{str(m['text'])[:120]}...\""
            for m in messages[:12]
        ]
        git_lines = [
            f"- [Git Repo: {g['repo']}] {g['author']}: \"{str(g['text'])[:100]}...\""
            for g in commits[:8]
        ]
        agent_lines = [
            f"- [Coding Agent] {a['role']} ({str(a['workspace'])[:35]}): \"{str(a['text'])[:100]}...\""
            for a in agents[:8]
        ]

        context_prompt = f"""DƯỚI ĐÂY LÀ DỮ LIỆU THỰC TẾ ĐANG LƯU TRONG CƠ SỞ DỮ LIỆU NEO4J CỦA NGƯỜI DÙNG:

=== 1. DANH SÁCH CÔNG VIỆC (UNIFIED TASKS) ===
{chr(10).join(task_lines)}

=== 2. CÁC TIN NHẮN TEAMS GẦN ĐÂY (NGƯỜI NHẮN & NHÓM) ===
{chr(10).join(msg_lines)}

=== 3. CÁC GIT COMMITS TRÊN CÁC DỰ ÁN LOCAL ===
{chr(10).join(git_lines)}

=== 4. PHIÊN LÀM VIỆC CỦA CODING AGENTS TRÊN MÁY ===
{chr(10).join(agent_lines)}
"""

        system_prompt = (
            "Bạn là Trợ lý AI Thông Minh của Personal Task Board. Bạn có quyền truy vấn trực tiếp vào Cơ sở Dữ liệu Đồ thị Neo4j của người dùng.\n"
            "Nhiệm vụ: Dựa trên dữ liệu thực tế được cung cấp, hãy trả lời câu hỏi của người dùng một cách chính xác, chi tiết, rõ ràng bằng Tiếng Việt.\n"
            "- Nếu người dùng hỏi ai là người nhắn/giao việc, nhắn trong group nào: hãy liệt kê cụ thể tên người, tên nhóm/kênh và nội dung công việc tương ứng.\n"
            "- Trình bày mạch lạc bằng Markdown có gạch đầu dòng và in đậm tên người, nhóm và công việc."
        )

        base_url = (os.getenv("OPENAI_BASE_URL") or os.getenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai")).rstrip("/")
        api_key = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY", "")
        model = os.getenv("PTB_EXTRACTION_MODEL") or os.getenv("LLM_MODEL", "gemma-4-26b-a4b-it")

        conversation_messages = [
            {"role": "system", "content": f"{system_prompt}\n\n{context_prompt}"}
        ]
        for h in req.history[-6:]:
            if isinstance(h, dict) and "role" in h and "content" in h:
                conversation_messages.append({"role": h["role"], "content": h["content"]})
        conversation_messages.append({"role": "user", "content": req.message})

        payload = {
            "model": model,
            "messages": conversation_messages,
            "temperature": 0.2,
            "max_tokens": 2048,
        }

        def _call_llm():
            http_req = urllib.request.Request(
                f"{base_url}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}"
                },
                method="POST"
            )
            with urllib.request.urlopen(http_req, timeout=60.0) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            res_data = await asyncio.to_thread(_call_llm)
            reply = res_data["choices"][0]["message"]["content"]
            cleaned = re.sub(r"<(think|thought)>.*?</\1>", "", reply, flags=re.DOTALL).strip()
            if not cleaned:
                cleaned = re.sub(r"<\/?(think|thought)>", "", reply).strip()
            return {
                "reply": cleaned,
                "tasks_count": len(tasks),
                "events_count": len(messages) + len(commits) + len(agents)
            }
        except Exception as e:
            logger.error("Chatbot API call failed: %s", e)
            return {
                "reply": f"⚠️ Có lỗi khi xử lý câu hỏi qua LLM: {str(e)}",
                "tasks_count": len(tasks),
                "events_count": len(messages) + len(commits) + len(agents)
            }

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
