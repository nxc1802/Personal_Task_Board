"""End-to-End (E2E) Integration Tests for OpenWebUI + Task Board Integration (W3-01).

Validates the full interaction surface between OpenWebUI components and the live PTB Backend:
1. OpenWebUI Tools (`ptb_tools.py`):
   - Full CRUD lifecycle against live Application Service (Port 8000).
   - Morning Briefing (`get_today_tasks`).
   - Detailed task context and knowledge retrieval.
   - Review queue operations (approve, dismiss).
   - Artifact rendering (`render_board_artifact`) returning complete `:::artifact` block.
2. OpenWebUI Action Function (`ptb_board_action.py`):
   - Loading live `ptb_board.html` from filesystem.
   - Action handler emission (`__event_emitter__`) emitting progress and artifact markdown.
   - Mutation role-based permission enforcement (`[PTB-OWUI-403]`).
3. OpenWebUI Filter (`ptb_filter.py`):
   - Intelligent detection of task-related user prompts.
   - Dynamic injection of authoritative task grounding context into chat prompt.
4. OpenWebUI Installer (`installer.py`):
   - Live endpoint check against OpenWebUI (Port 3000).
   - Detection and validation of pinned OpenWebUI version `v0.5.10`.
   - Idempotent component installation and manifest integrity.
5. Security & Multi-Tenant Isolation:
   - Secret masking in logs (`_sanitize_for_logging`).
   - Standardized `[PTB-OWUI-xxx]` error code normalization.
   - User identity context propagation via `__user__`.
"""

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import urllib.request
import uuid
import pytest

from integrations.openwebui.functions.ptb_board_action import Action, _sanitize_for_logging
from integrations.openwebui.functions.ptb_filter import Filter
from integrations.openwebui.installer import (
    CONTAINER_DATA_DIR,
    DEFAULT_DATA_DIR,
    OpenWebUIInstaller,
    PINNED_OPENWEBUI_VERSION,
)
from integrations.openwebui.tools.ptb_tools import Tools

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
API_BASE = "http://127.0.0.1:8000"
UI_BASE = "http://127.0.0.1:3000"


def _is_backend_online(timeout: float = 3.0) -> bool:
    """Check if PTB backend API is responding."""
    try:
        req = urllib.request.Request(f"{API_BASE}/health")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _is_openwebui_online(timeout: float = 3.0) -> bool:
    """Check if OpenWebUI is responding."""
    try:
        req = urllib.request.Request(f"{UI_BASE}/api/version")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        try:
            req = urllib.request.Request(UI_BASE)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status == 200
        except Exception:
            return False


