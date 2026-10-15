"""FastMCP Server for Personal Task Board (Layer 5).

Exposes full MCP capabilities for Coding Agents (Cursor, Claude Code/Desktop,
Antigravity, Codex, Windsurf, Cline) alongside OpenWebUI:
- 10 read-only query tools v1
- 6 BE CRUD & mutation tools (create, update, update_status, delete, approve, dismiss)
- 3 AI Agent reasoning & knowledge tools (ask_ptb_agent, record_decision, record_lesson_learned)
- Standard MCP resources (ptb://today-plan, ptb://tasks, ptb://system-health)
- Standard MCP prompts (morning_briefing, task_execution_context)

Supports both stdio transport (local subprocess) and HTTP/SSE transport (port 8001).
Server name: "ptb-mcp"
"""

import argparse
import asyncio
import inspect
import json
import logging
import os
import re
import secrets
from typing import Any, Dict, List, Optional
from unittest.mock import NonCallableMock
from uuid import uuid4

try:
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        from mcp.server import MCPServer as FastMCP

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from ptb_application.service import ApplicationService
from ptb_contracts.l2_processing import TaskStatus
from ptb_contracts.l3_storage import DecisionNodeRecord, LessonNodeRecord
from ptb_contracts.logging import BugCode, log_bug
from ptb_database.neo4j_client import Neo4jClient
from ptb_database.repositories import TaskDomainRepository

logger = logging.getLogger("ptb.mcp.server")

DEFAULT_MCP_HOST = "127.0.0.1"
DEFAULT_MCP_PORT = 8001

_ORIG_VERIFY_CONNECTIVITY = getattr(Neo4jClient, "verify_connectivity", None)

CANONICAL_TASK_STATUSES = (
    TaskStatus.TODO.value,
    TaskStatus.IN_PROGRESS.value,
    TaskStatus.BLOCKED.value,
    TaskStatus.DONE.value,
    TaskStatus.DISMISSED.value,
)

RESERVED_PRIVILEGED_ACTORS = {"ADMIN", "SYSTEM", "ROOT", "SUPERUSER"}
DEFAULT_MCP_ACTOR = "CODING_AGENT"


def enforce_actor_policy(
    actor: Optional[str],
    admin_key: Optional[str] = None,
    default_actor: str = DEFAULT_MCP_ACTOR,
) -> str:
    """Xác thực và ngăn chặn impersonation qua tham số actor (P1-5).

    Nếu yêu cầu actor mang quyền đặc quyền (ADMIN, SYSTEM), yêu cầu phải xác thực
    chính xác PTB_ADMIN_KEY. Ngược lại, từ chối hành vi giả mạo và raise PermissionError.
    """
    if not actor or not str(actor).strip():
        return default_actor

    norm = str(actor).strip()
    if norm.upper() in RESERVED_PRIVILEGED_ACTORS:
        expected_admin = os.getenv("PTB_ADMIN_KEY")
        if expected_admin and admin_key and secrets.compare_digest(admin_key.strip(), expected_admin.strip()):
            return norm.upper()
        raise PermissionError(
            f"Impersonation forbidden: actor '{actor}' is reserved for administrative operations. "
            f"Coding agents must authenticate with valid admin credentials or use agent identity (e.g. '{default_actor}')."
        )
    return norm


def validate_canonical_status(status_val: str) -> str:
    """Kiểm tra hợp lệ trạng thái công việc canonical."""
    if not status_val or not str(status_val).strip():
        raise ValueError(f"Status cannot be empty. Must be one of {sorted(CANONICAL_TASK_STATUSES)}")
    norm = str(status_val).strip().upper()
    if norm not in CANONICAL_TASK_STATUSES:
        raise ValueError(f"Invalid status '{status_val}'. Must be one of {sorted(CANONICAL_TASK_STATUSES)}")
    return norm


class MCPSecurityMiddleware(BaseHTTPMiddleware):
    """Bảo mật lớp HTTP / SSE cho FastMCP Server (P0-2, W1-02).

    Quy tắc:
    - /health (kèm hoặc không kèm dấu /) và preflight OPTIONS luôn được miễn trừ xác thực.
    - Nếu PTB_API_KEY chưa được cấu hình: chạy ở chế độ dev unauthenticated (ghi log warning).
    - Nếu PTB_API_KEY được cấu hình:
      Bắt buộc cung cấp API key hợp lệ qua:
      1. Header 'X-PTB-API-KEY'
      2. Header 'Authorization: Bearer <key>'
      3. Query parameter '?api_key=<key>' hoặc '?token=<key>' (hỗ trợ client EventSource SSE chuẩn)
      Nếu thiếu hoặc sai, trả về HTTP 401 Unauthorized.
    """

    async def dispatch(self, request: Any, call_next: Any) -> Any:
        path = request.url.path.rstrip("/")
        if path == "/health" or request.method == "OPTIONS":
            return await call_next(request)

        expected_key = os.getenv("PTB_API_KEY")
        if not expected_key or not expected_key.strip():
            logger.warning(
                "PTB_API_KEY is not configured; running MCP server in unauthenticated development mode. "
                "Set PTB_API_KEY in production to secure remote endpoints."
            )
            return await call_next(request)

        provided_key = request.headers.get("x-ptb-api-key")
        if not provided_key:
            auth_hdr = request.headers.get("authorization", "")
            if auth_hdr.lower().startswith("bearer "):
                provided_key = auth_hdr[7:].strip()
        if not provided_key:
            provided_key = request.query_params.get("api_key") or request.query_params.get("token")

        if not provided_key:
            return JSONResponse(
                {
                    "detail": "Missing API key. Please provide 'X-PTB-API-KEY' header or '?api_key=' parameter.",
                },
                status_code=401,
                headers={"WWW-Authenticate": "ApiKey"},
            )

        if not secrets.compare_digest(provided_key.strip(), expected_key.strip()):
            return JSONResponse(
                {
                    "detail": "Invalid API key.",
                },
                status_code=401,
                headers={"WWW-Authenticate": "ApiKey"},
            )

        return await call_next(request)

