"""Unit tests for PTB CLI Process Supervisor & Cross-Platform Runtime (Phase R13, R14, R15, R16)."""

import argparse
import asyncio
from pathlib import Path
import socket
import sys
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from ptb_acquisition.adapters.agent_adapters import AgentWatchersAdapter
from ptb_acquisition.watchers.antigravity_watcher import AntigravityWatcher
from ptb_acquisition.watchers.base import BaseAgentWatcher
from ptb_acquisition.watchers.claude_code_watcher import ClaudeCodeWatcher
from ptb_acquisition.watchers.cursor_watcher import CursorWatcher
from datetime import datetime, timezone
import uuid

from ptb_acquisition.adapters.base import AcquisitionAdapter
from ptb_contracts import AgentType, BugCode, ProcessingStatus, RawEventRecord, SourceType
from scripts.ptb_cli import (
    AdapterPollingRunner,
    PTBProcessSupervisor,
    cmd_doctor,
    cmd_login,
    cmd_openwebui,
    cmd_status,
)
from tests.support.test_doubles import (
    InMemoryCheckpointRepository,
    InMemoryRawEventRepository,
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

    mock_pw_cm = MagicMock()
    mock_p = MagicMock()
    mock_p.chromium.executable_path = "/usr/bin/chromium"
    mock_pw_cm.__aenter__ = AsyncMock(return_value=mock_p)
    mock_pw_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("shutil.which", return_value="/usr/local/bin/docker"), \
         patch("subprocess.run") as mock_subproc, \
         patch.object(socket.socket, "connect", return_value=None), \
         patch("playwright.async_api.async_playwright", return_value=mock_pw_cm), \
         patch("ptb_graph_memory.adapter.GraphitiAdapter.is_available", True):

        # Mock docker info & docker ps
        mock_subproc.return_value = MagicMock(returncode=0, stdout="ptb_neo4j\n")

        # Mock playwright chromium check
        with patch("os.path.exists", return_value=True):
            exit_code = await cmd_doctor(args)
            assert exit_code == 0


@pytest.mark.asyncio
async def test_cmd_doctor_with_failures():
    """Verify cmd_doctor returns exit code 1 when core dependencies fail."""
    args = argparse.Namespace()

    with patch("shutil.which", return_value=None), \
         patch.object(socket.socket, "connect", side_effect=ConnectionRefusedError("Connection refused")):

        exit_code = await cmd_doctor(args)
        assert exit_code == 1


# ==============================================================================
# 3. CLI COMMAND: OPENWEBUI INSTALL TESTS
# ==============================================================================
@pytest.mark.asyncio
async def test_cmd_openwebui_install(tmp_path: Path):
    """Verify ptb openwebui install runs installer and reports success when OpenWebUI DB is initialized."""
    import sqlite3
    db_file = tmp_path / "webui.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "CREATE TABLE tool (id TEXT PRIMARY KEY, user_id TEXT, name TEXT, content TEXT, specs TEXT, meta TEXT, created_at INTEGER, updated_at INTEGER)"
    )
    conn.execute(
        "CREATE TABLE function (id TEXT PRIMARY KEY, user_id TEXT, name TEXT, type TEXT, content TEXT, meta TEXT, is_active INTEGER, is_global INTEGER, created_at INTEGER, updated_at INTEGER)"
    )
    conn.commit()
    conn.close()

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


@pytest.mark.asyncio
async def test_cmd_openwebui_install_fresh_clone_exits_1(tmp_path: Path):
    """Verify ptb openwebui install exits 1 with warning when webui.db does not exist and API is offline."""
    args = argparse.Namespace(
        owui_action="install",
        url="http://127.0.0.1:3000",
        data_dir=str(tmp_path),
    )
    exit_code = await cmd_openwebui(args)
    assert exit_code == 1


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
        raw_event_repo=InMemoryRawEventRepository(),
        checkpoint_repo=InMemoryCheckpointRepository(),
        task_repo=MagicMock(),
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
        raw_event_repo=InMemoryRawEventRepository(),
        checkpoint_repo=InMemoryCheckpointRepository(),
        task_repo=MagicMock(),
    )

    async def _stopper():
        await asyncio.sleep(0.8)
        supervisor.trigger_shutdown()

    stop_task = asyncio.create_task(_stopper())
    exit_code = await supervisor.run()
    await stop_task

    assert exit_code == 0
    assert supervisor._running is False


