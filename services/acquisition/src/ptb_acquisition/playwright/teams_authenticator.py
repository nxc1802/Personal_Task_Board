"""In-App Microsoft Teams Automated Authenticator for Personal Task Board.

Provides headless automated sign-in with live status reporting, 
Microsoft Authenticator number matching support, OTP handling,
and automatic cookie capture directly to storage_state.json.
"""

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Dict, Optional
import uuid

logger = logging.getLogger("ptb.acquisition.teams_authenticator")


class TeamsAuthenticatorSession:
    """Manages an active automated login session for Microsoft Teams."""

    def __init__(self, session_id: str, storage_path: str):
        self.session_id = session_id
        self.storage_path = storage_path
        self.status = "INITIALIZING"
        self.step = "INIT"
        self.message = "Đang khởi tạo trình duyệt tự động..."
        self.mfa_code: Optional[str] = None
        self.error_message: Optional[str] = None
        self.cookie_count: int = 0
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)
        
        self._playwright_mgr = None
        self._browser = None
        self._context = None
        self._page = None
        self._otp_event = asyncio.Event()
        self._pending_otp: Optional[str] = None
        self._task: Optional[asyncio.Task] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "step": self.step,
            "message": self.message,
            "mfa_code": self.mfa_code,
            "error_message": self.error_message,
            "cookie_count": self.cookie_count,
            "updated_at": self.updated_at.isoformat(),
        }

    def _set_state(self, status: str, step: str, message: str, mfa_code: Optional[str] = None):
        self.status = status
        self.step = step
        self.message = message
        if mfa_code is not None:
            self.mfa_code = mfa_code
        self.updated_at = datetime.now(timezone.utc)
        logger.info(f"[{self.session_id}] Status: {status} | Step: {step} | Msg: {message}")

    async def submit_otp(self, otp_code: str):
        """Passes OTP to waiting browser session."""
        self._pending_otp = otp_code.strip()
        self._otp_event.set()

    async def cancel(self):
        """Cancels and cleans up the session."""
        self._set_state("CANCELLED", "DONE", "Phiên đăng nhập đã bị hủy.")
        if self._task and not self._task.done():
            self._task.cancel()
        await self._cleanup()

    async def _cleanup(self):
        try:
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._playwright_mgr:
                await self._playwright_mgr.stop()
        except Exception as e:
            logger.debug(f"Cleanup warning: {e}")
        finally:
            self._playwright_mgr = None
            self._context = None
            self._browser = None
            self._page = None

    async def run(self, email: str, password: str):
        """Executes the automated login flow using Playwright."""
        from playwright.async_api import async_playwright

        try:
            self._set_state("IN_PROGRESS", "STARTING_BROWSER", "Đang khởi tạo Chromium bảo mật trong hệ thống...")
            
            self._playwright_mgr = await async_playwright().start()
            self._browser = await self._playwright_mgr.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            )
            self._page = await self._context.new_page()

            self._set_state("IN_PROGRESS", "NAVIGATING", "Đang kết nối tới cổng đăng nhập Microsoft Teams...")
            await self._page.goto("https://teams.microsoft.com/v2/", wait_until="domcontentloaded", timeout=45000)

            # Wait for email input or check if already logged in
            self._set_state("IN_PROGRESS", "ENTERING_EMAIL", "Đang nhập email tài khoản Microsoft...")
            email_selector = 'input[type="email"], input[name="loginfmt"], #i0116'
            
            try:
                await self._page.wait_for_selector(email_selector, state="visible", timeout=20000)
            except Exception:
                # Check if already reached Teams
                if "teams.microsoft.com" in self._page.url or "teams.cloud.microsoft" in self._page.url:
                    await self._save_and_finish()
                    return
                raise RuntimeError("Không tìm thấy ô nhập email trên trang đăng nhập Microsoft.")

            await self._page.fill(email_selector, email)
            await asyncio.sleep(0.5)

            # Click Next / Submit Email
            try:
                await self._page.locator('#idSIButton9, input[type="submit"], button[type="submit"]').first.click(timeout=8000)
            except Exception:
                await self._page.keyboard.press("Enter")

            # Wait for password input or error
            self._set_state("IN_PROGRESS", "ENTERING_PASSWORD", "Đang nhập mật khẩu tài khoản...")
            pwd_selector = 'input[type="password"], input[name="passwd"], #i0118'
            
            # Check for immediate username error
            await asyncio.sleep(1.5)
            err_el = await self._page.query_selector('#usernameError, #loginfmtError, .alert-error, div[role="alert"]')
            if err_el:
                err_text = await err_el.inner_text()
                if err_text.strip():
                    raise ValueError(f"Lỗi tài khoản: {err_text.strip()}")

            try:
                await self._page.wait_for_selector(pwd_selector, state="visible", timeout=20000)
            except Exception:
                # Might be redirected or already authenticated
                if "teams.microsoft.com" in self._page.url or "teams.cloud.microsoft" in self._page.url:
                    await self._save_and_finish()
                    return
                raise RuntimeError("Không tìm thấy ô nhập mật khẩu. Vui lòng kiểm tra lại địa chỉ email.")

            await self._page.fill(pwd_selector, password)
            await asyncio.sleep(0.5)

            # Submit password
            try:
                await self._page.locator('#idSIButton9, input[type="submit"], button[type="submit"]').first.click(timeout=8000)
            except Exception:
                await self._page.keyboard.press("Enter")

            self._set_state("IN_PROGRESS", "VERIFYING", "Đang xử lý đăng nhập & kiểm tra bảo mật...")

            # Wait and poll for outcomes:
            # 1. Teams reached
            # 2. KMSI ("Stay signed in?")
            # 3. Authenticator number matching
            # 4. OTP prompt
            # 5. Password error
            max_wait_seconds = 180  # 3 minutes for user MFA interaction
            start_time = asyncio.get_event_loop().time()

            while (asyncio.get_event_loop().time() - start_time) < max_wait_seconds:
                await asyncio.sleep(1.5)
                current_url = self._page.url

                # Check if reached Teams
                if ("teams.microsoft.com" in current_url or "teams.cloud.microsoft" in current_url) and "login" not in current_url:
                    await self._save_and_finish()
                    return

                # Check for password error or login failure
                pwd_err = await self._page.query_selector('#passwordError, .alert-error, div[role="alert"]')
                if pwd_err:
                    err_msg = await pwd_err.inner_text()
                    if err_msg.strip():
                        raise ValueError(f"Lỗi đăng nhập: {err_msg.strip()}")

                try:
                    body_text = (await self._page.inner_text("body")).lower()
                    for err_keyword in [
                        "mật khẩu không đúng",
                        "tài khoản hoặc mật khẩu không chính xác",
                        "tài khoản này không tồn tại",
                        "that password isn't correct",
                        "incorrect account or password",
                        "account or password is not correct",
                    ]:
                        if err_keyword in body_text:
                            err_container = await self._page.query_selector('#passwordError, #usernameError, .alert-error, div[role="alert"]')
                            msg = (await err_container.inner_text()).strip() if err_container else "Tài khoản hoặc mật khẩu không chính xác."
                            raise ValueError(msg)
                except ValueError:
                    raise
                except Exception:
                    pass


                # Check for KMSI ("Stay signed in?" / "Duy trì đăng nhập?")
                kmsi_btn = await self._page.query_selector('#idSIButton9, input[value="Yes"], input[value="Có"]')
                kmsi_title = await self._page.query_selector('#kmsiTitle, .kmsi-title, div[data-bind*="kmsiTitle"]')
                if kmsi_btn and kmsi_title:
                    self._set_state("IN_PROGRESS", "KMSI", "Đang tự động xác nhận 'Duy trì đăng nhập'...")
                    try:
                        await self._page.locator('#idSIButton9, input[value="Yes"], input[value="Có"]').first.click(timeout=5000)
                        await asyncio.sleep(2)
                        continue
                    except Exception:
                        pass

                # Check for Microsoft Authenticator number challenge
                # Microsoft displays numbers in #richChallengedTile or div[data-bind*="displaySign"]
                number_el = await self._page.query_selector(
                    '#richChallengedTile, div[data-bind*="displaySign"], span[id*="DisplaySign"], .displaySign, #idRichContext_DisplaySign'
                )
                if number_el:
                    code_text = (await number_el.inner_text()).strip()
                    match = re.search(r'\b\d{1,3}\b', code_text)
                    code_val = match.group(0) if match else code_text
                    if code_val:
                        self._set_state(
                            "MFA_NUMBER",
                            "MFA_NUMBER",
                            f"Vui lòng mở ứng dụng Microsoft Authenticator trên điện thoại và chọn số: {code_val}",
                            mfa_code=code_val
                        )
                        continue

                # Check for SMS / OTP code prompt
                otp_input = await self._page.query_selector('#idTxtBx_SAOTCC_OTC, input[name="otc"], input[type="tel"]')
                if otp_input:
                    self._set_state(
                        "MFA_OTP",
                        "MFA_OTP",
                        "Microsoft đã gửi mã xác thực SMS/Email. Vui lòng nhập mã OTP để tiếp tục:"
                    )
                    # Wait for user OTP input
                    self._otp_event.clear()
                    try:
                        await asyncio.wait_for(self._otp_event.wait(), timeout=60.0)
                        if self._pending_otp:
                            await self._page.fill('#idTxtBx_SAOTCC_OTC, input[name="otc"], input[type="tel"]', self._pending_otp)
                            try:
                                await self._page.locator('#idSubmit_SAOTCC_Continue, #idSIButton9, input[type="submit"]').first.click(timeout=5000)
                            except Exception:
                                await self._page.keyboard.press("Enter")
                            self._set_state("IN_PROGRESS", "VERIFYING_OTP", "Đang xác thực mã OTP...")
                            await asyncio.sleep(2)
                            continue
                    except asyncio.TimeoutError:
                        raise TimeoutError("Quá thời gian chờ nhập mã OTP (60 giây).")

            # If loop finished without redirect
            raise TimeoutError("Quá thời gian chờ hoàn thành xác thực (180 giây).")

        except Exception as e:
            logger.error(f"[{self.session_id}] Login failed: {e}")
            self._set_state("ERROR", "FAILED", str(e))
            self.error_message = str(e)
        finally:
            await self._cleanup()

    async def _save_and_finish(self):
        """Saves session cookies and marks state as SUCCESS."""
        self._set_state("IN_PROGRESS", "SAVING_COOKIES", "Đang lưu phiên làm việc và cookie...")
        os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
        await self._context.storage_state(path=self.storage_path)

        # Count cookies
        cookie_count = 0
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                cdata = json.load(f)
                cookie_count = len(cdata.get("cookies", []))
        except Exception:
            pass

        self.cookie_count = cookie_count
        self._set_state(
            "SUCCESS",
            "COMPLETED",
            f"Đăng nhập thành công! Đã lưu {cookie_count} cookies vào hệ thống. Sẵn sàng tự động thu thập tin nhắn Teams."
        )


# Global session registry
_SESSIONS: Dict[str, TeamsAuthenticatorSession] = {}


def create_auth_session(storage_path: str) -> TeamsAuthenticatorSession:
    """Creates a new auth session and registers it."""
    # Clean old sessions (> 15 mins)
    now = datetime.now(timezone.utc)
    expired = [
        sid for sid, s in _SESSIONS.items()
        if (now - s.created_at).total_seconds() > 900
    ]
    for sid in expired:
        _SESSIONS.pop(sid, None)

    sid = str(uuid.uuid4())[:8]
    sess = TeamsAuthenticatorSession(session_id=sid, storage_path=storage_path)
    _SESSIONS[sid] = sess
    return sess


def get_auth_session(session_id: str) -> Optional[TeamsAuthenticatorSession]:
    """Retrieves an existing session by ID."""
    return _SESSIONS.get(session_id)
