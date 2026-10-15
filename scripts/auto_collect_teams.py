"""Auto Collect MS Teams Data (100% Automated Crawler & Interceptor).

Tự động đăng nhập ngầm bằng storage_state.json,
duyệt qua toàn bộ danh sách hội thoại/kênh Teams,
tự động click vào từng đoạn chat, tự động cuộn (scroll-up) để nạp lịch sử,
chặn bắt response JSON mạng và trích xuất tin nhắn,
lưu toàn bộ dữ liệu thô vào d:\\Working\\PTB\\raw_samples\\teams.
"""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "contracts" / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "services" / "acquisition" / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "services" / "processing" / "src"))

from playwright.async_api import async_playwright, Response
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
logger = logging.getLogger("ptb.auto_teams")

OUTPUT_DIR = Path(r"d:\Working\PTB\raw_samples\teams")
STORAGE_PATH = PROJECT_ROOT / "data" / "playwright" / "storage_state.json"


async def auto_collect_teams(
    max_chats: int = 15,
    scrolls_per_chat: int = 4,
    headless: bool = True,
):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("      TỰ ĐỘNG THU THẬP DỮ LIỆU MICROSOFT TEAMS (100% AUTOMATED)")
    print("=" * 70)
    print(f"[*] Phiên làm việc (Session) : {STORAGE_PATH.resolve()}")
    print(f"[*] Thư mục lưu dữ liệu thô  : {OUTPUT_DIR.resolve()}")
    print(f"[*] Số hội thoại tối đa      : {max_chats}")
    print(f"[*] Số lần cuộn mỗi chat     : {scrolls_per_chat}")
    print(f"[*] Chế độ chạy              : {'Chạy ngầm (Headless)' if headless else 'Có giao diện (Headed)'}")
    print("-" * 70)

    has_valid_session = STORAGE_PATH.exists() and STORAGE_PATH.stat().st_size > 100
    actual_headless = headless if has_valid_session else False
    if not has_valid_session:
        print("[!] Chưa có phiên đăng nhập sẵn có. Hệ thống sẽ mở trình duyệt để bạn đăng nhập lần đầu...")

    captured_raw_payloads = []
    captured_records = []
    seen_message_ids = set()
    interceptor = TeamsNetworkInterceptor(tenant_id="fpt-teams-auto")

    async def on_response(response: Response):
        url = response.url
        status = response.status
        if status != 200:
            return

        is_ms_domain = any(d in url for d in [
            "teams.cloud.microsoft", "teams.microsoft.com", "skype.com", "office.com", "substrate.office.com"
        ])
        if not is_ms_domain:
            return

        has_chat_kw = any(k in url.lower() for k in [
            "message", "chat", "conversation", "thread", "channel", "graphql", "spaces", "users/me"
        ])
        if not has_chat_kw:
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

        # Kiểm tra nội dung tin nhắn
        has_msg = False
        if isinstance(payload, list) and payload:
            has_msg = any(isinstance(x, dict) and ("content" in x or "body" in x or "id" in x or "message" in x) for x in payload)
        elif isinstance(payload, dict):
            has_msg = any(k in payload for k in ["messages", "value", "items", "conversations"]) or (
                "id" in payload and ("content" in payload or "body" in payload)
            )

        if has_msg:
            captured_raw_payloads.append({
                "url": url,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "payload": payload,
            })
            try:
                records = interceptor.parse_payload(payload, request_url=url)
                for rec in records:
                    if rec.external_id not in seen_message_ids:
                        seen_message_ids.add(rec.external_id)
                        captured_records.append(rec)
                        snip = (rec.normalized_text or "").replace("\n", " ")[:60]
                        print(f"  [Net] {rec.author_display_name or 'Unknown'}: \"{snip}\"")
            except Exception as e:
                logger.debug(f"Parse error: {e}")

    async with async_playwright() as p:
        print(f"[*] Đang khởi động trình duyệt Chromium ({'Headless' if actual_headless else 'Có giao diện'})...")
        browser = await p.chromium.launch(
            headless=actual_headless,
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
        )
        context_kwargs = {
            "viewport": {"width": 1440, "height": 900},
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        }
        if has_valid_session:
            context_kwargs["storage_state"] = str(STORAGE_PATH)

        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()

        page.on("response", on_response)
        context.on("response", on_response)

        print("[*] Đang kết nối tới Teams (https://teams.cloud.microsoft)...")
        try:
            await page.goto("https://teams.cloud.microsoft", wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            logger.warning(f"Goto: {e}")

        # Kiểm tra nếu bị redirect sang trang đăng nhập Microsoft
        if any(ind in page.url.lower() for ind in ["login.microsoftonline.com", "login.live.com", "/oauth2/"]):
            print("\n[!] Phát hiện yêu cầu đăng nhập. Đang chờ bạn hoàn tất đăng nhập trên trình duyệt...")
            for w in range(300):
                if page.is_closed():
                    break
                curr = page.url.lower()
                is_logged_in = ("teams.cloud.microsoft" in curr or "teams.microsoft.com" in curr) and (
                    "login.microsoftonline.com" not in curr and "login.live.com" not in curr
                )
                if is_logged_in:
                    # NGAY KHI ĐĂNG NHẬP XONG -> LƯU COOKIE NGAY LẬP TỨC
                    await context.storage_state(path=str(STORAGE_PATH))
                    print("\n" + "=" * 70)
                    print(f"  [✓] ĐÃ LƯU TOÀN BỘ COOKIE & TOKEN THÀNH CÔNG VÀO:")
                    print(f"      {STORAGE_PATH.resolve()}")
                    print("  [✓] TỪ LẦN CHẠY SAU, BẠN HOÀN TOÀN KHÔNG CẦN ĐĂNG NHẬP LẠI NỮA!")
                    print("=" * 70 + "\n")
                    break
                await asyncio.sleep(1)

        print("[*] Đang chờ giao diện Teams tải xong (tối đa 40s)...")
        for sec in range(40):
            try:
                # Kiểm tra xem danh sách chat đã xuất hiện chưa
                has_chat_ui = await page.evaluate("""() => {
                    const items = document.querySelectorAll(
                        '[data-tid="chat-list-item"], [data-fui-tree-item-value], [role="treeitem"], [data-tid="chat-pane-item"]'
                    );
                    return items.length > 0;
                }""")
                if has_chat_ui:
                    print(f"  [✓] Giao diện Teams đã sẵn sàng sau {sec + 1}s!")
                    # Lưu lại cookie sau khi UI nạp xong
                    await context.storage_state(path=str(STORAGE_PATH))
                    break
            except Exception as e:
                logger.debug("[context] Non-critical error while checking chat UI: %s", e)
            await asyncio.sleep(1)

        await asyncio.sleep(2)

        # Lấy danh sách các cuộc hội thoại trên thanh bên trái
        print("\n[*] Đang quét danh sách các cuộc hội thoại / kênh trên thanh bên...")
        chat_items = await page.evaluate("""() => {
            const list = [];
            // Tìm tất cả các mục hội thoại có thể click
            const els = Array.from(document.querySelectorAll(
                '[role="treeitem"], [data-tid="chat-list-item"], [data-fui-tree-item-value], div[data-tid*="chat-item"]'
            ));
            els.forEach((el, idx) => {
                const titleEl = el.querySelector('[data-tid="chat-list-item-title"], .fui-TreeItem__content, h4, span') || el;
                const title = (titleEl.innerText || titleEl.textContent || '').trim();
                const tid = el.getAttribute('data-tid') || '';
                const val = el.getAttribute('data-fui-tree-item-value') || '';
                if (title && !title.includes("Họp ngay") && title.length > 1) {
                    list.push({ index: idx, title: title.split('\\n')[0].trim(), tid: tid, value: val });
                }
            });
            return list;
        }""")

        # Lọc trùng lặp theo title
        unique_chats = []
        seen_titles = set()
        for c in chat_items:
            t = c.get("title", "")
            if t and t not in seen_titles:
                seen_titles.add(t)
                unique_chats.append(c)

        print(f"[*] Tìm thấy {len(unique_chats)} cuộc trò chuyện / kênh Teams.")
        target_chats = unique_chats[:max_chats]

        # -------------------------------------------------------------
        # TỰ ĐỘNG DUYỆT TỪNG CUỘC TRÒ CHUYỆN & CUỘN LẤY TIN NHẮN
        # -------------------------------------------------------------
        for idx, chat in enumerate(target_chats, 1):
            chat_name = chat.get("title")
            print(f"\n[{idx}/{len(target_chats)}] Đang tự động nạp chat: '{chat_name}'...")

            # Click vào mục chat
            try:
                clicked = await page.evaluate(f"""(targetTitle) => {{
                    const els = Array.from(document.querySelectorAll(
                        '[role="treeitem"], [data-tid="chat-list-item"], [data-fui-tree-item-value], div[data-tid*="chat-item"]'
                    ));
                    for (const el of els) {{
                        if ((el.innerText || '').includes(targetTitle)) {{
                            el.scrollIntoView({{ block: 'center' }});
                            el.click();
                            return true;
                        }}
                    }}
                    return false;
                }}""", chat_name)
                await asyncio.sleep(2.5)
            except Exception as click_err:
                logger.debug(f"Click chat err: {click_err}")

            # Tự động cuộn ngược (Scroll-up) để ép Teams nạp tin nhắn lịch sử
            for s in range(scrolls_per_chat):
                try:
                    await page.evaluate("""() => {
                        const viewport = document.querySelector('[data-tid="message-pane-list-viewport"]') ||
                                         document.querySelector('div[role="feed"]') ||
                                         document.querySelector('.message-pane') ||
                                         document.querySelector('[data-tid="chat-pane-message"]')?.parentElement;
                        if (viewport) {
                            viewport.scrollBy(0, -800);
                        } else {
                            window.scrollBy(0, -800);
                        }
                    }""")
                    await asyncio.sleep(1.2)
                except Exception as scroll_err:
                    break

            # Trích xuất toàn bộ tin nhắn đang hiển thị trên khung chat hiện tại
            try:
                dom_msgs = await page.evaluate("""(convName) => {
                    const results = [];
                    // Bắt tất cả các phần tử tin nhắn trên Teams v1 và v2
                    const nodes = document.querySelectorAll(
                        '[data-tid="chat-pane-message"], [role="article"], div[data-mid], [data-tid="message-wrapper"]'
                    );
                    nodes.forEach(node => {
                        const bodyEl = node.querySelector('[data-tid="message-body"], .ui-chat__message__content, [id^="content-"]') || node;
                        const authorEl = node.querySelector('[data-tid="message-author-name"], .ui-chat__message__author, strong, h5');
                        const timeEl = node.querySelector('time, [data-tid="message-timestamp"]');
                        const text = (bodyEl.innerText || bodyEl.textContent || '').trim();
                        if (text.length > 0 && text.length < 8000) {
                            results.push({
                                text: text,
                                author: authorEl ? (authorEl.innerText || authorEl.textContent || '').trim() : '',
                                time: timeEl ? (timeEl.getAttribute('datetime') || timeEl.innerText || '') : '',
                                conv_name: convName
                            });
                        }
                    });
                    return results;
                }""", chat_name)

                for item in dom_msgs:
                    msg_text = item.get("text", "").strip()
                    if not msg_text or len(msg_text) < 2:
                        continue
                    author = item.get("author") or "Teams Member"
                    h = hashlib.sha256(f"{chat_name}:{author}:{msg_text}".encode("utf-8")).hexdigest()[:16]
                    msg_id = f"teams-{h}"

                    if msg_id not in seen_message_ids:
                        seen_message_ids.add(msg_id)
                        now_utc = datetime.now(timezone.utc)
                        rec = RawEventRecord(
                            id=f"raw-teams-{h}",
                            tenant_id="fpt-teams-auto",
                            source_type=SourceType.MS_TEAMS_WEB,
                            external_id=msg_id,
                            idempotency_key=hashlib.sha256(f"teams:{msg_id}:{h}".encode("utf-8")).hexdigest(),
                            event_timestamp=now_utc,
                            captured_at=now_utc,
                            author_display_name=author,
                            author_external_id=author.lower().replace(" ", "."),
                            conversation_or_project_id=chat_name,
                            deep_link=f"https://teams.cloud.microsoft/",
                            raw_payload=item,
                            normalized_text=msg_text,
                            content_hash=h,
                            processing_status=ProcessingStatus.PENDING,
                            created_at=now_utc,
                        )
                        captured_records.append(rec)
                        snip = msg_text.replace("\n", " ")[:60]
                        print(f"  [+] {author}: \"{snip}...\"")
            except Exception as extract_err:
                logger.debug(f"DOM extract err: {extract_err}")

        # Lưu lại cookies cập nhật
        try:
            await context.storage_state(path=str(STORAGE_PATH))
            print(f"\n[✓] Đã cập nhật phiên làm việc mới nhất vào: {STORAGE_PATH}")
        except Exception as e:
            logger.debug("[context] Non-critical error updating storage_state: %s", e)

        await browser.close()

    # -------------------------------------------------------------
    # XUẤT TOÀN BỘ DỮ LIỆU ĐÃ THU THẬP RA THƯ MỤC
    # -------------------------------------------------------------
    print("\n" + "=" * 70)
    print("                 KẾT QUẢ THU THẬP TỰ ĐỘNG HOÀN TẤT")
    print("=" * 70)
    print(f"- Tổng số gói tin mạng thô bắt được: {len(captured_raw_payloads)}")
    print(f"- Tổng số tin nhắn đã trích xuất   : {len(captured_records)}")

    # 1. Ghi teams_raw_payloads.json
    raw_file = OUTPUT_DIR / "teams_raw_payloads.json"
    with open(raw_file, "w", encoding="utf-8") as f:
        json.dump(captured_raw_payloads, f, ensure_ascii=False, indent=2, default=str)
    print(f"  ✓ Đã ghi gói tin thô vào    : {raw_file.resolve()}")

    # 2. Ghi teams_messages.jsonl
    jsonl_file = OUTPUT_DIR / "teams_messages.jsonl"
    with open(jsonl_file, "w", encoding="utf-8") as f:
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
    print(f"  ✓ Đã ghi danh sách tin nhắn vào: {jsonl_file.resolve()}")

    # 3. Ghi README.md
    readme_file = OUTPUT_DIR / "README.md"
    authors_count = {}
    for r in captured_records:
        name = r.author_display_name or "Unknown"
        authors_count[name] = authors_count.get(name, 0) + 1

    top_authors_str = "\n".join([f"  - **{name}**: {cnt} tin nhắn" for name, cnt in list(authors_count.items())[:10]])
    readme_content = f"""# Microsoft Teams Raw Samples (Auto Collected)

- **Thời gian thu thập**: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}
- **Tổng số gói tin mạng thô**: {len(captured_raw_payloads)}
- **Tổng số tin nhắn trích xuất**: {len(captured_records)}
- **Số tác giả tham gia trao đổi**: {len(authors_count)}
- **Nguồn thu thập**: Microsoft Teams Web (100% tự động qua Playwright Auto Crawler)

### Top người gửi tin nhắn:
{top_authors_str or "  (Chưa có)"}

### Các tệp đính kèm:
- `teams_raw_payloads.json`: Gói tin mạng JSON nội bộ thô.
- `teams_messages.jsonl`: Dữ liệu tin nhắn chuẩn hóa theo format `RawEventRecord` (Contract C12) sẵn sàng đưa vào LLM / AI Intelligence Pipeline để trích xuất task và cam kết.
"""
    with open(readme_file, "w", encoding="utf-8") as f:
        f.write(readme_content)
    print(f"  ✓ Đã cập nhật file mô tả     : {readme_file.resolve()}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(auto_collect_teams(max_chats=15, scrolls_per_chat=4, headless=True))