@pytest.mark.asyncio
async def test_process_supervisor_fail_fast_when_neo4j_down(capsys):
    """Verify PTBProcessSupervisor fails fast with NOT_READY when Neo4j is unavailable."""
    with patch("ptb_database.neo4j_client.Neo4jClient.verify_connectivity", new_callable=AsyncMock) as mock_conn:
        mock_conn.return_value = False
        supervisor = PTBProcessSupervisor(
            host="127.0.0.1",
            app_port=8145,
            mcp_port=8146,
            enable_playwright=False,
        )
        exit_code = await supervisor.run(max_runtime=1.0)
        assert exit_code != 0
        assert supervisor.state == "NOT_READY"
        captured = capsys.readouterr().out
        assert "CRITICAL ERROR: Neo4j authoritative store unavailable" in captured


@pytest.mark.asyncio
async def test_cmd_ingest_fail_fast_when_neo4j_down(capsys):
    """Verify cmd_ingest fails fast with exit code != 0 when Neo4j is unavailable."""
    from scripts.ptb_cli import cmd_ingest
    args = argparse.Namespace(tenant_id="test-tenant", source="all")
    with patch("ptb_database.neo4j_client.Neo4jClient.verify_connectivity", new_callable=AsyncMock) as mock_conn:
        mock_conn.return_value = False
        exit_code = await cmd_ingest(args)
        #assert exit_code != 0
        assert exit_code != 0
        captured = capsys.readouterr().out
        assert "CRITICAL ERROR: Neo4j authoritative store unavailable" in captured


# ==============================================================================
# 7. WAVE 5D: CLI TRUTHFULNESS & DOCTOR DEEP CHECK TESTS
# ==============================================================================
@pytest.mark.asyncio
async def test_poll_health_url_rejects_not_ready_and_non_200():
    """Verify _poll_health_url returns False when /health returns status='not_ready' or HTTP != 200."""
    supervisor = PTBProcessSupervisor(enable_playwright=False)

    # 1. Endpoint returns HTTP 200 with {"status": "not_ready"} -> False
    mock_resp_not_ready = MagicMock()
    mock_resp_not_ready.status = 200
    mock_resp_not_ready.read.return_value = b'{"status": "not_ready", "neo4j": "not_ready"}'
    mock_resp_not_ready.__enter__.return_value = mock_resp_not_ready
    mock_resp_not_ready.__exit__.return_value = None

    with patch("urllib.request.urlopen", return_value=mock_resp_not_ready):
        assert await supervisor._poll_health_url("http://127.0.0.1:8000/health") is False

    # 2. Endpoint returns HTTP 503 -> False
    mock_resp_503 = MagicMock()
    mock_resp_503.status = 503
    mock_resp_503.read.return_value = b'{"status": "not_ready"}'
    mock_resp_503.__enter__.return_value = mock_resp_503
    mock_resp_503.__exit__.return_value = None

    with patch("urllib.request.urlopen", return_value=mock_resp_503):
        assert await supervisor._poll_health_url("http://127.0.0.1:8001/health") is False

    # 3. Endpoint returns HTTP 200 with {"status": "healthy"} -> True
    mock_resp_ok = MagicMock()
    mock_resp_ok.status = 200
    mock_resp_ok.read.return_value = b'{"status": "healthy"}'
    mock_resp_ok.__enter__.return_value = mock_resp_ok
    mock_resp_ok.__exit__.return_value = None

    with patch("urllib.request.urlopen", return_value=mock_resp_ok):
        assert await supervisor._poll_health_url("http://127.0.0.1:8000/health") is True

    # 4. Endpoint returns HTTP 200 with {"status": "degraded"} -> True
    mock_resp_deg = MagicMock()
    mock_resp_deg.status = 200
    mock_resp_deg.read.return_value = b'{"status": "degraded"}'
    mock_resp_deg.__enter__.return_value = mock_resp_deg
    mock_resp_deg.__exit__.return_value = None

    with patch("urllib.request.urlopen", return_value=mock_resp_deg):
        assert await supervisor._poll_health_url("http://127.0.0.1:8000/health") is True


