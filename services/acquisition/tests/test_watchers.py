"""Tests for Layer 1B Coding Agent Watchers."""

import json
import os
import sqlite3
import tempfile
import pytest

from ptb_contracts import AgentType, RawAgentSessionRecord
from ptb_acquisition.queue import LocalIngestionQueue
from ptb_acquisition.watchers import (
    AgentTurnFilter,
    AntigravityWatcher,
    BaseAgentWatcher,
    ClaudeCodeWatcher,
    CursorWatcher,
    TurnClassification,
)


def test_turn_filter_classification():
    # 1. Decision
    tags = AgentTurnFilter.classify_turn("Chúng ta quyết định sử dụng Neo4j thay vì PostgreSQL để tối ưu hóa.")
    assert TurnClassification.IS_DECISION in tags

    # 2. Bug Fix
    tags = AgentTurnFilter.classify_turn("Fixed root cause: timeout error in token refresh function.")
    assert TurnClassification.IS_BUG_FIX in tags

    # 3. Task Promise
    tags = AgentTurnFilter.classify_turn("Todo: Em sẽ làm module export này trước 5h chiều.")
    assert TurnClassification.IS_TASK_PROMISE in tags

    # 4. Chatter
    tags = AgentTurnFilter.classify_turn("Cảm ơn anh nhiều nhé, chúc anh ngày mới tốt lành!")
    assert len(tags) == 0
    assert not AgentTurnFilter.is_valuable_turn("Chào buổi sáng cả nhóm!")


def test_cursor_watcher_sqlite_extraction():
    with tempfile.TemporaryDirectory() as tmp_dir:
        ws_dir = os.path.join(tmp_dir, "workspace_123")
        os.makedirs(ws_dir)
        db_path = os.path.join(ws_dir, "state.vscdb")

        # Tạo database giả lập của Cursor
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")

        mock_chatdata = {
            "tabs": [
                {
                    "tabId": "tab-abc",
                    "bubbles": [
                        {
                            "type": "user",
                            "rawText": "Làm thế nào để refactor schema sang Neo4j?",
                            "timestamp": 1727390000000,
                        },
                        {
                            "type": "ai",
                            "text": "Chúng ta quyết định bỏ PostgreSQL và chuyển sang Single-Store Neo4j.",
                            "timestamp": 1727390005000,
                        },
                    ],
                }
            ]
        }

        cur.execute(
            "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
            ("workbench.panel.aichat.chatdata", json.dumps(mock_chatdata)),
        )
        conn.commit()
        conn.close()

        watcher = CursorWatcher(base_paths=[tmp_dir])
        records = watcher.scan_sessions()

        assert len(records) == 2
        assert records[0].agent_type == AgentType.CURSOR
        assert records[0].message_role == "user"
        assert "refactor schema" in records[0].content
        assert records[1].message_role == "assistant"
        assert "Single-Store Neo4j" in records[1].content
        assert records[0].idempotency_key != records[1].idempotency_key


def test_claude_code_watcher_extraction():
    with tempfile.TemporaryDirectory() as tmp_dir:
        transcript_file = os.path.join(tmp_dir, "session-001.jsonl")
        with open(transcript_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "role": "user",
                "content": "Hãy phân tích log lỗi crash server.",
                "created_at": "2026-09-27T01:00:00Z"
            }) + "\n")
            f.write(json.dumps({
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Root cause là do thiếu index trên trường external_id."},
                    {"type": "tool_use", "name": "run_command"}
                ],
                "created_at": "2026-09-27T01:00:05Z"
            }) + "\n")

        watcher = ClaudeCodeWatcher(base_paths=[tmp_dir])
        records = watcher.scan_sessions()

        assert len(records) == 2
        assert records[0].agent_type == AgentType.CLAUDE_CODE
        assert records[0].message_role == "user"
        assert "lỗi crash" in records[0].content
        assert records[1].message_role == "assistant"
        assert "Root cause" in records[1].content


def test_antigravity_watcher_extraction():
    with tempfile.TemporaryDirectory() as tmp_dir:
        session_id = "test-conv-999"
        logs_dir = os.path.join(tmp_dir, session_id, ".system_generated", "logs")
        os.makedirs(logs_dir)
        transcript_file = os.path.join(logs_dir, "transcript.jsonl")

        with open(transcript_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "step_index": 1,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "content": "Cần sửa lỗi đồng bộ giữa outbox và database.",
            }) + "\n")
            f.write(json.dumps({
                "step_index": 2,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "content": "Tôi sẽ triển khai Neo4j Single Store để loại bỏ hoàn toàn outbox.",
                "tool_calls": [{"name": "write_to_file"}]
            }) + "\n")

        watcher = AntigravityWatcher(base_paths=[tmp_dir])
        records = watcher.scan_sessions()

        assert len(records) == 2
        assert records[0].agent_type == AgentType.ANTIGRAVITY
        assert records[0].message_role == "user"
        assert records[1].message_role == "assistant"
        assert records[1].tool_invocations is not None
        assert len(records[1].tool_invocations) == 1


