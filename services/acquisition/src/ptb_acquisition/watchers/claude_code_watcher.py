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
        """Xác định đường dẫn lưu trữ Claude Code theo hệ điều hành sử dụng platformdirs và fallbacks."""
        paths = self.resolve_home_paths(
            dot_name=".claude",
            sub_path="projects",
            app_names=["claude", "Claude"],
        )
        home = os.path.expanduser("~")
        std_claude_base = os.path.join(home, ".claude")
        if std_claude_base not in paths:
            paths.append(std_claude_base)

        # 1. Thư mục Runtime & State theo chuẩn XDG / local machine: ~/.local/share/claude, ~/.local/state/claude
        local_share = os.path.join(home, ".local", "share", "claude")
        local_state = os.path.join(home, ".local", "state", "claude")
        for p in (local_share, local_state):
            if p not in paths:
                paths.append(p)

        # 2. Logs ứng dụng Claude trên máy (ví dụ AppData/Local/Claude/logs)
        app_logs = self.resolve_platform_paths(
            app_names=["Claude", "claude"],
            sub_path="logs",
        )
        for p in app_logs:
            if p not in paths:
                paths.append(p)

        # 3. Thư mục .claude/ cục bộ theo từng dự án làm việc hiện tại
        cwd_claude = os.path.join(os.getcwd(), ".claude")
        if cwd_claude not in paths:
            paths.append(cwd_claude)

        ws = os.getenv("PTB_WORKSPACE_ROOT")
        if ws:
            ws_claude = os.path.join(ws, ".claude")
            if ws_claude not in paths:
                paths.append(ws_claude)

        for proj in ("/workspace/Personal_Task_Board-main", "/project", r"d:\Working\PTB\Personal_Task_Board-main"):
            pc = os.path.join(proj, ".claude")
            if os.path.isdir(pc) and pc not in paths:
                paths.append(pc)

        # 4. Hỗ trợ container Docker quét thư mục host người dùng nếu được mount
        for host_mount in ("/host_home", "/c/Users/truon", "/mnt/c/Users/truon"):
            if os.path.isdir(host_mount):
                for sub in (
                    os.path.join(".local", "share", "claude"),
                    os.path.join(".local", "state", "claude"),
                    os.path.join("AppData", "Local", "Claude", "logs"),
                    ".claude",
                    os.path.join(".claude", "projects"),
                ):
                    hp = os.path.join(host_mount, sub)
                    if hp not in paths:
                        paths.append(hp)

        # 5. Biến môi trường tùy biến
        custom_env = os.getenv("CLAUDE_LOG_DIRS") or os.getenv("CODING_AGENT_LOG_DIRS")
        if custom_env:
            for cp in custom_env.split(","):
                cp_clean = cp.strip()
                if os.path.isdir(cp_clean) and cp_clean not in paths:
                    paths.append(cp_clean)

        # 6. Fallback thư mục mẫu dữ liệu kiểm thử
        from ptb_acquisition.watchers.base import resolve_raw_samples_dir
        samples_dir = resolve_raw_samples_dir()
        if samples_dir:
            cl_sample = samples_dir / "claude"
            if cl_sample.is_dir() and str(cl_sample) not in paths:
                paths.append(str(cl_sample))

        unique_paths = list(dict.fromkeys(os.path.normpath(p) for p in paths if p))
        existing = [p for p in unique_paths if os.path.isdir(p)]
        return existing if existing else unique_paths

    def scan_sessions(self) -> List[RawAgentSessionRecord]:
        """Quét tất cả các file transcript trong thư mục projects của Claude Code."""
        if not self.is_installed:
            return []
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
            return []
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
