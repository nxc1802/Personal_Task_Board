"""Live End-to-End (E2E) System Test for MCP Client Integration & Security (W3-03).

Kiểm thử toàn diện luồng vận hành thực tế của MCP Server & Coding Agents trên toàn hệ thống PTB:
1. Live Transport & Handshake Verification:
   - Live FastMCP Server (HTTP/SSE port 8001): Handshake, tool listing (10 tools read-only),
     standard resources (ptb://today-plan, ptb://tasks, ptb://system-health),
     and prompts (morning_briefing, task_execution_context).
   - Stdio Subprocess Transport: Handshake in trusted write mode (19 tools).
2. Coding Agent Simulation (Cursor, Claude Code, Antigravity):
   - Full mutation lifecycle: create_task -> update_task_status -> update_task -> delete_task.
   - Single Source of Truth Parity: Every mutation executed via MCP immediately reflects
     in the REST API (http://127.0.0.1:8000) and Neo4j database.
   - Actor Policy Enforcement: Authentic agent identities (CURSOR_AGENT, CLAUDE_CODE)
     faithfully recorded in StatusTransitionAudit without unauthorized ADMIN impersonation.
   - Destructive Action Safeguard: confirm=False aborts deletion, confirm=True executes.
3. Security, Fault Injection & Latency:
   - Read-only live server strictly rejects mutations.
   - Reserved privileged actors (ADMIN, SYSTEM) rejected without admin credentials.
   - Latency percentiles (p50, p95) measured over live SSE transport.
   - Zero credentials, secrets, or tokens leaked in responses or logs.
"""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import statistics
import sys
import time
from typing import Any, Dict, List, Optional
import urllib.request
import urllib.error

import pytest
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

from ptb_contracts import TaskStatus
from ptb_database.neo4j_client import Neo4jClient
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp import ClientSession

logger = logging.getLogger("ptb.e2e.mcp_live")
BASE_URL_API = "http://127.0.0.1:8000"
BASE_URL_MCP = "http://127.0.0.1:8001"
SSE_URL = f"{BASE_URL_MCP}/sse"


def _check_live_mcp_available() -> bool:
    """Kiểm tra xem MCP Server trên cổng 8001 có đang phản hồi không."""
    try:
        req = urllib.request.Request(f"{BASE_URL_MCP}/health")
        with urllib.request.urlopen(req, timeout=2) as res:
            return res.status == 200
    except Exception:
        return False


def _check_live_rest_available() -> bool:
    """Kiểm tra xem REST API trên cổng 8000 có đang phản hồi không."""
    try:
        req = urllib.request.Request(f"{BASE_URL_API}/health")
        with urllib.request.urlopen(req, timeout=2) as res:
            return res.status in (200, 503)
    except Exception:
        return False


def _extract_tool_payload(tool_result: Any) -> Any:
    """Trích xuất kết quả trả về từ đối tượng ToolResult của MCP SDK."""
    if hasattr(tool_result, "content") and tool_result.content:
        first = tool_result.content[0]
        if hasattr(first, "text"):
            try:
                return json.loads(first.text)
            except Exception:
                return first.text
    return None


# ==============================================================================
# 1. LIVE HEALTH & SSE TRANSPORT HANDSHAKE
# ==============================================================================