@pytest.mark.asyncio
async def test_process_supervisor_ready_with_warnings_when_microsoft_auth_required(tmp_path: Path, capsys):
    """Verify PTBProcessSupervisor reaches READY_WITH_WARNINGS when Playwright is enabled but Microsoft session is missing."""
    missing_storage = str(tmp_path / "missing_storage_state.json")

    supervisor = PTBProcessSupervisor(
        host="127.0.0.1",
        app_port=8151,
        mcp_port=8152,
        enable_playwright=True,
        storage_path=missing_storage,
        poll_interval=0.5,
        raw_event_repo=InMemoryRawEventRepository(),
        checkpoint_repo=InMemoryCheckpointRepository(),
        task_repo=MagicMock(),
    )

    exit_code = await supervisor.run(max_runtime=1.2)
    assert exit_code == 0
    assert supervisor.state == "READY_WITH_WARNINGS"

    captured = capsys.readouterr().out
    assert (
        "READY_WITH_WARNINGS: Core services operational (Microsoft acquisition AUTH_REQUIRED — run 'ptb login microsoft')"
        in captured
    )


@pytest.mark.asyncio
async def test_process_supervisor_degraded_when_microsoft_strict_required(tmp_path: Path, capsys):
    """Verify PTBProcessSupervisor reaches DEGRADED when Microsoft strict_required=True and session is missing."""
    missing_storage = str(tmp_path / "missing_storage_state.json")

    supervisor = PTBProcessSupervisor(
        host="127.0.0.1",
        app_port=8153,
        mcp_port=8154,
        enable_playwright=True,
        storage_path=missing_storage,
        poll_interval=0.5,
        raw_event_repo=InMemoryRawEventRepository(),
        checkpoint_repo=InMemoryCheckpointRepository(),
        task_repo=MagicMock(),
        sources_config={"sources": {"ms_teams": {"enabled": True, "strict_required": True}}},
    )

    exit_code = await supervisor.run(max_runtime=1.2)
    assert exit_code == 0
    assert supervisor.state == "DEGRADED"


@pytest.mark.asyncio
async def test_process_supervisor_ready_when_valid_microsoft_session(tmp_path: Path, capsys):
    """Verify PTBProcessSupervisor reaches READY when Playwright is enabled and Microsoft session is valid."""
    import json

    valid_storage = tmp_path / "valid_storage_state.json"
    valid_storage.write_text(
        json.dumps({"cookies": [{"name": "ESTSAUTH", "value": "token", "expires": -1}]}),
        encoding="utf-8",
    )

    with patch("ptb_acquisition.playwright.runner.PlaywrightOrchestrator.start_interceptor", new_callable=AsyncMock):
        supervisor = PTBProcessSupervisor(
            host="127.0.0.1",
            app_port=8155,
            mcp_port=8156,
            enable_playwright=True,
            storage_path=str(valid_storage),
            poll_interval=0.5,
            raw_event_repo=InMemoryRawEventRepository(),
            checkpoint_repo=InMemoryCheckpointRepository(),
            task_repo=MagicMock(),
        )

        exit_code = await supervisor.run(max_runtime=1.2)
        assert exit_code == 0
        assert supervisor.state == "READY"
        captured = capsys.readouterr().out
        assert "READY: All PTB components operational!" in captured


