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

from playwright.async_api import async_playwright
from ptb_acquisition.playwright.session import SessionManager, DEFAULT_STORAGE_PATH, SessionHealthState

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ptb.acquisition.login")

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.debug("[context] Non-critical error reconfiguring stdio: %s", e)


AUTH_COOKIE_NAMES = {
    "ESTSAUTH",
    "ESTSAUTHPERSISTENT",
    "skypetoken_asm",
    "skypetoken",
    "authtoken",
    "PPAuth",
    "RPSAuth",
    "WLSSC",
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
                    await asyncio.sleep(2.0)
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
    print("[*] Đang mở trình duyệt Chromium để đăng nhập...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
        )
        context = await browser.new_context(no_viewport=True)
        page = await context.new_page()

        if service in ("teams", "all"):
            print("\n[1/2] Đang mở Microsoft Teams Web (https://teams.microsoft.com)...")
            try:
                await page.goto("https://teams.microsoft.com", wait_until="domcontentloaded")
                await page.bring_to_front()
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

        # Chỉ lưu và in thông báo thành công nếu có phiên hợp lệ
        if session_mgr.validate_session() not in (
            SessionHealthState.LOGIN_REQUIRED,
            SessionHealthState.UNCONFIGURED,
            SessionHealthState.AUTH_EXPIRED,
        ):
            await session_mgr.save_session(context)
            print("\n" + "=" * 70)
            print(f"✓ ĐÃ HOÀN TẤT TỰ ĐỘNG THU THẬP & LƯU PHIÊN ĐĂNG NHẬP VÀO: {storage_path}")
            print("✓ Personal Task Board giờ đây có thể tự động thu thập ngầm dữ liệu thật!")
            print("=" * 70)
        else:
            print(f"\n[!] Kết thúc phiên đăng nhập: Chưa phát hiện token xác thực Microsoft. Phiên chưa được lưu.")
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
