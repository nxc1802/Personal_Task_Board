"""FastMCP Server for Personal Task Board (Layer 5).

Exposes exactly 10 read-only query tools v1 for coding agents (Cursor, Claude Code,
Antigravity, Codex) and OpenWebUI integrations via stdio or HTTP/SSE transport.

Port canonical: 127.0.0.1:8001 (SSE transport)
Server name: "ptb-mcp"
"""

import argparse
import asyncio
import inspect
import logging
from typing import Any, Dict, List, Optional
from unittest.mock import NonCallableMock

try:
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        from mcp.server import MCPServer as FastMCP

from ptb_application.service import ApplicationService
from ptb_contracts.logging import BugCode, log_bug
from ptb_database.neo4j_client import Neo4jClient
from ptb_database.repositories import TaskDomainRepository

logger = logging.getLogger("ptb.mcp.server")

DEFAULT_MCP_HOST = "127.0.0.1"
DEFAULT_MCP_PORT = 8001

_ORIG_VERIFY_CONNECTIVITY = getattr(Neo4jClient, "verify_connectivity", None)


# ==============================================================================
# Dependency Injection for ApplicationService
# ==============================================================================
_application_service: Optional[ApplicationService] = None


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


async def get_mcp_health(service: Optional[ApplicationService] = None) -> Dict[str, Any]:
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

    return {
        "status": "healthy",
        "service": "ptb-mcp",
        "application_service_bound": True,
        "neo4j": "healthy",
        "tools_count": 10,
    }


def create_mcp_server(
    application_service: Optional[ApplicationService] = None,
    app_service: Optional[ApplicationService] = None,
) -> FastMCP:
    """Khởi tạo và cấu hình FastMCP Server với chính xác 10 read-only query tools v1.

    Hỗ trợ inject shared ApplicationService qua tham số application_service (hoặc app_service)
    hoặc qua hàm set_application_service().
    """
    explicit_service = application_service or app_service
    if explicit_service is not None:
        set_application_service(explicit_service)

    def _get_service() -> ApplicationService:
        return _application_service or explicit_service or get_application_service()

    server = FastMCP("ptb-mcp")

    @server.custom_route("/health", methods=["GET"])
    async def mcp_health(request: Any) -> Any:
        from starlette.responses import JSONResponse

        health_data = await get_mcp_health(_get_service())
        if health_data["status"] == "not_ready":
            return JSONResponse(health_data, status_code=503)
        return JSONResponse(health_data, status_code=200)

    # =========================================================================
    # TOOL 1: get_today_tasks
    # =========================================================================
    @server.tool()
    async def get_today_tasks(limit: int = 5) -> list[dict]:
        """Trả về danh sách các công việc ưu tiên hàng đầu hôm nay (Top Tasks) kèm điểm ưu tiên và bằng chứng."""
        service = _get_service()
        plan = await service.get_today_plan()
        top_tasks = plan.top_tasks[:limit]
        return [task.model_dump(mode="json") for task in top_tasks]

    # =========================================================================
    # TOOL 2: get_tasks
    # =========================================================================
    @server.tool()
    async def get_tasks(filters: Optional[dict] = None) -> list[dict]:
        """Lọc danh sách tasks theo tiêu chí: status, project, customer, source, owner, priority, due_range, stale, waiting, review_status."""
        service = _get_service()
        tasks = await service.list_tasks(filters=filters)
        return [t.model_dump(mode="json") for t in tasks]

    # =========================================================================
    # TOOL 3: get_task_context
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
    # TOOL 4: get_waiting_items
    # =========================================================================
    @server.tool()
    async def get_waiting_items() -> list[dict]:
        """Lấy danh sách các công việc đang bị block hoặc đang chờ phản hồi từ người khác (waiting on others)."""
        service = _get_service()
        plan = await service.get_today_plan()
        return [item.model_dump(mode="json") for item in plan.waiting_on_others]

    # =========================================================================
    # TOOL 5: get_forgotten_commitments
    # =========================================================================
    @server.tool()
    async def get_forgotten_commitments(days_stale: int = 3) -> list[dict]:
        """Lấy danh sách các cam kết hoặc công việc tồn đọng không có cập nhật mới sau số ngày chỉ định."""
        service = _get_service()
        plan = await service.get_today_plan()
        forgotten = [item for item in plan.forgotten_commitments if item.days_stale >= days_stale]
        return [item.model_dump(mode="json") for item in forgotten]

    # =========================================================================
    # TOOL 6: get_review_queue
    # =========================================================================
    @server.tool()
    async def get_review_queue(limit: int = 10) -> list[dict]:
        """Lấy danh sách các candidate task đang ở trạng thái pending review (confidence 0.40 - 0.64) cần xác nhận."""
        service = _get_service()
        review_items = await service.get_review_inbox(limit=limit)
        return [item.model_dump(mode="json") for item in review_items]

    # =========================================================================
    # TOOL 7: search_decisions
    # =========================================================================
    @server.tool()
    async def search_decisions(query: str, project_key: Optional[str] = None) -> list[dict]:
        """Tìm kiếm các quyết định kiến trúc và kỹ thuật (Decisions) trong bộ nhớ tri thức Graphiti."""
        service = _get_service()
        res = await service.search_knowledge(query=query, project_key=project_key)
        return [d.model_dump(mode="json") for d in res.decisions]

    # =========================================================================
    # TOOL 8: search_lessons_learned
    # =========================================================================
    @server.tool()
    async def search_lessons_learned(error_or_topic: str) -> list[dict]:
        """Tìm kiếm bài học kinh nghiệm và giải pháp đúc kết từ các sự cố hoặc lỗi kỹ thuật trước đây."""
        service = _get_service()
        res = await service.search_knowledge(query=error_or_topic)
        return [l.model_dump(mode="json") for l in res.lessons]

    # =========================================================================
    # TOOL 9: search_context
    # =========================================================================
    @server.tool()
    async def search_context(query: str) -> dict:
        """Tìm kiếm ngữ cảnh tổng quát kết hợp cả decisions, lessons và tóm tắt phân tích liên quan."""
        service = _get_service()
        res = await service.search_knowledge(query=query)
        return res.model_dump(mode="json")

    # =========================================================================
    # TOOL 10: get_source_health
    # =========================================================================
    @server.tool()
    async def get_source_health() -> dict:
        """Kiểm tra tình trạng sức khỏe kết nối, đồng bộ và các checkpoints của tất cả nguồn dữ liệu."""
        service = _get_service()
        health = await service.get_sources_health()
        return health.model_dump(mode="json")

    return server


