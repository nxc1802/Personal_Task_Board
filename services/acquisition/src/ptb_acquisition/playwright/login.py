"""Interactive Login CLI for Layer 1A (Teams & Outlook Web).

Chạy lệnh này trên terminal để mở trình duyệt Chromium có giao diện (Headed):
    uv run python -m ptb_acquisition.playwright.login

Người dùng đăng nhập tài khoản Microsoft 365 và hoàn thành MFA.
Toàn bộ cookie và token phiên làm việc sẽ được tự động lưu vào:
    data/playwright/storage_state.json
để Layer 1A daemon chạy ngầm bắt dữ liệu thật từ Microsoft.
"""

import argparse
import asyncio
import json
import logging
import os
import sys

import io
from pathlib import Path

from playwright.async_api import async_playwright
from ptb_acquisition.playwright.session import SessionManager, DEFAULT_STORAGE_PATH, SessionHealthState

if sys.platform == "win32":
    if sys.stdout is None or sys.stderr is None:
        try:
            curr = Path(__file__).resolve()
            repo_root = curr.parents[4]
            for p in curr.parents:
                if (p / "packages").exists() or (p / "data").exists() or (p / "pyproject.toml").exists():
                    repo_root = p
                    break
            log_dir = repo_root / "data" / "playwright"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_f = open(log_dir / "host_login.log", "a", encoding="utf-8", buffering=1)
            if sys.stdout is None:
                sys.stdout = log_f
            if sys.stderr is None:
                sys.stderr = log_f
        except Exception:
            if sys.stdout is None:
                sys.stdout = io.StringIO()
            if sys.stderr is None:
                sys.stderr = io.StringIO()
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def bring_browser_to_front():
    """Forces the interactive browser window to the foreground on the Windows host desktop."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        h_desk = user32.OpenDesktopW("default", 0, False, 0x10000000 | 0x01FF)
        if h_desk:
            user32.SetThreadDesktop(h_desk)

        def enum_cb(hwnd, lParam):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buff = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buff, length + 1)
                    title = buff.value
                    if any(k in title.lower() for k in ["teams", "microsoft", "sign in", "đăng nhập", "login"]):
                        if "gemini" not in title.lower():
                            user32.keybd_event(0x12, 0, 0, 0)
                            user32.keybd_event(0x12, 0, 2, 0)
                            curr_tid = kernel32.GetCurrentThreadId()
                            fg_hwnd = user32.GetForegroundWindow()
                            fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None)
                            target_tid = user32.GetWindowThreadProcessId(hwnd, None)
                            user32.AttachThreadInput(curr_tid, target_tid, True)
                            user32.AttachThreadInput(fg_tid, target_tid, True)
                            user32.ShowWindow(hwnd, 3) # SW_MAXIMIZE
                            user32.BringWindowToTop(hwnd)
                            user32.SetForegroundWindow(hwnd)
                            user32.AttachThreadInput(curr_tid, target_tid, False)
                            user32.AttachThreadInput(fg_tid, target_tid, False)
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stderr if sys.stderr else None)
logger = logging.getLogger("ptb.acquisition.login")


AUTH_COOKIE_NAMES = {
    "ESTSAUTH",
    "ESTSAUTHPERSISTENT",
    "skypetoken_asm",
    "skypetoken",
    "authtoken",
    "PPAuth",
    "RPSAuth",
    "WLSSC",
    "X-OWA-CANARY",
}


async def wait_for_login_and_auto_capture(
    page,
    context,
    service_name: str,
    target_domains: list,
    max_wait_seconds: int = 300,
    storage_path: str = DEFAULT_STORAGE_PATH,
) -> bool:
    """Tự động theo dõi tiến trình đăng nhập và tự động thu thập cookie ngay khi đăng nhập thành công."""
    print(f"  -> Đang theo dõi tiến trình đăng nhập {service_name}...")
    print(f"  -> Cửa sổ trình duyệt đang mở để bạn nhập tài khoản. Hệ thống sẽ TỰ ĐỘNG bắt và lưu cookie ngay khi bạn đăng nhập xong (tối đa {max_wait_seconds}s).")

    login_indicators = (
        "login.microsoftonline.com",
        "login.live.com",
        "login.windows.net",
        "/oauth2/",
        "/common/oauth2",
        "/authorize",
    )

    start_time = asyncio.get_event_loop().time()
    saw_login_screen = False
    session_mgr = SessionManager(storage_path)

    while True:
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed > max_wait_seconds:
            print(f"  [!] Hết thời gian chờ đăng nhập {service_name} ({max_wait_seconds}s).")
            return False

        if hasattr(page, "is_closed") and page.is_closed():
            print(f"  [*] Cửa sổ trình duyệt đã được đóng bởi người dùng.")
            break

        try:
            current_url = page.url.lower() if hasattr(page, "url") else ""
        except Exception:
            break

        is_on_login_screen = any(ind in current_url for ind in login_indicators)
        if is_on_login_screen:
            saw_login_screen = True

        try:
            cookies = await context.cookies()
            auth_names_upper = {x.upper() for x in AUTH_COOKIE_NAMES}
            has_auth_token = any(c.get("name", "").upper() in auth_names_upper for c in cookies)

            # Đã ở trên domain đích (và không còn ở màn hình nhập tài khoản login)
            is_on_target = any(dom in current_url for dom in target_domains) and not is_on_login_screen

            # Kiểm tra xem có MSAL tokens trong localStorage không
            has_msal = False
            has_teams_ui = False
            if is_on_target:
                try:
                    has_msal = await page.evaluate("""() => {
                        for (let i = 0; i < localStorage.length; i++) {
                            const k = (localStorage.key(i) || '').toLowerCase();
                            const v = localStorage.getItem(localStorage.key(i)) || '';
                            if ((k.includes("accesstoken") || k.includes("idtoken") || k.includes("msal.account")) && v.length > 50) {
                                return true;
                            }
                        }
                        return false;
                    }""")
                except Exception:
                    has_msal = False

                try:
                    has_teams_ui = await page.evaluate("""() => {
                        // Tự động nhấn nút 'Use the web app instead' nếu Microsoft hiển thị trang khuyến khích tải desktop app
                        const webBtn = document.querySelector('a[data-tid="use-web-app"]') || 
                                       document.querySelector('button[data-tid="use-web-app"]') ||
                                       Array.from(document.querySelectorAll('a, button')).find(el => 
                                         el.innerText && (el.innerText.includes('Use the web app') || el.innerText.includes('Sử dụng ứng dụng web'))
                                       );
                        if (webBtn && webBtn.click) {
                            try { webBtn.click(); } catch(e) {}
                        }
                        return !!(
                            document.querySelector('[data-tid="chat-list-item"]') ||
                            document.querySelector('[data-tid="message-pane-list-viewport"]') ||
                            document.querySelector('[data-tid="app-bar-chat-btn"]') ||
                            document.querySelector('[data-tid="team-channel-item"]') ||
                            (document.querySelector('#app-bar') && document.querySelector('[data-tid="app-layout"]'))
                        );
                    }""")
                except Exception:
                    has_teams_ui = False

            # Điều kiện xác nhận đăng nhập thành công:
            # 1. Không còn trên màn hình đăng nhập (login.microsoftonline.com / login.live.com)
            # 2. Đã chuyển hướng về domain đích (teams.microsoft.com / outlook.office.com)
            # 3. Phải có cookie xác thực thật (ESTSAUTH, skypetoken, authtoken) HOẶC MSAL token trong localStorage HOẶC Teams UI thật đã tải
            is_authenticated = (
                not is_on_login_screen
                and is_on_target
                and (
                    has_auth_token
                    or has_msal
                    or (saw_login_screen and has_teams_ui)
                )
            )

            # Trong 5 giây đầu, không đóng sớm nếu chưa qua màn hình đăng nhập hoặc chưa tải UI
            if elapsed < 5.0 and not saw_login_screen and not has_teams_ui:
                is_authenticated = False

            if is_authenticated:
                # Lưu thử và kiểm chứng bằng SessionManager
                await session_mgr.save_session(context)
                validated_state = session_mgr.validate_session()
                if validated_state not in (
                    SessionHealthState.LOGIN_REQUIRED,
                    SessionHealthState.UNCONFIGURED,
                    SessionHealthState.AUTH_EXPIRED,
                ):
                    print(f"  [✓] TỰ ĐỘNG PHÁT HIỆN ĐĂNG NHẬP THÀNH CÔNG VÀO {service_name.upper()}!")
                    print(f"  -> Đã lưu phiên làm việc hợp lệ ({len(cookies)} cookies) vào {storage_path}.")
                    # Hiển thị banner thông báo nổi trực tiếp trên trang web để người dùng nhìn thấy
                    try:
                        await page.evaluate("""() => {
                            const b = document.createElement('div');
                            b.id = 'ptb-login-banner';
                            b.style.cssText = 'position:fixed;top:20px;left:50%;transform:translateX(-50%);background:#10b981;color:white;padding:16px 28px;border-radius:10px;font-size:16px;font-weight:bold;z-index:9999999;box-shadow:0 8px 30px rgba(0,0,0,0.35);font-family:sans-serif;text-align:center;';
                            b.innerHTML = '🎉 ĐĂNG NHẬP THÀNH CÔNG!<br><span style="font-size:13px;font-weight:normal;opacity:0.95;">Hệ thống đã tự động lưu cookie. Cửa sổ này sẽ đóng sau 5 giây...</span>';
                            document.body.appendChild(b);
                        }""")
                    except Exception:
                        pass
                    print("  -> Giữ cửa sổ mở 5 giây để bạn thấy thông báo xác nhận...")
                    await asyncio.sleep(5.0)
                    return True
        except Exception as e:
            logger.debug(f"Check cookies err: {e}")

        await asyncio.sleep(1.0)

    # Nếu người dùng đã tự đóng trình duyệt sau khi đăng nhập xong, kiểm tra xem có cookie hợp lệ không
    try:
        cookies = await context.cookies()
        auth_names_upper = {x.upper() for x in AUTH_COOKIE_NAMES}
        if any(c.get("name", "").upper() in auth_names_upper for c in cookies):
            await session_mgr.save_session(context)
            return True
    except Exception as e:
        logger.debug("[context] Non-critical error reading final cookies: %s", e)

    return False


async def run_interactive_login(
    storage_path: str = DEFAULT_STORAGE_PATH,
    service: str = "all",
    max_wait_seconds: int = 300,
    headless: bool = False,
):
    session_mgr = SessionManager(storage_path)
    print("=" * 70)
    print("MICROSOFT 365 INTERACTIVE LOGIN (LAYER 1A - AUTO-CAPTURE COOKIES)")
    print("=" * 70)
    print(f"Target Storage Path: {storage_path}")
    print(f"Target Service     : {service}")
    print("-" * 70)
    print("[*] Đang mở trình duyệt để đăng nhập...")

    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--start-maximized",
        "--window-position=0,0",
    ]

    async with async_playwright() as p:
        # Luôn mở Chromium chuyên dụng độc lập để đảm bảo 100% cửa sổ xuất hiện trực tiếp trên màn hình Desktop của người dùng
        browser = await p.chromium.launch(
            headless=headless,
            args=launch_args,
        )

        context = await browser.new_context(no_viewport=True)
        page = await context.new_page()
        try:
            await page.bring_to_front()
        except Exception:
            pass

        if service in ("teams", "all"):
            print("\n[1/2] Đang mở Microsoft Teams Web (https://teams.microsoft.com)...")
            try:
                await page.goto("https://teams.microsoft.com", wait_until="domcontentloaded")
                await page.bring_to_front()
                bring_browser_to_front()
            except Exception as e:
                logger.warning(f"Goto Teams: {e}")

            captured_teams = await wait_for_login_and_auto_capture(
                page=page,
                context=context,
                service_name="Microsoft Teams",
                target_domains=["teams.microsoft.com", "teams.live.com"],
                max_wait_seconds=max_wait_seconds,
                storage_path=storage_path,
            )
            if captured_teams:
                print(f"  ✓ Đã tự động lưu phiên Teams vào: {storage_path}")

        captured_outlook = False
        if service in ("outlook", "all"):
            print("\n[2/2] Đang mở Microsoft Outlook Web (https://outlook.office.com)...")
            try:
                await page.goto("https://outlook.office.com", wait_until="domcontentloaded")
                await page.bring_to_front()
            except Exception as e:
                logger.warning(f"Goto Outlook: {e}")

            captured_outlook = await wait_for_login_and_auto_capture(
                page=page,
                context=context,
                service_name="Microsoft Outlook",
                target_domains=["outlook.office.com", "outlook.live.com"],
                max_wait_seconds=max_wait_seconds,
                storage_path=storage_path,
            )
            if captured_outlook:
                print(f"  ✓ Đã tự động lưu phiên Outlook vào: {storage_path}")

        # Lưu session cookies và local storage từ context
        await session_mgr.save_session(context)
        valid_state = session_mgr.validate_session()
        if valid_state not in (
            SessionHealthState.LOGIN_REQUIRED,
            SessionHealthState.UNCONFIGURED,
            SessionHealthState.AUTH_EXPIRED,
        ):
            print("\n" + "=" * 70)
            print(f"✓ ĐÃ HOÀN TẤT TỰ ĐỘNG THU THẬP & LƯU PHIÊN ĐĂNG NHẬP VÀO: {storage_path}")
            print("✓ Personal Task Board giờ đây có thể tự động thu thập ngầm dữ liệu thật!")
            print("=" * 70)
        else:
            print(f"\n[*] Đã lưu phiên làm việc vào {storage_path} (Trạng thái thẩm định: {valid_state.value}).")
        try:
            await browser.close()
        except Exception as e:
            logger.debug("[context] Non-critical error closing browser: %s", e)


def import_cookie_file(input_file: str, storage_path: str = DEFAULT_STORAGE_PATH):
    """Nhập file cookie JSON có sẵn vào storage_state.json."""
    if not os.path.isfile(input_file):
        print(f"Lỗi: Không tìm thấy file {input_file}", file=sys.stderr)
        sys.exit(1)

    with open(input_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Chuẩn hóa định dạng nếu cần
    if isinstance(data, list):
        data = {"cookies": data, "origins": []}

    os.makedirs(os.path.dirname(os.path.abspath(storage_path)), exist_ok=True)
    with open(storage_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"✓ Đã import thành công {len(data.get('cookies', []))} cookies vào: {storage_path}")


def main():
    parser = argparse.ArgumentParser(description="Đăng nhập Microsoft 365 để lấy session cookie cho Layer 1A")
    parser.add_argument("--service", choices=["teams", "outlook", "all"], default="all", help="Dịch vụ cần đăng nhập")
    parser.add_argument("--storage-path", default=DEFAULT_STORAGE_PATH, help="Đường dẫn file storage_state.json")
    parser.add_argument("--import-file", help="Nhập file JSON cookies có sẵn thay vì đăng nhập trên giao diện")

    args = parser.parse_args()

    if args.import_file:
        import_cookie_file(args.import_file, args.storage_path)
    else:
        asyncio.run(run_interactive_login(args.storage_path, args.service))


if __name__ == "__main__":
    main()
