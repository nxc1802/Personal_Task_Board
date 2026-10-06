"""Unit and integration tests for OpenWebUI Integration & Board Artifact (Phase R11, R12, Wave 4).

Bao gồm:
- Docker Compose hardening (pin version v0.5.10, localhost 127.0.0.1 bind, ./data/openwebui:/app/backend/data mount).
- Volume Alignment: docker-compose.yml mount khớp với OpenWebUIInstaller.DEFAULT_DATA_DIR.
- PTB Board Artifact (Wave 4 Live-Only): 8 live views, no mock/demo fallback, error banners.
- OpenWebUI Auto-Installer: check_connection, install_components, filesystem contract tree (tools/, functions/, board/, ptb_manifest.json) không cần Docker, cross-platform pathlib.
- Action Function & OpenWebUI Tools definitions (Live-Only & offline fail-fast).
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch
import httpx
import pytest
import yaml

from integrations.openwebui.functions.ptb_board_action import Action
from integrations.openwebui.installer import (
    CONTAINER_DATA_DIR,
    DEFAULT_DATA_DIR,
    OpenWebUIInstaller,
    PINNED_OPENWEBUI_VERSION,
)
from integrations.openwebui.tools.ptb_tools import Tools

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent


# ==============================================================================
# 1. DOCKER COMPOSE HARDENING & VOLUME ALIGNMENT TESTS
# ==============================================================================
def test_docker_compose_configuration():
    """Verify docker-compose.yml pins OpenWebUI version, binds ports to 127.0.0.1, and uses host bind mount."""
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

    # 3. Direct bind mount & Network
    assert "./data/openwebui:/app/backend/data" in owui["volumes"], (
        "OpenWebUI service must bind mount ./data/openwebui:/app/backend/data"
    )
    top_volumes = compose.get("volumes") or {}
    assert "openwebui_data" not in top_volumes, (
        "Unused named volume 'openwebui_data' must be removed from volumes"
    )
    assert "ptb_network" in compose.get("networks", {})
    assert "ptb_network" in owui.get("networks", [])
    assert "ptb_network" in services["neo4j"].get("networks", [])


def test_docker_compose_volume_matches_installer_default_data_dir():
    """Verify docker-compose.yml mount ./data/openwebui:/app/backend/data aligns with OpenWebUIInstaller.DEFAULT_DATA_DIR."""
    assert DEFAULT_DATA_DIR == Path("./data/openwebui")
    assert OpenWebUIInstaller.DEFAULT_DATA_DIR == Path("./data/openwebui")
    assert OpenWebUIInstaller.CONTAINER_DATA_DIR == CONTAINER_DATA_DIR == "/app/backend/data"

    expected_mount = OpenWebUIInstaller.get_compose_volume_mount()
    assert expected_mount == "./data/openwebui:/app/backend/data"

    compose_path = REPO_ROOT / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    owui_volumes = compose["services"]["open-webui"]["volumes"]

    assert expected_mount in owui_volumes, (
        f"Expected volume mount '{expected_mount}' in open-webui volumes: {owui_volumes}"
    )

    # Cross-platform pathlib verification of host & container paths
    matching_entry = next(v for v in owui_volumes if v == expected_mount)
    host_part, container_part = matching_entry.split(":", 1)
    assert Path(host_part) == OpenWebUIInstaller.DEFAULT_DATA_DIR
    assert container_part == OpenWebUIInstaller.CONTAINER_DATA_DIR


# ==============================================================================
# 2. PTB BOARD ARTIFACT TESTS (WAVE 4 LIVE-ONLY)
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


def test_ptb_board_live_only_and_error_banners():
    """Verify ptb_board.html is configured for Live REST API (8000) and includes required system error banners."""
    board_path = REPO_ROOT / "integrations" / "openwebui" / "board" / "ptb_board.html"
    content = board_path.read_text(encoding="utf-8")

    # 1. CONFIG points to local Application Service
    assert 'apiBase: "http://127.0.0.1:8000"' in content, "CONFIG.apiBase must default to 127.0.0.1:8000"
    assert "isMockMode: DEMO_MODE" not in content, "CONFIG must not enable mock mode fallback"

    # 2. Banner element presence
    assert 'id="globalSystemBanner"' in content, "Must have globalSystemBanner element"
    assert 'id="bannerMessage"' in content
    assert "system-banner" in content
    assert "banner-error" in content
    assert "banner-warning" in content

    # 3. Required error/warning status messages
    msg_datastore_unavailable = "DATA STORE UNAVAILABLE (Neo4j connection error)"
    msg_microsoft_login = "MICROSOFT LOGIN REQUIRED (Run 'ptb login microsoft')"

    assert msg_datastore_unavailable in content, f"Must include: {msg_datastore_unavailable}"
    assert msg_microsoft_login in content, f"Must include: {msg_microsoft_login}"
    assert (
        "APPLICATION SERVICE OFFLINE" in content
        or "PTB-OWUI-001" in content
        or "BACKEND OFFLINE" in content
    ), "Must include explicit offline banner when Application Service is unreachable"


# ==============================================================================
# 3. OPENWEBUI AUTO-INSTALLER & FILESYSTEM CONTRACT TESTS
# ==============================================================================
@pytest.mark.asyncio
async def test_openwebui_installer_check_connection():
    """Verify OpenWebUIInstaller.check_connection against simulated responses."""
    installer = OpenWebUIInstaller(base_url="http://127.0.0.1:3000")

    # Case 1: OpenWebUI is online (HTTP 200)
    with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = httpx.Response(
            status_code=200,
            request=httpx.Request("GET", "http://127.0.0.1:3000"),
        )
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
        mock_get.return_value = httpx.Response(
            status_code=502,
            request=httpx.Request("GET", "http://127.0.0.1:3000"),
        )
        is_online = await installer.check_connection("http://127.0.0.1:3000")
        assert is_online is False


@pytest.mark.asyncio
async def test_openwebui_installer_install_components_and_summary(tmp_path: Path):
    """Verify OpenWebUIInstaller copies components, exports schemas & manifest, and generates summary."""
    installer = OpenWebUIInstaller(base_url="http://127.0.0.1:3000", default_data_dir=tmp_path)

    # Initial summary before install
    initial_summary = installer.get_install_summary()
    assert "Chưa có thành phần nào" in initial_summary

    # Run installation into temporary directory
    result = await installer.install_components(openwebui_data_dir=tmp_path)
    assert result["status"] == "success"
    assert result["success"] is True
    assert result["pinned_version"] == "v0.5.10"

    # Verify deployed files in tools/, functions/, board/, and root ptb_manifest.json
    deployed_tools = tmp_path / "tools" / "ptb_tools.py"
    deployed_tools_meta = tmp_path / "tools" / "ptb_tools.json"
    deployed_action = tmp_path / "functions" / "ptb_board_action.py"
    deployed_action_meta = tmp_path / "functions" / "ptb_board_action.json"
    deployed_board = tmp_path / "board" / "ptb_board.html"
    deployed_manifest = tmp_path / "ptb_manifest.json"

    assert deployed_tools.exists() and deployed_tools.stat().st_size > 100
    assert deployed_tools_meta.exists()
    assert deployed_action.exists() and deployed_action.stat().st_size > 100
    assert deployed_action_meta.exists()
    assert deployed_board.exists() and deployed_board.stat().st_size > 5000
    assert deployed_manifest.exists()

    # Verify SQLite database registration (webui.db)
    assert result.get("database_registered") is True
    assert "ptb_tools" in result.get("tools_registered", [])
    assert "ptb_board_action" in result.get("functions_registered", [])
    db_file = tmp_path / "webui.db"
    assert db_file.exists()

    import sqlite3
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM tool WHERE id = 'ptb_tools'")
    tool_row = cur.fetchone()
    assert tool_row is not None
    assert tool_row[0] == "ptb_tools"

    cur.execute("SELECT id, name, type, is_active, is_global FROM function WHERE id = 'ptb_board_action'")
    func_row = cur.fetchone()
    assert func_row is not None
    assert func_row[0] == "ptb_board_action"
    assert func_row[2] == "action"
    assert func_row[3] == 1
    assert func_row[4] == 1
    conn.close()

    # Verify install summary
    summary = installer.get_install_summary()
    assert "BÁO CÁO CÀI ĐẶT OPENWEBUI AUTO-INSTALLER" in summary
    assert "SUCCESS" in summary
    assert "v0.5.10" in summary
    assert "ptb_tools.py" in summary
    assert "ptb_board_action.py" in summary
    assert "ptb_board.html" in summary
    assert "ptb_manifest.json" in summary
    assert "PASSED (validation_ok=True)" in summary


@pytest.mark.asyncio
async def test_installer_validates_components_on_disk(tmp_path: Path):
    """Verify post-installation validation checks all components on disk, verifies file sizes > 0,
    and fails fast on missing or empty files."""
    installer = OpenWebUIInstaller(default_data_dir=tmp_path)
    result = await installer.install_components(openwebui_data_dir=tmp_path)

    # 1. Validation passed
    assert result["validation_ok"] is True
    assert result["status"] == "success"
    assert len(result["validated_files"]) == 6

    validated_paths = [vf["path"] for vf in result["validated_files"]]
    assert "tools/ptb_tools.py" in validated_paths
    assert "tools/ptb_tools.json" in validated_paths
    assert "functions/ptb_board_action.py" in validated_paths
    assert "functions/ptb_board_action.json" in validated_paths
    assert "board/ptb_board.html" in validated_paths
    assert "ptb_manifest.json" in validated_paths

    # Verify sizes > 0
    for vf in result["validated_files"]:
        assert vf["size_bytes"] > 0
        assert vf["status"] == "OK"

    # Verify summary reflects validation_ok
    summary = installer.get_install_summary()
    assert "PASSED (validation_ok=True)" in summary
    assert "tools/ptb_tools.py" in summary

    # 2. Test validation failure on missing file
    (tmp_path / "board" / "ptb_board.html").unlink()
    with pytest.raises(RuntimeError) as exc_info:
        installer.validate_installation(dest_dir=tmp_path, raise_on_error=True)
    assert "board/ptb_board.html" in str(exc_info.value)

    val_res = installer.validate_installation(dest_dir=tmp_path, raise_on_error=False)
    assert val_res["validation_ok"] is False
    assert "board/ptb_board.html" in val_res["missing_files"]

    # 3. Test validation failure on empty (0 byte) file
    (tmp_path / "board" / "ptb_board.html").write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError) as exc_info2:
        installer.validate_installation(dest_dir=tmp_path, raise_on_error=True)
    assert "board/ptb_board.html" in str(exc_info2.value)

    val_res_empty = installer.validate_installation(dest_dir=tmp_path, raise_on_error=False)
    assert val_res_empty["validation_ok"] is False
    assert "board/ptb_board.html" in val_res_empty["empty_files"]


@pytest.mark.asyncio
async def test_openwebui_installer_filesystem_contract_without_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Filesystem contract test: verify installer creates complete directory tree standalone without Docker."""
    target_dir = tmp_path / "openwebui_bind_mount"
    monkeypatch.setenv("OPENWEBUI_DATA_DIR", str(target_dir))

    installer = OpenWebUIInstaller()
    assert installer.default_data_dir == target_dir

    # Invoke via class-or-instance method without Docker running
    result = await OpenWebUIInstaller.install_components(openwebui_data_dir=target_dir)
    assert result["status"] == "success"
    assert Path(result["target_dir"]) == target_dir

    # 1. Verify required subdirectories exist
    for required_subdir in ("tools", "functions", "board"):
        subdir_path = target_dir / required_subdir
        assert subdir_path.exists() and subdir_path.is_dir(), (
            f"Directory '{required_subdir}/' must be created in target data directory"
        )

    # 2. Verify ptb_manifest.json contract and cross-platform POSIX relative paths
    manifest_path = target_dir / "ptb_manifest.json"
    assert manifest_path.exists(), "ptb_manifest.json must exist at root of target directory"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["pinned_version"] == PINNED_OPENWEBUI_VERSION
    assert manifest["default_data_dir"] == "./data/openwebui"
    assert manifest["container_data_dir"] == "/app/backend/data"
    assert manifest["compose_volume_mount"] == "./data/openwebui:/app/backend/data"
    assert manifest["directories"] == ["tools", "functions", "board"]

    # Every file listed in manifest["files"] must use POSIX separators and exist on disk
    for rel_file in manifest["files"]:
        assert "\\" not in rel_file, f"Manifest path '{rel_file}' must use cross-platform POSIX slashes"
        full_path = target_dir / Path(rel_file)
        assert full_path.exists() and full_path.is_file(), f"Manifest file missing on disk: {full_path}"

    # Verify container path alignment for ptb_board.html
    assert manifest["components"]["board"]["container_path"] == "/app/backend/data/board/ptb_board.html"


