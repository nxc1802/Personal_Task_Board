"""Playwright Orchestrator & Runner (Layer 1A).

Hỗ trợ 2 chế độ:
1. Login Mode (headless=False): Cho phép người dùng đăng nhập tương tác & lưu storage_state.json.
2. Interceptor Daemon (headless=True): Chạy ngầm, tái sử dụng session cookie và bắt network responses.
"""

import asyncio
import logging
import os
from typing import Optional

from playwright.async_api import async_playwright, BrowserContext, Page
from ptb_acquisition.playwright.session import SessionManager, DEFAULT_STORAGE_PATH
from ptb_acquisition.playwright.teams_interceptor import TeamsNetworkInterceptor
from ptb_acquisition.playwright.outlook_interceptor import OutlookNetworkInterceptor
from ptb_acquisition.queue import LocalIngestionQueue

logger = logging.getLogger("ptb.acquisition.playwright.runner")


class PlaywrightOrchestrator:
    def __init__(
        self,
        queue: Optional[LocalIngestionQueue] = None,
        storage_path: str = DEFAULT_STORAGE_PATH,
        headless: bool = True,
    ):
        self.queue = queue
        self.session_mgr = SessionManager(storage_path)
        self.headless = headless
        self.teams_interceptor = TeamsNetworkInterceptor(queue=queue)
        self.outlook_interceptor = OutlookNetworkInterceptor(queue=queue)
        self._running = False

    async def login_interactive(self, target_service: str = "all") -> None:
        """Mở trình duyệt có giao diện để người dùng hoàn tất đăng nhập và 2FA."""
        print(f"\n[+] Khởi chạy trình duyệt (Headed Mode) để đăng nhập {target_service}...")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()

            if target_service in ("teams", "all"):
                print("  -> Đang chuyển hướng tới Teams Web: https://teams.microsoft.com")
                await page.goto("https://teams.microsoft.com", wait_until="domcontentloaded")
                print("  -> Vui lòng đăng nhập và hoàn thành 2FA trên cửa sổ trình duyệt...")
                print("  -> Nhấn Enter trên terminal này sau khi đăng nhập thành công vào giao diện chính.")
                # Chờ người dùng đăng nhập xong
                await asyncio.get_event_loop().run_in_executor(None, input, "  [Nhấn Enter khi đã đăng nhập xong] ")

            if target_service in ("outlook", "all"):
                print("  -> Đang chuyển hướng tới Outlook Web: https://outlook.office.com")
                await page.goto("https://outlook.office.com", wait_until="domcontentloaded")
                await asyncio.get_event_loop().run_in_executor(None, input, "  [Nhấn Enter khi đã kiểm tra xong Outlook] ")

            # Lưu session
            await self.session_mgr.save_session(context)
            print(f"  ✓ Đã lưu thành công phiên đăng nhập vào: {self.session_mgr.storage_path}")
            await browser.close()

    async def start_interceptor(self, timeout_seconds: Optional[int] = None) -> None:
        """Chạy daemon headless bắt gói tin mạng."""
        if not self.session_mgr.has_valid_session():
            raise RuntimeError(
                f"Chưa có session hợp lệ tại {self.session_mgr.storage_path}. "
                "Vui lòng chạy login_interactive() trước!"
            )

        self._running = True
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)
            context = await browser.new_context(storage_state=str(self.session_mgr.storage_path))

            # Mở page Teams
            teams_page = await context.new_page()
            teams_page.on("response", lambda r: asyncio.create_task(self.teams_interceptor.handle_response(r)))
            logger.info("Đang điều hướng tới Teams Web...")
            await teams_page.goto("https://teams.microsoft.com", wait_until="domcontentloaded")

            # Mở page Outlook
            outlook_page = await context.new_page()
            outlook_page.on("response", lambda r: asyncio.create_task(self.outlook_interceptor.handle_response(r)))
            logger.info("Đang điều hướng tới Outlook Web...")
            await outlook_page.goto("https://outlook.office.com", wait_until="domcontentloaded")

            print("[✓] Layer 1A Network Interceptors đã được kích hoạt thành công!")
            elapsed = 0
            while self._running:
                await asyncio.sleep(5)
                elapsed += 5
                if timeout_seconds and elapsed >= timeout_seconds:
                    break

            await browser.close()

    def stop(self) -> None:
        self._running = False
