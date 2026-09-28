from core.browser import (
    create_browser_and_run
)

import asyncio

async def main():
    """
    Chạy thu thập toàn bộ chat với thư mục mặc định.
    """

    await create_browser_and_run()

if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        print(
            "\nScript interrupted by user. Exiting.",
            flush=True,
        )