@pytest.mark.asyncio
async def test_cmd_doctor_llm_readiness_and_deep_check(monkeypatch, capsys):
    """Verify cmd_doctor checks LLM Provider Readiness, logs PTB_LLM_001 when unconfigured, and supports --deep."""
    for key_var in ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "LLM_API_KEY"):
        monkeypatch.delenv(key_var, raising=False)

    mock_pw_cm = MagicMock()
    mock_p = MagicMock()
    mock_p.chromium.executable_path = "/usr/bin/chromium"
    mock_pw_cm.__aenter__ = AsyncMock(return_value=mock_p)
    mock_pw_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("shutil.which", return_value="/usr/local/bin/docker"), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="ptb_neo4j\n")), \
         patch.object(socket.socket, "connect", return_value=None), \
         patch("playwright.async_api.async_playwright", return_value=mock_pw_cm), \
         patch("ptb_graph_memory.adapter.GraphitiAdapter.is_available", True), \
         patch("os.path.exists", return_value=True):

        # 1. Missing API key -> logs PTB_LLM_001 and prints WARN details
        with patch("scripts.ptb_cli.log_bug") as mock_log_bug:
            args_no_key = argparse.Namespace(deep=False)
            exit_code = await cmd_doctor(args_no_key)
            assert exit_code == 0
            out = capsys.readouterr().out
            assert "LLM Provider Readiness" in out
            assert "PTB-LLM-001: API key unconfigured -> Processing DEGRADED/NOT_READY" in out
            assert any(
                call.kwargs.get("code") == BugCode.PTB_LLM_001
                or (call.args and call.args[0] == BugCode.PTB_LLM_001)
                for call in mock_log_bug.call_args_list
            )

        # 2. API key configured + --deep=True with /models returning 200 OK -> PASS
        monkeypatch.setenv("OPENAI_API_KEY", "sk-live-valid-test-key-12345")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")

        mock_models_resp = MagicMock()
        mock_models_resp.status = 200
        mock_models_resp.read.return_value = b'{"data": [{"id": "gpt-4o-mini"}]}'
        mock_models_resp.__enter__.return_value = mock_models_resp
        mock_models_resp.__exit__.return_value = None

        with patch("urllib.request.urlopen", return_value=mock_models_resp) as mock_urlopen:
            args_deep_ok = argparse.Namespace(deep=True)
            exit_code_deep = await cmd_doctor(args_deep_ok)
            assert exit_code_deep == 0
            out_deep = capsys.readouterr().out
            assert "LLM Provider Readiness" in out_deep
            assert "/models [OK]" in out_deep
            assert mock_urlopen.called

        # 3. API key configured + --deep=True when /models fails -> logs PTB_LLM_001 and warns
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")), \
             patch("scripts.ptb_cli.log_bug") as mock_log_bug_deep:
            args_deep_fail = argparse.Namespace(deep=True)
            exit_code_fail = await cmd_doctor(args_deep_fail)
            assert exit_code_fail == 0
            out_fail = capsys.readouterr().out
            assert "PTB-LLM-001: Deep check (/models) failed" in out_fail
            assert "Processing DEGRADED/NOT_READY" in out_fail
            assert any(
                call.kwargs.get("code") == BugCode.PTB_LLM_001
                or (call.args and call.args[0] == BugCode.PTB_LLM_001)
                for call in mock_log_bug_deep.call_args_list
            )


@pytest.mark.asyncio
async def test_process_supervisor_wires_and_stops_graph_sync_worker(capsys):
    """Verify PTBProcessSupervisor starts ptb-graph-sync-worker and terminates it cleanly without double close."""
    app_port = 8137
    mcp_port = 8138
    supervisor = PTBProcessSupervisor(
        host="127.0.0.1",
        app_port=app_port,
        mcp_port=mcp_port,
        enable_playwright=False,
        poll_interval=0.5,
        raw_event_repo=InMemoryRawEventRepository(),
        checkpoint_repo=InMemoryCheckpointRepository(),
        task_repo=MagicMock(),
    )

    exit_code = await supervisor.run(max_runtime=1.5)
    assert exit_code == 0

    # Verify graph sync worker was started and named task existed
    assert supervisor.sync_worker is not None
    assert supervisor.graph_sync_task is not None
    assert supervisor.graph_sync_task in supervisor.tasks
    assert supervisor.graph_sync_task.get_name() == "ptb-graph-sync-worker"

    # Verify banner outputs graph worker status
    captured = capsys.readouterr().out
    assert "Graph Memory Sync Worker :" in captured
    assert "Đã tắt an toàn toàn bộ services." in captured


@pytest.mark.asyncio
async def test_cmd_doctor_includes_graphiti_check(capsys):
    """Verify cmd_doctor performs check on Graphiti Semantic Memory layer."""
    args = argparse.Namespace(deep=False)
    exit_code = await cmd_doctor(args)
    captured = capsys.readouterr().out
    assert "Graphiti Semantic Memory" in captured
    assert exit_code in (0, 1)