@pytest.mark.asyncio
async def test_local_ingestion_queue_deduplication():
    queue = LocalIngestionQueue()

    record1 = RawAgentSessionRecord(
        session_id="s1",
        agent_type=AgentType.CURSOR,
        workspace_path="/ws",
        turn_index=0,
        message_role="user",
        content="Hello world",
        idempotency_key="hash-duplicate-test-123",
        timestamp="2026-09-27T00:00:00Z"
    )

    record2 = RawAgentSessionRecord(
        session_id="s1",
        agent_type=AgentType.CURSOR,
        workspace_path="/ws",
        turn_index=0,
        message_role="user",
        content="Hello world (dup)",
        idempotency_key="hash-duplicate-test-123",
        timestamp="2026-09-27T00:00:00Z"
    )

    pushed1 = await queue.put(record1)
    pushed2 = await queue.put(record2)

    assert pushed1 is True
    assert pushed2 is False
    assert queue.qsize == 1
    assert queue.stats["deduplicated"] == 1

    item = await queue.get()
    assert item.session_id == "s1"
    assert queue.stats["consumed"] == 1


# =========================================================================
# Sub-Agent 3D: Cross-Platform Coding Agent Watchers Audit & Safety Tests
# =========================================================================

from ptb_acquisition.watchers import (
    ALL_WATCHER_CLASSES,
    AiderWatcher,
    ClineWatcher,
    CodexWatcher,
    ContinueWatcher,
    CopilotWatcher,
    WindsurfWatcher,
)


def test_all_watchers_missing_directory_safe():
    """Tất cả các watchers khi không tìm thấy thư mục phải trả về NOT_INSTALLED và scan_sessions() == []."""
    for watcher_cls in ALL_WATCHER_CLASSES:
        # 1. Nonexistent path được chỉ định
        w_missing = watcher_cls(base_paths=["/path/to/definitely/nonexistent/agent_dir_xyz_123"])
        assert w_missing.is_installed is False, f"{watcher_cls.__name__} should have is_installed=False for missing dir"
        assert w_missing.status == "NOT_INSTALLED", f"{watcher_cls.__name__} status should be NOT_INSTALLED"
        sessions = w_missing.scan_sessions()
        assert sessions == [], f"{watcher_cls.__name__} scan_sessions() must return [] safely when not installed"

        # 2. Empty base_paths
        w_empty = watcher_cls(base_paths=[])
        assert w_empty.is_installed is False
        assert w_empty.status == "NOT_INSTALLED"
        assert w_empty.scan_sessions() == []


def test_all_watchers_default_paths_exist_check_safe(monkeypatch):
    """Khi tất cả đường dẫn mặc định không tồn tại trên máy người dùng, watcher đánh dấu NOT_INSTALLED."""
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    monkeypatch.setattr(os.path, "isdir", lambda p: False)
    monkeypatch.setattr(os.path, "isfile", lambda p: False)

    for watcher_cls in ALL_WATCHER_CLASSES:
        w = watcher_cls()
        assert w.is_installed is False, f"{watcher_cls.__name__} should be NOT_INSTALLED when dirs do not exist"
        assert w.status == "NOT_INSTALLED"
        assert w.scan_sessions() == []


def test_cross_platform_platformdirs_resolution(monkeypatch):
    """Kiểm tra BaseAgentWatcher.resolve_platform_paths trên các hệ điều hành mô phỏng (Windows, macOS, Linux)."""
    # Đảm bảo môi trường kiểm thử không bị ảnh hưởng bởi các thư mục cài đặt thực tế trên máy host
    monkeypatch.setattr(os.path, "exists", lambda p: False)

    # 1. macOS (darwin)
    monkeypatch.setattr("sys.platform", "darwin")
    mac_paths = BaseAgentWatcher.resolve_platform_paths(["Cursor"], sub_path="User/workspaceStorage")
    assert any("Library" in p and "Application Support" in p for p in mac_paths)

    # 2. Windows (win32)
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("APPDATA", "C:\\Users\\MockUser\\AppData\\Roaming")
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\MockUser\\AppData\\Local")
    monkeypatch.setenv("USERPROFILE", "C:\\Users\\MockUser")
    win_paths = BaseAgentWatcher.resolve_platform_paths(["Cursor"], sub_path="User/workspaceStorage")
    assert any("AppData" in p or "MockUser" in p for p in win_paths)

    # 3. Linux
    monkeypatch.setattr("sys.platform", "linux")
    linux_paths = BaseAgentWatcher.resolve_platform_paths(["Cursor"], sub_path="User/workspaceStorage")
    assert any(".config" in p or ".local" in p for p in linux_paths)


