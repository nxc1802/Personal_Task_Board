"""Base Class for Coding Agent Log Watchers.

Hỗ trợ quét đa nền tảng (macOS, Linux, Windows), đóng gói chuẩn Contract C12
(RawAgentSessionRecord), và tích hợp bộ lọc AgentTurnFilter.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

from ptb_contracts import AgentType, RawAgentSessionRecord
from ptb_acquisition.watchers.turn_filter import AgentTurnFilter

logger = logging.getLogger("ptb.acquisition.watchers")


class BaseAgentWatcher(ABC):
    def __init__(self, agent_type: AgentType, base_paths: Optional[List[str]] = None, filter_valuable_only: bool = False):
        self.agent_type = agent_type
        self.base_paths = base_paths or self.get_default_paths()
        self.filter_valuable_only = filter_valuable_only

    @property
    def is_installed(self) -> bool:
        """Kiểm tra xem ít nhất một thư mục log mặc định của agent có tồn tại trên máy không."""
        return any(os.path.exists(p) for p in self.base_paths)

    @property
    def status(self) -> str:
        """Trả về AVAILABLE nếu đã cài đặt, hoặc NOT_INSTALLED nếu không tìm thấy trên hệ thống."""
        return "AVAILABLE" if self.is_installed else "NOT_INSTALLED"

    @abstractmethod
    def get_default_paths(self) -> List[str]:
        """Trả về danh sách đường dẫn lưu trữ mặc định theo OS hiện tại."""
        pass

    @abstractmethod
    def scan_sessions(self) -> List[RawAgentSessionRecord]:
        """Quét và trích xuất toàn bộ các lượt tương tác thành RawAgentSessionRecord."""
        pass

    @staticmethod
    def generate_idempotency_key(session_id: str, turn_index: int, role: str) -> str:
        """Tạo khóa định danh duy nhất SHA256 cho từng turn hội thoại."""
        raw = f"{session_id}:{turn_index}:{role}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def is_turn_retained(self, content: str) -> bool:
        """Kiểm tra turn có được giữ lại dựa trên cấu hình filter."""
        if not self.filter_valuable_only:
            return True
        return AgentTurnFilter.is_valuable_turn(content)

    @staticmethod
    def read_jsonl(file_path: str) -> List[Dict[str, Any]]:
        """Đọc an toàn file JSONL, bỏ qua dòng lỗi định dạng."""
        items = []
        if not os.path.isfile(file_path):
            return items
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            logger.warning(f"Lỗi đọc file JSONL {file_path}: {e}")
        return items
