"""Antigravity IDE Log Watcher & Parser.

Đọc các file transcript từ thư mục ~/.gemini/antigravity-ide/brain/<conversation-id>/.system_generated/logs/
và đóng gói thành RawAgentSessionRecord (Contract C12).
"""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional

from ptb_contracts import AgentType, RawAgentSessionRecord
from ptb_acquisition.watchers.base import BaseAgentWatcher

logger = logging.getLogger("ptb.acquisition.watchers.antigravity")


class AntigravityWatcher(BaseAgentWatcher):
    def __init__(self, base_paths: Optional[List[str]] = None, filter_valuable_only: bool = False):
        super().__init__(
            agent_type=AgentType.ANTIGRAVITY,
            base_paths=base_paths,
            filter_valuable_only=filter_valuable_only,
        )

    def get_default_paths(self) -> List[str]:
        """Xác định đường dẫn lưu trữ brain logs của Antigravity sử dụng platformdirs."""
        paths = self.resolve_platform_paths(
            app_names=["antigravity", "antigravity-ide"],
            sub_path="brain",
        )
        home = os.path.expanduser("~")
        std_antigravity = os.path.join(home, ".gemini", "antigravity", "brain")
        std_antigravity_ide = os.path.join(home, ".gemini", "antigravity-ide", "brain")
        std_antigravity_alt = os.path.join(home, ".antigravity", "brain")
        for p in (std_antigravity, std_antigravity_ide, std_antigravity_alt):
            if p not in paths:
                paths.append(p)

        unique_paths = list(dict.fromkeys(os.path.normpath(p) for p in paths if p))
        existing = [p for p in unique_paths if os.path.isdir(p)]
        return existing if existing else unique_paths

    def scan_sessions(self) -> List[RawAgentSessionRecord]:
        """Quét tất cả các thư mục session trong brain/ và trích xuất."""
        if not self.is_installed:
            return []
        all_records: List[RawAgentSessionRecord] = []
        try:
            for brain_dir in self.base_paths:
                if not os.path.isdir(brain_dir):
                    continue
                try:
                    for sid in os.listdir(brain_dir):
                        session_dir = os.path.join(brain_dir, sid)
                        if not os.path.isdir(session_dir):
                            continue

                        log_dir = os.path.join(session_dir, ".system_generated", "logs")
                        transcript_path = os.path.join(log_dir, "transcript_full.jsonl")
                        if not os.path.isfile(transcript_path):
                            transcript_path = os.path.join(log_dir, "transcript.jsonl")
                        if not os.path.isfile(transcript_path):
                            continue

                        records = self.extract_from_transcript(transcript_path, session_id=sid, session_dir=session_dir)
                        all_records.extend(records)
                except Exception as dir_err:
                    logger.debug(f"Error accessing brain dir {brain_dir}: {dir_err}")
                    continue
        except Exception as e:
            logger.warning(f"Lỗi khi quét Antigravity sessions: {e}")
            return []
        return all_records

    def extract_from_transcript(self, transcript_path: str, session_id: str, session_dir: str = "") -> List[RawAgentSessionRecord]:
        """Đọc và chuyển đổi các bước trong transcript thành RawAgentSessionRecord."""
        entries = self.read_jsonl(transcript_path)
        records: List[RawAgentSessionRecord] = []
        turn_index = 0

        for step in entries:
            step_type = step.get("type", "")
            source = step.get("source", "")
            content = step.get("content", "")

            # Phân loại role
            if step_type == "USER_INPUT" or source == "USER_EXPLICIT":
                role = "user"
                # Làm sạch các tag <USER_SETTINGS_CHANGE> nếu có
                clean_content = re.sub(r"<USER_SETTINGS_CHANGE>[\s\S]*?</USER_SETTINGS_CHANGE>", "", content).strip()
            elif step_type == "PLANNER_RESPONSE" or source == "MODEL":
                role = "assistant"
                clean_content = content.strip()
            elif "TOOL" in step_type:
                role = "tool_result"
                clean_content = content.strip()
            else:
                continue

            if not clean_content:
                continue

            if not self.is_turn_retained(clean_content):
                continue

            tool_calls = step.get("tool_calls")
            key = self.generate_idempotency_key(session_id, turn_index, role)

            records.append(
                RawAgentSessionRecord(
                    session_id=f"antigravity-{session_id}",
                    agent_type=AgentType.ANTIGRAVITY,
                    workspace_path=session_dir,
                    turn_index=turn_index,
                    message_role=role,
                    content=clean_content,
                    tool_invocations=tool_calls,
                    timestamp=datetime.now(timezone.utc),
                    idempotency_key=key,
                )
            )
            turn_index += 1

        return records
