"""OpenAI Codex CLI Session Watcher & Parser.

Quét và đọc các file rollout JSONL từ ~/.codex/sessions/
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

logger = logging.getLogger("ptb.acquisition.watchers.codex")


class CodexWatcher(BaseAgentWatcher):
    def __init__(self, base_paths: Optional[List[str]] = None, filter_valuable_only: bool = False):
        super().__init__(
            agent_type=AgentType.CODEX,
            base_paths=base_paths,
            filter_valuable_only=filter_valuable_only,
        )

    def get_default_paths(self) -> List[str]:
        """Xác định đường dẫn sessions của OpenAI Codex CLI đa nền tảng qua platformdirs."""
        return self.resolve_home_paths(
            dot_name=".codex",
            sub_path="sessions",
            app_names=["codex", "Codex"],
        )

    def scan_sessions(self) -> List[RawAgentSessionRecord]:
        """Quét đệ quy toàn bộ thư mục sessions của Codex."""
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
                            if file_name.startswith("rollout-") and file_name.endswith(".jsonl"):
                                file_path = os.path.join(root, file_name)
                                records = self.extract_from_file(file_path)
                                all_records.extend(records)
                except Exception as walk_err:
                    logger.debug(f"Error walking dir {base_dir}: {walk_err}")
                    continue
        except Exception as e:
            logger.warning(f"Lỗi khi quét Codex sessions: {e}")
            return []
        return all_records

    def extract_from_file(self, file_path: str) -> List[RawAgentSessionRecord]:
        """Phân tích các dòng JSONL trong file rollout của Codex."""
        entries = self.read_jsonl(file_path)
        records: List[RawAgentSessionRecord] = []
        file_stem = Path(file_path).stem.replace("rollout-", "")
        session_id = f"codex-{file_stem}"
        workspace_path = os.path.dirname(file_path)
        turn_index = 0

        for entry in entries:
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue

            ptype = payload.get("type", "")
            raw_role = payload.get("role", "")
            if ptype == "message":
                if raw_role in ("user", "human"):
                    role = "user"
                elif raw_role in ("assistant", "developer"):
                    role = "assistant"
                else:
                    role = "assistant"

                # Trích xuất nội dung
                content = ""
                raw_c = payload.get("content")
                if isinstance(raw_c, str):
                    content = raw_c
                elif isinstance(raw_c, list):
                    parts = []
                    for block in raw_c:
                        if isinstance(block, dict):
                            parts.append(block.get("text") or block.get("input_text") or "")
                        elif isinstance(block, str):
                            parts.append(block)
                    content = "\n".join(parts)

                if not content.strip():
                    continue

                if not self.is_turn_retained(content):
                    continue

                ts_str = entry.get("timestamp")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                    except Exception:
                        ts = datetime.now(timezone.utc)
                else:
                    ts = datetime.now(timezone.utc)

                key = self.generate_idempotency_key(session_id, turn_index, role)
                records.append(
                    RawAgentSessionRecord(
                        session_id=session_id,
                        agent_type=AgentType.CODEX,
                        workspace_path=workspace_path,
                        turn_index=turn_index,
                        message_role=role,
                        content=content.strip(),
                        tool_invocations=None,
                        timestamp=ts,
                        idempotency_key=key,
                    )
                )
                turn_index += 1

        return records
