"""Unit tests for PTB CLI Process Supervisor & Cross-Platform Runtime (Phase R13, R14, R15, R16)."""

import argparse
import asyncio
from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from ptb_acquisition.adapters.agent_adapters import AgentWatchersAdapter
from ptb_acquisition.watchers.antigravity_watcher import AntigravityWatcher
from ptb_acquisition.watchers.base import BaseAgentWatcher
from ptb_acquisition.watchers.claude_code_watcher import ClaudeCodeWatcher
from ptb_acquisition.watchers.cursor_watcher import CursorWatcher
from ptb_contracts import AgentType
from scripts.ptb_cli import (
    PTBProcessSupervisor,
    cmd_doctor,
    cmd_login,
    cmd_openwebui,
    cmd_status,
)


# ==============================================================================
# 1. CROSS-PLATFORM WATCHER HARDENING TESTS
# ==============================================================================
def test_cross_platform_watchers_not_installed():
    """Verify watchers handle missing log directories safely with NOT_INSTALLED status."""
    non_existent_paths = ["/tmp/non_existent_cursor_storage_xyz_123", "/tmp/non_existent_cursor_storage_abc_456"]

    # 1. CursorWatcher
    cw = CursorWatcher(base_paths=non_existent_paths)
    assert cw.is_installed is False
    assert cw.status == "NOT_INSTALLED"
    sessions = cw.scan_sessions()
    assert sessions == [], "scan_sessions() must return empty list without throwing"

    # 2. ClaudeCodeWatcher
    clw = ClaudeCodeWatcher(base_paths=non_existent_paths)
    assert clw.is_installed is False
    assert clw.status == "NOT_INSTALLED"
    sessions_cl = clw.scan_sessions()
    assert sessions_cl == [], "scan_sessions() must return empty list without throwing"

    # 3. AntigravityWatcher
    agw = AntigravityWatcher(base_paths=non_existent_paths)
    assert agw.is_installed is False
    assert agw.status == "NOT_INSTALLED"
    sessions_ag = agw.scan_sessions()
    assert sessions_ag == [], "scan_sessions() must return empty list without throwing"


@pytest.mark.asyncio
async def test_agent_watchers_adapter_health_with_not_installed():
    """Verify AgentWatchersAdapter marks uninstalled watchers as NOT_INSTALLED."""
    dummy_paths = ["/path/does/not/exist/for/any/agent/12345"]
    watchers = [
        CursorWatcher(base_paths=dummy_paths),
        ClaudeCodeWatcher(base_paths=dummy_paths),
        AntigravityWatcher(base_paths=dummy_paths),
    ]
    adapter = AgentWatchersAdapter(watchers=watchers, tenant_id="test-tenant")

    # 1. Health report
    health = await adapter.health()
    assert health["source_type"] == "coding_agent"
    watchers_data = health["watchers"]
    assert watchers_data["cursor"]["status"] == "NOT_INSTALLED"
    assert watchers_data["cursor"]["installed"] is False
    assert watchers_data["claude_code"]["status"] == "NOT_INSTALLED"
    assert watchers_data["antigravity"]["status"] == "NOT_INSTALLED"

    # 2. Discover streams
    streams = await adapter.discover()
    for s in streams:
        if s["stream_id"] in ("cursor", "claude_code", "antigravity"):
            assert s["status"] == "NOT_INSTALLED"
            assert s["available"] is False

    # 3. Incremental polling does not crash
    polled = [record async for record in adapter.poll_incremental("all")]
    assert polled == []


# ==============================================================================
# 2. CLI COMMAND: DOCTOR TESTS
# ==============================================================================
@pytest.mark.asyncio
async def test_cmd_doctor_all_pass():
    """Verify cmd_doctor reports [✓] PASS when dependencies are operational."""
    args = argparse.Namespace()

    with patch("shutil.which", return_value="/usr/local/bin/docker"), \
         patch("subprocess.run") as mock_subproc, \
         patch("socket.socket") as mock_socket:

        # Mock docker info & docker ps
        mock_subproc.return_value = MagicMock(returncode=0, stdout="ptb_neo4j\n")

        # Mock socket connections (Neo4j Bolt 7687 and OpenWebUI 3000)
        sock_instance = MagicMock()
        sock_instance.connect.return_value = None
        mock_socket.return_value = sock_instance

        # Mock playwright chromium check
        with patch("os.path.exists", return_value=True):
            exit_code = await cmd_doctor(args)
            assert exit_code == 0