@pytest.mark.e2e
def test_e2e_live_mcp_health_check():
    """1. Kiểm tra tình trạng sức khỏe thực tế của MCP Server (Port 8001) và REST API (Port 8000)."""
    if not _check_live_mcp_available() or not _check_live_rest_available():
        pytest.skip("PTB live services are not running (PENDING live environment)")

    # 1a. Check REST API Health
    req_rest = urllib.request.Request(f"{BASE_URL_API}/health")
    with urllib.request.urlopen(req_rest, timeout=5) as res:
        assert res.status == 200
        data_rest = json.loads(res.read().decode("utf-8"))
        assert data_rest.get("service") == "ptb-application"
        assert data_rest.get("neo4j") == "healthy"

    # 1b. Check MCP Server Health
    req_mcp = urllib.request.Request(f"{BASE_URL_MCP}/health")
    with urllib.request.urlopen(req_mcp, timeout=5) as res:
        assert res.status == 200
        data_mcp = json.loads(res.read().decode("utf-8"))
        assert data_mcp.get("status") == "healthy"
        assert data_mcp.get("service") == "ptb-mcp"
        assert data_mcp.get("neo4j") == "healthy"
        assert data_mcp.get("application_service_bound") is True
        assert data_mcp.get("tools_count") == 10


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_e2e_live_mcp_sse_handshake_and_inventory():
    """2. Kết nối live MCP Client qua HTTP/SSE (port 8001) và kiểm tra danh mục 10 read-only tools."""
    if not _check_live_mcp_available():
        pytest.skip("MCP live SSE server is not running on port 8001")

    async with sse_client(SSE_URL) as (read, write):
        async with ClientSession(read, write) as session:
            init_res = await session.initialize()
            assert init_res.server_info.name == "ptb-mcp"

            # Verify 10 Read-Only Tools
            tools = await session.list_tools()
            tool_names = [t.name for t in tools.tools]
            assert len(tool_names) == 10, f"Expected 10 tools, found {len(tool_names)}: {tool_names}"
            expected_ro = {
                "get_today_tasks", "get_tasks", "get_task_context", "get_waiting_items",
                "get_forgotten_commitments", "get_review_queue", "search_decisions",
                "search_lessons_learned", "search_context", "get_source_health",
            }
            assert set(tool_names) == expected_ro

            # Verify standard MCP Resources
            resources = await session.list_resources()
            res_uris = {str(r.uri) for r in resources.resources}
            assert "ptb://today-plan" in res_uris
            assert "ptb://tasks" in res_uris
            assert "ptb://system-health" in res_uris

            # Verify standard MCP Prompts
            prompts = await session.list_prompts()
            prompt_names = {p.name for p in prompts.prompts}
            assert "morning_briefing" in prompt_names
            assert "task_execution_context" in prompt_names


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_e2e_live_mcp_resources_and_prompts_content():
    """3. Đọc dữ liệu thực tế từ MCP Resources và Prompts qua live SSE client."""
    if not _check_live_mcp_available():
        pytest.skip("MCP live SSE server is not running on port 8001")

    async with sse_client(SSE_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Read resource ptb://system-health
            r_health = await session.read_resource("ptb://system-health")
            assert len(r_health.contents) > 0
            health_json = json.loads(r_health.contents[0].text)
            assert health_json.get("status") == "healthy"
            assert health_json.get("service") == "ptb-mcp"
            assert health_json.get("neo4j") == "healthy"

            # Read resource ptb://today-plan
            r_plan = await session.read_resource("ptb://today-plan")
            assert len(r_plan.contents) > 0
            plan_json = json.loads(r_plan.contents[0].text)
            assert "top_tasks" in plan_json
            assert isinstance(plan_json["top_tasks"], list)

            # Get prompt morning_briefing
            p_briefing = await session.get_prompt("morning_briefing", arguments={})
            assert len(p_briefing.messages) > 0
            assert "today-plan" in p_briefing.messages[0].content.text


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_e2e_live_mcp_read_only_safeguard():
    """4. Kiểm tra live MCP Server (Read-Only mode) chặn hoàn toàn mọi mutation tool."""
    if not _check_live_mcp_available():
        pytest.skip("MCP live SSE server is not running on port 8001")

    async with sse_client(SSE_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Calling mutation tool on read-only server must return error or raise exception
            try:
                res = await session.call_tool("create_task", {"title": "Illegal Task In Read-Only"})
                assert res.is_error is True
                assert any("unknown tool" in getattr(c, "text", "").lower() for c in res.content)
            except Exception as e:
                err_msg = str(e).lower()
                assert "unknown tool" in err_msg or "not found" in err_msg or "tool error" in err_msg or "-32601" in err_msg


# ==============================================================================
# 2. CODING AGENT LIFECYCLE & REST API / NEO4J PARITY
# ==============================================================================

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_e2e_live_coding_agent_mutation_lifecycle_and_rest_parity():
    """5. Coding Agent (CURSOR_AGENT) chạy đầy đủ chu trình CRUD qua stdio subprocess write-mode:
    - MCP create_task -> phản ánh lập tức tại REST API (port 8000) và Neo4j
    - MCP update_task_status -> phản ánh lập tức tại REST API và ghi nhận StatusTransitionAudit
    - MCP update_task -> chỉnh sửa priority/mô tả phản ánh trên REST API
    - MCP delete_task confirm=False -> bị hủy an toàn
    - MCP delete_task confirm=True -> hoàn tất hủy task trên REST API
    """
    if not _check_live_rest_available():
        pytest.skip("REST API is not running on port 8000")

    # Launch trusted write mode MCP server subprocess connected to authoritative Neo4j
    agent_actor = "CURSOR_AGENT"
    env = os.environ.copy()
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "import asyncio; from ptb_mcp.server import run_stdio_server; asyncio.run(run_stdio_server(read_only=False))"],
        env=env,
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == 19

            # 5a. Create Task via MCP
            created_res = await session.call_tool("create_task", {
                "title": "E2E Live MCP Mutation Task",
                "description": "Created by Cursor Agent via MCP Stdio",
                "priority_score": 88.0,
                "project_key": "LIVE-E2E",
                "actor": agent_actor,
            })
            created_data = _extract_tool_payload(created_res)
            assert isinstance(created_data, dict)
            task_id = created_data["id"]
            assert created_data["title"] == "E2E Live MCP Mutation Task"
            assert created_data["priority_score"] == 88.0

            # 5b. Verify immediate reflection in REST API (Port 8000)
            req_get = urllib.request.Request(f"{BASE_URL_API}/api/tasks/{task_id}")
            with urllib.request.urlopen(req_get, timeout=5) as res_api:
                assert res_api.status == 200
                api_task_data = json.loads(res_api.read().decode("utf-8"))
                # Task detail response format
                t_obj = api_task_data.get("task", api_task_data)
                assert t_obj.get("title") == "E2E Live MCP Mutation Task"
                assert t_obj.get("priority_score") == 88.0
                assert t_obj.get("status") == "TODO"

            # 5c. Verify persistence in authoritative Neo4j
            neo_client = Neo4jClient()
            driver = neo_client.get_driver()
            async with driver.session() as s:
                res_n = await s.run("MATCH (t:UnifiedTask {id: $id}) RETURN t.title AS title, t.priority_score AS score", {"id": task_id})
                row = await res_n.single()
                assert row is not None
                assert row["title"] == "E2E Live MCP Mutation Task"
                assert row["score"] == 88.0

            # 5d. Update Status via MCP
            status_res = await session.call_tool("update_task_status", {
                "task_id": task_id,
                "new_status": "IN_PROGRESS",
                "actor": agent_actor,
            })
            status_data = _extract_tool_payload(status_res)
            assert status_data["success"] is True

            # Verify status in REST API immediately
            with urllib.request.urlopen(req_get, timeout=5) as res_api:
                t_obj = json.loads(res_api.read().decode("utf-8")).get("task", {})
                assert t_obj.get("status") == "IN_PROGRESS"

            # Verify audit log in Neo4j with authentic actor CURSOR_AGENT
            async with driver.session() as s:
                res_audit = await s.run(
                    """
                    MATCH (a:StatusTransitionAudit {task_id: $task_id})
                    WHERE a.new_status = 'IN_PROGRESS'
                    RETURN a.change_actor AS actor, a.new_status AS status
                    ORDER BY a.transitioned_at DESC
                    LIMIT 1
                    """,
                    {"task_id": task_id},
                )
                audit_row = await res_audit.single()
                assert audit_row is not None
                assert audit_row["actor"] == agent_actor
                assert audit_row["status"] == "IN_PROGRESS"

            # 5e. Update Task metadata via MCP
            upd_res = await session.call_tool("update_task", {
                "task_id": task_id,
                "priority_score": 98.0,
                "description": "Updated live by Cursor",
                "actor": agent_actor,
            })
            upd_data = _extract_tool_payload(upd_res)
            assert upd_data["success"] is True

            # Verify priority in REST API
            with urllib.request.urlopen(req_get, timeout=5) as res_api:
                t_obj = json.loads(res_api.read().decode("utf-8")).get("task", {})
                assert t_obj.get("priority_score") == 98.0

            # 5f. Destructive safeguard - confirm=False
            del_abort_res = await session.call_tool("delete_task", {
                "task_id": task_id,
                "confirm": False,
                "actor": agent_actor,
            })
            del_abort_data = _extract_tool_payload(del_abort_res)
            assert del_abort_data["success"] is False
            assert "confirmation required" in del_abort_data["error"]

            # Task still exists in REST API
            with urllib.request.urlopen(req_get, timeout=5) as res_api:
                assert res_api.status == 200

            # 5g. Destructive safeguard - confirm=True
            del_ok_res = await session.call_tool("delete_task", {
                "task_id": task_id,
                "confirm": True,
                "actor": agent_actor,
            })
            del_ok_data = _extract_tool_payload(del_ok_res)
            assert del_ok_data["success"] is True
            assert del_ok_data["task_id"] == task_id

            # Verify task is DISMISSED in REST API
            with urllib.request.urlopen(req_get, timeout=5) as res_api:
                t_obj = json.loads(res_api.read().decode("utf-8")).get("task", {})
                assert t_obj.get("status") == "DISMISSED"

            await neo_client.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_e2e_live_actor_impersonation_security():
    """6. Kiểm tra an ninh: từ chối tác tử giả mạo ADMIN/SYSTEM khi không có admin credentials."""
    env = os.environ.copy()
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "import asyncio; from ptb_mcp.server import run_stdio_server; asyncio.run(run_stdio_server(read_only=False))"],
        env=env,
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Attempting impersonation as ADMIN without credentials
            res_admin = await session.call_tool("create_task", {
                "title": "Impersonated Admin Task",
                "actor": "ADMIN",
            })
            data_admin = _extract_tool_payload(res_admin)
            assert data_admin["success"] is False
            assert "Impersonation forbidden" in data_admin["error"]

            # Attempting impersonation as SYSTEM without credentials
            res_sys = await session.call_tool("create_task", {
                "title": "Impersonated System Task",
                "actor": "SYSTEM",
            })
            data_sys = _extract_tool_payload(res_sys)
            assert data_sys["success"] is False
            assert "Impersonation forbidden" in data_sys["error"]