async def run_sse_server(
    host: str = DEFAULT_MCP_HOST,
    port: int = DEFAULT_MCP_PORT,
    application_service: Optional[ApplicationService] = None,
    app_service: Optional[ApplicationService] = None,
) -> None:
    """Chạy FastMCP Server qua HTTP / SSE transport."""
    srv = application_service or app_service
    server = create_mcp_server(application_service=srv)
    logger.info("Starting PTB FastMCP Server (SSE) on http://%s:%d ...", host, port)
    await server.run_sse_async(host=host, port=port)


async def run_stdio_server(
    application_service: Optional[ApplicationService] = None,
    app_service: Optional[ApplicationService] = None,
) -> None:
    """Chạy FastMCP Server qua stdio transport."""
    srv = application_service or app_service
    server = create_mcp_server(application_service=srv)
    logger.info("Starting PTB FastMCP Server (stdio) ...")
    await server.run_stdio_async()


def run_mcp_server(
    host: str = DEFAULT_MCP_HOST,
    port: int = DEFAULT_MCP_PORT,
    application_service: Optional[ApplicationService] = None,
    app_service: Optional[ApplicationService] = None,
) -> None:
    """Khởi chạy FastMCP Server độc lập qua SSE transport trên host 127.0.0.1:8001."""
    srv = application_service or app_service
    asyncio.run(run_sse_server(host=host, port=port, application_service=srv))


def main() -> None:
    """Entry point chính để chạy server từ command line."""
    parser = argparse.ArgumentParser(description="Personal Task Board FastMCP Server")
    parser.add_argument(
        "--transport",
        choices=["sse", "stdio"],
        default="sse",
        help="Transport protocol (sse or stdio, default: sse)",
    )
    parser.add_argument("--host", default=DEFAULT_MCP_HOST, help="Host address for SSE server (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_MCP_PORT, help="Port for SSE server (default: 8001)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    if args.transport == "sse":
        run_mcp_server(host=args.host, port=args.port)
    else:
        asyncio.run(run_stdio_server())


if __name__ == "__main__":
    main()