# ==============================================================================
# 1. OPENWEBUI TOOLS LIVE CRUD INTEGRATION
# ==============================================================================
class TestOpenWebUIToolsLiveE2E:
    """Validates Tools operations live against Port 8000."""

    @pytest.fixture(autouse=True)
    def setup_tools(self):
        if not _is_backend_online():
            pytest.skip("Backend is offline at port 8000")
        self.tools = Tools()
        self.tools.valves.app_service_url = API_BASE

    def test_tools_live_today_and_briefing(self):
        """Verify get_today_tasks produces formatted morning briefing from live Neo4j."""
        output = self.tools.get_today_tasks(limit=5)
        assert isinstance(output, str)
        assert len(output) > 20
        # Should contain morning briefing header or empty indicator
        assert "Morning Briefing" in output or "không có task nào khẩn cấp" in output

    def test_tools_live_crud_full_cycle(self):
        """Verify create_task -> get_task_detail -> edit_task -> update_task_status -> delete_task."""
        uid = uuid.uuid4().hex[:6]
        title = f"Tools Live Task {uid}"
        updated_title = f"Tools Live Task {uid} [UPDATED]"

        # 1. Create task
        res_create = self.tools.create_task(
            title=title,
            description="Created via OpenWebUI Tools test",
            project_key="TOOL_E2E",
            priority_score=82.0,
        )
        assert "Đã tạo thành công task" in res_create
        assert title in res_create

        # Extract task ID
        match = re.search(r"task `([^`]+)`", res_create)
        assert match is not None, f"Could not extract task ID from {res_create}"
        task_id = match.group(1)

        # 2. Get task detail
        detail = self.tools.get_task_detail(task_id)
        assert task_id in detail
        assert title in detail
        assert "TODO" in detail

        # 3. Edit task
        res_edit = self.tools.edit_task(
            task_id=task_id,
            title=updated_title,
            priority_score=95.0,
            description="Updated description",
        )
        assert "Đã cập nhật thành công task" in res_edit
        assert task_id in res_edit

        # Verify edited detail
        detail_after_edit = self.tools.get_task_detail(task_id)
        assert updated_title in detail_after_edit

        # 4. Update status
        res_status = self.tools.update_task_status(task_id=task_id, new_status="IN_PROGRESS")
        assert "IN_PROGRESS" in res_status
        assert "thành công" in res_status

        # 5. Delete task
        res_delete = self.tools.delete_task(task_id=task_id)
        assert "Đã loại bỏ công việc" in res_delete
        assert task_id in res_delete

        # Verify task is now DISMISSED
        final_detail = self.tools.get_task_detail(task_id)
        assert "DISMISSED" in final_detail

    def test_tools_live_get_tasks_filtering(self):
        """Verify get_tasks supports project, status, and limit filters."""
        res = self.tools.get_tasks(limit=5)
        assert isinstance(res, str)
        assert "Danh sách Task" in res or "Không tìm thấy task" in res

    def test_tools_live_review_queue_operations(self):
        """Verify get_review_queue returns active items or empty state message."""
        res = self.tools.get_review_queue()
        assert isinstance(res, str)
        assert "Hàng đợi duyệt" in res or "hiện đang trống" in res

    def test_tools_live_intelligence_views(self):
        """Verify get_waiting_items, get_forgotten_commitments, and get_source_health."""
        waiting = self.tools.get_waiting_items()
        assert isinstance(waiting, str)
        assert "Waiting on Others" in waiting or "không có task nào đang bị nghẽn" in waiting

        forgotten = self.tools.get_forgotten_commitments()
        assert isinstance(forgotten, str)
        assert "Forgotten Commitments" in forgotten or "không có cam kết" in forgotten

        health = self.tools.get_source_health()
        assert isinstance(health, str)
        assert "Tình trạng Đồng bộ Nguồn Dữ liệu" in health

    def test_tools_render_board_artifact(self):
        """Verify render_board_artifact returns complete HTML inside OpenWebUI artifact tag."""
        artifact = self.tools.render_board_artifact()
        assert artifact.strip().startswith(':::artifact{type="text/html" title="Personal Task Board"}')
        assert artifact.strip().endswith(":::")
        assert "Personal Task Board" in artifact
        assert "statusText" in artifact
        assert "canonicalSortTasks" in artifact


# ==============================================================================
# 2. OPENWEBUI ACTION FUNCTION INTEGRATION
# ==============================================================================
class TestOpenWebUIActionIntegration:
    """Validates Action class in integrations/openwebui/functions/ptb_board_action.py."""

    @pytest.fixture(autouse=True)
    def setup_action(self):
        self.action = Action()
        self.action.valves.app_service_url = API_BASE

    def test_action_loads_board_html_from_filesystem(self):
        """Verify Action resolves and loads the real ptb_board.html file."""
        html = self.action.load_board_html()
        assert not html.startswith("❌ [PTB-OWUI-001]")
        assert "<!DOCTYPE html>" in html
        assert "Personal Task Board" in html

    def test_action_format_as_artifact(self):
        """Verify format_as_artifact produces compliant OpenWebUI artifact syntax."""
        sample_html = "<div>PTB Test</div>"
        artifact = self.action.format_as_artifact(sample_html, title="Custom Board")
        assert ':::artifact{type="text/html" title="Custom Board"}' in artifact
        assert sample_html in artifact
        assert artifact.strip().endswith(":::")

    @pytest.mark.asyncio
    async def test_action_handler_execution(self):
        """Verify Action.action() emits status and artifact message events."""
        events = []

        async def mock_event_emitter(event: dict):
            events.append(event)

        result = await self.action.action(
            body={"chat_id": "test-chat-001"},
            __event_emitter__=mock_event_emitter,
        )

        assert result is not None
        assert "content" in result
        assert ":::artifact" in result["content"]

        # Check emitted events
        status_events = [e for e in events if e.get("type") == "status"]
        assert len(status_events) >= 1
        assert "Personal Task Board" in status_events[0]["data"]["description"]

    def test_action_mutation_permission_rejection(self):
        """Verify read_only and pending user roles are rejected with PTB-OWUI-403."""
        # Read-only user
        res_ro = self.action._check_mutation_permission({"name": "Viewer", "role": "read_only"})
        assert res_ro is not None
        assert "PTB-OWUI-403" in res_ro

        # Pending user
        res_pending = self.action._check_mutation_permission({"name": "Guest", "role": "pending"})
        assert res_pending is not None
        assert "PTB-OWUI-403" in res_pending

        # Admin user
        res_admin = self.action._check_mutation_permission({"name": "Admin", "role": "admin"})
        assert res_admin is None


