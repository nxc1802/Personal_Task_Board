import asyncio
import json
from playwright.async_api import async_playwright

async def inspect():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(
            storage_state="data/playwright/storage_state.json",
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        )
        page = await ctx.new_page()
        print("Navigating to https://teams.cloud.microsoft...")
        await page.goto("https://teams.cloud.microsoft", wait_until="domcontentloaded", timeout=60000)
        
        print("Waiting for Teams chat interface to load...")
        try:
            await page.wait_for_selector(
                '[role="treeitem"], [data-tid="chat-list-item"], [data-fui-tree-item-value], [data-tid="message-pane-list-viewport"]',
                timeout=45000
            )
            print("Teams chat UI detected!")
        except Exception as e:
            print("Wait for selector timeout, continuing:", e)
        await asyncio.sleep(5)
        
        print("Final URL:", page.url)
        print("Title:", await page.title())
        await page.screenshot(path="d:/Working/PTB/raw_samples/teams_ready.png")
        
        info = await page.evaluate("""() => {
            const chats = Array.from(document.querySelectorAll(
                '[data-tid="chat-list-item"], [data-fui-tree-item-value], [role="treeitem"], div[data-tid*="chat"], .chat-list-item'
            ));
            const msgPanes = Array.from(document.querySelectorAll(
                '[data-tid="message-pane-list-viewport"], [role="feed"], [role="article"], .ui-chat__message'
            ));
            return {
                chat_elements_count: chats.length,
                msg_elements_count: msgPanes.length,
                sample_chats: chats.slice(0, 5).map(el => ({
                    role: el.getAttribute('role'),
                    tid: el.getAttribute('data-tid'),
                    text: (el.innerText || '').slice(0, 40)
                }))
            };
        }""")
        with open("d:/Working/PTB/raw_samples/dom_info.json", "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
        print("DOM Info saved to dom_info.json successfully!")
        await b.close()

if __name__ == "__main__":
    asyncio.run(inspect())
