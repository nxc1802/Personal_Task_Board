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