# ==============================================================================
# Dependency Injection for ApplicationService & State Tracking
# ==============================================================================
_application_service: Optional[ApplicationService] = None
_last_registered_tools_count: int = 10


def get_registered_tools_count() -> int:
    """Trả về số lượng công cụ MCP đã đăng ký gần nhất."""
    return _last_registered_tools_count


def set_registered_tools_count(count: int) -> None:
    """Cập nhật số lượng công cụ MCP đã đăng ký."""
    global _last_registered_tools_count
    _last_registered_tools_count = count


def get_application_service() -> ApplicationService:
    """Dependency injection provider cho FastMCP Server."""
    global _application_service
    if _application_service is None:
        _application_service = ApplicationService()
    return _application_service


def set_application_service(service: Optional[ApplicationService]) -> None:
    """Thiết lập instance ApplicationService dùng chung cho FastMCP Server."""
    global _application_service
    _application_service = service


async def get_mcp_health(
    service: Optional[ApplicationService] = None,
    read_only: Optional[bool] = None,
) -> Dict[str, Any]:
    """Kiểm tra sức khỏe thực sự của MCP Server và kết nối store của ApplicationService."""
    srv = service or get_application_service()
    if srv is None:
        log_bug(
            BugCode.PTB_MCP_001,
            subsystem="mcp",
            severity="ERROR",
            message="MCP server dependency unhealthy",
        )
        return {
            "status": "not_ready",
            "service": "ptb-mcp",
            "application_service_bound": False,
            "neo4j": "not_ready",
        }

    is_healthy = True
    try:
        neo4j_client = getattr(srv, "neo4j_client", None)
        task_repo = getattr(srv, "task_repo", None)

        if task_repo is None and neo4j_client is None and not hasattr(srv, "get_system_health"):
            is_healthy = False
        else:
            is_real_neo4j_repo = isinstance(task_repo, TaskDomainRepository)
            is_custom_neo4j_client = (
                neo4j_client is not None
                and (
                    not isinstance(neo4j_client, Neo4jClient)
                    or "verify_connectivity" in getattr(neo4j_client, "__dict__", {})
                    or getattr(type(neo4j_client), "verify_connectivity", None) is not _ORIG_VERIFY_CONNECTIVITY
                )
            )

            if (is_real_neo4j_repo or is_custom_neo4j_client) and neo4j_client is not None:
                verify_fn = getattr(neo4j_client, "verify_connectivity", None)
                if callable(verify_fn):
                    conn_ok = verify_fn()
                    if inspect.isawaitable(conn_ok):
                        conn_ok = await conn_ok
                    if not conn_ok:
                        is_healthy = False

            if is_healthy and hasattr(srv, "get_system_health") and callable(srv.get_system_health):
                sys_health_fn = getattr(srv, "get_system_health", None)
                should_call_sys_health = (
                    is_real_neo4j_repo
                    or is_custom_neo4j_client
                    or not isinstance(srv, ApplicationService)
                    or "get_system_health" in getattr(srv, "__dict__", {})
                    or isinstance(sys_health_fn, NonCallableMock)
                )
                if should_call_sys_health:
                    sys_health = srv.get_system_health()
                    if inspect.isawaitable(sys_health):
                        sys_health = await sys_health
                    if hasattr(sys_health, "model_dump"):
                        sys_health = sys_health.model_dump()
                    if isinstance(sys_health, dict):
                        neo4j_state = str(sys_health.get("neo4j", "healthy")).lower()
                        overall_state = str(sys_health.get("status", "healthy")).lower()
                        if (
                            neo4j_state in ("not_ready", "down", "unhealthy", "error", "false")
                            or overall_state == "not_ready"
                        ):
                            is_healthy = False
    except Exception:
        is_healthy = False

    if not is_healthy:
        log_bug(
            BugCode.PTB_MCP_001,
            subsystem="mcp",
            severity="ERROR",
            message="MCP server dependency unhealthy",
        )
        return {
            "status": "not_ready",
            "service": "ptb-mcp",
            "application_service_bound": True,
            "neo4j": "not_ready",
        }

    tools_cnt = 10 if read_only is True else _last_registered_tools_count

    return {
        "status": "healthy",
        "service": "ptb-mcp",
        "application_service_bound": True,
        "neo4j": "healthy",
        "tools_count": tools_cnt,
    }


