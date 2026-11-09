import asyncio
import sys
import os

sys.path.insert(0, os.path.abspath("backend"))
from execution.browser.playwright_executor import PlaywrightBrowserExecutor
from execution.browser.browser_models import BrowserAction, BrowserActionType

async def main():
    executor = PlaywrightBrowserExecutor(headless=True)
    await executor.start()
    print("Executor started.")
    action = BrowserAction(
        action_type=BrowserActionType.NAVIGATE,
        url="https://example.com",
    )
    res = await executor.execute_action(action)
    print("Navigate success:", res.success)
    print("Current URL:", res.current_url)
    print("Page title:", res.current_title)
    await executor.stop()
    print("Executor stopped.")

if __name__ == "__main__":
    asyncio.run(main())
