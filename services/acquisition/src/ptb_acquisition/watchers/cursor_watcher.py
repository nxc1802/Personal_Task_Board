"""Cursor IDE Log Watcher & Parser.

Đọc trực tiếp file SQLite state.vscdb từ thư mục workspaceStorage của Cursor,
trích xuất lịch sử Composer/Chat thành các bản ghi RawAgentSessionRecord (Contract C12).
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

logger = logging.getLogger("ptb.acquisition.watchers.cursor")


class CursorWatcher(BaseAgentWatcher):
    def __init__(self, base_paths: Optional[List[str]] = None, filter_valuable_only: bool = False):
        super().__init__(
            agent_type=AgentType.CURSOR,
            base_paths=base_paths,
            filter_valuable_only=filter_valuable_only,
        )

    def get_default_paths(self) -> List[str]:
        """Xác định đường dẫn workspaceStorage của Cursor theo hệ điều hành sử dụng platformdirs."""
        return self.resolve_platform_paths(
            app_names=["Cursor", "cursor"],
            sub_path=os.path.join("User", "workspaceStorage"),
        )

    def scan_sessions(self) -> List[RawAgentSessionRecord]:
        """Quét tất cả các thư mục workspace của Cursor và trích xuất sessions."""
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
            logger.warning(f"Lỗi khi quét Cursor sessions: {e}")
            return []
        return all_records

    def extract_from_db(self, db_path: str, workspace_id: str = "unknown") -> List[RawAgentSessionRecord]:
        """Mở kết nối SQLite readonly và đọc dữ liệu chat/composer."""
        records: List[RawAgentSessionRecord] = []
        try:
            conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
            cursor = conn.cursor()

            # 1. Trích xuất từ workbench.panel.aichat.chatdata
            cursor.execute("SELECT value FROM ItemTable WHERE key = 'workbench.panel.aichat.chatdata'")
            row = cursor.fetchone()
            if row and row[0]:
                try:
                    chat_data = json.loads(row[0])
                    tabs = chat_data.get("tabs", [])
                    for tab in tabs:
                        tab_id = tab.get("tabId") or tab.get("id") or workspace_id
                        bubbles = tab.get("bubbles", [])
                        tab_records = self._parse_bubbles(bubbles, session_id=f"cursor-{tab_id}", workspace_path=workspace_id)
                        records.extend(tab_records)
                except Exception as e:
                    logger.debug(f"Lỗi parse chatdata in {db_path}: {e}")

            # 2. Trích xuất từ composer.composerData nếu có
            cursor.execute("SELECT value FROM ItemTable WHERE key = 'composer.composerData'")
            comp_row = cursor.fetchone()
            if comp_row and comp_row[0]:
                try:
                    comp_data = json.loads(comp_row[0])
                    all_composers = comp_data.get("allComposers", [])
                    for comp in all_composers:
                        comp_id = comp.get("composerId") or comp.get("id") or workspace_id
                        conv = comp.get("conversation", [])
                        comp_records = self._parse_composer_conversation(conv, session_id=f"cursor-composer-{comp_id}", workspace_path=workspace_id)
                        records.extend(comp_records)
                except Exception as e:
                    logger.debug(f"Lỗi parse composerData in {db_path}: {e}")

            # 3. Trích xuất từ aiService.prompts & aiService.generations (Cursor hiện đại)
            cursor.execute("SELECT value FROM ItemTable WHERE key = 'aiService.prompts'")
            prompts_row = cursor.fetchone()
            if prompts_row and prompts_row[0]:
                try:
                    prompts_data = json.loads(prompts_row[0])
                    if isinstance(prompts_data, list):
                        turn_idx = len(records)
                        for item in prompts_data:
                            text = item.get("text", "") if isinstance(item, dict) else str(item)
                            if text and self.is_turn_retained(text):
                                key = self.generate_idempotency_key(f"cursor-prompt-{workspace_id}", turn_idx, "user")
                                records.append(
                                    RawAgentSessionRecord(
                                        session_id=f"cursor-{workspace_id}",
                                        agent_type=AgentType.CURSOR,
                                        workspace_path=workspace_id,
                                        turn_index=turn_idx,
                                        message_role="user",
                                        content=text.strip(),
                                        tool_invocations=None,
                                        timestamp=datetime.now(timezone.utc),
                                        idempotency_key=key,
                                    )
                                )
                                turn_idx += 1
                except Exception as e:
                    logger.debug(f"Lỗi parse aiService.prompts in {db_path}: {e}")

            conn.close()
        except Exception as e:
            logger.warning(f"Không thể đọc file SQLite Cursor tại {db_path}: {e}")

        return records

    def _parse_bubbles(self, bubbles: List[Dict[str, Any]], session_id: str, workspace_path: str) -> List[RawAgentSessionRecord]:
        records: List[RawAgentSessionRecord] = []
        turn_index = 0
        for bubble in bubbles:
            raw_type = bubble.get("type")
            text = bubble.get("rawText") or bubble.get("text") or ""
            if not text and "modelResponse" in bubble:
                text = bubble.get("modelResponse", "")

            if raw_type == "user":
                role = "user"
            elif raw_type == "ai":
                role = "assistant"
            else:
                role = "user" if raw_type == 1 else "assistant"

            if not text.strip():
                continue

            if not self.is_turn_retained(text):
                continue

            # Lấy tool calls nếu có
            tool_calls = []
            if "selections" in bubble:
                tool_calls.append({"type": "code_selection", "data": bubble["selections"]})

            ts_val = bubble.get("timestamp")
            if ts_val:
                try:
                    ts = datetime.fromtimestamp(ts_val / 1000.0, timezone.utc) if ts_val > 1e11 else datetime.fromtimestamp(ts_val, timezone.utc)
                except Exception:
                    ts = datetime.now(timezone.utc)
            else:
                ts = datetime.now(timezone.utc)

            key = self.generate_idempotency_key(session_id, turn_index, role)
            records.append(
                RawAgentSessionRecord(
                    session_id=session_id,
                    agent_type=AgentType.CURSOR,
                    workspace_path=workspace_path,
                    turn_index=turn_index,
                    message_role=role,
                    content=text.strip(),
                    tool_invocations=tool_calls if tool_calls else None,
                    timestamp=ts,
                    idempotency_key=key,
                )
            )
            turn_index += 1
        return records

    def _parse_composer_conversation(self, conversation: List[Dict[str, Any]], session_id: str, workspace_path: str) -> List[RawAgentSessionRecord]:
        records: List[RawAgentSessionRecord] = []
        turn_index = 0
        for msg in conversation:
            role = "user" if msg.get("type") in (1, "user") else "assistant"
            text = msg.get("text") or msg.get("content") or ""
            if not text.strip():
                continue

            if not self.is_turn_retained(text):
                continue

            key = self.generate_idempotency_key(session_id, turn_index, role)
            records.append(
                RawAgentSessionRecord(
                    session_id=session_id,
                    agent_type=AgentType.CURSOR,
                    workspace_path=workspace_path,
                    turn_index=turn_index,
                    message_role=role,
                    content=text.strip(),
                    tool_invocations=None,
                    timestamp=datetime.now(timezone.utc),
                    idempotency_key=key,
                )
            )
            turn_index += 1
        return records
