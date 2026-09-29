"""Continue.dev IDE Extension Watcher & Parser.

Quét thư mục ~/.continue/sessions/ đọc các file session JSON
và chuyển đổi thành RawAgentSessionRecord (Contract C12).
"""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ptb_contracts import AgentType, RawAgentSessionRecord
from ptb_acquisition.watchers.base import BaseAgentWatcher

logger = logging.getLogger("ptb.acquisition.watchers.continue")


class ContinueWatcher(BaseAgentWatcher):
    def __init__(self, base_paths: Optional[List[str]] = None, filter_valuable_only: bool = False):
        super().__init__(
            agent_type=AgentType.CONTINUE,
            base_paths=base_paths,
            filter_valuable_only=filter_valuable_only,
        )

    def get_default_paths(self) -> List[str]:
        """Xác định đường dẫn sessions của Continue.dev đa nền tảng qua platformdirs."""
        return self.resolve_home_paths(
            dot_name=".continue",
            sub_path="sessions",
            app_names=["continue", "Continue"],
        )

    def scan_sessions(self) -> List[RawAgentSessionRecord]:
        """Quét toàn bộ thư mục sessions của Continue.dev."""
        if not self.is_installed:
            return []
        all_records: List[RawAgentSessionRecord] = []
        try:
            for base_dir in self.base_paths:
                if not os.path.isdir(base_dir):
                    continue
                try:
                    for root, _, files in os.walk(base_dir):
                        for f in files:
                            if f.endswith(".json"):
                                fp = os.path.join(root, f)
                                records = self.extract_from_file(fp)
                                all_records.extend(records)
                except Exception as walk_err:
                    logger.debug(f"Error walking dir {base_dir}: {walk_err}")
                    continue
        except Exception as e:
            logger.warning(f"Lỗi khi quét Continue sessions: {e}")
            return []
        return all_records

    def extract_from_file(self, file_path: str) -> List[RawAgentSessionRecord]:
        records: List[RawAgentSessionRecord] = []
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as fl:
                data = json.load(fl)
            session_id = data.get("sessionId") or Path(file_path).stem
            workspace_path = data.get("workspaceDirectory") or os.path.dirname(file_path)
            history = data.get("history") or []

            turn_idx = 0
            for item in history:
                role = item.get("role", "user")
                content = item.get("content", "")
                if isinstance(content, list):
                    content = " ".join(str(c) for c in content)

                if not content.strip() or not self.is_turn_retained(content):
                    continue

                idem_key = self.generate_idempotency_key(f"continue-{session_id}", turn_idx, role)
                records.append(
                    RawAgentSessionRecord(
                        session_id=f"continue-{session_id}",
                        agent_type=AgentType.CONTINUE,
                        workspace_path=workspace_path,
                        turn_index=turn_idx,
                        message_role=role,
                        content=content.strip(),
                        tool_invocations=None,
                        timestamp=datetime.now(timezone.utc),
                        idempotency_key=idem_key,
                    )
                )
                turn_idx += 1
        except Exception as e:
            logger.debug(f"Lỗi đọc session Continue tại {file_path}: {e}")
        return records
