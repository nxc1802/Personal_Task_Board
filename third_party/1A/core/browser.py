from playwright.async_api import (
    async_playwright,
)

from connectors.teams.runner import (
    run_teams
)

from connectors.outlook.runner import (
    run_outlook
)

from pathlib import Path

USER_DATA_DIR = Path("data/playwright_user")

# False --> Mở browser, True --> Đóng browser
HEADLESS = False

# ============================================================
# PATH DEFINITIONS
# ============================================================


async def create_browser_and_run():
    playwright = await async_playwright().start()
    
    try:
        browser_context = (
            await playwright.chromium
            .launch_persistent_context(
                user_data_dir=USER_DATA_DIR,
                headless=HEADLESS,
            )
        )

        # # Chạy vô trang tổng Microsoft
        # if browser_context.pages:
        #     page = browser_context.pages[0]
        # else:
        #     page = await browser_context.new_page()

        # if "outlook.cloud.microsoft" not in page.url:
        #     print(
        #         "Đang mở Outlook...",
        #         flush=True,
        #     )

        #     await page.goto(
        #         "https://teams.microsoft.com/",
        #         wait_until="domcontentloaded",
        #         timeout=120000,
        #     )

        # print(
        #     "Đang chờ giao diện Teams...",
        #     flush=True,
        # )

        await run_teams(browser_context)

        await run_outlook(browser_context)

    finally:
        await playwright.stop()




