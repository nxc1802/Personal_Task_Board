"""Cline & Roo Code Extension Watcher & Parser.

Quét các thư mục tasks của saoudrizwan.claude-dev và rooveterinaryinc.roo-cline
trong globalStorage của VS Code và chuyển đổi thành RawAgentSessionRecord.
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

logger = logging.getLogger("ptb.acquisition.watchers.cline")


class ClineWatcher(BaseAgentWatcher):
    def __init__(self, base_paths: Optional[List[str]] = None, filter_valuable_only: bool = False):
        super().__init__(
            agent_type=AgentType.CLINE,
            base_paths=base_paths,
            filter_valuable_only=filter_valuable_only,
        )

    def get_default_paths(self) -> List[str]:
        """Xác định đường dẫn tasks của Cline / Roo Code đa nền tảng qua platformdirs."""
        paths: List[str] = []
        global_storage_bases = self.resolve_platform_paths(
            app_names=["Code", "Code - Insiders", "Cursor", "Windsurf", "code"],
            sub_path=os.path.join("User", "globalStorage"),
        )

        for base in global_storage_bases:
            for ext_name in ("saoudrizwan.claude-dev", "rooveterinaryinc.roo-cline"):
                p = os.path.join(base, ext_name, "tasks")
                if p not in paths:
                    paths.append(p)

        unique_paths = list(dict.fromkeys(os.path.normpath(p) for p in paths if p))
        existing = [p for p in unique_paths if os.path.isdir(p)]
        return existing if existing else unique_paths

    def scan_sessions(self) -> List[RawAgentSessionRecord]:
        """Quét và trích xuất các task session của Cline và Roo Code."""
        if not self.is_installed:
            return []
        all_records: List[RawAgentSessionRecord] = []
        try:
            for tasks_dir in self.base_paths:
                if not os.path.isdir(tasks_dir):
                    continue
                is_roo = "roo-cline" in tasks_dir
                agent_type = AgentType.ROO_CODE if is_roo else AgentType.CLINE

                try:
                    for task_id in os.listdir(tasks_dir):
                        task_dir = os.path.join(tasks_dir, task_id)
                        if not os.path.isdir(task_dir):
                            continue

                        # Tìm ui_messages.json hoặc api_conversation_history.json
                        hist_file = os.path.join(task_dir, "api_conversation_history.json")
                        if not os.path.isfile(hist_file):
                            hist_file = os.path.join(task_dir, "ui_messages.json")
                        if os.path.isfile(hist_file):
                            records = self.extract_from_file(hist_file, task_id, agent_type)
                            all_records.extend(records)
                except Exception as dir_err:
                    logger.debug(f"Error accessing tasks dir {tasks_dir}: {dir_err}")
                    continue
        except Exception as e:
            logger.warning(f"Lỗi khi quét Cline/Roo sessions: {e}")
            return []
        return all_records

    def extract_from_file(self, file_path: str, task_id: str, agent_type: AgentType) -> List[RawAgentSessionRecord]:
        records: List[RawAgentSessionRecord] = []
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as fl:
                messages = json.load(fl)

            turn_idx = 0
            for msg in messages:
                role = msg.get("role") or msg.get("type", "user")
                content = msg.get("content") or msg.get("text", "")
                if isinstance(content, list):
                    content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))

                if not content.strip() or not self.is_turn_retained(content):
                    continue

                ts_raw = msg.get("ts") or msg.get("timestamp")
                ts = datetime.fromtimestamp(ts_raw / 1000.0, timezone.utc) if ts_raw else datetime.now(timezone.utc)

                key = self.generate_idempotency_key(f"{agent_type.value}-{task_id}", turn_idx, role)
                records.append(
                    RawAgentSessionRecord(
                        session_id=f"{agent_type.value}-{task_id}",
                        agent_type=agent_type,
                        workspace_path=os.path.dirname(file_path),
                        turn_index=turn_idx,
                        message_role=role,
                        content=content.strip(),
                        tool_invocations=None,
                        timestamp=ts,
                        idempotency_key=key,
                    )
                )
                turn_idx += 1
        except Exception as e:
            logger.debug(f"Lỗi đọc task Cline tại {file_path}: {e}")
        return records
