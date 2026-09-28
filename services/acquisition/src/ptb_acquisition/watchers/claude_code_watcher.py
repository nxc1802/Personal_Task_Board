"""Claude Code Transcript Watcher & Parser.

Đọc các file JSONL transcript từ thư mục ~/.claude/projects/
và chuyển đổi thành RawAgentSessionRecord (Contract C12).
"""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional

from ptb_contracts import AgentType, RawAgentSessionRecord
from ptb_acquisition.watchers.base import BaseAgentWatcher

logger = logging.getLogger("ptb.acquisition.watchers.claude")


class ClaudeCodeWatcher(BaseAgentWatcher):
    def __init__(self, base_paths: Optional[List[str]] = None, filter_valuable_only: bool = False):
        super().__init__(
            agent_type=AgentType.CLAUDE_CODE,
            base_paths=base_paths,
            filter_valuable_only=filter_valuable_only,
        )

    def get_default_paths(self) -> List[str]:
        """Xác định đường dẫn lưu trữ Claude Code theo hệ điều hành sử dụng platformdirs."""
        paths = []
        try:
            import platformdirs
            cfg_proj = os.path.join(platformdirs.user_config_dir("claude", appauthor=False), "projects")
            data_proj = os.path.join(platformdirs.user_data_dir("claude", appauthor=False), "projects")
            for p in (cfg_proj, data_proj):
                if p not in paths:
                    paths.append(p)
        except Exception as e:
            logger.debug(f"platformdirs resolution error for Claude: {e}")

        home = os.path.expanduser("~")
        std_claude_proj = os.path.join(home, ".claude", "projects")
        std_claude_base = os.path.join(home, ".claude")
        for p in (std_claude_proj, std_claude_base):
            if p not in paths:
                paths.append(p)

        existing = [p for p in paths if os.path.isdir(p)]
        return existing if existing else paths

    def scan_sessions(self) -> List[RawAgentSessionRecord]:
        """Quét tất cả các file transcript trong thư mục projects của Claude Code."""
        all_records: List[RawAgentSessionRecord] = []
        try:
            for base_dir in self.base_paths:
                if not os.path.isdir(base_dir):
                    continue
                try:
                    for root, _, files in os.walk(base_dir):
                        for file_name in files:
                            if file_name.endswith(".jsonl") or "transcript" in file_name.lower():
                                file_path = os.path.join(root, file_name)
                                records = self.extract_from_transcript_file(file_path)
                                all_records.extend(records)
                except Exception as walk_err:
                    logger.debug(f"Error walking dir {base_dir}: {walk_err}")
                    continue
        except Exception as e:
            logger.warning(f"Lỗi khi quét Claude Code sessions: {e}")
        return all_records

    def extract_from_transcript_file(self, file_path: str) -> List[RawAgentSessionRecord]:
        """Đọc và bóc tách từng dòng trong file transcript của Claude Code."""
        entries = self.read_jsonl(file_path)
        records: List[RawAgentSessionRecord] = []
        session_id = Path(file_path).stem
        workspace_path = os.path.dirname(file_path)
        turn_index = 0

        for entry in entries:
            # Nhận diện cấu trúc entry
            role = entry.get("role") or entry.get("type") or "assistant"
            if role in ("user", "human"):
                msg_role = "user"
            elif role in ("assistant", "model", "ai"):
                msg_role = "assistant"
            elif role in ("tool", "tool_result"):
                msg_role = "tool_result"
            else:
                msg_role = "system"

            content = ""
            raw_content = entry.get("content")
            if isinstance(raw_content, str):
                content = raw_content
            elif isinstance(raw_content, list):
                # Claude messages API content blocks: [{'type': 'text', 'text': '...'}]
                text_parts = []
                for block in raw_content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            text_parts.append(f"[Tool: {block.get('name')}]")
                    elif isinstance(block, str):
                        text_parts.append(block)
                content = "\n".join(text_parts)

            if not content.strip():
                continue

            if not self.is_turn_retained(content):
                continue

            # Tool invocations
            tool_calls = entry.get("tool_calls") or entry.get("tool_invocations")

            ts_raw = entry.get("timestamp") or entry.get("created_at")
            if ts_raw:
                try:
                    if isinstance(ts_raw, (int, float)):
                        ts = datetime.fromtimestamp(ts_raw, timezone.utc)
                    else:
                        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                except Exception:
                    ts = datetime.now(timezone.utc)
            else:
                ts = datetime.now(timezone.utc)

            key = self.generate_idempotency_key(session_id, turn_index, msg_role)
            records.append(
                RawAgentSessionRecord(
                    session_id=f"claude-{session_id}",
                    agent_type=AgentType.CLAUDE_CODE,
                    workspace_path=workspace_path,
                    turn_index=turn_index,
                    message_role=msg_role,
                    content=content.strip(),
                    tool_invocations=tool_calls,
                    timestamp=ts,
                    idempotency_key=key,
                )
            )
            turn_index += 1

        return records
