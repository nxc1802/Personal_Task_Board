"""OpenAI Codex Desktop / CLI Session Watcher & Parser.

Quét và đọc các phiên làm việc của Codex từ:
1. SQLite Database: ~/.codex/thread_history_1.sqlite (thread_items, thread_turns)
2. Chỉ mục phiên: ~/.codex/session_index.jsonl (thread_name metadata)
3. Thư mục session theo thời gian: ~/.codex/sessions/ (rollout-*.jsonl đệ quy qua các năm 2025/, 2026/...)
4. Hoạt động & telemetry: ~/.codex/logs_2.sqlite
Chuyển đổi thành RawAgentSessionRecord (Contract C12).
"""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Set

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
        """Xác định đường dẫn sessions và database của OpenAI Codex đa nền tảng qua platformdirs và fallbacks."""
        # Bao gồm cả ~/.codex/sessions và ~/.codex
        paths = self.resolve_home_paths(
            dot_name=".codex",
            sub_path="sessions",
            app_names=["codex", "Codex"],
        )
        base_codex = self.resolve_home_paths(
            dot_name=".codex",
            sub_path="",
            app_names=["codex", "Codex"],
        )
        for p in base_codex:
            if p not in paths:
                paths.append(p)

        # Hỗ trợ container Docker quét thư mục host người dùng nếu được mount
        for host_mount in ("/host_home", "/c/Users/truon", "/mnt/c/Users/truon"):
            if os.path.isdir(host_mount):
                for sub in ("", "sessions"):
                    hp = os.path.join(host_mount, ".codex", sub) if sub else os.path.join(host_mount, ".codex")
                    if hp not in paths:
                        paths.append(hp)

        custom_env = os.getenv("CODEX_SESSIONS_DIR") or os.getenv("CODING_AGENT_LOG_DIRS")
        if custom_env:
            for cp in custom_env.split(","):
                cp_clean = cp.strip()
                if os.path.isdir(cp_clean) and cp_clean not in paths:
                    paths.append(cp_clean)

        from ptb_acquisition.watchers.base import resolve_raw_samples_dir
        samples_dir = resolve_raw_samples_dir()
        if samples_dir:
            codex_sample = samples_dir / "codex"
            if codex_sample.is_dir() and str(codex_sample) not in paths:
                paths.append(str(codex_sample))

        unique_paths = list(dict.fromkeys(os.path.normpath(p) for p in paths if p))
        existing = [p for p in unique_paths if os.path.isdir(p)]
        return existing if existing else unique_paths

    def scan_sessions(self) -> List[RawAgentSessionRecord]:
        """Quét toàn bộ sessions của Codex từ cả SQLite database và các file rollout JSONL."""
        if not self.is_installed:
            return []
        all_records: List[RawAgentSessionRecord] = []
        seen_keys: Set[str] = set()

        try:
            # 1. Tìm và nạp chỉ mục session_index.jsonl nếu có
            thread_names: Dict[str, str] = {}
            for base_dir in self.base_paths:
                if not os.path.isdir(base_dir):
                    continue
                # Tìm trong base_dir hoặc parent
                candidates = [
                    os.path.join(base_dir, "session_index.jsonl"),
                    os.path.join(os.path.dirname(base_dir), "session_index.jsonl"),
                ]
                for idx_p in candidates:
                    if os.path.isfile(idx_p):
                        thread_names.update(self._load_session_index(idx_p))

            # 2. Quét SQLite database thread_history_1.sqlite
            for base_dir in self.base_paths:
                if not os.path.isdir(base_dir):
                    continue
                candidates = [
                    os.path.join(base_dir, "thread_history_1.sqlite"),
                    os.path.join(os.path.dirname(base_dir), "thread_history_1.sqlite"),
                ]
                for db_path in candidates:
                    if os.path.isfile(db_path):
                        db_records = self.extract_from_sqlite(db_path, thread_names)
                        for r in db_records:
                            if r.idempotency_key not in seen_keys:
                                seen_keys.add(r.idempotency_key)
                                all_records.append(r)

            # 3. Quét đệ quy tìm các file rollout-*.jsonl (hỗ trợ phân cấp 2025/, 2026/...)
            for base_dir in self.base_paths:
                if not os.path.isdir(base_dir):
                    continue
                try:
                    for root, _, files in os.walk(base_dir):
                        for file_name in files:
                            if file_name.startswith("rollout-") and file_name.endswith(".jsonl"):
                                file_path = os.path.join(root, file_name)
                                file_records = self.extract_from_file(file_path)
                                for r in file_records:
                                    if r.idempotency_key not in seen_keys:
                                        seen_keys.add(r.idempotency_key)
                                        all_records.append(r)
                except Exception as walk_err:
                    logger.debug(f"Error walking dir {base_dir}: {walk_err}")
                    continue

        except Exception as e:
            logger.warning(f"Lỗi khi quét Codex sessions: {e}")
            return []
        return all_records

    def _load_session_index(self, index_path: str) -> Dict[str, str]:
        """Đọc file session_index.jsonl để lấy ánh xạ id -> thread_name."""
        names: Dict[str, str] = {}
        if not os.path.isfile(index_path):
            return names
        try:
            with open(index_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        tid = data.get("id")
                        tname = data.get("thread_name")
                        if tid and tname:
                            names[tid] = tname
                    except Exception:
                        continue
        except Exception as e:
            logger.debug(f"Lỗi đọc session_index.jsonl tại {index_path}: {e}")
        return names

    def extract_from_sqlite(self, db_path: str, thread_names: Dict[str, str]) -> List[RawAgentSessionRecord]:
        """Trích xuất tin nhắn hội thoại từ SQLite thread_history_1.sqlite."""
        records: List[RawAgentSessionRecord] = []
        conn = None
        try:
            conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT thread_id, item_id, item_type, created_at_ms, item_json
                FROM thread_items
                WHERE item_type IN ('userMessage', 'agentMessage')
                ORDER BY thread_id, rollout_ordinal, created_at_ms
            """)
            rows = cursor.fetchall()

            thread_turns: Dict[str, int] = {}
            for thread_id, item_id, item_type, created_at_ms, item_json in rows:
                try:
                    data = json.loads(item_json)
                except Exception:
                    continue

                role = "user" if item_type == "userMessage" else "assistant"
                content = ""
                if role == "user":
                    raw_c = data.get("content", [])
                    if isinstance(raw_c, list):
                        parts = []
                        for block in raw_c:
                            if isinstance(block, dict) and block.get("type") == "text":
                                parts.append(block.get("text", ""))
                            elif isinstance(block, str):
                                parts.append(block)
                        content = "\n".join(parts)
                    elif isinstance(raw_c, str):
                        content = raw_c
                else:
                    content = data.get("text", "")

                clean_content = content.strip()
                if not clean_content or not self.is_turn_retained(clean_content):
                    continue

                turn_idx = thread_turns.get(thread_id, 0)
                thread_turns[thread_id] = turn_idx + 1

                if created_at_ms:
                    try:
                        ts = datetime.fromtimestamp(created_at_ms / 1000.0, timezone.utc)
                    except Exception:
                        ts = datetime.now(timezone.utc)
                else:
                    ts = datetime.now(timezone.utc)

                session_id = f"codex-{thread_id}"
                workspace_path = thread_names.get(thread_id, "")
                key = self.generate_idempotency_key(session_id, turn_idx, role)

                records.append(
                    RawAgentSessionRecord(
                        session_id=session_id,
                        agent_type=AgentType.CODEX,
                        workspace_path=workspace_path,
                        turn_index=turn_idx,
                        message_role=role,
                        content=clean_content,
                        tool_invocations=None,
                        timestamp=ts,
                        idempotency_key=key,
                    )
                )
        except Exception as e:
            logger.warning(f"Lỗi đọc SQLite Codex tại {db_path}: {e}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception as close_err:
                    logger.debug("Failed closing SQLite connection: %s", close_err)
        return records

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
