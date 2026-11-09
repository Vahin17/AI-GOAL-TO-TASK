import asyncio
import subprocess
import time
from playwright.async_api import async_playwright

viewports = [
    ("Desktop 1440", 1440, 900),
    ("Desktop 1280", 1280, 800),
    ("Desktop 1024", 1024, 768),
    ("Tablet 768", 768, 1024),
    ("Small Tablet 640", 640, 900),
    ("Mobile 375", 375, 667),
]

async def main():
    server = subprocess.Popen(["npm", "--prefix", "frontend", "start"], shell=True)
    time.sleep(3)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            for name, w, h in viewports:
                page = await browser.new_page(viewport={"width": w, "height": h}, color_scheme="light")
                await page.goto("http://localhost:3000", wait_until="networkidle")
                
                # Check Simulator container and buttons
                controls_container = page.locator("#simulator .lg\\:col-span-5")
                preview_container = page.locator("#simulator .lg\\:col-span-7")
                
                c_box = await controls_container.bounding_box()
                p_box = await preview_container.bounding_box()
                
                sim_btn = page.locator("button:has-text('Simulate Demo Pipeline')")
                launch_btn = page.locator("#simulator a:has-text('Launch ARDO Engine')")
                reset_btn = page.locator("button[title='Reset Simulation']")
                
                s_box = await sim_btn.bounding_box()
                l_box = await launch_btn.bounding_box()
                r_box = await reset_btn.bounding_box()
                
                print(f"\n=== {name} (w={w}) ===")
                print(f"Controls container: x={c_box['x']:.1f}, w={c_box['width']:.1f}, right={c_box['x']+c_box['width']:.1f}")
                print(f"Preview container:  x={p_box['x']:.1f}, w={p_box['width']:.1f}")
                print(f"Simulate button:    x={s_box['x']:.1f}, w={s_box['width']:.1f}, right={s_box['x']+s_box['width']:.1f}, y={s_box['y']:.1f}, h={s_box['height']:.1f}")
                print(f"Launch button:      x={l_box['x']:.1f}, w={l_box['width']:.1f}, right={l_box['x']+l_box['width']:.1f}, y={l_box['y']:.1f}, h={l_box['height']:.1f}")
                print(f"Reset button:       x={r_box['x']:.1f}, w={r_box['width']:.1f}, right={r_box['x']+r_box['width']:.1f}, y={r_box['y']:.1f}, h={r_box['height']:.1f}")
                
                # Check overlap
                overlap = False
                max_right = max(s_box['x']+s_box['width'], l_box['x']+l_box['width'], r_box['x']+r_box['width'])
                if w >= 1024 and max_right > p_box['x']:
                    print(f"  WARNING: BUTTON OVERLAPS PREVIEW! (Max right={max_right:.1f} > Preview x={p_box['x']:.1f})")
                    overlap = True
                
                # Check if controls exceed container
                container_right = c_box['x'] + c_box['width'] + 1 # 1px tolerance
                if max_right > container_right:
                    print(f"  WARNING: BUTTON OVERFLOWS CONTROLS CONTAINER! (Max right={max_right:.1f} > Container right={container_right:.1f})")
                
                await page.close()
            await browser.close()
    finally:
        server.terminate()
        server.kill()

if __name__ == "__main__":
    asyncio.run(main())

