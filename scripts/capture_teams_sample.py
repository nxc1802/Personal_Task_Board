"""Capture Teams Raw Samples & Interactive Login Script.

Mở trình duyệt để người dùng đăng nhập Microsoft Teams,
tự động bắt các gói tin phản hồi mạng (network response JSON),
chuẩn hóa và lưu mẫu dữ liệu vào thư mục d:\\Working\\PTB\\raw_samples\\teams.
"""

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys

# Đảm bảo import được các module của dự án
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "contracts" / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "services" / "acquisition" / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "services" / "processing" / "src"))

from playwright.async_api import async_playwright, Response
from ptb_acquisition.playwright.session import (
    DEFAULT_STORAGE_PATH,
    SessionHealthState,
    SessionManager,
)
import hashlib
from ptb_acquisition.playwright.teams_interceptor import TeamsNetworkInterceptor
from ptb_contracts import ProcessingStatus, RawEventRecord, SourceType

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ptb.capture_teams")

OUTPUT_DIR = Path(r"d:\Working\PTB\raw_samples\teams")
STORAGE_PATH = PROJECT_ROOT / "data" / "playwright" / "storage_state.json"


async def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    STORAGE_PATH.parent.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("      MICROSOFT TEAMS SAMPLE DATA CAPTURE (LOCAL INTERCEPTOR)")
    print("=" * 70)
    print(f"[*] Thư mục lưu dữ liệu mẫu : {OUTPUT_DIR.resolve()}")
    print(f"[*] File lưu phiên đăng nhập : {STORAGE_PATH.resolve()}")
    print("-" * 70)

    session_mgr = SessionManager(str(STORAGE_PATH))
    existing_storage = str(STORAGE_PATH) if STORAGE_PATH.exists() and STORAGE_PATH.stat().st_size > 50 else None

    captured_raw_payloads = []
    captured_records = []
    seen_message_ids = set()
    interceptor = TeamsNetworkInterceptor(tenant_id="local-teams-sample")

    async def on_response(response: Response):
        url = response.url
        status = response.status

        # Bỏ qua nếu lỗi hoặc redirect sang trang đăng nhập
        if status != 200:
            return

        # Lọc các URL endpoint chứa dữ liệu từ Teams / Skype backend
        is_relevant_url = any(dom in url for dom in ["teams.microsoft.com", "skype.com", "teams.live.com"])
        has_msg_keyword = any(kw in url.lower() for kw in ["api", "chat", "msg", "conversation", "thread", "message", "channel"])

        if not (is_relevant_url and has_msg_keyword):
            return

        # Thử đọc JSON
        try:
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type and "javascript" not in content_type:
                return
            payload = await response.json()
        except Exception:
            return

        if not isinstance(payload, (dict, list)):
            return

        # Kiểm tra xem payload có chứa messages / conversations không
        has_message_data = False
        if isinstance(payload, list) and len(payload) > 0:
            has_message_data = any(isinstance(x, dict) and ("content" in x or "body" in x or "id" in x) for x in payload)
        elif isinstance(payload, dict):
            has_message_data = bool(
                "messages" in payload
                or "value" in payload
                or ("id" in payload and ("content" in payload or "body" in payload))
            )

        if has_message_data:
            # Lưu raw payload
            captured_raw_payloads.append({
                "url": url,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "status": status,
                "payload": payload,
            })

            # Chuẩn hóa qua TeamsNetworkInterceptor
            try:
                records = interceptor.parse_payload(payload, request_url=url)
                for rec in records:
                    if rec.external_id not in seen_message_ids:
                        seen_message_ids.add(rec.external_id)
                        captured_records.append(rec)
                        print(f"  [+] Bắt được tin nhắn từ: {rec.author_display_name or 'Ẩn danh'} | ID: {rec.external_id}")
                        snippet = (rec.normalized_text or "").replace("\n", " ")[:60]
                        print(f"      Nội dung: \"{snippet}...\"")
            except Exception as parse_err:
                logger.debug(f"Lỗi parse: {parse_err}")

    async with async_playwright() as p:
        print("[*] Đang khởi chạy trình duyệt trực tiếp trên màn hình...")
        launch_kwargs = {
            "headless": False,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
        }
        browser = None
        for ch in ["msedge", "chrome", None]:
            try:
                kw = dict(launch_kwargs)
                if ch:
                    kw["channel"] = ch
                browser = await p.chromium.launch(**kw)
                print(f"[*] Đã mở trình duyệt {ch.upper() if ch else 'Chromium'} trực tiếp trên màn hình của bạn!")
                break
            except Exception as e:
                logger.debug(f"Không thể mở với channel={ch}: {e}")
                continue

        if browser is None:
            browser = await p.chromium.launch(**launch_kwargs)

        context_kwargs = {"no_viewport": True}
        if existing_storage:
            print(f"[*] Tải phiên làm việc sẵn có từ {existing_storage}")
            context_kwargs["storage_state"] = existing_storage

        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()

        # Hook listener vào cả page lẫn context
        page.on("response", on_response)
        context.on("response", on_response)

        print("[*] Đang điều hướng tới Microsoft Teams Web...")
        print("  -> URL: https://teams.microsoft.com/v2/")
        print("-" * 70)
        print(">>> HƯỚNG DẪN:")
        print("  1. Nếu chưa đăng nhập, vui lòng nhập tài khoản và 2FA trên cửa sổ trình duyệt.")
        print("  2. Khi vào giao diện Teams, bạn hãy click vào một vài đoạn chat hoặc kênh.")
        print("  3. Sau khi bắt đủ tin nhắn hoặc kiểm tra xong, bạn chỉ cần ĐÓNG cửa sổ trình duyệt.")
        print("-" * 70)

        try:
            await page.goto("https://teams.microsoft.com/v2/", wait_until="domcontentloaded", timeout=60000)
            await page.bring_to_front()
        except Exception as goto_err:
            logger.warning(f"Điều hướng ban đầu: {goto_err}")

        # Vòng lặp chờ người dùng tương tác, tự động sweep và lưu cookies
        max_duration = 300  # tối đa 5 phút
        elapsed = 0
        logged_in_saved = False

        while elapsed < max_duration:
            # Kiểm tra nếu người dùng đã đóng trình duyệt
            if page.is_closed():
                print("\n[*] Cửa sổ trình duyệt đã được đóng. Tiến hành lưu dữ liệu mẫu...")
                break

            try:
                curr_url = page.url.lower()
            except Exception:
                break

            # Kiểm tra xem đã vượt qua màn hình login chưa
            is_login_page = any(x in curr_url for x in ["login.microsoftonline.com", "login.live.com", "/oauth2/"])
            is_teams_app = "teams.microsoft.com" in curr_url and not is_login_page

            if is_teams_app:
                # Cập nhật session cookie liên tục để không bị mất khi đóng trình duyệt
                try:
                    await session_mgr.save_session(context)
                except Exception as save_err:
                    logger.debug(f"Lỗi lưu session: {save_err}")

                # Tự động trích xuất tin nhắn đang hiển thị trên giao diện Teams (DOM fallback)
                try:
                    dom_msgs = await page.evaluate("""() => {
                        const results = [];
                        const articles = document.querySelectorAll('[role="article"], [data-tid="chat-pane-message"]');
                        articles.forEach(art => {
                            const bodyEl = art.querySelector('[data-tid="message-body"], .ui-chat__message__content, [data-tid="chat-pane-message-content"]') || art;
                            const authorEl = art.querySelector('[data-tid="message-author-name"], .ui-chat__message__author, strong, h5');
                            const timeEl = art.querySelector('time, [data-tid="message-timestamp"]');
                            const text = (bodyEl.innerText || bodyEl.textContent || '').trim();
                            if (text.length > 0 && text.length < 5000) {
                                results.push({
                                    text: text,
                                    author: authorEl ? (authorEl.innerText || authorEl.textContent || '').trim() : 'Unknown',
                                    time: timeEl ? (timeEl.getAttribute('datetime') || timeEl.innerText || '') : '',
                                });
                            }
                        });
                        return results;
                    }""")

                    if dom_msgs:
                        for item in dom_msgs:
                            msg_text = item.get("text", "").strip()
                            if not msg_text or len(msg_text) < 2:
                                continue
                            author = item.get("author") or "Teams User"
                            h = hashlib.sha256(f"{author}:{msg_text}".encode("utf-8")).hexdigest()[:16]
                            msg_id = f"teams-dom-{h}"
                            if msg_id not in seen_message_ids:
                                seen_message_ids.add(msg_id)
                                now_utc = datetime.now(timezone.utc)
                                rec = RawEventRecord(
                                    id=f"raw-teams-{h}",
                                    tenant_id="local-teams-sample",
                                    source_type=SourceType.MS_TEAMS_WEB,
                                    external_id=msg_id,
                                    idempotency_key=hashlib.sha256(f"teams:{msg_id}:{h}".encode("utf-8")).hexdigest(),
                                    event_timestamp=now_utc,
                                    captured_at=now_utc,
                                    author_display_name=author,
                                    author_external_id=author.lower().replace(" ", "."),
                                    conversation_or_project_id="teams-active-chat",
                                    deep_link=f"https://teams.microsoft.com/v2/",
                                    raw_payload=item,
                                    normalized_text=msg_text,
                                    content_hash=h,
                                    processing_status=ProcessingStatus.PENDING,
                                    created_at=now_utc,
                                )
                                captured_records.append(rec)
                                captured_raw_payloads.append({
                                    "url": page.url,
                                    "captured_at": now_utc.isoformat(),
                                    "type": "dom_extracted",
                                    "payload": item,
                                })
                                print(f"  [+] Trích xuất tin nhắn từ: {author} | ID: {msg_id}")
                                print(f"      Nội dung: \"{msg_text[:60]}...\"")
                except Exception as dom_err:
                    logger.debug(f"DOM extract error: {dom_err}")

            # Thử tự động cuộn nhẹ viewport tin nhắn nếu có để kích hoạt network request
            if is_teams_app and elapsed % 4 == 0:
                try:
                    await page.evaluate("""() => {
                        const el = document.querySelector('[data-tid="message-pane-list-viewport"]') ||
                                   document.querySelector('div[role="feed"]') ||
                                   document.querySelector('.message-pane');
                        if (el && el.scrollBy) {
                            el.scrollBy(0, -300);
                        }
                    }""")
                except Exception as e:
                    logger.debug("[context] Non-critical error during scroll: %s", e)

            await asyncio.sleep(2)
            elapsed += 2

        # Lưu lại session một lần nữa nếu browser còn mở
        if not page.is_closed():
            try:
                await session_mgr.save_session(context)
            except Exception as e:
                logger.debug("[context] Non-critical error saving session: %s", e)
            try:
                await browser.close()
            except Exception as e:
                logger.debug("[context] Non-critical error closing browser: %s", e)

    # Xuất dữ liệu ra folder d:\Working\PTB\raw_samples\teams
    print("\n" + "=" * 70)
    print("                KẾT QUẢ THU THẬP DỮ LIỆU MẪU TEAMS")
    print("=" * 70)
    print(f"- Số gói tin mạng (raw payloads) bắt được: {len(captured_raw_payloads)}")
    print(f"- Số tin nhắn trích xuất thành công    : {len(captured_records)}")

    # 1. Ghi teams_raw_payloads.json
    raw_payloads_file = OUTPUT_DIR / "teams_raw_payloads.json"
    with open(raw_payloads_file, "w", encoding="utf-8") as f:
        json.dump(captured_raw_payloads, f, ensure_ascii=False, indent=2, default=str)
    print(f"  ✓ Đã ghi gói tin thô vào : {raw_payloads_file.resolve()}")

    # 2. Ghi teams_messages.jsonl
    messages_jsonl_file = OUTPUT_DIR / "teams_messages.jsonl"
    with open(messages_jsonl_file, "w", encoding="utf-8") as f:
        for rec in captured_records:
            rec_dict = {
                "id": rec.id,
                "external_id": rec.external_id,
                "tenant_id": rec.tenant_id,
                "source_type": rec.source_type.value if hasattr(rec.source_type, "value") else str(rec.source_type),
                "author_display_name": rec.author_display_name,
                "author_external_id": rec.author_external_id,
                "conversation_or_project_id": rec.conversation_or_project_id,
                "deep_link": rec.deep_link,
                "event_timestamp": rec.event_timestamp.isoformat() if rec.event_timestamp else None,
                "captured_at": rec.captured_at.isoformat() if rec.captured_at else None,
                "normalized_text": rec.normalized_text,
                "content_hash": rec.content_hash,
                "idempotency_key": rec.idempotency_key,
                "raw_payload": rec.raw_payload,
            }
            f.write(json.dumps(rec_dict, ensure_ascii=False) + "\n")
    print(f"  ✓ Đã ghi danh sách tin nhắn vào: {messages_jsonl_file.resolve()}")

    # 3. Ghi file README / tóm tắt mẫu dữ liệu
    readme_file = OUTPUT_DIR / "README.md"
    readme_content = f"""# Microsoft Teams Raw Samples

- **Thời gian thu thập**: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}
- **Tổng số gói tin mạng thô**: {len(captured_raw_payloads)}
- **Tổng số tin nhắn trích xuất**: {len(captured_records)}
- **Nguồn thu thập**: Microsoft Teams Web qua Layer 1A Playwright Network Interceptor
- **Các tệp đính kèm**:
  - `teams_raw_payloads.json`: Chứa nguyên vẹn các response JSON trả về từ API nội bộ Microsoft Teams (`api/chats`, `conversations`, `messages`, `skype.com`).
  - `teams_messages.jsonl`: Dữ liệu tin nhắn đã được chuẩn hóa thành `RawEventRecord` (Contract C12) của Personal Task Board.
"""
    with open(readme_file, "w", encoding="utf-8") as f:
        f.write(readme_content)
    print(f"  ✓ Đã tạo file mô tả: {readme_file.resolve()}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
