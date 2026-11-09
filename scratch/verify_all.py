import asyncio
import subprocess
import time
from playwright.async_api import async_playwright

async def main():
    print("Starting Next.js production server for full validation...")
    server = subprocess.Popen(["npm", "--prefix", "frontend", "start"], shell=True)
    time.sleep(3)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()

            # ── 1. LIGHT THEME READABILITY VALIDATION ───────────────────
            print("\n=== 1. LIGHT THEME READABILITY CHECKS ===")
            page_light = await browser.new_page(viewport={"width": 1280, "height": 900}, color_scheme="light")
            await page_light.goto("http://localhost:3000", wait_until="networkidle")

            # Check essential text contrast elements in light theme
            test_elements = [
                ("Navigation link", "header nav a"),
                ("Hero H1", "h1"),
                ("Hero Supporting text", "p.max-w-2xl"),
                ("Stage 01 Heading", "#architecture h3"),
                ("Stage 01 Description", "#architecture p.leading-relaxed"),
                ("Stage 04 Description", "#architecture div:has-text('04') p"),
                ("Supporting Infra Heading", "#architecture h3.text-2xl"),
                ("Supporting Infra List Item", "#architecture li span"),
                ("Tech Stack Title", "#techstack h3"),
                ("Tech Stack Description", "#techstack p.leading-relaxed"),
                ("Feature Title", "#features h3"),
                ("Feature Description", "#features p.leading-relaxed"),
                ("Simulator Preset Button", ".preset-btn"),
                ("Simulator Custom Goal Label", "label[for='customGoalInput']"),
                ("Footer text", "footer span.text-base"),
            ]

            for label, selector in test_elements:
                loc = page_light.locator(selector).first
                color = await loc.evaluate("el => window.getComputedStyle(el).color")
                print(f"  [Light] {label:28}: color={color}")
                assert color != "rgb(255, 255, 255)", f"{label} should NOT be white in light theme!"
                assert color != "rgba(255, 255, 255, 1)", f"{label} should NOT be white in light theme!"

            print("Light theme readability checks: ALL PASSED.")

            # ── 2. DARK THEME PRESERVATION VALIDATION ──────────────────
            print("\n=== 2. DARK THEME PRESERVATION CHECKS ===")
            toggle_btn = page_light.locator("header button[title='Toggle Light/Dark Theme']")
            await toggle_btn.click()
            await page_light.wait_for_timeout(400)

            is_dark_active = await page_light.evaluate("""() => {
                return document.documentElement.classList.contains('dark') &&
                       document.querySelector('.landing-root').classList.contains('dark');
            }""")
            assert is_dark_active, "Dark theme classes should be present after toggle!"

            # Check essential dark mode text colors (should be light/white)
            dark_h1_color = await page_light.evaluate("() => window.getComputedStyle(document.querySelector('h1')).color")
            dark_p_color = await page_light.evaluate("() => window.getComputedStyle(document.querySelector('p.max-w-2xl')).color")
            print(f"  [Dark] Hero H1 color: {dark_h1_color}")
            print(f"  [Dark] Hero P color:  {dark_p_color}")
            assert "255" in dark_h1_color or "white" in dark_h1_color or "lab" in dark_h1_color, "H1 should be light in dark theme"
            print("Dark theme preservation checks: ALL PASSED.")
            await page_light.close()

            # ── 3. SIMULATOR RESPONSIVENESS & OVERLAP CHECKS ───────────
            print("\n=== 3. SIMULATOR VIEWPORT OVERLAP & CONTAINER CHECKS ===")
            viewports = [
                ("Desktop (1280x800)", 1280, 800),
                ("Desktop (1024x768)", 1024, 768),
                ("Tablet (768x1024)", 768, 1024),
                ("Mobile (375x667)", 375, 667),
            ]

            for vp_name, width, height in viewports:
                page_vp = await browser.new_page(viewport={"width": width, "height": height}, color_scheme="light")
                await page_vp.goto("http://localhost:3000", wait_until="networkidle")

                c_box = await page_vp.locator("#simulator .lg\\:col-span-5").bounding_box()
                p_box = await page_vp.locator("#simulator .lg\\:col-span-7").bounding_box()

                s_box = await page_vp.locator("button:has-text('Simulate Demo Pipeline')").bounding_box()
                l_box = await page_vp.locator("#simulator a:has-text('Launch ARDO Engine')").bounding_box()
                r_box = await page_vp.locator("button[title='Reset Simulation']").bounding_box()

                # 1. Height check
                for b_name, box in [("Simulate", s_box), ("Launch", l_box), ("Reset", r_box)]:
                    assert abs(box['height'] - 58) < 2, f"{b_name} button height expected ~58px, got {box['height']} on {vp_name}"

                # 2. Container containment check
                container_right = c_box['x'] + c_box['width'] + 2 # 2px tolerance
                max_button_right = max(s_box['x'] + s_box['width'], l_box['x'] + l_box['width'], r_box['x'] + r_box['width'])
                assert max_button_right <= container_right, (
                    f"Button exceeds container on {vp_name}! Max right={max_button_right:.1f}, Container right={container_right:.1f}"
                )

                # 3. Overlap check with demo preview
                if width >= 1024:
                    assert max_button_right <= p_box['x'], (
                        f"Button OVERLAPS Demo Preview on {vp_name}! Max right={max_button_right:.1f}, Preview left={p_box['x']:.1f}"
                    )
                    gap = p_box['x'] - max_button_right
                    print(f"  {vp_name}: Buttons inside container. Gap to preview = {gap:.1f}px (NO OVERLAP)")
                else:
                    assert s_box['y'] + s_box['height'] <= p_box['y'] and l_box['y'] + l_box['height'] <= p_box['y'], (
                        f"Stacked controls overlap preview vertically on {vp_name}!"
                    )
                    print(f"  {vp_name}: Buttons cleanly stacked above preview (NO OVERLAP)")

                await page_vp.close()

            # ── 4. BUTTON FUNCTIONALITY TEST ───────────────────────────
            print("\n=== 4. SIMULATOR BUTTON INTERACTION CHECKS ===")
            page_func = await browser.new_page(viewport={"width": 1280, "height": 800}, color_scheme="light")
            await page_func.goto("http://localhost:3000", wait_until="networkidle")

            sim_btn = page_func.locator("button:has-text('Simulate Demo Pipeline')")
            reset_btn = page_func.locator("button[title='Reset Simulation']")
            launch_link = page_func.locator("#simulator a:has-text('Launch ARDO Engine')")

            # Click simulate
            await sim_btn.click()
            await page_func.wait_for_timeout(1000)
            status_sim = await page_func.locator("#statusText").text_content()
            print(f"  Simulator running status: '{status_sim}'")
            assert "Initializing" in status_sim or "Executing" in status_sim or "Completed" in status_sim

            # Click reset
            await reset_btn.click()
            await page_func.wait_for_timeout(300)
            status_reset = await page_func.locator("#statusText").text_content()
            print(f"  Simulator reset status: '{status_reset}'")
            assert status_reset == "Demonstration Engine Ready"

            # Check launch navigation
            await launch_link.click()
            await page_func.wait_for_url("**/app")
            print(f"  Launch button navigated to: {page_func.url}")
            assert "/app" in page_func.url

            await page_func.close()
            await browser.close()
            print("\n>>> ALL VALIDATION REQUIREMENTS COMPLETED SUCCESSFULLY! <<<")
    finally:
        server.terminate()
        server.kill()

if __name__ == "__main__":
    asyncio.run(main())
