"""VS Code GitHub Copilot & Cloud Code Chat Watcher.

Đọc lịch sử hội thoại của GitHub Copilot Chat từ state.vscdb trong workspaceStorage của VS Code
và chuyển đổi thành RawAgentSessionRecord (Contract C12).
"""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Dict, List, Optional

from ptb_contracts import AgentType, RawAgentSessionRecord
from ptb_acquisition.watchers.base import BaseAgentWatcher

logger = logging.getLogger("ptb.acquisition.watchers.copilot")


class CopilotWatcher(BaseAgentWatcher):
    def __init__(self, base_paths: Optional[List[str]] = None, filter_valuable_only: bool = False):
        super().__init__(
            agent_type=AgentType.GITHUB_COPILOT,
            base_paths=base_paths,
            filter_valuable_only=filter_valuable_only,
        )

    def get_default_paths(self) -> List[str]:
        """Xác định đường dẫn workspaceStorage của VS Code Copilot đa nền tảng qua platformdirs."""
        return self.resolve_platform_paths(
            app_names=["Code", "Code - Insiders", "code"],
            sub_path=os.path.join("User", "workspaceStorage"),
        )

    def scan_sessions(self) -> List[RawAgentSessionRecord]:
        """Quét tất cả các thư mục workspace của VS Code Copilot và trích xuất sessions."""
        if not self.is_installed:
            return []
        all_records: List[RawAgentSessionRecord] = []
        try:
            for storage_dir in self.base_paths:
                if not os.path.isdir(storage_dir):
                    continue
                try:
                    for item in os.listdir(storage_dir):
                        ws_dir = os.path.join(storage_dir, item)
                        db_path = os.path.join(ws_dir, "state.vscdb")
                        if os.path.isfile(db_path):
                            records = self.extract_from_db(db_path, workspace_id=item)
                            all_records.extend(records)
                except Exception as dir_err:
                    logger.debug(f"Error accessing storage dir {storage_dir}: {dir_err}")
                    continue
        except Exception as e:
            logger.warning(f"Lỗi khi quét Copilot sessions: {e}")
            return []
        return all_records

    def extract_from_db(self, db_path: str, workspace_id: str = "unknown") -> List[RawAgentSessionRecord]:
        records: List[RawAgentSessionRecord] = []
        try:
            conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
            cursor = conn.cursor()

            # Quét các key chứa dữ liệu Copilot Chat hoặc interactive session
            cursor.execute(
                "SELECT key, value FROM ItemTable WHERE key LIKE '%copilot%' OR key LIKE '%interactive-session%' OR key LIKE '%chat.ChatSessionStore%'"
            )
            rows = cursor.fetchall()
            turn_idx = 0

            for key, val in rows:
                if not val:
                    continue
                try:
                    data = json.loads(val)
                    if isinstance(data, dict):
                        # 1. Chat index entries
                        if "entries" in data and isinstance(data["entries"], dict):
                            for sess_id, sinfo in data["entries"].items():
                                title = sinfo.get("title", "")
                                if title and title != "New Chat" and self.is_turn_retained(title):
                                    idem_key = self.generate_idempotency_key(f"copilot-{sess_id}", turn_idx, "user")
                                    records.append(
                                        RawAgentSessionRecord(
                                            session_id=f"copilot-{sess_id}",
                                            agent_type=AgentType.GITHUB_COPILOT,
                                            workspace_path=workspace_id,
                                            turn_index=turn_idx,
                                            message_role="user",
                                            content=title,
                                            tool_invocations=None,
                                            timestamp=datetime.now(timezone.utc),
                                            idempotency_key=idem_key,
                                        )
                                    )
                                    turn_idx += 1

                        # 2. Input values hoặc prompt history
                        input_val = data.get("inputValue") or data.get("text")
                        if input_val and isinstance(input_val, str) and self.is_turn_retained(input_val):
                            idem_key = self.generate_idempotency_key(f"copilot-in-{workspace_id}", turn_idx, "user")
                            records.append(
                                RawAgentSessionRecord(
                                    session_id=f"copilot-{workspace_id}",
                                    agent_type=AgentType.GITHUB_COPILOT,
                                    workspace_path=workspace_id,
                                    turn_index=turn_idx,
                                    message_role="user",
                                    content=input_val,
                                    tool_invocations=None,
                                    timestamp=datetime.now(timezone.utc),
                                    idempotency_key=idem_key,
                                )
                            )
                            turn_idx += 1
                except Exception as e:
                    logger.debug("Lỗi parse row trong sqlite copilot: %s", e)

            conn.close()
        except Exception as e:
            logger.debug(f"Lỗi đọc sqlite VSCode tại {db_path}: {e}")

        return records
