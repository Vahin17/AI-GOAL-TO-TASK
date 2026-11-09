import asyncio
import json
import logging
from pathlib import Path
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("acceptance_runner")

TEST_CASES = [
    {
        "case_id": "A",
        "goal": "open wikipedia in the new tab",
        "screenshot_name": "case_a_wikipedia_tab.png",
    },
    {
        "case_id": "B",
        "goal": "open gmail in the new tab",
        "screenshot_name": "case_b_gmail_tab.png",
    },
    {
        "case_id": "C",
        "goal": "visit example.com",
        "screenshot_name": "case_c_example_com.png",
    },
    {
        "case_id": "D",
        "goal": "open wikipedia in the new tab and give me a screenshot",
        "screenshot_name": "case_d_wikipedia_screenshot.png",
    },
    {
        "case_id": "E",
        "goal": "Open Wikipedia in a new tab, search for Artificial Intelligence, open the relevant article, scroll down, and take a screenshot.",
        "screenshot_name": "case_e_multistep_ai.png",
    },
    {
        "case_id": "F",
        "goal": "visit https://httpbin.org/html in a new tab and take a screenshot",
        "screenshot_name": "case_f_arbitrary_url.png",
    },
]

async def run_all():
    artifacts_dir = Path(r"C:\Users\vahin\.gemini\antigravity-ide\brain\0203ca2c-2eeb-45f7-b79b-9646687a925b")
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        for tc in TEST_CASES:
            case_id = tc["case_id"]
            goal = tc["goal"]
            ss_path = artifacts_dir / tc["screenshot_name"]

            logger.info(f"\n{'='*60}\nRUNNING CASE {case_id}: '{goal}'\n{'='*60}")
            session_meta = {"session_id": None, "task_count": 0, "tasks": []}

            async def on_response(response):
                if "/orchestrate" in response.url and response.request.method == "POST":
                    try:
                        data = await response.json()
                        session_meta["session_id"] = data.get("session_id")
                        session_meta["task_count"] = data.get("task_count")
                        session_meta["tasks"] = [n.get("task_name") for n in data.get("initial_graph", [])]
                        logger.info(f"[E2E] Case {case_id} session={session_meta['session_id']}, tasks={session_meta['task_count']}")
                    except Exception:
                        pass

            page.on("response", on_response)

            # Navigate to frontend UI
            await page.goto("http://localhost:3000/", wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(1000)

            # Enter goal in ChatPanel
            textarea = page.locator("textarea")
            await textarea.wait_for(state="visible", timeout=10000)
            await textarea.fill(goal)
            await page.wait_for_timeout(500)

            # Click send button
            send_btn = page.locator("button:has(svg.lucide-send):not([disabled])").first
            await send_btn.wait_for(state="visible", timeout=5000)
            await send_btn.click()
            logger.info(f"Case {case_id}: Submitted goal to frontend.")

            # Wait for execution to finish
            completed = False
            for _ in range(45):
                await page.wait_for_timeout(1000)
                content = await page.content()
                if "SESSION COMPLETE" in content:
                    completed = True
                    logger.info(f"Case {case_id}: Detected SESSION COMPLETE in UI.")
                    break

            # Switch to Evidence tab
            evidence_tab = page.locator("button:has-text('Evidence')").first
            if await evidence_tab.is_visible():
                await evidence_tab.click()
                await page.wait_for_timeout(1500)

            # Check for screenshot thumbnail if applicable
            has_screenshot = False
            img_len = 0
            imgs = page.locator("img[alt*='Screenshot'], img[src^='data:image/png;base64']")
            count = await imgs.count()
            if count > 0:
                src = await imgs.first.get_attribute("src") or ""
                img_len = len(src)
                has_screenshot = src.startswith("data:image/png;base64,") and img_len > 1000
                if has_screenshot:
                    # Click to expand modal
                    await imgs.first.click(force=True)
                    await page.wait_for_timeout(1000)

            # Capture UI screenshot artifact
            await page.screenshot(path=str(ss_path), full_page=True)
            logger.info(f"Case {case_id}: Screenshot saved to {ss_path.name}")

            # Close modal if open
            close_btn = page.locator("button:has(svg.lucide-x)").first
            if await close_btn.is_visible():
                await close_btn.click()
                await page.wait_for_timeout(500)

            # Collect metrics from backend
            backend_metrics = {}
            if session_meta["session_id"]:
                import urllib.request
                try:
                    req = urllib.request.Request(f"http://127.0.0.1:8000/orchestrate/{session_meta['session_id']}/state")
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        s_data = json.loads(resp.read().decode())
                        backend_metrics = s_data.get("metrics", {})
                except Exception as e:
                    logger.warning(f"Failed to fetch state: {e}")

            results.append({
                "case_id": case_id,
                "goal": goal,
                "session_id": session_meta["session_id"],
                "task_count": session_meta["task_count"],
                "tasks": session_meta["tasks"],
                "ui_completed": completed,
                "has_screenshot": has_screenshot,
                "screenshot_chars": img_len,
                "screenshot_file": ss_path.name,
                "backend_completed_tasks": backend_metrics.get("tasks_completed", 0),
                "backend_success_rate": backend_metrics.get("success_rate", 0.0),
            })

            # Small pause before next case
            await page.wait_for_timeout(2000)

        await browser.close()

    print("\n" + "=" * 80)
    print("ALL 6 CASES ACCEPTANCE RESULTS SUMMARY:")
    print("=" * 80)
    for r in results:
        print(f"Case {r['case_id']}: '{r['goal']}'")
        print(f"  Session: {r['session_id']}, Tasks: {r['task_count']}")
        print(f"  Tasks Planned: {r['tasks']}")
        print(f"  UI Completed: {r['ui_completed']}, Backend Completed: {r['backend_completed_tasks']}, Success Rate: {r['backend_success_rate']}")
        print(f"  Screenshot Artifact: {r['screenshot_file']} (has_img={r['has_screenshot']}, chars={r['screenshot_chars']})")
        print("-" * 80)
    print("=" * 80 + "\n")

if __name__ == "__main__":
    asyncio.run(run_all())