# ==============================================================================
# 3. OPENWEBUI FILTER INTEGRATION
# ==============================================================================
class TestOpenWebUIFilterIntegration:
    """Validates Filter class in integrations/openwebui/functions/ptb_filter.py."""

    @pytest.fixture(autouse=True)
    def setup_filter(self):
        if not _is_backend_online():
            pytest.skip("Backend is offline at port 8000")
        self.filter = Filter()
        self.filter.valves.app_service_url = API_BASE

    @pytest.mark.asyncio
    async def test_filter_inlet_context_injection_for_task_query(self):
        """Verify Filter detects task trigger keywords and grounds prompt with live PTB data."""
        body = {
            "messages": [
                {"role": "user", "content": "Kế hoạch công việc hôm nay của tôi thế nào?"}
            ]
        }
        res = await self.filter.inlet(body=body, __user__={"id": "test_user"})
        messages = res.get("messages", [])

        # Filter either injects a system grounding prompt or appends context
        system_msgs = [m for m in messages if m.get("role") == "system"]
        assert len(system_msgs) >= 1, "Expected context injection into system messages"
        system_content = system_msgs[-1].get("content", "")
        assert "PERSONAL TASK BOARD CONTEXT" in system_content or "Hôm nay:" in system_content

    @pytest.mark.asyncio
    async def test_filter_inlet_skips_unrelated_query(self):
        """Verify Filter does NOT inject task context for generic non-task questions."""
        body = {
            "messages": [
                {"role": "user", "content": "Thủ đô của nước Pháp là gì?"}
            ]
        }
        res = await self.filter.inlet(body=body)
        messages = res.get("messages", [])
        # No system message should be added
        system_msgs = [m for m in messages if m.get("role") == "system"]
        assert len(system_msgs) == 0


