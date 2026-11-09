import asyncio
import subprocess
import time
import os
import signal
from playwright.async_api import async_playwright

async def main():
    print("Starting Next.js production server on http://localhost:3000...", flush=True)
    server_process = subprocess.Popen(
        ["node", "frontend/node_modules/next/dist/bin/next", "start", "frontend", "-p", "3000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    # Wait for server to be responsive
    for _ in range(15):
        time.sleep(1)
        try:
            import urllib.request
            with urllib.request.urlopen("http://localhost:3000", timeout=1) as resp:
                if resp.status == 200:
                    print("Server is ready!", flush=True)
                    break
        except Exception:
            pass

    try:
        print("Running comprehensive layout and content validation...", flush=True)
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            
            # ── 1. Desktop Test (1280x800) ──────────────────────────────────
            desktop_ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
            page = await desktop_ctx.new_page()

            print("\n--- DESKTOP (1280x800) TESTS ---", flush=True)
            await page.goto("http://localhost:3000", wait_until="networkidle")
            await asyncio.sleep(1)

            # Check Hero Headline and Description
            h1_text = await page.inner_text("h1")
            print(f"Hero H1: '{h1_text.strip()}'", flush=True)
            assert "Turn Intent Into Action." in h1_text, f"Unexpected H1: {h1_text}"

            hero_p = await page.locator("section >> p").first.inner_text()
            print(f"Hero Supporting: '{hero_p.strip()}'", flush=True)
            assert "An intelligent AI orchestration engine that transforms natural-language goals into structured tasks" in hero_p

            # Check Academic Wording Absent
            body_text = await page.inner_text("body")
            for bad_phrase in [
                "Final Year Computer Engineering Project",
                "2026–2027",
                "Core Engine Implemented",
                "Currently progressing through live"
            ]:
                assert bad_phrase not in body_text, f"Found forbidden phrase '{bad_phrase}' in body!"
            print("Academic project references check: PASSED (None found in page).", flush=True)

            # Check Replacement Phrases Present
            assert "AI-POWERED GOAL-TO-ACTION AUTOMATION" in body_text
            assert "FROM NATURAL LANGUAGE TO REAL-WORLD ACTION" in body_text
            assert "Transforming user intent into structured plans, coordinated browser actions, and verified execution." in body_text
            assert "BUILT FOR INTELLIGENT TASK EXECUTION" in body_text
            print("Professional product messaging check: PASSED (All 4 replacement phrases verified).", flush=True)

            # Scroll to Simulator
            await page.locator("#simulator").scroll_into_view_if_needed()
            await asyncio.sleep(0.5)

            # Measure button dimensions on desktop
            sim_btn = page.locator("button:has-text('Simulate Demo Pipeline')")
            launch_btn = page.locator("#simulator a:has-text('Launch ARDO Engine')")
            reset_btn = page.locator("button[title='Reset Simulation']")

            sim_box = await sim_btn.bounding_box()
            launch_box = await launch_btn.bounding_box()
            reset_box = await reset_btn.bounding_box()

            print(f"\nDesktop Button Dimensions:")
            print(f"  Simulate Demo Pipeline: width={sim_box['width']:.1f}px, height={sim_box['height']:.1f}px, y={sim_box['y']:.1f}px")
            print(f"  Launch ARDO Engine:     width={launch_box['width']:.1f}px, height={launch_box['height']:.1f}px, y={launch_box['y']:.1f}px")
            print(f"  Reset Button:           width={reset_box['width']:.1f}px, height={reset_box['height']:.1f}px, y={reset_box['y']:.1f}px")

            # Verify equal heights (~58px) and row alignment
            assert abs(sim_box['height'] - launch_box['height']) < 2, "Primary button heights do not match!"
            assert abs(sim_box['height'] - reset_box['height']) < 2, "Reset button height does not match primary buttons!"
            assert abs(sim_box['y'] - launch_box['y']) < 2, "Buttons are not on the same horizontal row!"
            assert abs(reset_box['width'] - reset_box['height']) < 4, "Reset button is not compact and square!"
            print("Desktop button layout alignment & dimensions: PASSED.")

            # Test Functionality 1: Simulate Demo Pipeline
            print("\nTesting Simulate Demo Pipeline button...", flush=True)
            await sim_btn.click()
            await asyncio.sleep(1.5)
            sim_output = await page.locator("#simOutput").inner_text()
            assert "Target Objective:" in sim_output or "Demo Simulation" in sim_output
            print("Simulate Demo Pipeline functionality: PASSED.")

            # Test Functionality 2: Reset button
            print("Testing Reset Simulation button...", flush=True)
            await reset_btn.click()
            await asyncio.sleep(0.5)
            reset_status = await page.locator("#statusText").inner_text()
            print(f"Status after reset: '{reset_status}'", flush=True)
            assert "Demonstration Engine Ready" in reset_status
            print("Reset button functionality: PASSED.")

            # Test Functionality 3: Launch ARDO Engine navigates to /app
            print("Testing Launch ARDO Engine navigation...", flush=True)
            await launch_btn.click()
            await page.wait_for_url("**/app")
            print(f"Successfully navigated to: {page.url}", flush=True)
            assert "/app" in page.url
            print("Launch ARDO Engine link functionality: PASSED.")

            await desktop_ctx.close()

            # ── 2. Mobile Test (390x844) ───────────────────────────────────
            print("\n--- MOBILE (390x844) TESTS ---", flush=True)
            mobile_ctx = await browser.new_context(viewport={"width": 390, "height": 844})
            mobile_page = await mobile_ctx.new_page()

            await mobile_page.goto("http://localhost:3000", wait_until="networkidle")
            await asyncio.sleep(1)

            await mobile_page.locator("#simulator").scroll_into_view_if_needed()
            await asyncio.sleep(0.5)

            m_sim_btn = mobile_page.locator("button:has-text('Simulate Demo Pipeline')")
            m_launch_btn = mobile_page.locator("#simulator a:has-text('Launch ARDO Engine')")
            m_reset_btn = mobile_page.locator("button[title='Reset Simulation']")

            m_sim_box = await m_sim_btn.bounding_box()
            m_launch_box = await m_launch_btn.bounding_box()
            m_reset_box = await m_reset_btn.bounding_box()

            print(f"Mobile Button Dimensions:")
            print(f"  Simulate Demo Pipeline: width={m_sim_box['width']:.1f}px, height={m_sim_box['height']:.1f}px, y={m_sim_box['y']:.1f}px")
            print(f"  Launch ARDO Engine:     width={m_launch_box['width']:.1f}px, height={m_launch_box['height']:.1f}px, y={m_launch_box['y']:.1f}px")
            print(f"  Reset Button:           width={m_reset_box['width']:.1f}px, height={m_reset_box['height']:.1f}px, y={m_reset_box['y']:.1f}px")

            # In mobile, buttons stack cleanly with full width
            assert m_sim_box['y'] < m_launch_box['y'] < m_reset_box['y'], "Buttons should stack vertically on mobile!"
            assert abs(m_sim_box['height'] - 58) < 2, f"Simulate button height expected 58px, got {m_sim_box['height']}"
            assert abs(m_launch_box['height'] - 58) < 2, f"Launch button height expected 58px, got {m_launch_box['height']}"
            assert abs(m_reset_box['height'] - 58) < 2, f"Reset button height expected 58px, got {m_reset_box['height']}"
            print("Mobile button stacked layout and 58px height: PASSED.")

            await mobile_ctx.close()
            await browser.close()
            print("\nALL VALIDATION CHECKS PASSED PERFECTLY!", flush=True)
    finally:
        server_process.terminate()
        server_process.kill()

if __name__ == "__main__":
    asyncio.run(main())
