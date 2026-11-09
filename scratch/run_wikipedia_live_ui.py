import asyncio
import json
import logging
from pathlib import Path
from playwright.async_api import async_playwright
import urllib.request

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("wikipedia_runner")

async def test_live_ui():
    artifacts_dir = Path(r"C:\Users\vahin\.gemini\antigravity-ide\brain\0203ca2c-2eeb-45f7-b79b-9646687a925b")

    cases = [
        ("open wikipedia in the new tab", "wiki_live_ui_new_tab.png"),
        ("open wikipedia in the new tab and give me a screenshot", "wiki_live_ui_screenshot.png"),
    ]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        for goal, ss_name in cases:
            logger.info(f"\n{'='*70}\nSTARTING TEST FOR GOAL: '{goal}'\n{'='*70}")
            session_info = {"id": None, "tasks": []}

            async def on_response(resp):
                if "/orchestrate" in resp.url and resp.request.method == "POST":
                    try:
                        d = await resp.json()
                        session_info["id"] = d.get("session_id")
                        session_info["tasks"] = [n.get("task_name") for n in d.get("initial_graph", [])]
                        logger.info(f"[UI-TEST] session_id={session_info['id']} tasks={session_info['tasks']}")
                    except Exception:
                        pass

            page.on("response", on_response)

            await page.goto("http://localhost:3000/", wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(1000)

            textarea = page.locator("textarea")
            await textarea.wait_for(state="visible", timeout=10000)
            await textarea.fill(goal)
            await page.wait_for_timeout(500)

            send_btn = page.locator("button:has(svg.lucide-send):not([disabled])").first
            await send_btn.wait_for(state="visible", timeout=5000)
            await send_btn.click()
            logger.info(f"Submitted goal '{goal}' to UI.")

            # Wait for SESSION COMPLETE or max 35s
            for _ in range(35):
                await page.wait_for_timeout(1000)
                txt = await page.content()
                if "SESSION COMPLETE" in txt:
                    logger.info("Detected SESSION COMPLETE in UI.")
                    break

            # Switch to Evidence tab
            evidence_tab = page.locator("button:has-text('Evidence')").first
            if await evidence_tab.is_visible():
                await evidence_tab.click()
                await page.wait_for_timeout(1500)

            # Check for screenshot
            imgs = page.locator("img[alt*='Screenshot'], img[src^='data:image/png;base64']")
            count = await imgs.count()
            if count > 0:
                await imgs.first.click(force=True)
                await page.wait_for_timeout(1000)

            ss_dest = artifacts_dir / ss_name
            await page.screenshot(path=str(ss_dest), full_page=True)
            logger.info(f"Saved screenshot to {ss_dest.name} ({ss_dest.stat().st_size} bytes)")

            # Fetch state from backend API
            if session_info["id"]:
                try:
                    req = urllib.request.Request(f"http://127.0.0.1:8000/orchestrate/{session_info['id']}/state")
                    with urllib.request.urlopen(req, timeout=5) as r:
                        state_data = json.loads(r.read().decode())
                        metrics = state_data.get("metrics", {})
                        logger.info(
                            f"[BACKEND STATE] session={session_info['id']} "
                            f"completed={metrics.get('tasks_completed')}/{metrics.get('tasks_total')} "
                            f"success_rate={metrics.get('success_rate')}"
                        )
                except Exception as e:
                    logger.warning(f"Error reading state: {e}")

            await page.wait_for_timeout(2000)

        await browser.close()

if __name__ == "__main__":
    asyncio.run(test_live_ui())
