import asyncio
import subprocess
import time
from playwright.async_api import async_playwright

viewports = [
    ("Desktop (1440x900)", 1440, 900),
    ("Desktop (1280x800)", 1280, 800),
    ("Desktop (1024x768)", 1024, 768),
    ("Tablet (768x1024)", 768, 1024),
    ("Small Tablet (640x900)", 640, 900),
    ("Mobile (375x667)", 375, 667),
]

async def main():
    print("Starting Next.js production server...")
    server = subprocess.Popen(["npm", "--prefix", "frontend", "start"], shell=True)
    time.sleep(3)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            
            for vp_name, width, height in viewports:
                page = await browser.new_page(viewport={"width": width, "height": height}, color_scheme="dark")
                await page.goto("http://localhost:3000", wait_until="networkidle")

                c_box = await page.locator("#simulator .lg\\:col-span-6").first.bounding_box()
                p_box = await page.locator("#simulator .lg\\:col-span-6").nth(1).bounding_box()

                s_btn = page.locator("button:has-text('Simulate')").first
                l_btn = page.locator("#simulator a[href='/app']")
                r_btn = page.locator("button[title='Reset Simulation']")

                s_box = await s_btn.bounding_box()
                l_box = await l_btn.bounding_box()
                r_box = await r_btn.bounding_box()

                print(f"\n=== {vp_name} (w={width}) ===")
                print(f"  Simulate: x={s_box['x']:.1f}, w={s_box['width']:.1f}, y={s_box['y']:.1f}, h={s_box['height']:.1f}")
                print(f"  Launch:   x={l_box['x']:.1f}, w={l_box['width']:.1f}, y={l_box['y']:.1f}, h={l_box['height']:.1f}")
                print(f"  Reset:    x={r_box['x']:.1f}, w={r_box['width']:.1f}, y={r_box['y']:.1f}, h={r_box['height']:.1f}")

                # 1. Single Row Verification: All three buttons must share the exact same y-coordinate!
                assert abs(s_box['y'] - l_box['y']) < 2, f"Simulate and Launch are not on same row on {vp_name}!"
                assert abs(l_box['y'] - r_box['y']) < 2, f"Launch and Reset are not on same row on {vp_name}!"
                print("  => Single row alignment: PASSED (all 3 buttons on identical y-axis)")

                # 2. No Overlap Between Buttons: left to right order
                s_right = s_box['x'] + s_box['width']
                l_right = l_box['x'] + l_box['width']
                r_right = r_box['x'] + r_box['width']
                assert s_right <= l_box['x'] + 1, f"Simulate overlaps Launch on {vp_name}!"
                assert l_right <= r_box['x'] + 1, f"Launch overlaps Reset on {vp_name}!"
                print("  => Horizontal ordering & spacing: PASSED (Simulate -> Launch -> Reset)")

                # 3. Inside Controls Container & No Overlap with Preview Panel
                container_right = c_box['x'] + c_box['width'] + 2
                assert r_right <= container_right, f"Reset button overflows controls container on {vp_name}!"
                if width >= 1024:
                    assert r_right <= p_box['x'], f"Buttons overlap preview panel on {vp_name}!"
                    gap = p_box['x'] - r_right
                    print(f"  => Container containment: PASSED (Gap to preview = {gap:.1f}px)")
                else:
                    print("  => Container containment: PASSED")

                # 4. Height match check
                assert abs(s_box['height'] - 56) < 2
                assert abs(l_box['height'] - 56) < 2
                assert abs(r_box['height'] - 56) < 2
                print("  => Uniform height: PASSED (56px)")

                # Take screenshot
                await page.locator("#simulator").scroll_into_view_if_needed()
                await page.screenshot(path=f"scratch/single_row_{width}.png")

                await page.close()

            # Test functionality
            print("\n=== FUNCTIONALITY CHECKS ===")
            page_func = await browser.new_page(viewport={"width": 1280, "height": 800}, color_scheme="dark")
            await page_func.goto("http://localhost:3000", wait_until="networkidle")

            sim_btn = page_func.locator("button:has-text('Simulate')").first
            reset_btn = page_func.locator("button[title='Reset Simulation']")
            launch_link = page_func.locator("#simulator a[href='/app']")

            await sim_btn.click()
            await page_func.wait_for_timeout(800)
            status_sim = await page_func.locator("#statusText").text_content()
            print(f"  Simulator running status: '{status_sim}'")

            await reset_btn.click()
            await page_func.wait_for_timeout(300)
            status_reset = await page_func.locator("#statusText").text_content()
            print(f"  Simulator reset status: '{status_reset}'")
            assert status_reset == "Demonstration Engine Ready"

            await launch_link.click()
            await page_func.wait_for_url("**/app")
            print(f"  Launch button navigated to: {page_func.url}")
            assert "/app" in page_func.url

            await page_func.close()
            await browser.close()
            print("\n>>> ALL SINGLE-ROW CHECKS COMPLETED AND VERIFIED! <<<")
    finally:
        server.terminate()
        server.kill()

if __name__ == "__main__":
    asyncio.run(main())