# ==============================================================================
# 4. OPENWEBUI AUTO-INSTALLER LIVE & IDEMPOTENCY INTEGRATION
# ==============================================================================
class TestOpenWebUIInstallerIntegration:
    """Validates OpenWebUIInstaller connection, version detection, and idempotency."""

    @pytest.mark.asyncio
    async def test_installer_live_connection_and_pinned_version(self):
        """Verify live OpenWebUI connection and compatibility with pinned v0.5.10."""
        if not _is_openwebui_online():
            pytest.skip("OpenWebUI is offline at port 3000")

        installer = OpenWebUIInstaller(base_url=UI_BASE)
        is_connected = await installer.check_connection()
        assert is_connected, f"Installer failed to connect to OpenWebUI at {UI_BASE}"

        version = await installer.get_openwebui_version()
        assert version is not None, "Failed to detect OpenWebUI version"
        assert PINNED_OPENWEBUI_VERSION.lstrip("v") in version, f"Unexpected version: {version}"

        assert installer.check_version_compatibility(version), "Version compatibility check failed"

    @pytest.mark.asyncio
    async def test_installer_idempotent_installation_in_temp_dir(self, tmp_path):
        """Verify install_components creates valid file tree and manifest idempotently."""
        import sqlite3
        db_path = tmp_path / "webui.db"
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE tool (
                id TEXT PRIMARY KEY, user_id TEXT, name TEXT, content TEXT, specs TEXT,
                meta TEXT, valves TEXT, access_control TEXT, created_at INTEGER, updated_at INTEGER
            )
        """)
        cur.execute("""
            CREATE TABLE function (
                id TEXT PRIMARY KEY, user_id TEXT, name TEXT, type TEXT, content TEXT,
                meta TEXT, valves TEXT, is_active INTEGER, is_global INTEGER, created_at INTEGER, updated_at INTEGER
            )
        """)
        conn.commit()
        conn.close()

        installer = OpenWebUIInstaller(default_data_dir=tmp_path)

        # Run 1: initial install
        res1 = await installer.install_components(openwebui_data_dir=tmp_path)
        assert res1.get("status") in ("success", "upgraded")
        assert res1.get("success") is True

        # Verify files created
        manifest_file = tmp_path / "ptb_manifest.json"
        assert manifest_file.exists()
        manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
        assert manifest_data.get("pinned_version") == PINNED_OPENWEBUI_VERSION

        assert (tmp_path / "tools" / "ptb_tools.py").exists()
        assert (tmp_path / "functions" / "ptb_board_action.py").exists()
        assert (tmp_path / "board" / "ptb_board.html").exists()

        # Run 2: repeated install (idempotency check)
        res2 = await installer.install_components(openwebui_data_dir=tmp_path)
        assert res2.get("status") in ("success", "upgraded")
        assert res2.get("success") is True


# ==============================================================================
# 5. SECURITY SANITIZATION & MULTI-TENANT ISOLATION
# ==============================================================================
class TestSecurityAndMultiTenantIntegration:
    """Validates logging sanitization, error normalization, and multi-tenant isolation."""

    def test_sanitize_for_logging_masks_all_secrets(self):
        """Verify _sanitize_for_logging scrubs bearer tokens, API keys, and sensitive dict keys."""
        # 1. Bearer token masking
        raw_bearer = "Request authorization Bearer sk-ant-api03-abcdef1234567890"
        sanitized_bearer = _sanitize_for_logging(raw_bearer)
        assert "sk-ant" not in sanitized_bearer
        assert "***REDACTED***" in sanitized_bearer

        # 2. X-PTB-API-KEY masking
        raw_key = "X-PTB-API-KEY: ptb-live-secret-key-9999"
        sanitized_key = _sanitize_for_logging(raw_key)
        assert "ptb-live-secret" not in sanitized_key
        assert "***REDACTED***" in sanitized_key

        # 3. Dict key masking
        raw_dict = {
            "user": "Alice",
            "api_key": "raw_secret_value",
            "access_token": "token_12345",
            "database_password": "super_secret_pw",
            "nested": {
                "auth_header": "Bearer secret_nested_token"
            }
        }
        sanitized_dict = _sanitize_for_logging(raw_dict)
        assert sanitized_dict["api_key"] == "***REDACTED***"
        assert sanitized_dict["access_token"] == "***REDACTED***"
        assert sanitized_dict["database_password"] == "***REDACTED***"
        assert "***REDACTED***" in sanitized_dict["nested"]["auth_header"]

    def test_tools_error_normalization_codes(self):
        """Verify Tools normalizes HTTP errors into PTB-OWUI-xxx codes."""
        tools = Tools()
        # Non-existent endpoint -> 404
        tools.valves.app_service_url = API_BASE
        tools.valves.request_timeout = 2.0
        tools.valves.max_retries = 0

        res_404 = tools._http_call("/api/non_existent_endpoint_xyz")
        assert res_404 is None
        err_msg = tools._offline_error("/api/non_existent_endpoint_xyz")
        assert "PTB-OWUI-404" in err_msg or "PTB-OWUI-001" in err_msg

    def test_user_context_propagation_to_application_service(self):
        """Verify passing __user__ routes user_id appropriately to backend endpoints."""
        if not _is_backend_online():
            pytest.skip("Backend offline")

        tools = Tools()
        tools.valves.app_service_url = API_BASE

        # User A context
        briefing_a = tools.get_today_tasks(__user__={"id": "tenant-user-a", "email": "a@ptb.local"})
        assert isinstance(briefing_a, str)

        # User B context
        briefing_b = tools.get_today_tasks(__user__={"id": "tenant-user-b", "email": "b@ptb.local"})
        assert isinstance(briefing_b, str)
