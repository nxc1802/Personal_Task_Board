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
        """Xác định đường dẫn lưu trữ brain logs của Antigravity sử dụng platformdirs và fallbacks."""
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

        # Hỗ trợ container Docker quét thư mục host người dùng nếu được mount
        for host_mount in ("/host_home", "/c/Users/truon", "/mnt/c/Users/truon"):
            if os.path.isdir(host_mount):
                for sub in ("antigravity", "antigravity-ide"):
                    hp = os.path.join(host_mount, ".gemini", sub, "brain")
                    if hp not in paths:
                        paths.append(hp)

        # Kiểm tra biến môi trường người dùng tùy biến
        custom_env = os.getenv("ANTIGRAVITY_BRAIN_DIR") or os.getenv("CODING_AGENT_LOG_DIRS")
        if custom_env:
            for cp in custom_env.split(","):
                cp_clean = cp.strip()
                if os.path.isdir(cp_clean) and cp_clean not in paths:
                    paths.append(cp_clean)

        # Kiểm tra fallback thư mục mẫu dữ liệu (không hardcode)
        from ptb_acquisition.watchers.base import resolve_raw_samples_dir
        samples_dir = resolve_raw_samples_dir()
        if samples_dir:
            ag_sample = samples_dir / "antigravity"
            if ag_sample.is_dir() and str(ag_sample) not in paths:
                paths.append(str(ag_sample))

        unique_paths = list(dict.fromkeys(os.path.normpath(p) for p in paths if p))
        existing = [p for p in unique_paths if os.path.isdir(p)]
        return existing if existing else unique_paths

    def scan_sessions(self, max_sessions: int = 50) -> List[RawAgentSessionRecord]:
        """Quét tất cả các thư mục session trong brain/ và trích xuất."""
        if not self.is_installed:
            return []
        all_records: List[RawAgentSessionRecord] = []
        try:
            for brain_dir in self.base_paths:
                if not os.path.isdir(brain_dir):
                    continue
                try:
                    # 1. Quét nếu brain_dir trực tiếp chứa transcript (dạng sample hoặc session rời)
                    for direct_name in ("transcript.jsonl", "transcript_full.jsonl"):
                        direct_p = os.path.join(brain_dir, direct_name)
                        if os.path.isfile(direct_p):
                            sid = os.path.basename(brain_dir)
                            records = self.extract_from_transcript(direct_p, session_id=sid, session_dir=brain_dir)
                            all_records.extend(records[-15:])

                    # 2. Quét các session con dạng <brain_dir>/<session_id>/.system_generated/logs/
                    session_dirs = [
                        os.path.join(brain_dir, sid)
                        for sid in os.listdir(brain_dir)
                        if os.path.isdir(os.path.join(brain_dir, sid))
                    ]
                    # Sắp xếp an toàn theo session mới nhất trước
                    def _safe_mtime(p: str) -> float:
                        try:
                            return os.path.getmtime(p)
                        except Exception:
                            return 0.0

                    session_dirs.sort(key=_safe_mtime, reverse=True)
                    for session_dir in session_dirs[:max_sessions]:
                        sid = os.path.basename(session_dir)
                        log_dir = os.path.join(session_dir, ".system_generated", "logs")
                        transcript_path = os.path.join(log_dir, "transcript.jsonl")
                        if not os.path.isfile(transcript_path):
                            transcript_path = os.path.join(log_dir, "transcript_full.jsonl")
                        if not os.path.isfile(transcript_path):
                            continue

                        records = self.extract_from_transcript(transcript_path, session_id=sid, session_dir=session_dir)
                        # Lấy tối đa 15 turn gần nhất của mỗi session để giữ focus vào task/cam kết
                        all_records.extend(records[-15:])
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

            # Phân loại role: chỉ giữ user (yêu cầu) và assistant (phản hồi/cam kết)
            if step_type == "USER_INPUT" or source == "USER_EXPLICIT":
                role = "user"
                # Làm sạch các tag <USER_SETTINGS_CHANGE> nếu có
                clean_content = re.sub(r"<USER_SETTINGS_CHANGE>[\s\S]*?</USER_SETTINGS_CHANGE>", "", content).strip()
            elif step_type == "PLANNER_RESPONSE" or source == "MODEL":
                role = "assistant"
                clean_content = content.strip()
            else:
                # Bỏ qua các tool execution results (terminal output, diffs, inspects) vì không chứa intent/cam kết
                continue

            if not clean_content:
                continue

            if not self.is_turn_retained(clean_content):
                continue

            tool_calls = step.get("tool_calls")

            # Ghi nhận thông tin tasks background logs nếu có
            tasks_dir = os.path.join(session_dir, ".system_generated", "tasks") if session_dir else None
            effective_tools = tool_calls
            if tasks_dir and os.path.isdir(tasks_dir):
                task_files = [f for f in os.listdir(tasks_dir) if f.endswith(".log")]
                if task_files:
                    if not effective_tools:
                        effective_tools = [{"type": "background_tasks", "task_count": len(task_files), "tasks_dir": tasks_dir}]
                    elif isinstance(effective_tools, list):
                        effective_tools = list(effective_tools)
                        effective_tools.append({"type": "background_tasks", "task_count": len(task_files), "tasks_dir": tasks_dir})

            # Trích xuất timestamp từ created_at trong step log
            created_at_raw = step.get("created_at") or step.get("timestamp")
            if created_at_raw:
                try:
                    ts = datetime.fromisoformat(str(created_at_raw).replace("Z", "+00:00"))
                except Exception:
                    ts = datetime.now(timezone.utc)
            else:
                ts = datetime.now(timezone.utc)

            key = self.generate_idempotency_key(session_id, turn_index, role)

            records.append(
                RawAgentSessionRecord(
                    session_id=f"antigravity-{session_id}",
                    agent_type=AgentType.ANTIGRAVITY,
                    workspace_path=session_dir,
                    turn_index=turn_index,
                    message_role=role,
                    content=clean_content,
                    tool_invocations=effective_tools,
                    timestamp=ts,
                    idempotency_key=key,
                )
            )
            turn_index += 1

        return records
