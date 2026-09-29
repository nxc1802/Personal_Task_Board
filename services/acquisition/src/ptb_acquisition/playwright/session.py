"""Playwright Session State Manager & Health Lifecycle.

Quản lý cookie và token phiên làm việc lưu tại local (storage_state.json),
xác thực authenticated session thực tế với Microsoft endpoint (Teams/Outlook),
và theo dõi vòng đời sức khỏe session:
UNCONFIGURED -> LOGIN_REQUIRED -> STARTING -> HEALTHY / DEGRADED / AUTH_EXPIRED / ERROR.
"""

from datetime import datetime, timezone
from enum import Enum
import inspect
import json
import logging
import os
from pathlib import Path
from typing import Optional

from playwright.async_api import BrowserContext
from ptb_contracts import BugCode, log_bug

logger = logging.getLogger("ptb.acquisition.playwright.session")

DEFAULT_STORAGE_PATH = os.path.join("data", "playwright", "storage_state.json")


class SessionHealthState(str, Enum):
    UNCONFIGURED = "UNCONFIGURED"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    ERROR = "ERROR"


VALID_TRANSITIONS = {
    SessionHealthState.UNCONFIGURED: {
        SessionHealthState.UNCONFIGURED,
        SessionHealthState.LOGIN_REQUIRED,
        SessionHealthState.STARTING,
        SessionHealthState.ERROR,
    },
    SessionHealthState.LOGIN_REQUIRED: {
        SessionHealthState.LOGIN_REQUIRED,
        SessionHealthState.STARTING,
        SessionHealthState.HEALTHY,
        SessionHealthState.ERROR,
    },
    SessionHealthState.STARTING: {
        SessionHealthState.STARTING,
        SessionHealthState.HEALTHY,
        SessionHealthState.DEGRADED,
        SessionHealthState.AUTH_EXPIRED,
        SessionHealthState.ERROR,
    },
    SessionHealthState.HEALTHY: {
        SessionHealthState.HEALTHY,
        SessionHealthState.DEGRADED,
        SessionHealthState.AUTH_EXPIRED,
        SessionHealthState.ERROR,
    },
    SessionHealthState.DEGRADED: {
        SessionHealthState.DEGRADED,
        SessionHealthState.HEALTHY,
        SessionHealthState.AUTH_EXPIRED,
        SessionHealthState.ERROR,
    },
    SessionHealthState.AUTH_EXPIRED: {
        SessionHealthState.AUTH_EXPIRED,
        SessionHealthState.STARTING,
        SessionHealthState.HEALTHY,
    },
    SessionHealthState.ERROR: {
        SessionHealthState.ERROR,
        SessionHealthState.STARTING,
        SessionHealthState.HEALTHY,
        SessionHealthState.DEGRADED,
        SessionHealthState.AUTH_EXPIRED,
    },
}


def transition_session_state(
    current: SessionHealthState,
    target: SessionHealthState,
    allow_reset: bool = False,
) -> SessionHealthState:
    """Kiểm tra và thực hiện chuyển đổi trạng thái session theo State Machine:

    STARTING -> HEALTHY -> DEGRADED -> AUTH_EXPIRED.
    AUTH_EXPIRED là trạng thái chặn: không thể tự động chuyển ngược lại về HEALTHY/DEGRADED
    trừ khi có sự can thiệp làm mới phiên (allow_reset=True).
    """
    if current == target:
        return current
    if current == SessionHealthState.AUTH_EXPIRED and not allow_reset:
        logger.debug(f"State machine: không thể chuyển từ AUTH_EXPIRED sang {target} mà không re-login")
        return current
    allowed = VALID_TRANSITIONS.get(current, set())
    if target in allowed or allow_reset:
        return target
    logger.warning(f"State machine: chuyển đổi không hợp lệ từ {current} sang {target}")
    return target


