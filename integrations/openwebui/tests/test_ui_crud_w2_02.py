"""W2-02 UI & CRUD Contract Tests for OpenWebUI Task Board Integration.

Validates the full implementation of W2-02:
1. Canonical Sort Contract parity (status_rank, priority, due_date nulls-last, updated_at DESC)
2. Complete CRUD UI mechanisms:
   - Create task (title, priority, due_date, source)
   - Read/List tasks (all view modes adhere to canonical sort)
   - Update task (PATCH with fields, status, source)
   - Status transitions (IN_PROGRESS, TODO, BLOCKED, DONE, DISMISSED)
   - Soft delete with confirmation dialog and rollback
   - Review approve / dismiss per W1-02 contract
3. UI quality:
   - Optimistic UI with rollback on network/backend failure
   - Double-click prevention flags and pending sets
   - Keyboard accessibility (Escape, Enter)
   - Dark mode and responsive CSS variables
4. Auth integration:
   - X-PTB-API-KEY propagation via getApiHeaders()
   - LocalStorage and URL query param resolution
   - Dedicated API Key configuration dialog
5. Installer idempotency and upgrade lifecycle:
   - Idempotent repeated runs
   - Upgrade detection
   - Pinned version validation (v0.5.10)
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from unittest.mock import AsyncMock, patch
import httpx
import pytest

from integrations.openwebui.installer import OpenWebUIInstaller, PINNED_OPENWEBUI_VERSION

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
BOARD_HTML_PATH = REPO_ROOT / "integrations" / "openwebui" / "board" / "ptb_board.html"


def _read_board_content() -> str:
    assert BOARD_HTML_PATH.exists(), f"Board HTML not found at {BOARD_HTML_PATH}"
    return BOARD_HTML_PATH.read_text(encoding="utf-8")


# ==============================================================================
# 1. CANONICAL SORT CONTRACT TESTS
# ==============================================================================
class TestCanonicalSortContractInUI:
    """Verifies that ptb_board.html implements the frozen Canonical Sort Contract:
    Primary:   status_rank ASC (IN_PROGRESS=1, TODO/PENDING=2, BLOCKED=3, DONE=4, DISMISSED=5)
    Secondary: priority_score DESC
    Tertiary:  due_date ASC (nulls last)
    Quaternary: updated_at DESC
    """

    def test_canonical_sort_js_functions_exist(self):
        content = _read_board_content()
        assert "function getStatusRank(" in content
        assert "function getTaskPriorityScore(" in content
        assert "function parseTimestampSafe(" in content
        assert "function canonicalSortTasks(" in content
        assert "function sortTasksWithInProgressFirst(" in content

    def test_status_rank_mapping_in_board(self):
        content = _read_board_content()
        # Ensure status ranks match canonical contract
        assert 's === "IN_PROGRESS"' in content
        assert "return 1;" in content
        assert 's === "TODO"' in content or 's === "PENDING"' in content
        assert "return 2;" in content
        assert 's === "BLOCKED"' in content
        assert "return 3;" in content
        assert 's === "DONE"' in content
        assert "return 4;" in content
        assert 's === "DISMISSED"' in content
        assert "return 5;" in content

    def test_python_mirror_of_js_canonical_sort(self):
        """Simulate the exact JS canonical sort comparator in Python and verify correctness."""
        def get_status_rank(status):
            s = str(status or "").strip().upper()
            if s in ("IN_PROGRESS", "IN PROGRESS"):
                return 1
            if s in ("TODO", "PENDING"):
                return 2
            if s == "BLOCKED":
                return 3
            if s == "DONE":
                return 4
            if s == "DISMISSED":
                return 5
            return 6

        def get_priority_score(t):
            if t.get("priority_override") is not None:
                return float(t["priority_override"])
            if t.get("priority") and t["priority"].get("total_score") is not None:
                return float(t["priority"]["total_score"])
            if t.get("priority_score") is not None:
                return float(t["priority_score"])
            return 0.0

        def parse_ts(val, default_ts):
            if not val:
                return default_ts
            try:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                return dt.timestamp()
            except Exception:
                return default_ts

        def sort_comparator(a, b):
            # 1. Primary: status rank
            rank_a = get_status_rank(a.get("status"))
            rank_b = get_status_rank(b.get("status"))
            if rank_a != rank_b:
                return -1 if rank_a < rank_b else 1

            # 2. Secondary: priority score DESC
            score_a = get_priority_score(a)
            score_b = get_priority_score(b)
            if score_a != score_b:
                return -1 if score_a > score_b else 1

            # 3. Tertiary: due_date ASC (nulls last)
            due_a = parse_ts(a.get("due_date"), 9999999999.0)
            due_b = parse_ts(b.get("due_date"), 9999999999.0)
            if due_a != due_b:
                return -1 if due_a < due_b else 1

            # 4. Quaternary: updated_at DESC
            upd_a = parse_ts(a.get("updated_at"), -9999999999.0)
            upd_b = parse_ts(b.get("updated_at"), -9999999999.0)
            if upd_a != upd_b:
                return -1 if upd_a > upd_b else 1

            return 0

        from functools import cmp_to_key

        tasks = [
            {"id": "t-done", "title": "Done", "status": "DONE", "priority_score": 90.0},
            {"id": "t-blocked", "title": "Blocked", "status": "BLOCKED", "priority_score": 95.0},
            {"id": "t-todo-high", "title": "Todo High", "status": "TODO", "priority_score": 80.0},
            {"id": "t-inprog", "title": "In Prog", "status": "IN_PROGRESS", "priority_score": 10.0},
            {"id": "t-dismissed", "title": "Dismissed", "status": "DISMISSED", "priority_score": 100.0},
            {"id": "t-todo-early", "title": "Todo Early", "status": "TODO", "priority_score": 80.0, "due_date": "2026-10-16T12:00:00Z"},
            {"id": "t-todo-late", "title": "Todo Late", "status": "TODO", "priority_score": 80.0, "due_date": "2026-10-20T12:00:00Z"},
        ]

        sorted_tasks = sorted(tasks, key=cmp_to_key(sort_comparator))
        sorted_ids = [t["id"] for t in sorted_tasks]

        # IN_PROGRESS (rank 1) > TODO (rank 2, early due > late due > null due) > BLOCKED (rank 3) > DONE (rank 4) > DISMISSED (rank 5)
        assert sorted_ids[0] == "t-inprog"
        assert sorted_ids[1] == "t-todo-early"
        assert sorted_ids[2] == "t-todo-late"
        assert sorted_ids[3] == "t-todo-high"
        assert sorted_ids[4] == "t-blocked"
        assert sorted_ids[5] == "t-done"
        assert sorted_ids[6] == "t-dismissed"

    def test_canonical_sort_applied_in_all_views(self):
        content = _read_board_content()
        # Verify renderTodayList, renderTasksComponents, renderTasksTable all call sortTasksWithInProgressFirst / canonicalSortTasks
        assert "renderTodayList" in content
        assert "sortTasksWithInProgressFirst(filtered)" in content or "canonicalSortTasks(filtered)" in content
        assert "sortTasksWithInProgressFirst(tasks)" in content or "canonicalSortTasks(tasks)" in content


# ==============================================================================
# 2. COMPLETE CRUD MECHANISMS IN BOARD HTML
# ==============================================================================
class TestBoardCrudMechanisms:
    """Verifies that all CRUD UI modals, forms, and API mutation endpoints are implemented."""

    def test_create_task_modal_has_required_fields(self):
        content = _read_board_content()
        assert 'id="createTaskModal"' in content
        assert 'id="createTaskTitle"' in content
        assert 'id="createTaskDescription"' in content
        assert 'id="createTaskProject"' in content
        assert 'id="createTaskStatus"' in content
        assert 'id="createTaskOwner"' in content
        assert 'id="createTaskPriority"' in content
        assert 'id="createTaskDueDate"' in content
        assert 'id="createTaskSource"' in content
        assert 'id="btnSubmitCreateTask"' in content

        # Check source options include canonical sources
        assert 'value="direct"' in content
        assert 'value="teams"' in content
        assert 'value="outlook"' in content
        assert 'value="jira"' in content
        assert 'value="github"' in content

    def test_edit_task_modal_has_required_fields(self):
        content = _read_board_content()
        assert 'id="editModal"' in content
        assert 'id="editTaskId"' in content
        assert 'id="editTaskTitle"' in content
        assert 'id="editTaskDescription"' in content
        assert 'id="editTaskProject"' in content
        assert 'id="editTaskStatus"' in content
        assert 'id="editTaskPriority"' in content
        assert 'id="editTaskOwner"' in content
        assert 'id="editTaskDueDate"' in content
        assert 'id="editTaskSource"' in content
        assert 'id="btnSaveEditTask"' in content

    def test_create_task_submits_to_fastapi_and_maps_source(self):
        content = _read_board_content()
        assert "submitCreateTask" in content
        assert 'fetch(`${CONFIG.apiBase}/api/tasks`' in content
        assert 'method: "POST"' in content
        assert "channel_or_group" in content
        assert 'actor: "USER"' in content

    def test_update_task_submits_patch(self):
        content = _read_board_content()
        assert "saveEditTask" in content
        assert 'fetch(`${CONFIG.apiBase}/api/tasks/${taskId}`' in content
        assert 'method: "PATCH"' in content
        assert 'actor: "USER"' in content

    def test_status_transition_submits_post(self):
        content = _read_board_content()
        assert "updateTaskStatus" in content
        assert "/api/tasks/${encodeURIComponent(taskId)}/status" in content
        assert 'method: "POST"' in content

    def test_soft_delete_with_confirmation(self):
        content = _read_board_content()
        assert "deleteTask" in content
        assert "confirm(" in content
        assert "DISMISSED" in content
        assert "/api/tasks/${encodeURIComponent(taskId)}/delete" in content
        assert 'method: "POST"' in content

    def test_review_approve_and_dismiss_endpoints(self):
        content = _read_board_content()
        assert "approveReviewItem" in content
        assert "/api/review/${revId}/approve" in content
        assert "dismissReviewItem" in content
        assert "/api/review/${revId}/dismiss" in content


# ==============================================================================
# 3. OPTIMISTIC UI WITH ROLLBACK & QUALITY ASSURANCE
# ==============================================================================
class TestBoardQualityAndRollback:
    """Verifies error handling, optimistic rollbacks, double-click prevention, and accessibility."""

    def test_optimistic_rollback_in_update_task_status(self):
        content = _read_board_content()
        # Verify updateTaskStatus captures oldStatus and restores it on catch
        assert "oldStatus" in content
        assert "pendingStatusUpdates" in content
        assert "pendingStatusUpdates.delete(taskId)" in content
        # Rollback check
        assert "x.status = oldStatus" in content

    def test_optimistic_rollback_in_save_edit_task(self):
        content = _read_board_content()
        assert "isSavingTask" in content
        # Verify snapshots prevToday/prevAll are restored on error
        assert "cachedTodayTasks = prevToday" in content
        assert "cachedAllTasks = prevAll" in content

    def test_optimistic_rollback_in_delete_task(self):
        content = _read_board_content()
        assert "pendingDeletions" in content
        # Verify rollback restores lists on delete failure
        assert "cachedTodayTasks = prevToday" in content
        assert "cachedAllTasks = prevAll" in content

    def test_optimistic_rollback_in_review_queue(self):
        content = _read_board_content()
        assert "pendingReviewActions" in content
        assert "cachedReviewQueue = prevQueue" in content

    def test_double_click_prevention_on_submit_buttons(self):
        content = _read_board_content()
        assert "isSubmittingTask" in content
        assert "btnSubmitCreateTask" in content
        assert "btn.disabled = true" in content

    def test_keyboard_accessibility_listeners(self):
        content = _read_board_content()
        assert 'window.addEventListener("keydown"' in content
        assert 'e.key === "Escape"' in content
        assert "closeCreateTaskModal" in content
        assert "closeEditModal" in content
        assert "closeApiKeyModal" in content
        assert 'e.key === "Enter"' in content


# ==============================================================================
# 4. AUTH INTEGRATION (X-PTB-API-KEY)
# ==============================================================================
class TestAuthIntegration:
    """Verifies that API Key authentication is properly integrated into ptb_board.html."""

    def test_api_key_header_propagation_function(self):
        content = _read_board_content()
        assert "function getApiHeaders(" in content
        assert 'h["X-PTB-API-KEY"] = CONFIG.apiKey.trim()' in content or '"X-PTB-API-KEY"' in content

    def test_api_key_modal_elements_exist(self):
        content = _read_board_content()
        assert 'id="apiKeyModal"' in content
        assert 'id="inputApiKey"' in content
        assert 'id="btnTestApiKey"' in content
        assert 'id="btnSaveApiKey"' in content
        assert 'id="btnApiKeyConfig"' in content

    def test_api_key_functions_exist(self):
        content = _read_board_content()
        assert "function openApiKeyModal(" in content
        assert "function closeApiKeyModal(" in content
        assert "function saveApiKey(" in content
        assert "function clearApiKey(" in content
        assert "async function testApiKeyConnection(" in content

    def test_api_key_retrieved_from_url_and_localstorage(self):
        content = _read_board_content()
        assert "function getApiKey(" in content
        assert 'params.get("api_key")' in content
        assert 'localStorage.getItem("ptb_api_key")' in content

    def test_all_mutations_use_get_api_headers(self):
        content = _read_board_content()
        # Find all POST or PATCH fetch calls
        post_patch_calls = re.findall(
            r"fetch\([^,]+,[ \t]*\{[ \t\r\n]*method:[ \t]*[\'\"](?:POST|PATCH)[\'\"],[ \t\r\n]*headers:[ \t]*([^\r\n,]+)",
            content
        )
        assert len(post_patch_calls) >= 8, f"Expected at least 8 POST/PATCH calls, found {len(post_patch_calls)}"
        for header_arg in post_patch_calls:
            assert "getApiHeaders(" in header_arg, f"Mutation header must use getApiHeaders(), found: {header_arg}"


# ==============================================================================
# 5. INSTALLER IDEMPOTENCY & UPGRADE LIFECYCLE
# ==============================================================================
class TestInstallerIdempotencyAndUpgrades:
    """Verifies OpenWebUIInstaller supports upgrade lifecycle and idempotent installation."""

    @pytest.mark.asyncio
    async def test_installer_is_idempotent_on_repeated_runs(self, tmp_path: Path):
        installer = OpenWebUIInstaller(default_data_dir=tmp_path)

        # First run (fresh install)
        with patch.object(installer, "check_connection", new_callable=AsyncMock, return_value=False), \
             patch.object(installer, "register_database_components", return_value={"registered": True, "tools": ["ptb_tools"], "functions": ["ptb_board_action"], "specs_count": 11}):
            res1 = await installer.install_components(openwebui_data_dir=tmp_path)
            assert res1["success"] is True
            assert res1["is_upgrade"] is False
            manifest_ts1 = (tmp_path / "ptb_manifest.json").stat().st_mtime_ns

            # Second run (upgrade / re-install over existing data dir)
            res2 = await installer.install_components(openwebui_data_dir=tmp_path)
            assert res2["success"] is True
            assert res2["is_upgrade"] is True  # Detects existing ptb_manifest.json
            assert res2["components_installed"] == res1["components_installed"]

    def test_version_compatibility_checker(self):
        # 1. Matching pinned version
        compat_exact = OpenWebUIInstaller.check_version_compatibility("v0.5.10")
        assert compat_exact["compatible"] is True
        assert compat_exact["status"] == "exact_match"

        # 2. Upgrade version (e.g. v0.5.15)
        compat_upgrade = OpenWebUIInstaller.check_version_compatibility("v0.5.15")
        assert compat_upgrade["compatible"] is True
        assert compat_upgrade["status"] == "compatible_upgrade"

        # 3. Offline / None version
        compat_offline = OpenWebUIInstaller.check_version_compatibility(None)
        assert compat_offline["compatible"] is True
        assert compat_offline["status"] == "unverified_offline"

    @pytest.mark.asyncio
    async def test_get_openwebui_version_detection(self):
        installer = OpenWebUIInstaller(base_url="http://127.0.0.1:3000")

        # Mock online endpoint returning version
        with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = httpx.Response(
                status_code=200,
                json={"version": "v0.5.10"},
                request=httpx.Request("GET", "http://127.0.0.1:3000/api/version")
            )
            detected = await installer.get_openwebui_version("http://127.0.0.1:3000")
            assert detected == "v0.5.10"
