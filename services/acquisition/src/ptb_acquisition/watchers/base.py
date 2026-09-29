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
        self.base_paths = base_paths if base_paths is not None else self.get_default_paths()
        self.filter_valuable_only = filter_valuable_only

    @property
    def is_installed(self) -> bool:
        """Kiểm tra xem ít nhất một thư mục log mặc định của agent có tồn tại trên máy không."""
        return any(os.path.exists(p) for p in self.base_paths) if self.base_paths else False

    @property
    def status(self) -> str:
        """Trả về AVAILABLE nếu đã cài đặt, hoặc NOT_INSTALLED nếu không tìm thấy trên hệ thống."""
        return "AVAILABLE" if self.is_installed else "NOT_INSTALLED"

    @classmethod
    def resolve_platform_paths(
        cls,
        app_names: List[str],
        sub_path: str = "",
        direct_fallbacks: Optional[List[str]] = None,
    ) -> List[str]:
        """Định vị các đường dẫn đa nền tảng sử dụng platformdirs và OS fallbacks.

        Trả về danh sách các đường dẫn tồn tại (existing). Nếu không có đường dẫn nào tồn tại,
        trả về toàn bộ danh sách ứng viên (candidate paths) để phục vụ kiểm tra is_installed.
        """
        paths: List[str] = []

        # 1. Dò tìm qua platformdirs cho từng app_name
        try:
            import platformdirs

            for app in app_names:
                for get_dir in (
                    lambda a: platformdirs.user_config_dir(a, appauthor=False, roaming=True),
                    lambda a: platformdirs.user_data_dir(a, appauthor=False, roaming=True),
                    lambda a: platformdirs.user_config_dir(a, appauthor=False),
                    lambda a: platformdirs.user_data_dir(a, appauthor=False),
                ):
                    try:
                        base = get_dir(app)
                        target = os.path.join(base, sub_path) if sub_path else base
                        if target not in paths:
                            paths.append(target)
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"platformdirs resolution error for {app_names}: {e}")

        # 2. OS-specific fallbacks (macOS, Windows, Linux)
        home = os.path.expanduser("~")
        for app in app_names:
            if sys.platform == "darwin":
                candidates = [
                    os.path.join(home, "Library", "Application Support", app),
                    os.path.join(home, f".{app.lower()}"),
                ]
            elif sys.platform == "win32":
                appdata = os.getenv("APPDATA")
                localappdata = os.getenv("LOCALAPPDATA")
                userprofile = os.getenv("USERPROFILE", home)
                candidates = []
                if appdata:
                    candidates.append(os.path.join(appdata, app))
                if localappdata:
                    candidates.append(os.path.join(localappdata, app))
                candidates.append(os.path.join(userprofile, f".{app.lower()}"))
            else:  # linux và unix-like khác
                candidates = [
                    os.path.join(home, ".config", app),
                    os.path.join(home, ".local", "share", app),
                    os.path.join(home, f".{app.lower()}"),
                ]

            for c in candidates:
                target = os.path.join(c, sub_path) if sub_path else c
                if target not in paths:
                    paths.append(target)

        # 3. Direct fallbacks nếu được chỉ định
        if direct_fallbacks:
            for p in direct_fallbacks:
                target = os.path.join(p, sub_path) if sub_path else p
                if target not in paths:
                    paths.append(target)

        unique_paths = list(dict.fromkeys(os.path.normpath(p) for p in paths if p))
        existing = [p for p in unique_paths if os.path.exists(p)]
        return existing if existing else unique_paths

    @classmethod
    def resolve_home_paths(
        cls,
        dot_name: str,
        sub_path: str = "",
        app_names: Optional[List[str]] = None,
    ) -> List[str]:
        """Định vị các thư mục dot-folder (ví dụ ~/.codex, ~/.claude) kết hợp platformdirs."""
        paths: List[str] = []
        home = os.path.expanduser("~")

        # 1. Thư mục ~/.dot_name[/sub_path]
        std_home = os.path.join(home, dot_name)
        paths.append(os.path.join(std_home, sub_path) if sub_path else std_home)

        # 2. Dò tìm qua platformdirs
        apps = app_names or [dot_name.lstrip(".")]
        try:
            import platformdirs

            for app in apps:
                for get_dir in (
                    lambda a: platformdirs.user_data_dir(a, appauthor=False, roaming=True),
                    lambda a: platformdirs.user_config_dir(a, appauthor=False, roaming=True),
                    lambda a: platformdirs.user_data_dir(a, appauthor=False),
                    lambda a: platformdirs.user_config_dir(a, appauthor=False),
                ):
                    try:
                        base = get_dir(app)
                        target = os.path.join(base, sub_path) if sub_path else base
                        if target not in paths:
                            paths.append(target)
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"platformdirs resolution error for {apps}: {e}")

        # 3. OS fallbacks
        if sys.platform == "win32":
            userprofile = os.getenv("USERPROFILE", home)
            paths.append(os.path.join(userprofile, dot_name, sub_path) if sub_path else os.path.join(userprofile, dot_name))
            appdata = os.getenv("APPDATA")
            if appdata:
                for app in apps:
                    paths.append(os.path.join(appdata, app, sub_path) if sub_path else os.path.join(appdata, app))
        elif sys.platform == "darwin":
            for app in apps:
                p = os.path.join(home, "Library", "Application Support", app)
                paths.append(os.path.join(p, sub_path) if sub_path else p)
        else:
            for app in apps:
                paths.append(os.path.join(home, ".config", app, sub_path) if sub_path else os.path.join(home, ".config", app))

        unique_paths = list(dict.fromkeys(os.path.normpath(p) for p in paths if p))
        existing = [p for p in unique_paths if os.path.exists(p)]
        return existing if existing else unique_paths

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