@pytest.mark.asyncio
async def test_cmd_doctor_with_failures():
    """Verify cmd_doctor returns exit code 1 when core dependencies fail."""
    args = argparse.Namespace()

    with patch("shutil.which", return_value=None), \
         patch("socket.socket") as mock_socket:

        sock_instance = MagicMock()
        sock_instance.connect.side_effect = ConnectionRefusedError("Connection refused")
        mock_socket.return_value = sock_instance

        exit_code = await cmd_doctor(args)
        assert exit_code == 1


# ==============================================================================
# 3. CLI COMMAND: OPENWEBUI INSTALL TESTS
# ==============================================================================
@pytest.mark.asyncio
async def test_cmd_openwebui_install(tmp_path: Path):
    """Verify ptb openwebui install runs installer and reports success."""
    args = argparse.Namespace(
        owui_action="install",
        url="http://127.0.0.1:3000",
        data_dir=str(tmp_path),
    )

    exit_code = await cmd_openwebui(args)
    assert exit_code == 0
    assert (tmp_path / "tools" / "ptb_tools.py").exists()
    assert (tmp_path / "functions" / "ptb_board_action.py").exists()
    assert (tmp_path / "artifacts" / "ptb_board.html").exists()


# ==============================================================================
# 4. CLI COMMAND: LOGIN MICROSOFT TESTS
# ==============================================================================
@pytest.mark.asyncio
async def test_cmd_login_microsoft():
    """Verify ptb login microsoft delegates to run_interactive_login."""
    args = argparse.Namespace(
        login_target="microsoft",
        service="teams",
        storage_path="/tmp/test_storage_state.json",
    )

    with patch("ptb_acquisition.playwright.login.run_interactive_login", new_callable=AsyncMock) as mock_login:
        exit_code = await cmd_login(args)
        assert exit_code == 0
        mock_login.assert_called_once_with(
            storage_path="/tmp/test_storage_state.json",
            service="teams",
        )


# ==============================================================================
# 5. CLI COMMAND: STATUS TESTS
# ==============================================================================
@pytest.mark.asyncio
async def test_cmd_status_reporting(capsys):
    """Verify ptb status outputs all 5 subsystems without crashing."""
    args = argparse.Namespace()
    exit_code = await cmd_status(args)
    assert exit_code == 0

    captured = capsys.readouterr().out
    assert "Neo4j Single-Store Database" in captured
    assert "Microsoft Session State" in captured
    assert "Coding Agent Watchers" in captured
    assert "Processing Queue Status" in captured
    assert "Other Ingestion Adapters" in captured


# ==============================================================================
# 6. PROCESS SUPERVISOR (ptb run) LIFECYCLE TESTS
# ==============================================================================
@pytest.mark.asyncio
async def test_process_supervisor_startup_and_graceful_shutdown(capsys):
    """Verify PTBProcessSupervisor starts services, passes health check, prints banner, and shuts down."""
    # Use distinct random high ports for unit testing
    app_port = 8123
    mcp_port = 8124

    supervisor = PTBProcessSupervisor(
        host="127.0.0.1",
        app_port=app_port,
        mcp_port=mcp_port,
        enable_playwright=False,
        poll_interval=0.5,
    )

    # Run supervisor with max_runtime=1.5s to let it bind, pass health checks, and shutdown
    exit_code = await supervisor.run(max_runtime=1.5)
    assert exit_code == 0

    captured = capsys.readouterr().out
    # Verify READY banner required by specification
    assert "READY: All PTB components operational!" in captured
    assert f"http://127.0.0.1:{app_port}" in captured
    assert f"http://127.0.0.1:{mcp_port}" in captured
    assert "ProcessingWorker Loop    : ACTIVE" in captured
    assert "Coding Agent Watchers    : ACTIVE" in captured
    assert "Đã tắt an toàn toàn bộ services." in captured


@pytest.mark.asyncio
async def test_process_supervisor_trigger_shutdown():
    """Verify supervisor triggers shutdown immediately upon signal or event."""
    supervisor = PTBProcessSupervisor(
        host="127.0.0.1",
        app_port=8135,
        mcp_port=8136,
        enable_playwright=False,
        poll_interval=0.5,
    )

    async def _stopper():
        await asyncio.sleep(0.8)
        supervisor.trigger_shutdown()

    stop_task = asyncio.create_task(_stopper())
    exit_code = await supervisor.run()
    await stop_task

    assert exit_code == 0
    assert supervisor._running is False