def test_cross_platform_home_paths_resolution(monkeypatch):
    """Kiểm tra BaseAgentWatcher.resolve_home_paths cho các dot-folders (.claude, .codex, .continue, .aider)."""
    # Windows
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("USERPROFILE", "C:\\Users\\MockUser")
    monkeypatch.setenv("APPDATA", "C:\\Users\\MockUser\\AppData\\Roaming")
    codex_win = BaseAgentWatcher.resolve_home_paths(".codex", sub_path="sessions", app_names=["codex"])
    assert any("codex" in p.lower() for p in codex_win)

    # macOS
    monkeypatch.setattr("sys.platform", "darwin")
    codex_mac = BaseAgentWatcher.resolve_home_paths(".codex", sub_path="sessions", app_names=["codex"])
    assert any(".codex" in p for p in codex_mac)

    # Linux
    monkeypatch.setattr("sys.platform", "linux")
    continue_linux = BaseAgentWatcher.resolve_home_paths(".continue", sub_path="sessions", app_names=["continue"])
    assert any(".continue" in p or "continue" in p for p in continue_linux)


def test_all_watchers_get_default_paths_valid():
    """Tất cả các watcher phải trả về danh sách đường dẫn ứng viên hợp lệ, không rỗng."""
    for watcher_cls in ALL_WATCHER_CLASSES:
        w = watcher_cls()
        paths = w.get_default_paths()
        assert isinstance(paths, list), f"{watcher_cls.__name__} get_default_paths() must return a list"
        assert len(paths) > 0, f"{watcher_cls.__name__} should return candidate paths"
        for p in paths:
            assert isinstance(p, str) and len(p.strip()) > 0


def test_codex_watcher_extraction():
    """Kiểm tra trích xuất session từ file rollout JSONL của Codex."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        rollout_file = os.path.join(tmp_dir, "rollout-2026-09-28-auth-fix.jsonl")
        with open(rollout_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": "Hãy viết script tự động hóa kiểm tra bảo mật API.",
                },
                "timestamp": "2026-09-28T12:00:00Z"
            }) + "\n")
            f.write(json.dumps({
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": "Tôi sẽ triển khai module kiểm thử bảo mật với OWASP ZAP.",
                },
                "timestamp": "2026-09-28T12:00:10Z"
            }) + "\n")

        watcher = CodexWatcher(base_paths=[tmp_dir])
        assert watcher.is_installed is True
        assert watcher.status == "AVAILABLE"
        records = watcher.scan_sessions()

        assert len(records) == 2
        assert records[0].agent_type == AgentType.CODEX
        assert records[0].message_role == "user"
        assert "bảo mật API" in records[0].content
        assert records[1].message_role == "assistant"
        assert "OWASP ZAP" in records[1].content
        assert records[0].idempotency_key != records[1].idempotency_key


def test_copilot_watcher_extraction():
    """Kiểm tra trích xuất chat từ state.vscdb của GitHub Copilot."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        ws_dir = os.path.join(tmp_dir, "ws_vscode_copilot")
        os.makedirs(ws_dir)
        db_path = os.path.join(ws_dir, "state.vscdb")

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")

        copilot_data = {
            "entries": {
                "sess-copilot-001": {
                    "title": "Tối ưu hóa truy vấn Cypher trong Neo4j",
                }
            },
            "inputValue": "Làm sao để tạo index composite trong Cypher?"
        }

        cur.execute(
            "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
            ("interactive-session.copilot", json.dumps(copilot_data)),
        )
        conn.commit()
        conn.close()

        watcher = CopilotWatcher(base_paths=[tmp_dir])
        assert watcher.is_installed is True
        assert watcher.status == "AVAILABLE"
        records = watcher.scan_sessions()

        assert len(records) >= 1
        contents = [r.content for r in records]
        assert any("Tối ưu hóa truy vấn Cypher" in c for c in contents)
        assert any(r.agent_type == AgentType.GITHUB_COPILOT for r in records)