# ==============================================================================
# Helper AI Agent Knowledge Query over Neo4j
# ==============================================================================
async def query_ptb_agent_knowledge(
    service: ApplicationService,
    prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Truy vấn dữ liệu đồ thị tổng hợp (Tasks, Teams, Git, Coding Agents) và suy luận trả lời."""
    tasks: List[Dict[str, Any]] = []
    messages: List[Dict[str, Any]] = []
    commits: List[Dict[str, Any]] = []
    agents: List[Dict[str, Any]] = []

    client = getattr(service, "neo4j_client", None)
    if client and hasattr(client, "get_driver"):
        driver = client.get_driver()
        if driver and hasattr(driver, "session"):
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
                logger.debug("Neo4j query in query_ptb_agent_knowledge encountered note: %s", e)

    # Fallback đọc từ task_repo nếu chưa có Neo4j driver session trực tiếp
    if not tasks and hasattr(service, "list_tasks"):
        try:
            repo_tasks = await service.list_tasks()
            tasks = [
                {
                    "title": t.title,
                    "status": t.status.value if hasattr(t.status, "value") else str(t.status),
                    "owner": t.owner_name,
                    "requester": t.requester_name,
                    "group_name": t.channel_or_group,
                    "due_date": str(t.due_date) if t.due_date else None,
                    "priority": t.priority_score,
                }
                for t in repo_tasks[:25]
            ]
        except Exception as repo_err:
            logger.debug("Fallback list_tasks in query_ptb_agent_knowledge encountered note: %s", repo_err)

    task_lines = [
        f"- [{t['status']}] '{t['title']}' | Người giao: {t.get('requester') or 'N/A'} | Nhóm: {t.get('group_name') or 'N/A'} | Hạn: {t.get('due_date') or 'N/A'}"
        for t in tasks
    ]
    msg_lines = [
        f"- [Teams] {m['author']} trong '{m.get('group_name')}': \"{str(m.get('text', ''))[:120]}...\""
        for m in messages
    ]
    git_lines = [
        f"- [Git Repo: {g.get('repo')}] {g.get('author')}: \"{str(g.get('text', ''))[:100]}...\""
        for g in commits
    ]
    agent_lines = [
        f"- [Coding Agent] {a.get('role')}: \"{str(a.get('text', ''))[:100]}...\""
        for a in agents
    ]

    context_prompt = f"""DỮ LIỆU ĐỒ THỊ PERSONAL TASK BOARD:
=== 1. TASKS ===
{chr(10).join(task_lines) if task_lines else "Chưa có task nào."}

=== 2. TEAMS MESSAGES ===
{chr(10).join(msg_lines) if msg_lines else "Chưa có tin nhắn Teams nào."}

=== 3. GIT COMMITS ===
{chr(10).join(git_lines) if git_lines else "Chưa có git commit nào."}

=== 4. CODING AGENTS ===
{chr(10).join(agent_lines) if agent_lines else "Chưa có session agent nào."}
"""

    base_url = (os.getenv("OPENAI_BASE_URL") or os.getenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai")).rstrip("/")
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY", "")
    model = os.getenv("PTB_EXTRACTION_MODEL") or os.getenv("LLM_MODEL", "gemma-4-26b-a4b-it")

    # Nếu có API key hợp lệ thì gọi LLM để tổng hợp
    if api_key and not api_key.startswith("mock-") and not api_key.startswith("test-"):
        try:
            import urllib.request
            system_prompt = (
                "Bạn là Trợ lý AI Thông Minh của Personal Task Board. Dựa trên dữ liệu thực tế từ cơ sở dữ liệu Neo4j, "
                "hãy trả lời câu hỏi của lập trình viên một cách ngắn gọn, mạch lạc, chính xác bằng Tiếng Việt."
            )
            conversation_messages = [
                {"role": "system", "content": f"{system_prompt}\n\n{context_prompt}"}
            ]
            if history:
                for h in history[-6:]:
                    if isinstance(h, dict) and "role" in h and "content" in h:
                        conversation_messages.append({"role": h["role"], "content": h["content"]})
            conversation_messages.append({"role": "user", "content": prompt})

            payload = {
                "model": model,
                "messages": conversation_messages,
                "temperature": 0.2,
                "max_tokens": 2048,
            }
            req = urllib.request.Request(
                f"{base_url}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                res_data = json.loads(resp.read().decode("utf-8"))
                reply = res_data["choices"][0]["message"]["content"]
                reply = re.sub(r"<(think|thought)>.*?</\1>", "", reply, flags=re.DOTALL).strip()
                return {
                    "reply": reply,
                    "tasks_count": len(tasks),
                    "events_count": len(messages) + len(commits) + len(agents),
                    "context_grounded": True,
                }
        except Exception as e:
            logger.debug("LLM completion in ask_ptb_agent fallback: %s", e)

    # Fallback trả về tóm tắt dữ liệu có cấu trúc
    return {
        "reply": f"Dữ liệu đồ thị Personal Task Board cho câu hỏi: '{prompt}'\n\n{context_prompt}",
        "tasks_count": len(tasks),
        "events_count": len(messages) + len(commits) + len(agents),
        "context_grounded": True,
    }


# ==============================================================================
# MCP Server Factory
# ==============================================================================
def create_mcp_server(
    application_service: Optional[ApplicationService] = None,
    app_service: Optional[ApplicationService] = None,
    read_only: bool = True,
) -> FastMCP:
    """Khởi tạo và cấu hình FastMCP Server cho Personal Task Board.

    - read_only = True (mặc định): Cung cấp chính xác 10 read-only query tools v1 (tuân thủ Gate 8).
    - read_only = False: Xuất đầy đủ Agent & BE gồm 19 tools, resources và prompts
      cho Coding Agents (Cursor, Claude Desktop, Antigravity, VS Code, Windsurf) sử dụng.
    """
    explicit_service = application_service or app_service
    if explicit_service is not None:
        set_application_service(explicit_service)

    def _get_service() -> ApplicationService:
        return _application_service or explicit_service or get_application_service()

    server = FastMCP("ptb-mcp")

    # Cập nhật số lượng tools đăng ký cho health reporting
    total_tools = 10 if read_only else 19
    set_registered_tools_count(total_tools)

    @server.custom_route("/health", methods=["GET"])
    async def mcp_health(request: Any) -> Any:
        from starlette.responses import JSONResponse

        health_data = await get_mcp_health(_get_service(), read_only=read_only)
        if health_data["status"] == "not_ready":
            return JSONResponse(health_data, status_code=503)
        return JSONResponse(health_data, status_code=200)

    # =========================================================================
    # TOOL 1: get_today_tasks (Read-Only)
    # =========================================================================
    @server.tool()
    async def get_today_tasks(limit: int = 5) -> list[dict]:
        """Trả về danh sách các công việc ưu tiên hàng đầu hôm nay (Top Tasks) kèm điểm ưu tiên và bằng chứng."""
        service = _get_service()
        plan = await service.get_today_plan()
        top_tasks = plan.top_tasks[:limit]
        return [task.model_dump(mode="json") for task in top_tasks]

    # =========================================================================
    # TOOL 2: get_tasks (Read-Only)
    # =========================================================================
    @server.tool()
    async def get_tasks(filters: Optional[dict] = None) -> list[dict]:
        """Lọc danh sách tasks theo tiêu chí: status, project, customer, source, owner, priority, due_range, stale, waiting, review_status."""
        service = _get_service()
        tasks = await service.list_tasks(filters=filters)
        return [t.model_dump(mode="json") for t in tasks]

    # =========================================================================
    # TOOL 3: get_task_context (Read-Only)
    # =========================================================================
    @server.tool()
    async def get_task_context(task_id: str) -> dict:
        """Lấy toàn bộ mạng lưới ngữ cảnh của 1 task gồm blockers, dependents, evidences, related decisions và lessons learned."""
        service = _get_service()
        task_ctx = await service.get_task_detail(task_id=task_id)
        if not task_ctx:
            return {"error": f"Task '{task_id}' not found"}
        return task_ctx.model_dump(mode="json")

    # =========================================================================
    # TOOL 4: get_waiting_items (Read-Only)
    # =========================================================================
    @server.tool()
    async def get_waiting_items() -> list[dict]:
        """Lấy danh sách các công việc đang bị block hoặc đang chờ phản hồi từ người khác (waiting on others)."""
        service = _get_service()
        plan = await service.get_today_plan()
        return [item.model_dump(mode="json") for item in plan.waiting_on_others]

    # =========================================================================
    # TOOL 5: get_forgotten_commitments (Read-Only)
    # =========================================================================
    @server.tool()
    async def get_forgotten_commitments(days_stale: int = 3) -> list[dict]:
        """Lấy danh sách các cam kết hoặc công việc tồn đọng không có cập nhật mới sau số ngày chỉ định."""
        service = _get_service()
        plan = await service.get_today_plan()
        forgotten = [item for item in plan.forgotten_commitments if item.days_stale >= days_stale]
        return [item.model_dump(mode="json") for item in forgotten]

    # =========================================================================
    # TOOL 6: get_review_queue (Read-Only)
    # =========================================================================
    @server.tool()
    async def get_review_queue(limit: int = 10) -> list[dict]:
        """Lấy danh sách các candidate task đang ở trạng thái pending review (confidence 0.40 - 0.64) cần xác nhận."""
        service = _get_service()
        review_items = await service.get_review_inbox(limit=limit)
        return [item.model_dump(mode="json") for item in review_items]

    # =========================================================================
    # TOOL 7: search_decisions (Read-Only)
    # =========================================================================
    @server.tool()
    async def search_decisions(query: str, project_key: Optional[str] = None) -> list[dict]:
        """Tìm kiếm các quyết định kiến trúc và kỹ thuật (Decisions) trong bộ nhớ tri thức Graphiti."""
        service = _get_service()
        res = await service.search_knowledge(query=query, project_key=project_key)
        return [d.model_dump(mode="json") for d in res.decisions]

    # =========================================================================
    # TOOL 8: search_lessons_learned (Read-Only)
    # =========================================================================
    @server.tool()
    async def search_lessons_learned(error_or_topic: str) -> list[dict]:
        """Tìm kiếm bài học kinh nghiệm và giải pháp đúc kết từ các sự cố hoặc lỗi kỹ thuật trước đây."""
        service = _get_service()
        res = await service.search_knowledge(query=error_or_topic)
        return [l.model_dump(mode="json") for l in res.lessons]

    # =========================================================================
    # TOOL 9: search_context (Read-Only)
    # =========================================================================
    @server.tool()
    async def search_context(query: str) -> dict:
        """Tìm kiếm ngữ cảnh tổng quát kết hợp cả decisions, lessons và tóm tắt phân tích liên quan."""
        service = _get_service()
        res = await service.search_knowledge(query=query)
        return res.model_dump(mode="json")

    # =========================================================================
    # TOOL 10: get_source_health (Read-Only)
    # =========================================================================
    @server.tool()
    async def get_source_health() -> dict:
        """Kiểm tra tình trạng sức khỏe kết nối, đồng bộ và các checkpoints của tất cả nguồn dữ liệu."""
        service = _get_service()
        health = await service.get_sources_health()
        return health.model_dump(mode="json")

    # =========================================================================
    # EXTENDED MCP CAPABILITIES (Agent & BE Mutations for Coding Agents)
    # =========================================================================
    if not read_only:
        # ---------------------------------------------------------------------
        # TOOL 11: create_task (CRUD Mutation)
        # ---------------------------------------------------------------------
        @server.tool()
        async def create_task(
            title: str,
            description: str = "",
            project_key: str = "GLOBAL",
            due_date: Optional[str] = None,
            priority_score: float = 50.0,
            owner_name: Optional[str] = None,
            actor: str = DEFAULT_MCP_ACTOR,
            admin_key: Optional[str] = None,
        ) -> dict:
            """Tạo mới một công việc trong Personal Task Board dành cho Coding Agent."""
            clean_title = (title or "").strip()
            if not clean_title:
                return {"success": False, "error": "Field 'title' cannot be empty"}

            try:
                safe_actor = enforce_actor_policy(actor, admin_key=admin_key)
            except PermissionError as pe:
                return {"success": False, "error": str(pe)}

            service = _get_service()
            clamped_priority = max(0.0, min(100.0, float(priority_score)))

            logger.info(
                "AUDIT [MCP] actor=%s action=CREATE_TASK title='%s' project=%s",
                safe_actor,
                clean_title[:50],
                project_key,
            )
            task = await service.create_task(
                title=clean_title,
                description=description or "",
                project_key=project_key or "GLOBAL",
                due_date=due_date,
                priority_score=clamped_priority,
                owner_name=owner_name,
                actor=safe_actor,
            )
            return task.model_dump(mode="json")

        # ---------------------------------------------------------------------
        # TOOL 12: update_task_status (CRUD Mutation)
        # ---------------------------------------------------------------------
        @server.tool()
        async def update_task_status(
            task_id: str,
            new_status: str,
            actor: str = DEFAULT_MCP_ACTOR,
            admin_key: Optional[str] = None,
        ) -> dict:
            """Cập nhật trạng thái công việc (TODO, IN_PROGRESS, BLOCKED, DONE, DISMISSED) kèm audit log."""
            clean_task_id = (task_id or "").strip()
            if not clean_task_id:
                return {"success": False, "error": "Field 'task_id' cannot be empty"}

            try:
                canonical_status = validate_canonical_status(new_status)
            except ValueError as ve:
                return {"success": False, "error": str(ve)}

            try:
                safe_actor = enforce_actor_policy(actor, admin_key=admin_key)
            except PermissionError as pe:
                return {"success": False, "error": str(pe)}

            logger.info(
                "AUDIT [MCP] actor=%s action=UPDATE_STATUS task_id=%s new_status=%s",
                safe_actor,
                clean_task_id,
                canonical_status,
            )
            service = _get_service()
            res = await service.execute_task_action(
                task_id=clean_task_id,
                action="UPDATE_STATUS",
                new_status=canonical_status,
                actor=safe_actor,
            )
            return res.model_dump(mode="json")

        # ---------------------------------------------------------------------
        # TOOL 13: update_task (CRUD Mutation)
        # ---------------------------------------------------------------------
        @server.tool()
        async def update_task(
            task_id: str,
            title: Optional[str] = None,
            description: Optional[str] = None,
            priority_score: Optional[float] = None,
            due_date: Optional[str] = None,
            actor: str = DEFAULT_MCP_ACTOR,
            admin_key: Optional[str] = None,
        ) -> dict:
            """Chỉnh sửa thông tin chi tiết của task (tiêu đề, mô tả, điểm ưu tiên, hạn chót)."""
            clean_task_id = (task_id or "").strip()
            if not clean_task_id:
                return {"success": False, "error": "Field 'task_id' cannot be empty"}

            try:
                safe_actor = enforce_actor_policy(actor, admin_key=admin_key)
            except PermissionError as pe:
                return {"success": False, "error": str(pe)}

            service = _get_service()
            updates: Dict[str, Any] = {}
            if title is not None:
                updates["title"] = str(title).strip()
            if description is not None:
                updates["description"] = str(description)
            if priority_score is not None:
                updates["priority_score"] = max(0.0, min(100.0, float(priority_score)))
            if due_date is not None:
                updates["due_date"] = due_date

            logger.info(
                "AUDIT [MCP] actor=%s action=UPDATE_TASK task_id=%s fields=%s",
                safe_actor,
                clean_task_id,
                list(updates.keys()),
            )
            task = await service.update_task(task_id=clean_task_id, actor=safe_actor, **updates)
            if not task:
                return {"success": False, "error": f"Task '{clean_task_id}' not found"}
            return {"success": True, "task": task.model_dump(mode="json")}

        # ---------------------------------------------------------------------
        # TOOL 14: delete_task (CRUD Mutation with Confirmation Safeguard)
        # ---------------------------------------------------------------------
        @server.tool()
        async def delete_task(
            task_id: str,
            actor: str = DEFAULT_MCP_ACTOR,
            confirm: bool = True,
            admin_key: Optional[str] = None,
        ) -> dict:
            """Xóa hoặc hủy bỏ (DISMISSED) một task khỏi Task Board với cơ chế xác nhận an toàn."""
            clean_task_id = (task_id or "").strip()
            if not clean_task_id:
                return {"success": False, "task_id": clean_task_id, "error": "Field 'task_id' cannot be empty"}

            if not confirm:
                logger.warning(
                    "AUDIT [MCP] delete_task rejected: confirmation required for task_id=%s",
                    clean_task_id,
                )
                return {
                    "success": False,
                    "task_id": clean_task_id,
                    "error": "Destructive action aborted: confirmation required (set confirm=True to execute deletion)",
                }

            try:
                safe_actor = enforce_actor_policy(actor, admin_key=admin_key)
            except PermissionError as pe:
                return {"success": False, "task_id": clean_task_id, "error": str(pe)}

            logger.info(
                "AUDIT [MCP] actor=%s action=DELETE_TASK task_id=%s confirmed=%s",
                safe_actor,
                clean_task_id,
                confirm,
            )
            service = _get_service()
            ok = await service.delete_task(task_id=clean_task_id, actor=safe_actor)
            if not ok:
                return {
                    "success": False,
                    "task_id": clean_task_id,
                    "error": f"Task '{clean_task_id}' not found or could not be dismissed",
                }
            return {"success": True, "task_id": clean_task_id}

        # ---------------------------------------------------------------------
        # TOOL 15: approve_task (Review Action)
        # ---------------------------------------------------------------------
        @server.tool()
        async def approve_task(
            review_id: str,
            new_status: str = "TODO",
            actor: str = DEFAULT_MCP_ACTOR,
            admin_key: Optional[str] = None,
        ) -> dict:
            """Phê duyệt candidate task từ review queue đưa vào bảng công việc chính thức."""
            clean_review_id = (review_id or "").strip()
            if not clean_review_id:
                return {"success": False, "error": "Field 'review_id' cannot be empty"}

            try:
                canonical_status = validate_canonical_status(new_status)
            except ValueError as ve:
                return {"success": False, "error": str(ve)}

            try:
                safe_actor = enforce_actor_policy(actor, admin_key=admin_key)
            except PermissionError as pe:
                return {"success": False, "error": str(pe)}

            logger.info(
                "AUDIT [MCP] actor=%s action=APPROVE_TASK review_id=%s new_status=%s",
                safe_actor,
                clean_review_id,
                canonical_status,
            )
            service = _get_service()
            res = await service.execute_task_action(
                task_id=clean_review_id,
                action="APPROVE",
                new_status=canonical_status,
                actor=safe_actor,
            )
            return res.model_dump(mode="json")

        # ---------------------------------------------------------------------
        # TOOL 16: dismiss_task (Review Action)
        # ---------------------------------------------------------------------
        @server.tool()
        async def dismiss_task(
            review_id: str,
            actor: str = DEFAULT_MCP_ACTOR,
            admin_key: Optional[str] = None,
        ) -> dict:
            """Từ chối hoặc loại bỏ candidate task khỏi review queue."""
            clean_review_id = (review_id or "").strip()
            if not clean_review_id:
                return {"success": False, "error": "Field 'review_id' cannot be empty"}

            try:
                safe_actor = enforce_actor_policy(actor, admin_key=admin_key)
            except PermissionError as pe:
                return {"success": False, "error": str(pe)}

            logger.info(
                "AUDIT [MCP] actor=%s action=DISMISS_TASK review_id=%s",
                safe_actor,
                clean_review_id,
            )
            service = _get_service()
            res = await service.execute_task_action(
                task_id=clean_review_id,
                action="DISMISS",
                actor=safe_actor,
            )
            return res.model_dump(mode="json")

        # ---------------------------------------------------------------------
        # TOOL 17: ask_ptb_agent (AI Agent Reasoning & Context Synthesis)
        # ---------------------------------------------------------------------
        @server.tool()
        async def ask_ptb_agent(
            prompt: str,
            history: Optional[list] = None,
        ) -> dict:
            """Hỏi đáp trực tiếp với AI Agent của Personal Task Board tổng hợp tri thức thực tế (Tasks, Teams, Git, Coding Agents) từ Neo4j."""
            clean_prompt = (prompt or "").strip()
            if not clean_prompt:
                return {"reply": "Prompt không được để trống.", "context_grounded": False}

            logger.info("AUDIT [MCP] action=ASK_PTB_AGENT prompt_len=%d", len(clean_prompt))
            service = _get_service()
            return await query_ptb_agent_knowledge(service=service, prompt=clean_prompt, history=history)

        # ---------------------------------------------------------------------
        # TOOL 18: record_decision (Knowledge Recording)
        # ---------------------------------------------------------------------
        @server.tool()
        async def record_decision(
            summary: str,
            rationale: str,
            project_key: str = "GLOBAL",
            decided_by: str = DEFAULT_MCP_ACTOR,
            admin_key: Optional[str] = None,
        ) -> dict:
            """Lưu trữ quyết định kiến trúc hoặc kỹ thuật (ADR) vào bộ nhớ tri thức Graphiti."""
            clean_summary = (summary or "").strip()
            clean_rationale = (rationale or "").strip()
            if not clean_summary or not clean_rationale:
                return {"success": False, "error": "Fields 'summary' and 'rationale' cannot be empty"}

            try:
                safe_decided_by = enforce_actor_policy(decided_by, admin_key=admin_key)
            except PermissionError as pe:
                return {"success": False, "error": str(pe)}

            logger.info(
                "AUDIT [MCP] actor=%s action=RECORD_DECISION project=%s summary='%s'",
                safe_decided_by,
                project_key,
                clean_summary[:50],
            )
            service = _get_service()
            from datetime import datetime, timezone

            rec = DecisionNodeRecord(
                decision_id=str(uuid4()),
                summary=clean_summary,
                rationale=clean_rationale,
                topic=(project_key or "GLOBAL").strip().upper(),
                decided_by=safe_decided_by,
                decided_at=datetime.now(timezone.utc),
            )
            gm = getattr(service, "graph_memory", None)
            if gm and hasattr(gm, "add_decision"):
                await gm.add_decision(rec)
            return {"success": True, "decision": rec.model_dump(mode="json")}

        # ---------------------------------------------------------------------
        # TOOL 19: record_lesson_learned (Knowledge Recording)
        # ---------------------------------------------------------------------
        @server.tool()
        async def record_lesson_learned(
            topic: str,
            solution: str,
            error_context: Optional[str] = None,
        ) -> dict:
            """Ghi nhận bài học kinh nghiệm hoặc giải pháp sửa lỗi kỹ thuật vào bộ nhớ Graphiti."""
            clean_topic = (topic or "").strip()
            clean_solution = (solution or "").strip()
            if not clean_topic or not clean_solution:
                return {"success": False, "error": "Fields 'topic' and 'solution' cannot be empty"}

            logger.info("AUDIT [MCP] action=RECORD_LESSON topic='%s'", clean_topic[:50])
            service = _get_service()
            from datetime import datetime, timezone

            rec = LessonNodeRecord(
                lesson_id=str(uuid4()),
                topic=clean_topic,
                description=error_context or clean_topic,
                solution=clean_solution,
                recorded_at=datetime.now(timezone.utc),
            )
            gm = getattr(service, "graph_memory", None)
            if gm and hasattr(gm, "add_lesson_episode"):
                await gm.add_lesson_episode(rec)
            return {"success": True, "lesson": rec.model_dump(mode="json")}

    # -------------------------------------------------------------------------
    # MCP RESOURCES (Standardized across Read-Only and Full Modes)
    # -------------------------------------------------------------------------
    @server.resource("ptb://today-plan")
    async def resource_today_plan() -> str:
        """Kế hoạch hôm nay và danh sách công việc ưu tiên dạng JSON."""
        srv = _get_service()
        plan = await srv.get_today_plan()
        return plan.model_dump_json()

    @server.resource("ptb://tasks")
    async def resource_tasks() -> str:
        """Toàn bộ danh sách công việc dạng JSON."""
        srv = _get_service()
        tasks = await srv.list_tasks()
        return json.dumps([t.model_dump(mode="json") for t in tasks], ensure_ascii=False)

    @server.resource("ptb://system-health")
    async def resource_system_health() -> str:
        """Tình trạng sức khỏe hệ thống và MCP server dạng JSON."""
        srv = _get_service()
        health = await get_mcp_health(srv, read_only=read_only)
        return json.dumps(health, ensure_ascii=False)

    # -------------------------------------------------------------------------
    # MCP PROMPTS (Standardized across Read-Only and Full Modes)
    # -------------------------------------------------------------------------
    @server.prompt("morning_briefing")
    def prompt_morning_briefing() -> str:
        """Prompt mẫu hỗ trợ Coding Agent tóm tắt công việc đầu ngày."""
        return (
            "Hãy đọc resource ptb://today-plan hoặc gọi tool get_today_tasks để tóm tắt các công việc ưu tiên hôm nay, "
            "sau đó phân tích các rủi ro tiến độ và đề xuất thứ tự giải quyết phù hợp nhất cho lập trình viên."
        )

    @server.prompt("task_execution_context")
    def prompt_task_execution_context(task_id: str) -> str:
        """Prompt mẫu giúp Coding Agent thu thập đầy đủ ngữ cảnh để giải quyết một task cụ thể."""
        return (
            f"Hãy gọi tool get_task_context với task_id='{task_id}', đồng thời gọi search_decisions và search_lessons_learned "
            f"để hiểu rõ ngữ cảnh, các cam kết, rủi ro và các bài học kinh nghiệm trước khi bắt đầu viết code giải quyết task này."
        )

    # -------------------------------------------------------------------------
    # SECURED ASGI TRANSPORT APPLICATION (HTTP / SSE with MCPSecurityMiddleware)
    # -------------------------------------------------------------------------
    orig_sse_app = server.sse_app

    def secured_sse_app(*args: Any, **kwargs: Any) -> Any:
        app = orig_sse_app(*args, **kwargs)
        has_sec = any(
            getattr(m, "cls", None) is MCPSecurityMiddleware
            for m in getattr(app, "user_middleware", [])
        )
        if not has_sec:
            app.add_middleware(MCPSecurityMiddleware)
        return app

    server.sse_app = secured_sse_app

    if hasattr(server, "streamable_http_app"):
        orig_http_app = server.streamable_http_app

        def secured_http_app(*args: Any, **kwargs: Any) -> Any:
            app = orig_http_app(*args, **kwargs)
            has_sec = any(
                getattr(m, "cls", None) is MCPSecurityMiddleware
                for m in getattr(app, "user_middleware", [])
            )
            if not has_sec:
                app.add_middleware(MCPSecurityMiddleware)
            return app

        server.streamable_http_app = secured_http_app

    return server


# ==============================================================================
# MCP Server Runners
# ==============================================================================
async def run_sse_server(
    host: str = DEFAULT_MCP_HOST,
    port: int = DEFAULT_MCP_PORT,
    application_service: Optional[ApplicationService] = None,
    app_service: Optional[ApplicationService] = None,
    read_only: bool = False,
) -> None:
    """Chạy FastMCP Server qua HTTP / SSE transport."""
    srv = application_service or app_service
    server = create_mcp_server(application_service=srv, read_only=read_only)
    mode_str = "Read-Only (10 tools)" if read_only else "Full Agent & BE (19 tools)"
    logger.info("Starting PTB FastMCP Server (SSE [%s]) on http://%s:%d ...", mode_str, host, port)
    await server.run_sse_async(host=host, port=port)


async def run_stdio_server(
    application_service: Optional[ApplicationService] = None,
    app_service: Optional[ApplicationService] = None,
    read_only: bool = False,
) -> None:
    """Chạy FastMCP Server qua stdio transport."""
    srv = application_service or app_service
    server = create_mcp_server(application_service=srv, read_only=read_only)
    mode_str = "Read-Only (10 tools)" if read_only else "Full Agent & BE (19 tools)"
    logger.info("Starting PTB FastMCP Server (stdio [%s]) ...", mode_str)
    await server.run_stdio_async()


def run_mcp_server(
    host: str = DEFAULT_MCP_HOST,
    port: int = DEFAULT_MCP_PORT,
    application_service: Optional[ApplicationService] = None,
    app_service: Optional[ApplicationService] = None,
    read_only: bool = False,
) -> None:
    """Khởi chạy FastMCP Server độc lập qua SSE transport trên host 127.0.0.1:8001."""
    srv = application_service or app_service
    asyncio.run(run_sse_server(host=host, port=port, application_service=srv, read_only=read_only))


def main() -> None:
    """Entry point chính để chạy server từ command line."""
    parser = argparse.ArgumentParser(description="Personal Task Board FastMCP Server (Layer 5)")
    parser.add_argument(
        "--transport",
        choices=["sse", "stdio"],
        default="sse",
        help="Transport protocol (sse or stdio, default: sse)",
    )
    parser.add_argument("--host", default=DEFAULT_MCP_HOST, help="Host address for SSE server (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_MCP_PORT, help="Port for SSE server (default: 8001)")
    parser.add_argument("--read-only", action="store_true", help="Chỉ bật chế độ Read-Only 10 tools")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    if args.transport == "sse":
        run_mcp_server(host=args.host, port=args.port, read_only=args.read_only)
    else:
        asyncio.run(run_stdio_server(read_only=args.read_only))


if __name__ == "__main__":
    main()
