"""Auto Collect MS Outlook Data (100% Automated Crawler & Interceptor).

Tự động đăng nhập ngầm bằng storage_state.json,
duyệt qua hòm thư Outlook Web (OWA),
tự động click vào các email để nạp nội dung chi tiết và hội thoại,
chặn bắt response JSON mạng (service.svc, REST OWA) qua OutlookNetworkInterceptor,
lưu toàn bộ dữ liệu thô vào d:\\Working\\PTB\\raw_samples\\outlook.
"""

import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "contracts" / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "services" / "acquisition" / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "services" / "processing" / "src"))

from playwright.async_api import async_playwright, Response
from ptb_acquisition.playwright.outlook_interceptor import OutlookNetworkInterceptor
from ptb_contracts import RawEventRecord, SourceType

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ptb.auto_outlook")

OUTPUT_DIR = Path(r"d:\Working\PTB\raw_samples\outlook")
STORAGE_PATH = PROJECT_ROOT / "data" / "playwright" / "storage_state.json"


async def auto_collect_outlook(
    max_emails: int = 15,
    headless: bool = True,
):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("      TỰ ĐỘNG THU THẬP DỮ LIỆU MICROSOFT OUTLOOK (100% AUTOMATED)")
    print("=" * 70)
    print(f"[*] Phiên làm việc (Session) : {STORAGE_PATH.resolve()}")
    print(f"[*] Thư mục lưu dữ liệu thô  : {OUTPUT_DIR.resolve()}")
    print(f"[*] Số email quét tối đa     : {max_emails}")
    print(f"[*] Chế độ chạy              : {'Chạy ngầm (Headless)' if headless else 'Có giao diện (Headed)'}")
    print("-" * 70)

    captured_raw_payloads = []
    captured_records = []
    seen_ids = set()
    interceptor = OutlookNetworkInterceptor(tenant_id="fpt-outlook-auto")

    async def on_response(response: Response):
        url = response.url
        status = response.status
        if status != 200:
            return

        is_outlook = any(d in url for d in [
            "outlook.office.com", "outlook.live.com", "outlook.office365.com", "substrate.office.com"
        ])
        if not is_outlook:
            return

        is_mail_api = any(k in url.lower() for k in [
            "service.svc", "messages", "mailfolders", "conversation", "getitem", "finditem", "findconversation"
        ])
        if not is_mail_api:
            return

        try:
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type and "javascript" not in content_type:
                return
            payload = await response.json()
        except Exception:
            return

        if not isinstance(payload, (dict, list)):
            return

        action_name = url.split("action=")[1].split("&")[0] if "action=" in url else "api"
        logger.info(f"[+] Bắt gói tin Outlook: {action_name} ({url[:65]}...)")
        captured_raw_payloads.append({
            "url": url,
            "status": status,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        })

        recs = interceptor.parse_payload(payload, request_url=url)
        for r in recs:
            if r.id not in seen_ids and r.external_id not in seen_ids:
                seen_ids.add(r.id)
                seen_ids.add(r.external_id)
                captured_records.append(r)
                subj = r.raw_payload.get("Subject") or r.raw_payload.get("subject") or "No Subject"
                author = r.author_display_name or r.author_external_id or "Unknown"
                logger.info(f"   -> Email mới: [{author}] {subj[:50]}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = await browser.new_context(
            storage_state=str(STORAGE_PATH),
            viewport={"width": 1366, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        )

        page = await context.new_page()
        page.on("response", on_response)

        print("[*] Đang điều hướng tới Outlook Web (https://outlook.office.com/mail/)...")
        try:
            await page.goto("https://outlook.office.com/mail/", wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print(f"[!] Warning khi mở Outlook: {e}")

        # Chờ nạp giao diện hòm thư
        print("[*] Đang đợi hòm thư và danh sách email nạp qua mạng (12 giây)...")
        await asyncio.sleep(12)

        # Cập nhật cookies nếu có làm mới phiên
        try:
            await context.storage_state(path=str(STORAGE_PATH))
        except Exception as e:
            logger.debug("[context] Non-critical error updating storage_state: %s", e)

        # Tự động cuộn và click vào các email để nạp nội dung chi tiết
        print("[*] Bắt đầu tự động duyệt qua các email trong Inbox...")
        for i in range(max_emails):
            try:
                # Tìm các item email trong danh sách OWA
                clicked = await page.evaluate(f"""() => {{
                    const selectors = [
                        'div[role="option"]',
                        'div[data-convid]',
                        'div[aria-label*="mail"]',
                        'div[role="listitem"]',
                        'div.customScrollBar div[tabindex="0"]'
                    ];
                    let items = [];
                    for (const sel of selectors) {{
                        const found = document.querySelectorAll(sel);
                        if (found && found.length > 0) {{
                            items = Array.from(found);
                            break;
                        }}
                    }}
                    if (items.length > {i}) {{
                        const target = items[{i}];
                        target.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                        target.click();
                        return true;
                    }}
                    return false;
                }}""")

                if clicked:
                    await asyncio.sleep(2.5)
                else:
                    # Cuộn nhẹ danh sách email
                    await page.evaluate("""() => {
                        const listPane = document.querySelector('div[role="region"]') || 
                                         document.querySelector('div[role="listbox"]') ||
                                         document.querySelector('div.customScrollBar') || window;
                        if (listPane && listPane.scrollBy) listPane.scrollBy(0, 300);
                    }""")
                    await asyncio.sleep(2)
            except Exception as loop_err:
                logger.debug(f"Email click loop error: {loop_err}")

        print("\n[*] Hoàn tất duyệt email. Đang đợi 5 giây hoàn tất các network requests cuối...")
        await asyncio.sleep(5)
        await browser.close()

    # Lưu dữ liệu thô ra file
    raw_payloads_file = OUTPUT_DIR / "outlook_raw_payloads.json"
    with open(raw_payloads_file, "w", encoding="utf-8") as f:
        json.dump(captured_raw_payloads, f, indent=2, ensure_ascii=False)

    messages_jsonl_file = OUTPUT_DIR / "outlook_messages.jsonl"
    with open(messages_jsonl_file, "w", encoding="utf-8") as f:
        for r in captured_records:
            f.write(r.model_dump_json() + "\n")

    print("\n" + "=" * 70)
    print("                     KẾT QUẢ THU THẬP OUTLOOK")
    print("=" * 70)
    print(f"[✓] Số gói tin mạng JSON bắt được : {len(captured_raw_payloads)}")
    print(f"[✓] Số email chuẩn hóa bóc tách   : {len(captured_records)}")
    print(f"[✓] Tệp Raw Payloads             : {raw_payloads_file.resolve()}")
    print(f"[✓] Tệp Normalized RawEvents     : {messages_jsonl_file.resolve()}")
    print("=" * 70)

    # Viết tệp README tổng kết
    readme_path = OUTPUT_DIR / "README.md"
    readme_content = f"""# Bộ Dữ Liệu Thô Microsoft Outlook (Live Captured)

- **Thời gian thu thập**: `{datetime.now(timezone.utc).isoformat()}`
- **Tài khoản**: `{STORAGE_PATH.name}`
- **Số gói tin mạng OWA JSON**: `{len(captured_raw_payloads)}`
- **Số email trích xuất chuẩn hóa**: `{len(captured_records)}`

## Cấu trúc thư mục:
- `outlook_raw_payloads.json`: Toàn bộ các gói tin JSON gốc bắt từ `service.svc` và REST API của Outlook Web.
- `outlook_messages.jsonl`: Các sự kiện email chuẩn hóa `RawEventRecord` v1 sẵn sàng nạp vào Neo4j và xử lý qua `ProcessingPipeline`.
"""
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(readme_content)

    return captured_records


if __name__ == "__main__":
    asyncio.run(auto_collect_outlook(max_emails=12, headless=True))