# ==============================================================================
# 3. LATENCY BENCHMARKS & SECRETS AUDIT
# ==============================================================================

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_e2e_live_mcp_latency_benchmarks():
    """7. Đo kiểm độ trễ gọi công cụ MCP thực tế qua HTTP/SSE (p50, p95)."""
    if not _check_live_mcp_available():
        pytest.skip("MCP live SSE server is not running on port 8001")

    async with sse_client(SSE_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            latencies_ms: List[float] = []
            rounds = 15

            for _ in range(rounds):
                t0 = time.perf_counter()
                res = await session.call_tool("get_today_tasks", {"limit": 5})
                dt_ms = (time.perf_counter() - t0) * 1000.0
                latencies_ms.append(dt_ms)

            latencies_sorted = sorted(latencies_ms)
            p50 = statistics.median(latencies_sorted)
            idx_p95 = int(0.95 * len(latencies_sorted))
            p95 = latencies_sorted[min(idx_p95, len(latencies_sorted) - 1)]

            logger.info("Live SSE MCP Latency: p50=%.2fms, p95=%.2fms (N=%d)", p50, p95, rounds)
            # Live HTTP/SSE network calls over loopback
            assert p50 < 300.0, f"Live SSE p50 latency too high: {p50:.2f}ms"
            assert p95 < 800.0, f"Live SSE p95 latency too high: {p95:.2f}ms"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_e2e_live_mcp_zero_secrets_leak():
    """8. Kiểm tra không để lộ khóa bí mật hoặc token trong dữ liệu trả về từ MCP Server."""
    if not _check_live_mcp_available():
        pytest.skip("MCP live SSE server is not running on port 8001")

    async with sse_client(SSE_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Inspect resources
            res = await session.read_resource("ptb://system-health")
            content = res.contents[0].text
            assert "AQ.Ab8RN6J" not in content
            assert "AIzaSy" not in content
            assert "sk-" not in content
            assert "personal_task_board" not in content

            # Inspect health endpoint
            req = urllib.request.Request(f"{BASE_URL_MCP}/health")
            with urllib.request.urlopen(req, timeout=5) as resp:
                health_body = resp.read().decode("utf-8")
                assert "AQ.Ab8RN6J" not in health_body
                assert "AIzaSy" not in health_body
                assert "sk-" not in health_body
