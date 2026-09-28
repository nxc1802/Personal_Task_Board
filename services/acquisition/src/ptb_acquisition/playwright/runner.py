"""Playwright Orchestrator & Runner (Layer 1A).

Hỗ trợ 2 chế độ:
1. Login Mode (headless=False): Cho phép người dùng đăng nhập tương tác & lưu storage_state.json.
2. Interceptor Daemon (headless=True): Chạy ngầm, xác thực phiên thực tế (verify_authenticated_session),
   thực hiện Bootstrap Capture Sweep để backfill lịch sử chat/inbox và bắt network responses liên tục.
"""

import asyncio
import logging
import os
from typing import Any, Dict, Optional

from playwright.async_api import async_playwright, BrowserContext, Page
from ptb_acquisition.playwright.session import (
    DEFAULT_STORAGE_PATH,
    SessionHealthState,
    SessionManager,
)
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
        pipeline: Optional[Any] = None,
        raw_event_repo: Optional[Any] = None,
        tenant_id: str = "local-microsoft",
    ):
        self.queue = queue
        self.pipeline = pipeline
        self.raw_event_repo = raw_event_repo
        self.session_mgr = SessionManager(storage_path)
        self.headless = headless
        self.tenant_id = tenant_id
        self.teams_interceptor = TeamsNetworkInterceptor(
            queue=queue,
            tenant_id=f"{tenant_id}-teams",
            pipeline=pipeline,
            raw_event_repo=raw_event_repo,
        )
        self.outlook_interceptor = OutlookNetworkInterceptor(
            queue=queue,
            tenant_id=f"{tenant_id}-outlook",
            pipeline=pipeline,
            raw_event_repo=raw_event_repo,
        )
        self._running = False
        self.health_state = SessionHealthState.UNCONFIGURED
        self.sweep_status = "PENDING"
        self.sweep_stats: Dict[str, Any] = {
            "status": "PENDING",
            "teams_events_captured": 0,
            "outlook_events_captured": 0,
            "total_captured_events": 0,
            "oldest_captured_event": None,
            "newest_captured_event": None,
        }

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
                await asyncio.get_event_loop().run_in_executor(None, input, "  [Nhấn Enter khi đã đăng nhập xong] ")

            if target_service in ("outlook", "all"):
                print("  -> Đang chuyển hướng tới Outlook Web: https://outlook.office.com")
                await page.goto("https://outlook.office.com", wait_until="domcontentloaded")
                await asyncio.get_event_loop().run_in_executor(None, input, "  [Nhấn Enter khi đã kiểm tra xong Outlook] ")

            # Lưu session
            await self.session_mgr.save_session(context)
            self.health_state = SessionHealthState.HEALTHY
            print(f"  ✓ Đã lưu thành công phiên đăng nhập vào: {self.session_mgr.storage_path}")
            await browser.close()

    async def sweep_teams(self, page: Page, max_scrolls: int = 5) -> int:
        """Thực hiện navigation/scroll có kiểm soát trên Teams để kích hoạt nạp lịch sử chat."""
        initial_count = len(self.teams_interceptor.captured_records)
        try:
            if hasattr(page, "wait_for_timeout"):
                await page.wait_for_timeout(2000)

            # Các selector phổ biến của danh sách hội thoại / khung tin nhắn Teams
            container_selectors = [
                '[data-tid="message-pane-list-viewport"]',
                'div[role="feed"]',
                'div[role="region"]',
                'div[data-tid="chat-list"]',
                '.message-pane',
            ]
            for sel in container_selectors:
                try:
                    if hasattr(page, "wait_for_selector"):
                        await page.wait_for_selector(sel, timeout=2000)
                        break
                except Exception:
                    pass

            for step in range(max_scrolls):
                if not self._running and self._running is not None:
                    break
                try:
                    if hasattr(page, "evaluate"):
                        await page.evaluate("""() => {
                            const el = document.querySelector('[data-tid="message-pane-list-viewport"]') ||
                                       document.querySelector('div[role="feed"]') ||
                                       document.querySelector('div[role="region"]') ||
                                       window;
                            if (el.scrollBy) {
                                el.scrollBy(0, -500);
                            } else if (el === window) {
                                window.scrollBy(0, -500);
                            }
                        }""")
                    if hasattr(page, "wait_for_timeout"):
                        await page.wait_for_timeout(1000)
                except Exception as scroll_err:
                    logger.debug(f"Teams sweep scroll step {step} error: {scroll_err}")
                    break
        except Exception as e:
            logger.warning(f"Lỗi khi thực hiện sweep_teams: {e}")

        captured = len(self.teams_interceptor.captured_records) - initial_count
        return max(captured, 0)

    async def sweep_outlook(self, page: Page, max_scrolls: int = 5) -> int:
        """Thực hiện navigation/scroll có kiểm soát trên Outlook để kích hoạt nạp danh sách email/inbox."""
        initial_count = len(self.outlook_interceptor.captured_records)
        try:
            if hasattr(page, "wait_for_timeout"):
                await page.wait_for_timeout(2000)

            list_selectors = [
                '[aria-label="Message list"]',
                '[role="listbox"]',
                'div[role="treegrid"]',
                'div[role="main"]',
            ]
            for sel in list_selectors:
                try:
                    if hasattr(page, "wait_for_selector"):
                        await page.wait_for_selector(sel, timeout=2000)
                        break
                except Exception:
                    pass

            for step in range(max_scrolls):
                if not self._running and self._running is not None:
                    break
                try:
                    if hasattr(page, "evaluate"):
                        await page.evaluate("""() => {
                            const el = document.querySelector('[aria-label="Message list"]') ||
                                       document.querySelector('div[role="listbox"]') ||
                                       document.querySelector('div[role="main"]') ||
                                       window;
                            if (el.scrollBy) {
                                el.scrollBy(0, 500);
                            } else if (el === window) {
                                window.scrollBy(0, 500);
                            }
                        }""")
                    if hasattr(page, "wait_for_timeout"):
                        await page.wait_for_timeout(1000)
                except Exception as scroll_err:
                    logger.debug(f"Outlook sweep scroll step {step} error: {scroll_err}")
                    break
        except Exception as e:
            logger.warning(f"Lỗi khi thực hiện sweep_outlook: {e}")

        captured = len(self.outlook_interceptor.captured_records) - initial_count
        return max(captured, 0)

    async def run_bootstrap_sweep(
        self,
        teams_page: Optional[Page] = None,
        outlook_page: Optional[Page] = None,
        max_scrolls: int = 5,
    ) -> Dict[str, Any]:
        """Kích hoạt Bootstrap Capture Sweep trên Teams và Outlook (Backfill qua web UI)."""
        self.sweep_status = "RUNNING"
        teams_captured = 0
        outlook_captured = 0

        if teams_page:
            logger.info("Đang thực hiện Bootstrap Capture Sweep cho Teams...")
            teams_captured = await self.sweep_teams(teams_page, max_scrolls=max_scrolls)

        if outlook_page:
            logger.info("Đang thực hiện Bootstrap Capture Sweep cho Outlook...")
            outlook_captured = await self.sweep_outlook(outlook_page, max_scrolls=max_scrolls)

        all_events = self.teams_interceptor.captured_records + self.outlook_interceptor.captured_records
        oldest_ts = None
        newest_ts = None
        if all_events:
            ts_list = [e.event_timestamp for e in all_events if e.event_timestamp]
            if ts_list:
                oldest_ts = min(ts_list).isoformat()
                newest_ts = max(ts_list).isoformat()

        self.sweep_status = "COMPLETED"
        self.sweep_stats = {
            "status": "COMPLETED",
            "teams_events_captured": teams_captured,
            "outlook_events_captured": outlook_captured,
            "total_captured_events": len(all_events),
            "oldest_captured_event": oldest_ts,
            "newest_captured_event": newest_ts,
        }
        logger.info(f"Bootstrap Capture Sweep hoàn thành: {self.sweep_stats}")
        return self.sweep_stats

    async def start_interceptor(
        self,
        timeout_seconds: Optional[int] = None,
        run_sweep: bool = True,
        sweep_scrolls: int = 5,
    ) -> None:
        """Chạy daemon headless bắt gói tin mạng và thực hiện bootstrap capture sweep."""
        validation_state = self.session_mgr.validate_session()
        if validation_state in (
            SessionHealthState.UNCONFIGURED,
            SessionHealthState.LOGIN_REQUIRED,
            SessionHealthState.AUTH_EXPIRED,
            SessionHealthState.ERROR,
        ):
            self.health_state = validation_state
            raise RuntimeError(
                f"Session không hợp lệ ({validation_state.value}) tại {self.session_mgr.storage_path}. "
                "Vui lòng chạy login_interactive() trước!"
            )

        self._running = True
        self.health_state = SessionHealthState.STARTING

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)
            context = await browser.new_context(storage_state=str(self.session_mgr.storage_path))

            # Xác thực session với Microsoft trước khi tiến hành
            health = await self.session_mgr.verify_authenticated_session(context)
            if health == SessionHealthState.AUTH_EXPIRED:
                self.health_state = SessionHealthState.AUTH_EXPIRED
                logger.error("Session Microsoft đã hết hạn (AUTH_EXPIRED). Cần đăng nhập lại.")
                await browser.close()
                self._running = False
                return

            self.health_state = health

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

            # Bootstrap Capture Sweep: Khi khởi động chạy backfill cho Teams/Outlook
            if run_sweep:
                logger.info("Bắt đầu Bootstrap Capture Sweep cho Teams và Outlook...")
                await self.run_bootstrap_sweep(teams_page, outlook_page, max_scrolls=sweep_scrolls)

            print("[✓] Layer 1A Network Interceptors & Bootstrap Sweep đã sẵn sàng!")
            elapsed = 0
            while self._running:
                await asyncio.sleep(5)
                elapsed += 5
                if timeout_seconds and elapsed >= timeout_seconds:
                    break

            await browser.close()

    def get_health(self) -> Dict[str, Any]:
        """Trả về thông tin trạng thái sức khỏe của session và runner."""
        return {
            "status": self.health_state.value,
            "session_valid": self.session_mgr.has_valid_session(),
            "bootstrap_sweep": self.sweep_status,
            "sweep_stats": self.sweep_stats,
            "teams_captured": len(self.teams_interceptor.captured_records),
            "outlook_captured": len(self.outlook_interceptor.captured_records),
        }

    def stop(self) -> None:
        self._running = False
