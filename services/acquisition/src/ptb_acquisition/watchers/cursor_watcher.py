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
        """Xác định đường dẫn workspaceStorage, globalStorage và projects của Cursor theo hệ điều hành sử dụng platformdirs và fallbacks."""
        paths = self.resolve_platform_paths(
            app_names=["Cursor", "cursor"],
            sub_path=os.path.join("User", "workspaceStorage"),
        )
        global_paths = self.resolve_platform_paths(
            app_names=["Cursor", "cursor"],
            sub_path=os.path.join("User", "globalStorage"),
        )
        for p in global_paths:
            if p not in paths:
                paths.append(p)

        home = os.path.expanduser("~")
        cursor_projects = os.path.join(home, ".cursor", "projects")
        if cursor_projects not in paths:
            paths.append(cursor_projects)

        # Hỗ trợ container Docker quét thư mục host người dùng nếu được mount
        for host_mount in ("/host_home", "/c/Users/truon", "/mnt/c/Users/truon"):
            if os.path.isdir(host_mount):
                for sub in (
                    os.path.join("AppData", "Roaming", "Cursor", "User", "workspaceStorage"),
                    os.path.join("AppData", "Roaming", "Cursor", "User", "globalStorage"),
                    os.path.join(".cursor", "projects"),
                ):
                    hp = os.path.join(host_mount, sub)
                    if hp not in paths:
                        paths.append(hp)

        # Kiểm tra biến môi trường
        custom_env = os.getenv("CURSOR_STORAGE_DIR") or os.getenv("CODING_AGENT_LOG_DIRS")
        if custom_env:
            for cp in custom_env.split(","):
                cp_clean = cp.strip()
                if os.path.isdir(cp_clean) and cp_clean not in paths:
                    paths.append(cp_clean)

        # Kiểm tra fallback thư mục mẫu dữ liệu (không hardcode)
        from ptb_acquisition.watchers.base import resolve_raw_samples_dir
        samples_dir = resolve_raw_samples_dir()
        if samples_dir:
            c_sample = samples_dir / "cursor"
            if c_sample.is_dir() and str(c_sample) not in paths:
                paths.append(str(c_sample))

        unique_paths = list(dict.fromkeys(os.path.normpath(p) for p in paths if p))
        existing = [p for p in unique_paths if os.path.isdir(p)]
        return existing if existing else unique_paths

    def _load_conversation_titles(self, db_path: str) -> Dict[str, str]:
        """Đọc tiêu đề các phiên hội thoại từ conversation-search.db."""
        titles: Dict[str, str] = {}
        conn = None
        try:
            conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
            cursor = conn.cursor()
            cursor.execute("SELECT id, title FROM conversations WHERE title IS NOT NULL AND title != ''")
            for conv_id, title in cursor.fetchall():
                if conv_id and title:
                    titles[conv_id] = title
        except Exception as e:
            logger.debug(f"Lỗi đọc conversation-search.db tại {db_path}: {e}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception as close_err:
                    logger.debug("Failed closing SQLite connection: %s", close_err)
        return titles

    def scan_sessions(self) -> List[RawAgentSessionRecord]:
        """Quét tất cả các thư mục workspace, globalStorage và projects của Cursor từ cả DB và JSONL."""
        if not self.is_installed:
            return []
        all_records: List[RawAgentSessionRecord] = []
        seen_keys: set = set()

        try:
            # 1. Tìm và nạp tiêu đề conversation từ conversation-search.db
            conv_titles: Dict[str, str] = {}
            for storage_dir in self.base_paths:
                if not os.path.isdir(storage_dir):
                    continue
                candidates = [
                    os.path.join(storage_dir, "conversation-search.db"),
                    os.path.join(storage_dir, "User", "globalStorage", "conversation-search.db"),
                    os.path.join(os.path.dirname(storage_dir), "globalStorage", "conversation-search.db"),
                ]
                for c_path in candidates:
                    if os.path.isfile(c_path):
                        conv_titles.update(self._load_conversation_titles(c_path))

            for storage_dir in self.base_paths:
                if not os.path.isdir(storage_dir):
                    continue
                try:
                    # 2. Nếu chính storage_dir chứa state.vscdb (như globalStorage hoặc workspace folder lẻ)
                    direct_db = os.path.join(storage_dir, "state.vscdb")
                    if os.path.isfile(direct_db):
                        records = self.extract_from_db(direct_db, workspace_id=os.path.basename(storage_dir))
                        for r in records:
                            if r.idempotency_key not in seen_keys:
                                seen_keys.add(r.idempotency_key)
                                all_records.append(r)

                    # 3. Quét tìm database state.vscdb theo chuẩn VS Code workspaceStorage/<ws_id>/
                    for item in os.listdir(storage_dir):
                        ws_dir = os.path.join(storage_dir, item)
                        if os.path.isdir(ws_dir):
                            db_path = os.path.join(ws_dir, "state.vscdb")
                            if os.path.isfile(db_path):
                                records = self.extract_from_db(db_path, workspace_id=item)
                                for r in records:
                                    if r.idempotency_key not in seen_keys:
                                        seen_keys.add(r.idempotency_key)
                                        all_records.append(r)

                    # 4. Quét đệ quy tìm các file .jsonl (agent-transcripts thật hoặc samples)
                    for root, _, files in os.walk(storage_dir):
                        for f in files:
                            if f.endswith(".jsonl"):
                                j_path = os.path.join(root, f)
                                raw_stem = Path(f).stem
                                j_sid = f"cursor-{raw_stem}"
                                title = conv_titles.get(raw_stem)
                                jsonl_recs = self.extract_from_jsonl(j_path, session_id=j_sid, title=title)
                                for r in jsonl_recs:
                                    if r.idempotency_key not in seen_keys:
                                        seen_keys.add(r.idempotency_key)
                                        all_records.append(r)
                except Exception as dir_err:
                    logger.debug(f"Error accessing storage dir {storage_dir}: {dir_err}")
                    continue
        except Exception as e:
            logger.warning(f"Lỗi khi quét Cursor sessions: {e}")
            return []
        return all_records

    def extract_from_jsonl(self, jsonl_path: str, session_id: Optional[str] = None, title: Optional[str] = None) -> List[RawAgentSessionRecord]:
        """Đọc và trích xuất session từ file .jsonl transcript của Cursor."""
        records: List[RawAgentSessionRecord] = []
        file_stem = Path(jsonl_path).stem
        sid = session_id or f"cursor-{file_stem}"
        ws_path = title or str(Path(jsonl_path).parent)
        turn_idx = 0
        try:
            with open(jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    line_str = line.strip()
                    if not line_str:
                        continue
                    try:
                        data = json.loads(line_str)
                    except Exception:
                        continue

                    role = data.get("role", "user")
                    msg_obj = data.get("message", {})
                    content_val = ""
                    if isinstance(msg_obj, dict):
                        contents = msg_obj.get("content", [])
                        if isinstance(contents, list):
                            texts = []
                            for c in contents:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    texts.append(c.get("text", ""))
                            content_val = "\n".join(texts)
                        elif isinstance(contents, str):
                            content_val = contents
                    elif isinstance(msg_obj, str):
                        content_val = msg_obj
                    elif "text" in data:
                        content_val = str(data["text"])
                    elif "content" in data:
                        content_val = data["content"] if isinstance(data["content"], str) else json.dumps(data["content"])

                    clean_text = content_val.strip()
                    if not clean_text or not self.is_turn_retained(clean_text):
                        continue

                    ts_raw = data.get("timestamp") or data.get("createdAt")
                    if ts_raw:
                        try:
                            if isinstance(ts_raw, (int, float)):
                                ts = datetime.fromtimestamp(ts_raw / 1000.0, timezone.utc) if ts_raw > 1e11 else datetime.fromtimestamp(ts_raw, timezone.utc)
                            else:
                                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                        except Exception:
                            ts = datetime.now(timezone.utc)
                    else:
                        ts = datetime.now(timezone.utc)

                    key = self.generate_idempotency_key(sid, turn_idx, role)
                    records.append(
                        RawAgentSessionRecord(
                            session_id=sid,
                            agent_type=AgentType.CURSOR,
                            workspace_path=ws_path,
                            turn_index=turn_idx,
                            message_role=role,
                            content=clean_text,
                            tool_invocations=None,
                            timestamp=ts,
                            idempotency_key=key,
                        )
                    )
                    turn_idx += 1
        except Exception as e:
            logger.warning(f"Lỗi đọc JSONL Cursor tại {jsonl_path}: {e}")
        return records

    def extract_from_db(self, db_path: str, workspace_id: str = "unknown") -> List[RawAgentSessionRecord]:
        """Mở kết nối SQLite readonly và đọc dữ liệu chat/composer."""
        records: List[RawAgentSessionRecord] = []
        conn = None
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

        except Exception as e:
            logger.warning(f"Không thể đọc file SQLite Cursor tại {db_path}: {e}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception as close_err:
                    logger.debug("Error closing cursor sqlite: %s", close_err)

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