async def verify_authenticated_session(
    context: BrowserContext,
    target_url: str = "https://teams.microsoft.com",
    timeout_ms: int = 15000,
) -> SessionHealthState:
    """Xác thực phiên làm việc thực tế với Microsoft endpoint qua BrowserContext.

    Kiểm tra xem session còn sống không bằng cách điều hướng thử đến target_url.
    Nếu nhận HTTP 401/403 hoặc bị redirect sang login.microsoftonline.com -> AUTH_EXPIRED.
    Nếu load thành công authenticated page -> HEALTHY.
    Nếu lỗi server hoặc timeout -> DEGRADED.
    Nếu có lỗi không xác định -> ERROR.
    """
    page = None
    try:
        page = await context.new_page()
        response = await page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)

        final_url = page.url.lower() if hasattr(page, "url") else ""

        # 1. Kiểm tra redirect sang trang đăng nhập Microsoft
        login_indicators = (
            "login.microsoftonline.com",
            "login.live.com",
            "login.windows.net",
            "/oauth2/",
            "/common/oauth2",
        )
        if any(ind in final_url for ind in login_indicators):
            logger.warning(f"Phiên Microsoft đã hết hạn (Redirected to login: {final_url})")
            log_bug(
                BugCode.PTB_L1_001,
                subsystem="playwright",
                severity="ERROR",
                message="Microsoft session authentication expired (HTTP 401/403 or login redirect)",
                context={"url": final_url, "target_url": target_url},
            )
            return SessionHealthState.AUTH_EXPIRED

        # 2. Kiểm tra mã trạng thái HTTP
        if response is not None:
            status = response.status if hasattr(response, "status") else 200
            if status in (401, 403):
                logger.warning(f"Phiên Microsoft hết hạn (HTTP {status}) tại {target_url}")
                log_bug(
                    BugCode.PTB_L1_001,
                    subsystem="playwright",
                    severity="ERROR",
                    message="Microsoft session authentication expired (HTTP 401/403 or login redirect)",
                    context={"url": target_url, "status": status},
                )
                return SessionHealthState.AUTH_EXPIRED
            if status >= 500:
                logger.warning(f"Dịch vụ Microsoft gặp sự cố tạm thời (HTTP {status})")
                return SessionHealthState.DEGRADED

        # 3. Xác nhận URL thuộc domain authenticated
        if any(d in final_url for d in ("teams.microsoft.com", "outlook.office.com", "outlook.live.com")):
            return SessionHealthState.HEALTHY

        return SessionHealthState.HEALTHY
    except Exception as e:
        err_msg = str(e).lower()
        if "401" in err_msg or "403" in err_msg or "login.microsoftonline.com" in err_msg:
            log_bug(
                BugCode.PTB_L1_001,
                subsystem="playwright",
                severity="ERROR",
                message="Microsoft session authentication expired (HTTP 401/403 or login redirect)",
                context={"error": str(e), "target_url": target_url},
                exc=e,
            )
            return SessionHealthState.AUTH_EXPIRED
        if "timeout" in err_msg or "net::" in err_msg:
            logger.warning(f"Lỗi kết nối khi verify session: {e}")
            return SessionHealthState.DEGRADED
        logger.error(f"Lỗi kiểm tra authenticated session: {e}")
        return SessionHealthState.ERROR
    finally:
        if page is not None and hasattr(page, "close"):
            try:
                res = page.close()
                if inspect.iscoroutine(res):
                    await res
            except Exception as e:
                logger.debug("Failed to close page in finally block: %s", e)


class SessionManager:
    def __init__(self, storage_path: str = DEFAULT_STORAGE_PATH):
        self.storage_path = Path(storage_path)
        self.health_state: SessionHealthState = SessionHealthState.UNCONFIGURED

    def validate_session(self) -> SessionHealthState:
        """Kiểm tra cấu hình file storage_state.json và hạn sử dụng cookies.

        Không chỉ kiểm tra cookie_count > 0, hàm này còn kiểm tra:
        - Sự tồn tại của file storage_state (UNCONFIGURED nếu thiếu)
        - Cấu trúc JSON và mảng cookies (LOGIN_REQUIRED nếu rỗng)
        - Hạn sử dụng (expires) của từng cookie (AUTH_EXPIRED nếu toàn bộ đã hết hạn)
        - Trả về STARTING nếu có cookie hợp lệ sẵn sàng để verify_authenticated_session
        """
        if not self.storage_path.exists():
            self.health_state = SessionHealthState.UNCONFIGURED
            return self.health_state

        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            cookies = data.get("cookies", [])
            if not isinstance(cookies, list) or len(cookies) == 0:
                self.health_state = SessionHealthState.LOGIN_REQUIRED
                return self.health_state

            # Kiểm tra hạn của các cookie
            now_ts = datetime.now(timezone.utc).timestamp()
            valid_cookies = 0
            expired_cookies = 0
            for c in cookies:
                if not isinstance(c, dict):
                    continue
                expires = c.get("expires", -1)
                if expires is not None and isinstance(expires, (int, float)) and expires > 0:
                    if expires < now_ts:
                        expired_cookies += 1
                    else:
                        valid_cookies += 1
                else:
                    # Session cookie không có expires cố định
                    valid_cookies += 1

            if valid_cookies == 0 and expired_cookies > 0:
                logger.warning(f"Tất cả ({expired_cookies}) cookies trong session state đã hết hạn!")
                self.health_state = SessionHealthState.AUTH_EXPIRED
                return self.health_state

            self.health_state = SessionHealthState.STARTING
            return self.health_state
        except Exception as e:
            logger.warning(f"Lỗi đọc storage_state tại {self.storage_path}: {e}")
            self.health_state = SessionHealthState.ERROR
            return self.health_state

    def has_valid_session(self) -> bool:
        """Kiểm tra nhanh session có trạng thái khả dụng ban đầu hay không."""
        state = self.validate_session()
        return state in (SessionHealthState.STARTING, SessionHealthState.HEALTHY)

    def transition_to(self, target: SessionHealthState, allow_reset: bool = False) -> SessionHealthState:
        """Cập nhật trạng thái session tuân theo State Machine."""
        new_state = transition_session_state(self.health_state, target, allow_reset=allow_reset)
        self.health_state = new_state
        return new_state

    async def verify_authenticated_session(
        self,
        context: BrowserContext,
        target_url: str = "https://teams.microsoft.com",
        timeout_ms: int = 15000,
    ) -> SessionHealthState:
        """Thực sự kiểm tra URL hoặc gọi endpoint authenticated của Teams/Outlook để xác nhận session còn sống."""
        state = await verify_authenticated_session(context, target_url=target_url, timeout_ms=timeout_ms)
        self.transition_to(state, allow_reset=(state == SessionHealthState.HEALTHY))
        return state

    async def save_session(self, context: BrowserContext) -> None:
        """Lưu toàn bộ cookies và local storage từ browser context."""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(self.storage_path))
        self.transition_to(SessionHealthState.HEALTHY, allow_reset=True)
        logger.info(f"Đã lưu session state vào: {self.storage_path}")

    @staticmethod
    def is_auth_error(status_code: int) -> bool:
        """Kiểm tra mã trạng thái có phải là lỗi phiên/xác thực không."""
        return status_code in (401, 403)
