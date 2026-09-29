"""Windsurf (Codeium Cascade) IDE Watcher & Parser.

Đọc dữ liệu Cascade từ workspaceStorage của Windsurf và ~/.codeium/windsurf/
chuyển đổi thành RawAgentSessionRecord (Contract C12).
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

logger = logging.getLogger("ptb.acquisition.watchers.windsurf")


class WindsurfWatcher(BaseAgentWatcher):
    def __init__(self, base_paths: Optional[List[str]] = None, filter_valuable_only: bool = False):
        super().__init__(
            agent_type=AgentType.WINDSURF,
            base_paths=base_paths,
            filter_valuable_only=filter_valuable_only,
        )

    def get_default_paths(self) -> List[str]:
        """Xác định đường dẫn workspaceStorage của Windsurf và Codeium đa nền tảng qua platformdirs."""
        paths: List[str] = []
        ws_paths = self.resolve_platform_paths(
            app_names=["Windsurf", "windsurf"],
            sub_path=os.path.join("User", "workspaceStorage"),
        )
        for p in ws_paths:
            if p not in paths:
                paths.append(p)

        codeium_paths = self.resolve_platform_paths(
            app_names=["codeium", "Codeium"],
            sub_path="windsurf",
        )
        for p in codeium_paths:
            if p not in paths:
                paths.append(p)

        home = os.path.expanduser("~")
        codeium_home = os.path.join(home, ".codeium", "windsurf")
        windsurf_home = os.path.join(home, ".windsurf")
        for p in (codeium_home, windsurf_home):
            if p not in paths:
                paths.append(p)

        unique_paths = list(dict.fromkeys(os.path.normpath(p) for p in paths if p))
        existing = [p for p in unique_paths if os.path.isdir(p)]
        return existing if existing else unique_paths

    def scan_sessions(self) -> List[RawAgentSessionRecord]:
        """Quét tất cả các thư mục workspace và logs của Windsurf."""
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
                        elif item in ("cascade", "memories") or os.path.isdir(ws_dir):
                            records = self.extract_from_codeium_dir(ws_dir)
                            all_records.extend(records)
                except Exception as dir_err:
                    logger.debug(f"Error accessing windsurf storage {storage_dir}: {dir_err}")
                    continue
        except Exception as e:
            logger.warning(f"Lỗi khi quét Windsurf sessions: {e}")
            return []
        return all_records

    def extract_from_db(self, db_path: str, workspace_id: str = "unknown") -> List[RawAgentSessionRecord]:
        records: List[RawAgentSessionRecord] = []
        try:
            conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM ItemTable WHERE key LIKE '%cascade%' OR key LIKE '%codeium%'")
            rows = cursor.fetchall()
            turn_idx = 0
            for key, val in rows:
                if not val:
                    continue
                try:
                    data = json.loads(val)
                    if isinstance(data, dict):
                        text = json.dumps(data)
                        if self.is_turn_retained(text):
                            idem_key = self.generate_idempotency_key(f"windsurf-{workspace_id}", turn_idx, "assistant")
                            records.append(
                                RawAgentSessionRecord(
                                    session_id=f"windsurf-{workspace_id}",
                                    agent_type=AgentType.WINDSURF,
                                    workspace_path=workspace_id,
                                    turn_index=turn_idx,
                                    message_role="assistant",
                                    content=text[:500],
                                    tool_invocations=None,
                                    timestamp=datetime.now(timezone.utc),
                                    idempotency_key=idem_key,
                                )
                            )
                            turn_idx += 1
                except Exception as e:
                    logger.debug("Lỗi parse row trong sqlite windsurf: %s", e)
            conn.close()
        except Exception as e:
            logger.debug(f"Lỗi đọc sqlite Windsurf tại {db_path}: {e}")
        return records

    def extract_from_codeium_dir(self, dir_path: str) -> List[RawAgentSessionRecord]:
        records: List[RawAgentSessionRecord] = []
        turn_idx = 0
        for root, _, files in os.walk(dir_path):
            for f in files:
                if f.endswith(".md") or f.endswith(".json"):
                    fp = os.path.join(root, f)
                    try:
                        with open(fp, "r", encoding="utf-8", errors="ignore") as fl:
                            text = fl.read().strip()
                            if text and self.is_turn_retained(text):
                                idem_key = self.generate_idempotency_key(f"windsurf-file-{f}", turn_idx, "assistant")
                                records.append(
                                    RawAgentSessionRecord(
                                        session_id=f"windsurf-{f}",
                                        agent_type=AgentType.WINDSURF,
                                        workspace_path=root,
                                        turn_index=turn_idx,
                                        message_role="assistant",
                                        content=text[:1000],
                                        tool_invocations=None,
                                        timestamp=datetime.now(timezone.utc),
                                        idempotency_key=idem_key,
                                    )
                                )
                                turn_idx += 1
                    except Exception as e:
                        logger.debug("Lỗi đọc file log windsurf %s: %s", fp, e)
        return records