# ==============================================================================
# 4. ACTION FUNCTION & PTB TOOLS INTEGRATION TESTS (LIVE-ONLY & OFFLINE)
# ==============================================================================
@pytest.mark.asyncio
async def test_ptb_board_action_function():
    """Verify OpenWebUI Action function loads board artifact, handles live responses, and fails fast when offline."""
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

    # 5. Live API response for /board review & /board approve
    live_review_payload = [
        {
            "id": "rev-cand-101",
            "reason": "Confidence trung bình 0.58",
            "candidate_task": {
                "id": "cand-101",
                "title": "Kiểm tra log lỗi đồng bộ webhook Jira",
                "extraction_confidence": 0.58,
            },
        }
    ]
    with patch.object(action, "_http_request", return_value=live_review_payload):
        emitted.clear()
        body_rev = {"messages": [{"role": "user", "content": "/board review"}]}
        await action.inlet(body=body_rev, __event_emitter__=mock_emitter)
        assert len(emitted) > 0
        assert "Hàng đợi duyệt" in emitted[0]["data"]["content"]
        assert "rev-cand-101" in emitted[0]["data"]["content"]

    with patch.object(action, "_http_request", return_value={"status": "approved"}):
        emitted.clear()
        body_app = {"messages": [{"role": "user", "content": "/board approve cand-101"}]}
        await action.inlet(body=body_app, __event_emitter__=mock_emitter)
        assert len(emitted) > 0
        assert "Đã phê duyệt" in emitted[0]["data"]["content"]

    # 6. Offline fail-fast behavior (no fake success)
    if hasattr(action.valves, "enable_mock_fallback"):
        action.valves.enable_mock_fallback = False

    with patch.object(action, "_http_request", return_value=None):
        emitted.clear()
        body_app_offline = {"messages": [{"role": "user", "content": "/board approve cand-101"}]}
        await action.inlet(body=body_app_offline, __event_emitter__=mock_emitter)
        assert len(emitted) > 0
        offline_msg = emitted[0]["data"]["content"]
        assert "Đã phê duyệt" not in offline_msg
        assert (
            "APPLICATION SERVICE OFFLINE" in offline_msg
            or "PTB-OWUI-001" in offline_msg
            or "Không thể kết nối" in offline_msg
        )


