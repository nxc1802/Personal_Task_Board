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
from ptb_acquisition.playwright.session import SessionManager, DEFAULT_STORAGE_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ptb.acquisition.login")


async def run_interactive_login(storage_path: str = DEFAULT_STORAGE_PATH, service: str = "all"):
    session_mgr = SessionManager(storage_path)
    print("=" * 70)
    print("MICROSOFT 365 INTERACTIVE LOGIN (LAYER 1A - REAL SESSION CAPTURE)")
    print("=" * 70)
    print(f"Target Storage Path: {storage_path}")
    print(f"Target Service     : {service}")
    print("-" * 70)
    print("[*] Đang mở trình duyệt Chromium trên màn hình máy tính của bạn...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        if service in ("teams", "all"):
            print("\n[1/2] Đang mở Microsoft Teams Web...")
            await page.goto("https://teams.microsoft.com", wait_until="domcontentloaded")
            print("  -> Vui lòng nhập email, mật khẩu và hoàn thành MFA trên cửa sổ trình duyệt vừa mở.")
            print("  -> Chờ cho đến khi giao diện Teams chính tải xong.")
            input("  [Nhấn Enter tại terminal này sau khi bạn đã đăng nhập xong vào Teams] ")

        if service in ("outlook", "all"):
            print("\n[2/2] Đang mở Microsoft Outlook Web...")
            await page.goto("https://outlook.office.com", wait_until="domcontentloaded")
            input("  [Nhấn Enter tại terminal này sau khi bạn đã thấy hòm thư Outlook] ")

        # Lưu session cookies và tokens
        await session_mgr.save_session(context)
        print("\n" + "=" * 70)
        print(f"✓ ĐÃ LƯU PHIÊN ĐĂNG NHẬP THẬT THÀNH CÔNG VÀO: {storage_path}")
        print("✓ Layer 1A Daemon giờ đây có thể bắt gói tin JSON thật từ Microsoft!")
        print("=" * 70)
        await browser.close()


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
