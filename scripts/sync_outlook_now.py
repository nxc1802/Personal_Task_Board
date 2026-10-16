import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sync_outlook")

# Import PTB modules
ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "packages" / "contracts" / "src"))
sys.path.insert(0, str(ROOT_DIR / "packages" / "database" / "src"))
sys.path.insert(0, str(ROOT_DIR / "services" / "acquisition" / "src"))
sys.path.insert(0, str(ROOT_DIR / "services" / "processing" / "src"))
sys.path.insert(0, str(ROOT_DIR / "services" / "intelligence" / "src"))
sys.path.insert(0, str(ROOT_DIR / "services" / "application" / "src"))

from dotenv import load_dotenv
load_dotenv(ROOT_DIR / ".env")

from ptb_contracts import RawEventRecord, SourceType
from ptb_database import Neo4jClient, RawEventRepository, TaskDomainRepository
from ptb_acquisition.playwright.outlook_interceptor import OutlookNetworkInterceptor
from ptb_acquisition.queue import LocalIngestionQueue
from ptb_processing.pipeline import ProcessingPipeline
from ptb_processing.worker import ProcessingWorker
from playwright.async_api import async_playwright

async def sync_outlook():
    print("=" * 70)
    print("MICROSOFT OUTLOOK REAL DATA ACQUISITION & PROCESSING PIPELINE")
    print("=" * 70)

    storage_path = ROOT_DIR / "data" / "playwright" / "storage_state.json"
    if not storage_path.exists():
        print(f"[-] Session storage not found: {storage_path}")
        return

    # 1. Connect to Database
    neo4j_uri = os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687")
    neo4j_user = os.getenv("NEO4J_USERNAME", "neo4j")
    neo4j_password = os.getenv("NEO4J_PASSWORD", "personal_task_board_2026")
    print(f"[*] Kết nối cơ sở dữ liệu Neo4j: {neo4j_uri}...")

    neo4j_client = Neo4jClient(uri=neo4j_uri, username=neo4j_user, password=neo4j_password)
    try:
        await neo4j_client.verify_connectivity()
        print("✓ Kết nối Neo4j thành công!")
    except Exception as e:
        print(f"[-] Không thể kết nối Neo4j: {e}")
        return

    raw_repo = RawEventRepository(neo4j_client)
    task_repo = TaskDomainRepository(neo4j_client)
    queue = LocalIngestionQueue(maxsize=1000)
    interceptor = OutlookNetworkInterceptor(
        queue=queue,
        tenant_id="local-user",
        raw_event_repo=raw_repo,
    )

    captured_records = []

    async def on_response(response):
        url = response.url
        status = response.status
        content_type = response.headers.get("content-type", "")
        if status == 200 and ("json" in content_type or "javascript" in content_type):
            if any(k in url.lower() for k in ["service.svc", "conversation", "message", "finditem", "getitem"]):
                try:
                    payload = await response.json()
                    recs = interceptor.parse_payload(payload, request_url=url)
                    if recs:
                        print(f"  [+] Bắt được {len(recs)} email/hội thoại từ API: {url[:75]}...")
                        for r in recs:
                            if not any(x.id == r.id for x in captured_records):
                                captured_records.append(r)
                                try:
                                    await raw_repo.persist_raw_event(r)
                                except Exception as p_err:
                                    logger.debug(f"Persist raw event: {p_err}")
                except Exception as ex:
                    logger.debug(f"JSON parse error: {ex}")

    print("[*] Khởi động Chromium ngầm với phiên lưu trữ Microsoft 365...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ]
        )
        context = await browser.new_context(
            storage_state=str(storage_path),
            viewport={"width": 1400, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        )
        page = await context.new_page()
        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        target_url = "https://outlook.cloud.microsoft/mail/"
        print(f"[*] Đang nạp dữ liệu từ: {target_url}...")
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print(f"Goto warning: {e}")

        print("[*] Đang chờ giao diện Outlook Web nạp hoàn tất (12 giây)...")
        await asyncio.sleep(12)

        # Sweep: Click on top emails to trigger body load
        print("[*] Đang duyệt qua danh sách email trong hộp thư đến...")
        try:
            items_count = await page.evaluate("""() => {
                const items = Array.from(document.querySelectorAll('div[role="option"], div[role="listitem"], [data-convid]'));
                return items.length;
            }""")
            print(f"  -> Tìm thấy {items_count} email trong danh sách hiển thị.")

            for idx in range(min(items_count or 1, 8)):
                mail_subj = await page.evaluate("""(i) => {
                    const items = Array.from(document.querySelectorAll('div[role="option"], div[role="listitem"], [data-convid]'));
                    if (items[i]) {
                        if (items[i].click) items[i].click();
                        return items[i].innerText.slice(0, 50).replace(/\\n/g, ' ');
                    }
                    return null;
                }""", idx)
                print(f"  -> [{idx+1}/{min(items_count or 1, 8)}] Đang đọc email: '{mail_subj}'...")
                await asyncio.sleep(2.5)

            # Scroll list
            await page.evaluate("""() => {
                const mailList = document.querySelector('div[role="listbox"]') ||
                                 document.querySelector('div[role="feed"]') ||
                                 document.querySelector('div[data-app-section="ItemListView"]') ||
                                 window;
                if (mailList.scrollBy) mailList.scrollBy(0, 600);
            }""")
            await asyncio.sleep(3.0)

        except Exception as e:
            print(f"Sweep warning: {e}")

        await asyncio.sleep(3)
        await browser.close()

    print(f"\n✓ ĐÃ THU THẬP THÀNH CÔNG: {len(captured_records)} BẢN GHI EMAIL TỪ MICROSOFT OUTLOOK!")

    if captured_records:
        print("\n--- Danh sách email mẫu vừa thu thập ---")
        for i, rec in enumerate(captured_records[:10]):
            content_preview = rec.normalized_text or (rec.raw_payload.get("subject") if isinstance(rec.raw_payload, dict) else "") or ""
            clean_preview = content_preview.replace("\n", " ")[:75]
            author = getattr(rec, "author_canonical_name", None) or getattr(rec, "author_id", "Unknown")
            print(f"  [{i+1}] Tác giả: {author:<20} | Tiêu đề/Nội dung: {clean_preview}")

    # Process Batch with ProcessingPipeline & Worker
    print("\n[*] Đang chạy tiến trình bóc tách công việc (ProcessingPipeline & Worker)...")
    pipeline = ProcessingPipeline(task_repo=task_repo)
    worker = ProcessingWorker(raw_event_repo=raw_repo, pipeline=pipeline, max_retries=2)

    extracted_tasks = await worker.process_batch(limit=50)
    task_count = len(extracted_tasks) if isinstance(extracted_tasks, list) else len(captured_records)
    print(f"✓ ĐÃ TRÍCH XUẤT THÀNH CÔNG: {task_count} CÔNG VIỆC VÀO TASK BOARD!")

    all_tasks = await task_repo.list_tasks()
    print(f"\n--- Tổng cộng {len(all_tasks)} tasks trong hệ thống ---")
    for t in all_tasks[:12]:
        t_dict = t.model_dump() if hasattr(t, "model_dump") else (t if isinstance(t, dict) else {})
        title = t_dict.get("title") or t_dict.get("name") or "Chưa có tiêu đề"
        status = t_dict.get("status", "TODO")
        owner = t_dict.get("owner_name") or "Chưa gán"
        print(f"  • [{status}] {title} (Phụ trách: {owner})")

if __name__ == "__main__":
    asyncio.run(sync_outlook())
