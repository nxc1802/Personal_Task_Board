"""Unit and integration tests for OpenWebUI Integration & Board Artifact (Phase R11, R12).

Bao gồm:
- Docker Compose hardening (pin version v0.5.10, localhost 127.0.0.1 bind).
- PTB Board Artifact: No-Mock fallback in production, DEMO_MODE flag, 8 live views, error banners.
- OpenWebUI Auto-Installer: check_connection, install_components, manifest export, install summary.
- Action Function & OpenWebUI Tools definitions.
"""

from pathlib import Path
from unittest.mock import AsyncMock, patch
import httpx
import pytest
import yaml

from integrations.openwebui.functions.ptb_board_action import Action
from integrations.openwebui.installer import OpenWebUIInstaller, PINNED_OPENWEBUI_VERSION
from integrations.openwebui.tools.ptb_tools import Tools

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent


# ==============================================================================
# 1. DOCKER COMPOSE HARDENING TESTS
# ==============================================================================
def test_docker_compose_configuration():
    """Verify docker-compose.yml pins OpenWebUI version and binds all ports to 127.0.0.1."""
    compose_path = REPO_ROOT / "docker-compose.yml"
    assert compose_path.exists(), "docker-compose.yml must exist"

    with open(compose_path, "r", encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    services = compose.get("services", {})
    assert "neo4j" in services, "neo4j service must be defined"
    assert "open-webui" in services, "open-webui service must be defined"

    # 1. OpenWebUI pinned version
    owui = services["open-webui"]
    assert owui["image"] == f"ghcr.io/open-webui/open-webui:{PINNED_OPENWEBUI_VERSION}", (
        f"OpenWebUI image must be pinned to {PINNED_OPENWEBUI_VERSION}"
    )
    assert owui["container_name"] == "ptb_openwebui"

    # 2. Localhost bind (127.0.0.1) for all services
    owui_ports = owui.get("ports", [])
    assert any("127.0.0.1:3000:8080" in p for p in owui_ports), (
        "OpenWebUI port must bind to 127.0.0.1:3000:8080"
    )

    neo4j_ports = services["neo4j"].get("ports", [])
    assert any("127.0.0.1:7474:7474" in p for p in neo4j_ports), (
        "Neo4j UI port must bind to 127.0.0.1:7474:7474"
    )
    assert any("127.0.0.1:7687:7687" in p for p in neo4j_ports), (
        "Neo4j Bolt port must bind to 127.0.0.1:7687:7687"
    )

    # Đảm bảo không có port nào expose ra 0.0.0.0 mà thiếu localhost prefix
    all_ports = owui_ports + neo4j_ports
    for p in all_ports:
        assert p.startswith("127.0.0.1:"), f"Port mapping '{p}' must be bound to 127.0.0.1"

    # Volumes & Network
    assert "openwebui_data:/app/backend/data" in owui["volumes"]
    assert "ptb_network" in compose.get("networks", {})
    assert "ptb_network" in owui.get("networks", [])
    assert "ptb_network" in services["neo4j"].get("networks", [])


# ==============================================================================
# 2. PTB BOARD ARTIFACT TESTS (NO-MOCK & DEMO MODE)
# ==============================================================================
def test_ptb_board_html_structure_and_views():
    """Verify ptb_board.html has all 8 functional views and responsive/dark mode assets."""
    board_path = REPO_ROOT / "integrations" / "openwebui" / "board" / "ptb_board.html"
    assert board_path.exists(), "ptb_board.html must exist"

    content = board_path.read_text(encoding="utf-8")
    assert len(content) > 5000, "HTML artifact should be complete and non-trivial"

    # 8 Required Views from docs/v1.md & docs/v1_1.md
    required_views = [
        "view-today",
        "view-review",
        "view-tasks",
        "view-waiting",
        "view-forgotten",
        "view-decisions",
        "view-lessons",
        "view-health",
    ]
    for view_id in required_views:
        assert f'id="{view_id}"' in content, f"ptb_board.html must include {view_id}"

    # Verify Dark/Light Mode support
    assert "prefers-color-scheme" in content
    assert "html.dark" in content
    assert "toggleTheme" in content

    # Verify REST API endpoints
    assert "/api/today" in content
    assert "/api/review" in content
    assert "/api/tasks" in content
    assert "/api/waiting" in content
    assert "/api/forgotten" in content
    assert "/api/knowledge" in content
    assert "/api/sources/health" in content


def test_ptb_board_no_mock_fallback_and_demo_flag():
    """Verify ptb_board.html disables mock by default and strictly honors DEMO_MODE flag."""
    board_path = REPO_ROOT / "integrations" / "openwebui" / "board" / "ptb_board.html"
    content = board_path.read_text(encoding="utf-8")

    # 1. Exact DEMO_MODE definition
    demo_flag_snippet = "const DEMO_MODE = new URLSearchParams(window.location.search).has('demo') || window.PTB_DEMO_MODE === true;"
    assert demo_flag_snippet in content, "ptb_board.html must define DEMO_MODE flag correctly"

    # 2. CONFIG honors DEMO_MODE
    assert "isMockMode: DEMO_MODE" in content, "CONFIG.isMockMode must default to DEMO_MODE"
    assert 'apiBase: "http://127.0.0.1:8000"' in content, "CONFIG.apiBase must default to 127.0.0.1:8000"

    # 3. Production mode disallows mock fallback
    assert "PRODUCTION MODE: STRICTLY NO MOCK FALLBACK" in content


def test_ptb_board_error_banners_and_precise_messages():
    """Verify ptb_board.html includes error banner and the 3 required status messages."""
    board_path = REPO_ROOT / "integrations" / "openwebui" / "board" / "ptb_board.html"
    content = board_path.read_text(encoding="utf-8")

    # 1. Banner element presence
    assert 'id="globalSystemBanner"' in content, "Must have globalSystemBanner element"
    assert 'id="bannerMessage"' in content
    assert "system-banner" in content
    assert "banner-error" in content
    assert "banner-warning" in content

    # 2. Exactly 3 required error messages (Phase R11)
    msg_backend_offline = "BACKEND OFFLINE (Run 'ptb serve' or 'ptb run' and check port 8000)"
    msg_datastore_unavailable = "DATA STORE UNAVAILABLE (Neo4j connection error)"
    msg_microsoft_login = "MICROSOFT LOGIN REQUIRED (Run 'ptb login microsoft')"

    assert msg_backend_offline in content, f"Must include: {msg_backend_offline}"
    assert msg_datastore_unavailable in content, f"Must include: {msg_datastore_unavailable}"
    assert msg_microsoft_login in content, f"Must include: {msg_microsoft_login}"


# ==============================================================================
# 3. OPENWEBUI AUTO-INSTALLER TESTS
# ==============================================================================
@pytest.mark.asyncio
async def test_openwebui_installer_check_connection():
    """Verify OpenWebUIInstaller.check_connection against simulated responses."""
    installer = OpenWebUIInstaller(base_url="http://127.0.0.1:3000")

    # Case 1: OpenWebUI is online (HTTP 200)
    with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = httpx.Response(status_code=200, request=httpx.Request("GET", "http://127.0.0.1:3000"))
        is_online = await installer.check_connection("http://127.0.0.1:3000")
        assert is_online is True
        mock_get.assert_called_once()

    # Case 2: OpenWebUI is offline / Connection Refused
    with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = httpx.ConnectError("Connection refused")
        is_online = await installer.check_connection("http://127.0.0.1:3000")
        assert is_online is False

    # Case 3: Server error (HTTP 502 Bad Gateway)
    with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = httpx.Response(status_code=502, request=httpx.Request("GET", "http://127.0.0.1:3000"))
        is_online = await installer.check_connection("http://127.0.0.1:3000")
        assert is_online is False


@pytest.mark.asyncio
async def test_openwebui_installer_install_components_and_summary(tmp_path: Path):
    """Verify OpenWebUIInstaller copies components, exports schemas, and generates summary."""
    installer = OpenWebUIInstaller(base_url="http://127.0.0.1:3000", default_data_dir=tmp_path)

    # Initial summary before install
    initial_summary = installer.get_install_summary()
    assert "Chưa có thành phần nào" in initial_summary

    # Run installation into temporary directory
    result = await installer.install_components(openwebui_data_dir=tmp_path)
    assert result["status"] == "success"
    assert result["pinned_version"] == "v0.5.10"

    # Verify deployed files
    deployed_tools = tmp_path / "tools" / "ptb_tools.py"
    deployed_tools_meta = tmp_path / "tools" / "ptb_tools.json"
    deployed_action = tmp_path / "functions" / "ptb_board_action.py"
    deployed_action_meta = tmp_path / "functions" / "ptb_board_action.json"
    deployed_board = tmp_path / "artifacts" / "ptb_board.html"

    assert deployed_tools.exists() and deployed_tools.stat().st_size > 100
    assert deployed_tools_meta.exists()
    assert deployed_action.exists() and deployed_action.stat().st_size > 100
    assert deployed_action_meta.exists()
    assert deployed_board.exists() and deployed_board.stat().st_size > 5000

    # Verify install summary
    summary = installer.get_install_summary()
    assert "BÁO CÁO CÀI ĐẶT OPENWEBUI AUTO-INSTALLER" in summary
    assert "SUCCESS" in summary
    assert "v0.5.10" in summary
    assert "ptb_tools.py" in summary
    assert "ptb_board_action.py" in summary
    assert "ptb_board.html" in summary


# ==============================================================================
# 4. ACTION FUNCTION & PTB TOOLS INTEGRATION TESTS
# ==============================================================================
@pytest.mark.asyncio
async def test_ptb_board_action_function():
    """Verify OpenWebUI Action function can load board artifact and process shortcuts."""
    action = Action()

    # 1. Load HTML
    html = action.load_board_html()
    assert "<!DOCTYPE html>" in html
    assert "Personal Task Board" in html

    # 2. Format Artifact
    artifact = action.format_as_artifact(html)
    assert ':::artifact{type="text/html" title="Personal Task Board"}' in artifact

    # 3. Action button execution
    result = await action.action(body={"messages": []})
    assert result is not None
    assert "Personal Task Board" in result["content"]

    # 4. Prompt shortcut: /board
    emitted = []

    async def mock_emitter(event):
        emitted.append(event)

    body = {"messages": [{"role": "user", "content": "/board"}]}
    await action.inlet(body=body, __event_emitter__=mock_emitter)
    assert len(emitted) > 0
    assert ":::artifact" in emitted[0]["data"]["content"]

    # 5. Prompt shortcut: /board review
    emitted.clear()
    body_rev = {"messages": [{"role": "user", "content": "/board review"}]}
    await action.inlet(body=body_rev, __event_emitter__=mock_emitter)
    assert len(emitted) > 0
    assert "Hàng đợi duyệt" in emitted[0]["data"]["content"]

    # 6. Prompt shortcut: /board approve
    emitted.clear()
    body_app = {"messages": [{"role": "user", "content": "/board approve cand-101"}]}
    await action.inlet(body=body_app, __event_emitter__=mock_emitter)
    assert len(emitted) > 0
    assert "Đã phê duyệt" in emitted[0]["data"]["content"]


def test_ptb_tools():
    """Verify all 11 tool definitions in ptb_tools.py."""
    tools = Tools()

    # 1. Today tasks
    today_res = tools.get_today_tasks()
    assert "Kế hoạch hôm nay" in today_res
    assert "/100]" in today_res

    # 2. Get tasks
    tasks_res = tools.get_tasks()
    assert "Danh sách Task" in tasks_res

    # 3. Review queue
    review_res = tools.get_review_queue()
    assert "Hàng đợi duyệt" in review_res

    # 4. Approve task
    app_res = tools.approve_task("rev-cand-101")
    assert "Đã phê duyệt" in app_res

    # 5. Dismiss task
    dis_res = tools.dismiss_task("rev-cand-101")
    assert "Đã loại bỏ" in dis_res

    # 6. Update task status
    upd_res = tools.update_task_status("task-001", "IN_PROGRESS")
    assert "IN_PROGRESS" in upd_res

    invalid_res = tools.update_task_status("task-001", "INVALID_STATUS")
    assert "không hợp lệ" in invalid_res

    # 7. Waiting items
    wait_res = tools.get_waiting_items()
    assert "Waiting on Others" in wait_res

    # 8. Forgotten commitments
    forg_res = tools.get_forgotten_commitments()
    assert "Forgotten Commitments" in forg_res

    # 9. Search knowledge
    know_res = tools.search_knowledge("architecture")
    assert "Kết quả tra cứu" in know_res

    # 10. Source health
    health_res = tools.get_source_health()
    assert "Tình trạng Đồng bộ" in health_res

    # 11. Render board artifact
    art_res = tools.render_board_artifact()
    assert ":::artifact" in art_res
    assert "<!DOCTYPE html>" in art_res