def test_windsurf_watcher_extraction():
    """Kiểm tra trích xuất session từ Windsurf (Codeium Cascade)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        # 1. SQLite state.vscdb
        ws_dir = os.path.join(tmp_dir, "ws_windsurf")
        os.makedirs(ws_dir)
        db_path = os.path.join(ws_dir, "state.vscdb")

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")

        cascade_data = {
            "conversation": "Cascade đề xuất refactor toàn bộ authentication service",
            "model": "claude-3-5-sonnet"
        }
        cur.execute(
            "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
            ("codeium.cascade.history", json.dumps(cascade_data)),
        )
        conn.commit()
        conn.close()

        watcher = WindsurfWatcher(base_paths=[tmp_dir])
        assert watcher.is_installed is True
        assert watcher.status == "AVAILABLE"
        records = watcher.scan_sessions()

        assert len(records) >= 1
        assert records[0].agent_type == AgentType.WINDSURF
        assert "Cascade" in records[0].content


def test_continue_watcher_extraction():
    """Kiểm tra trích xuất session từ Continue.dev JSON files."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        session_file = os.path.join(tmp_dir, "continue_session_001.json")
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump({
                "sessionId": "cont-sess-456",
                "workspaceDirectory": "/my/project",
                "history": [
                    {"role": "user", "content": "Tạo kịch bản backup tự động cho database."},
                    {"role": "assistant", "content": "Đã tạo backup script crontab mẫu."},
                ]
            }, f)

        watcher = ContinueWatcher(base_paths=[tmp_dir])
        assert watcher.is_installed is True
        assert watcher.status == "AVAILABLE"
        records = watcher.scan_sessions()

        assert len(records) == 2
        assert records[0].agent_type == AgentType.CONTINUE
        assert records[0].message_role == "user"
        assert "backup tự động" in records[0].content
        assert records[1].message_role == "assistant"
        assert "backup script" in records[1].content


def test_aider_watcher_extraction():
    """Kiểm tra trích xuất session từ file .aider.chat.history.md."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        history_file = os.path.join(tmp_dir, ".aider.chat.history.md")
        with open(history_file, "w", encoding="utf-8") as f:
            f.write("#### Cần kiểm tra unit test của module acquisition\n\n")
            f.write("> Đang chạy pytest trên toàn bộ test suite...\n\n")
            f.write("Đã hoàn tất 100% tests thành công.\n")

        watcher = AiderWatcher(base_paths=[tmp_dir])
        assert watcher.is_installed is True
        assert watcher.status == "AVAILABLE"
        records = watcher.scan_sessions()

        assert len(records) >= 1
        assert records[0].agent_type == AgentType.AIDER
        assert any("kiểm tra unit test" in r.content for r in records)


def test_cline_watcher_extraction():
    """Kiểm tra trích xuất session từ extension Cline / Roo Code."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        task_dir = os.path.join(tmp_dir, "task-cline-789")
        os.makedirs(task_dir)
        hist_file = os.path.join(task_dir, "api_conversation_history.json")
        with open(hist_file, "w", encoding="utf-8") as f:
            json.dump([
                {"role": "user", "content": "Sửa lỗi crash khi không có internet.", "ts": 1727390000000},
                {"role": "assistant", "content": "Đã thêm try-catch và retry policy.", "ts": 1727390010000},
            ], f)

        watcher = ClineWatcher(base_paths=[tmp_dir])
        assert watcher.is_installed is True
        assert watcher.status == "AVAILABLE"
        records = watcher.scan_sessions()

        assert len(records) == 2
        assert records[0].agent_type == AgentType.CLINE
        assert records[0].message_role == "user"
        assert "Sửa lỗi crash" in records[0].content
        assert records[1].message_role == "assistant"
        assert "retry policy" in records[1].content


def test_watchers_error_resilience():
    """Kiểm tra các watchers không crash khi gặp file SQLite bị hỏng hoặc JSONL dị tật."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        # File rác không phải SQLite
        corrupt_db = os.path.join(tmp_dir, "ws_corrupt")
        os.makedirs(corrupt_db)
        with open(os.path.join(corrupt_db, "state.vscdb"), "w") as f:
            f.write("not a sqlite database at all")

        # File JSONL bị hỏng một số dòng
        corrupt_jsonl = os.path.join(tmp_dir, "corrupt.jsonl")
        with open(corrupt_jsonl, "w") as f:
            f.write("{invalid json line\n")
            f.write(json.dumps({"role": "user", "content": "Dòng hợp lệ duy nhất"}) + "\n")
            f.write("another broken line}\n")

        # Cursor đọc sqlite hỏng -> không crash, trả về []
        cursor_w = CursorWatcher(base_paths=[tmp_dir])
        assert cursor_w.scan_sessions() == []

        # Claude Code đọc JSONL có dòng lỗi -> chỉ lấy dòng hợp lệ, không crash
        claude_w = ClaudeCodeWatcher(base_paths=[tmp_dir])
        records = claude_w.scan_sessions()
        assert len(records) == 1
        assert records[0].content == "Dòng hợp lệ duy nhất"

