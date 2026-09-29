"""Script to inspect and verify live Layer 1A ingestion using real storage_state.json."""

import asyncio
import json
import logging
import sys
from pathlib import Path

from playwright.async_api import async_playwright, Response
from ptb_acquisition.playwright.outlook_interceptor import OutlookNetworkInterceptor
from ptb_acquisition.playwright.teams_interceptor import TeamsNetworkInterceptor
from ptb_acquisition.queue import LocalIngestionQueue

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("verify_live_l1a")

STORAGE_PATH = Path("data/playwright/storage_state.json")

async def main():
    if not STORAGE_PATH.exists():
        print(f"Error: {STORAGE_PATH} not found.")
        sys.exit(1)

    print("=" * 60)
    print("PTB LAYER 1A: KIỂM CHỨNG TRÊN TÀI KHOẢN & DỮ LIỆU THỰC")
    print(f"Session state: {STORAGE_PATH.resolve()} ({STORAGE_PATH.stat().st_size} bytes)")
    print("=" * 60)

    queue = LocalIngestionQueue(maxsize=1000)
    teams_interceptor = TeamsNetworkInterceptor(queue=queue, tenant_id="fpt-real")
    outlook_interceptor = OutlookNetworkInterceptor(queue=queue, tenant_id="fpt-real")

    intercepted_urls = []

    async def on_outlook_resp(response: Response):
        url = response.url
        if "service.svc" in url and "action=" in url and response.status == 200:
            try:
                data = await response.json()
                action = url.split("action=")[1].split("&")[0]
                intercepted_urls.append(("OUTLOOK", response.status, action))
                logger.info(f"[OUTLOOK] action={action}, top keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
                if isinstance(data, dict) and "Body" in data:
                    body = data["Body"]
                    if "Conversations" in body and isinstance(body["Conversations"], list) and body["Conversations"]:
                        conv0 = body["Conversations"][0]
                        logger.info(f"  [FindConversation] body['Conversations'][0] keys: {list(conv0.keys())}")
                    if isinstance(body, dict) and "ResponseMessages" in body:
                        rm = body["ResponseMessages"]
                        items = rm.get("Items", [])
                        if items and isinstance(items[0], dict):
                            logger.info(f"  Items[0] keys: {list(items[0].keys())}")
                            if "Conversation" in items[0] and isinstance(items[0]["Conversation"], dict):
                                logger.info(f"  Items[0]['Conversation'] keys: {list(items[0]['Conversation'].keys())}")
                            if "Items" in items[0]:
                                logger.info(f"  Items[0]['Items'] type: {type(items[0]['Items'])}")
                
                # Try parsing with existing interceptor
                recs = outlook_interceptor.parse_payload(data, request_url=url)
                if recs:
                    logger.info(f"  >>> Extracted {len(recs)} records from {action}!")
                    for r in recs:
                        outlook_interceptor.captured_records.append(r)
                        await queue.put(r)
            except Exception as e:
                logger.debug(f"Outlook json parse err: {e}")

    async def on_teams_resp(response: Response):
        url = response.url
        if response.status == 200 and ("teams" in url or "skype" in url):
            if any(k in url for k in ["api", "chat", "msg", "conversation", "thread", "message", "channel"]):
                intercepted_urls.append(("TEAMS", response.status, url[:80]))
                logger.info(f"[TEAMS] URL: {url[:100]}")
                try:
                    data = await response.json()
                    recs = teams_interceptor.parse_payload(data, request_url=url)
                    if recs:
                        logger.info(f"  >>> Extracted {len(recs)} records from Teams!")
                        for r in recs:
                            teams_interceptor.captured_records.append(r)
                            await queue.put(r)
                except Exception as e:
                    logger.debug("Lỗi parse payload Teams: %s", e)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            storage_state=str(STORAGE_PATH),
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        )

        # 1. VERIFY OUTLOOK WEB
        print("\n[1/2] Đang điều hướng tới Outlook Web (https://outlook.office.com/mail/)...")
        outlook_page = await context.new_page()
        outlook_page.on("response", on_outlook_resp)

        try:
            resp = await outlook_page.goto("https://outlook.office.com/mail/", wait_until="domcontentloaded", timeout=45000)
            print(f"  -> Outlook HTTP Status: {resp.status if resp else 'N/A'}")
            print(f"  -> Outlook Page Title: {await outlook_page.title()}")
            print("  -> Chờ 20 giây để Outlook tải email list và hội thoại...")
            await asyncio.sleep(20)
        except Exception as e:
            print(f"  [!] Outlook warning: {e}")

        # 2. VERIFY TEAMS WEB
        print("\n[2/2] Đang điều hướng tới Teams Web...")
        teams_page = await context.new_page()
        teams_page.on("response", on_teams_resp)

        try:
            resp = await teams_page.goto("https://teams.microsoft.com/v2/", wait_until="domcontentloaded", timeout=45000)
            print(f"  -> Teams HTTP Status: {resp.status if resp else 'N/A'}")
            print(f"  -> Teams Current URL: {teams_page.url}")
            print(f"  -> Teams Page Title: {await teams_page.title()}")
            print("  -> Chờ 20 giây để Teams Web nạp tin nhắn qua network...")
            await asyncio.sleep(20)
        except Exception as e:
            print(f"  [!] Teams warning: {e}")

        await browser.close()

    print("\n" + "=" * 60)
    print("KẾT QUẢ THU THẬP THỰC TẾ LAYER 1A:")
    print("=" * 60)
    print(f"- Tổng số endpoints quan trọng được kích hoạt: {len(intercepted_urls)}")
    print(f"- Số bản ghi Outlook thu thập được: {len(outlook_interceptor.captured_records)}")
    print(f"- Số bản ghi Teams thu thập được:   {len(teams_interceptor.captured_records)}")
    print(f"- Số bản ghi trong hàng đợi IngestionQueue: {queue.qsize}")

    if outlook_interceptor.captured_records:
        print("\n[Dữ liệu thực tế từ OUTLOOK]:")
        for i, rec in enumerate(outlook_interceptor.captured_records[:5], 1):
            print(f"  #{i} ID: {rec.external_id}")
            print(f"     Người gửi: {rec.author_display_name} <{rec.author_external_id}>")
            print(f"     Thời gian: {rec.event_timestamp}")
            print(f"     Idempotency Key: {rec.idempotency_key}")
            snippet = str(rec.raw_payload.get("Subject") or rec.raw_payload.get("Preview") or "")[:80]
            print(f"     Tiêu đề/Xem trước: {snippet}")

    if teams_interceptor.captured_records:
        print("\n[Dữ liệu thực tế từ TEAMS]:")
        for i, rec in enumerate(teams_interceptor.captured_records[:5], 1):
            print(f"  #{i} ID: {rec.external_id}")
            print(f"     Người gửi: {rec.author_display_name} <{rec.author_external_id}>")
            print(f"     Thời gian: {rec.event_timestamp}")
            print(f"     Idempotency Key: {rec.idempotency_key}")
            snippet = str(rec.raw_payload.get("body", {}).get("content") or rec.raw_payload.get("content") or "")[:80]
            print(f"     Nội dung: {snippet}")

if __name__ == "__main__":
    asyncio.run(main())
