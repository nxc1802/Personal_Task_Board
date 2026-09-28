"""Playwright Session State Manager.

Quản lý cookie và token phiên làm việc lưu tại local (storage_state.json),
phát hiện phiên hết hạn (401/403) và hỗ trợ lưu phiên sau đăng nhập.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional
from playwright.async_api import BrowserContext

logger = logging.getLogger("ptb.acquisition.playwright.session")

DEFAULT_STORAGE_PATH = os.path.join("data", "playwright", "storage_state.json")


class SessionManager:
    def __init__(self, storage_path: str = DEFAULT_STORAGE_PATH):
        self.storage_path = Path(storage_path)

    def has_valid_session(self) -> bool:
        """Kiểm tra file storage_state.json đã tồn tại và có cookie hay không."""
        if not self.storage_path.exists():
            return False
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                cookies = data.get("cookies", [])
                return len(cookies) > 0
        except Exception as e:
            logger.warning(f"Lỗi đọc storage_state tại {self.storage_path}: {e}")
            return False

    async def save_session(self, context: BrowserContext) -> None:
        """Lưu toàn bộ cookies và local storage từ browser context."""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(self.storage_path))
        logger.info(f"Đã lưu session state vào: {self.storage_path}")

    @staticmethod
    def is_auth_error(status_code: int) -> bool:
        """Kiểm tra mã trạng thái có phải là lỗi phiên/xác thực không."""
        return status_code in (401, 403)
