"""Aider CLI Chat History Watcher & Parser.

Quét file .aider.chat.history.md trong các thư mục dự án và ~/.aider/
chuyển đổi thành RawAgentSessionRecord (Contract C12).
"""

from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional

from ptb_contracts import AgentType, RawAgentSessionRecord
from ptb_acquisition.watchers.base import BaseAgentWatcher

logger = logging.getLogger("ptb.acquisition.watchers.aider")


class AiderWatcher(BaseAgentWatcher):
    def __init__(self, base_paths: Optional[List[str]] = None, filter_valuable_only: bool = False):
        super().__init__(
            agent_type=AgentType.AIDER,
            base_paths=base_paths,
            filter_valuable_only=filter_valuable_only,
        )

    def get_default_paths(self) -> List[str]:
        """Xác định đường dẫn lưu trữ Aider CLI đa nền tảng qua platformdirs."""
        paths = self.resolve_home_paths(
            dot_name=".aider",
            sub_path="",
            app_names=["aider", "Aider"],
        )

        # Quét thư mục làm việc hiện tại CHỈ KHI có file hoặc thư mục của aider
        cwd = os.getcwd()
        cwd_history = os.path.join(cwd, ".aider.chat.history.md")
        cwd_dir = os.path.join(cwd, ".aider")
        if os.path.isfile(cwd_history) or os.path.isdir(cwd_dir):
            if cwd not in paths:
                paths.append(cwd)

        existing = [p for p in paths if os.path.exists(p)]
        return existing if existing else paths

    def scan_sessions(self) -> List[RawAgentSessionRecord]:
        """Quét và trích xuất hội thoại từ file history của Aider."""
        if not self.is_installed:
            return []
        all_records: List[RawAgentSessionRecord] = []
        try:
            for p in self.base_paths:
                if os.path.isfile(p) and p.endswith(".aider.chat.history.md"):
                    all_records.extend(self.extract_from_file(p))
                elif os.path.isdir(p):
                    try:
                        for root, _, files in os.walk(p):
                            for f in files:
                                if f == ".aider.chat.history.md":
                                    fp = os.path.join(root, f)
                                    all_records.extend(self.extract_from_file(fp))
                    except Exception as walk_err:
                        logger.debug(f"Error walking dir {p}: {walk_err}")
                        continue
        except Exception as e:
            logger.warning(f"Lỗi khi quét Aider sessions: {e}")
            return []
        return all_records

    def extract_from_file(self, file_path: str) -> List[RawAgentSessionRecord]:
        records: List[RawAgentSessionRecord] = []
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as fl:
                content = fl.read()

            # Aider phân chia các turn bằng #### hoặc >
            turns = re.split(r"\n(?=#### |\n> )", content)
            session_id = Path(file_path).parent.name or "aider-session"
            workspace_path = str(Path(file_path).parent)

            turn_idx = 0
            for raw_turn in turns:
                text = raw_turn.strip()
                if not text:
                    continue

                role = "user" if text.startswith("> ") or text.startswith("#### ") else "assistant"
                clean_text = re.sub(r"^(#### |> )", "", text).strip()

                if not clean_text or not self.is_turn_retained(clean_text):
                    continue

                idem_key = self.generate_idempotency_key(f"aider-{session_id}", turn_idx, role)
                records.append(
                    RawAgentSessionRecord(
                        session_id=f"aider-{session_id}",
                        agent_type=AgentType.AIDER,
                        workspace_path=workspace_path,
                        turn_index=turn_idx,
                        message_role=role,
                        content=clean_text[:1000],
                        tool_invocations=None,
                        timestamp=datetime.now(timezone.utc),
                        idempotency_key=idem_key,
                    )
                )
                turn_idx += 1
        except Exception as e:
            logger.debug(f"Lỗi đọc file aider history {file_path}: {e}")
        return records