@pytest.mark.asyncio
async def test_board_action_missing_file_raises_explicit_error(capsys):
    """Verify that when ptb_board.html is missing, Action logs PTB-OWUI-001 to stderr
    and returns an explicit text error message instead of synthetic HTML fallback."""
    action = Action()
    action.valves.board_html_path = "/nonexistent/custom/path/ptb_board.html"

    # 1. load_board_html() returns explicit text error
    html = action.load_board_html()
    assert "❌ [PTB-OWUI-001]" in html
    assert "Board HTML file not found" in html
    assert "Run 'ptb openwebui install'" in html
    assert "<!DOCTYPE html>" not in html
    assert "<html" not in html
    assert "<h2>Personal Task Board</h2>" not in html

    # Stderr check
    captured = capsys.readouterr()
    assert "[PTB-OWUI-001]" in captured.err

    # 2. action() returns explicit error message instead of artifact
    result = await action.action(body={"messages": []})
    assert result is not None
    assert "❌ [PTB-OWUI-001]" in result["content"]
    assert ":::artifact" not in result["content"]

    # 3. inlet /board shortcut returns explicit error without artifact wrapper
    emitted = []

    async def mock_emitter(event):
        emitted.append(event)

    body = {"messages": [{"role": "user", "content": "/board view"}]}
    await action.inlet(body=body, __event_emitter__=mock_emitter)
    assert len(emitted) > 0
    inlet_msg = emitted[0]["data"]["content"]
    assert "❌ [PTB-OWUI-001]" in inlet_msg
    assert ":::artifact" not in inlet_msg


def test_ptb_tools_live_and_offline():
    """Verify all 11 tool definitions in ptb_tools.py under Live API responses and Offline fail-fast."""
    tools = Tools()
    if hasattr(tools.valves, "enable_mock_fallback"):
        tools.valves.enable_mock_fallback = False

    # --- A. Offline Fail-Fast Verification (No Mock Data Returned) ---
    with patch.object(tools, "_http_call", return_value=None):
        offline_today = tools.get_today_tasks()
        assert "Investigate deployment failure on staging" not in offline_today

        offline_approve = tools.approve_task("rev-cand-101")
        assert "Đã phê duyệt thành công" not in offline_approve
        assert (
            "APPLICATION SERVICE OFFLINE" in offline_approve
            or "PTB-OWUI-001" in offline_approve
            or "Không thể kết nối" in offline_approve
        )

        offline_dismiss = tools.dismiss_task("rev-cand-101")
        assert "Đã loại bỏ review item" not in offline_dismiss
        assert (
            "APPLICATION SERVICE OFFLINE" in offline_dismiss
            or "PTB-OWUI-001" in offline_dismiss
            or "Không thể kết nối" in offline_dismiss
        )

        offline_status = tools.update_task_status("task-001", "IN_PROGRESS")
        assert "thành công" not in offline_status
        assert (
            "APPLICATION SERVICE OFFLINE" in offline_status
            or "PTB-OWUI-001" in offline_status
            or "Không thể" in offline_status
        )

    # --- B. Live API Responses Verification for All 11 Tools ---
    def fake_live_http_call(endpoint: str, method: str = "GET", payload=None, timeout: float = 3.5):
        if endpoint.startswith("/api/today"):
            return {
                "summary_headline": "Hôm nay có 1 việc ưu tiên cao trên hệ thống live.",
                "identified_risks": ["Rủi ro trễ hạn sprint"],
                "top_tasks": [
                    {
                        "task_id": "task-live-001",
                        "title": "Triển khai Neo4j single-store",
                        "status": "TODO",
                        "project_key": "PTB",
                        "requester_name": "Lead",
                        "due_date": "2026-09-30",
                        "priority": {
                            "total_score": 95.0,
                            "llm_explanation": "Blocker cho toàn bộ pipeline.",
                        },
                        "primary_evidence_snippet": "Cần hoàn thành Neo4j single-store hôm nay.",
                    }
                ],
            }
        if endpoint.startswith("/api/tasks") and method == "GET":
            return [
                {
                    "id": "task-live-001",
                    "title": "Triển khai Neo4j single-store",
                    "status": "TODO",
                    "project_key": "PTB",
                    "priority_score": 95.0,
                }
            ]
        if endpoint == "/api/review":
            return [
                {
                    "id": "rev-cand-101",
                    "reason": "Confidence 0.58",
                    "candidate_task": {
                        "id": "cand-101",
                        "title": "Kiểm tra webhook Jira",
                        "extraction_confidence": 0.58,
                        "evidences": [{"snippet": "Kiểm tra giúp webhook Jira."}],
                    },
                }
            ]
        if endpoint.endswith("/approve") or endpoint.endswith("/dismiss") or endpoint.endswith("/status"):
            return {"status": "ok"}
        if endpoint == "/api/waiting":
            return [
                {
                    "task_id": "task-live-002",
                    "title": "Đợi cấp quyền tenant",
                    "waiting_for_person_name": "Admin",
                    "waiting_days": 2,
                    "reason": "Chờ phê duyệt quyền truy cập.",
                }
            ]
        if endpoint == "/api/forgotten":
            return [
                {
                    "commitment_id": "comm-live-01",
                    "title": "Gửi báo cáo tuần",
                    "promised_to_name": "An",
                    "days_stale": 3,
                    "last_conversation_snippet": "Chiều mình gửi báo cáo nhé.",
                    "suggested_action": "Gửi ngay báo cáo tuần.",
                }
            ]
        if endpoint.startswith("/api/knowledge"):
            return {
                "decisions": [
                    {
                        "decision_id": "ADR-001",
                        "summary": "Neo4j-only Architecture",
                        "decided_by": "Architecture Lead",
                    }
                ],
                "lessons": [
                    {
                        "lesson_id": "LES-001",
                        "topic": "APOC Config",
                        "solution": "Allowlist apoc.*",
                    }
                ],
            }
        if endpoint == "/api/sources/health":
            return {
                "overall_health": "healthy",
                "tenants": [
                    {
                        "tenant_name": "Microsoft Teams",
                        "source_type": "ms_teams",
                        "status": "healthy",
                        "items_synced_total": 120,
                    }
                ],
            }
        return None

    with patch.object(tools, "_http_call", side_effect=fake_live_http_call):
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
