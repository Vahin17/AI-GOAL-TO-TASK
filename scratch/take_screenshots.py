import asyncio
import subprocess
import time
from playwright.async_api import async_playwright

async def main():
    server = subprocess.Popen(["npm", "--prefix", "frontend", "start"], shell=True)
    time.sleep(3)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()

            # 1. Light Theme Desktop
            page = await browser.new_page(viewport={"width": 1280, "height": 900}, color_scheme="light")
            await page.goto("http://localhost:3000", wait_until="networkidle")
            await page.screenshot(path="scratch/light_desktop_hero.png", clip={"x": 0, "y": 0, "width": 1280, "height": 900})
            
            # Scroll to simulator
            sim = page.locator("#simulator")
            await sim.scroll_into_view_if_needed()
            await page.screenshot(path="scratch/light_desktop_sim.png")

            # Scroll to architecture
            arch = page.locator("#architecture")
            await arch.scroll_into_view_if_needed()
            await page.screenshot(path="scratch/light_desktop_arch.png")

            # 2. Dark Theme Desktop
            # Click theme toggle
            toggle = page.locator("header button[title='Toggle Light/Dark Theme']")
            await toggle.click()
            await page.wait_for_timeout(500)
            await page.screenshot(path="scratch/dark_desktop_arch.png")

            await sim.scroll_into_view_if_needed()
            await page.screenshot(path="scratch/dark_desktop_sim.png")

            # 3. Tablet (768x1024)
            page_tab = await browser.new_page(viewport={"width": 768, "height": 1024}, color_scheme="light")
            await page_tab.goto("http://localhost:3000", wait_until="networkidle")
            sim_tab = page_tab.locator("#simulator")
            await sim_tab.scroll_into_view_if_needed()
            await page_tab.screenshot(path="scratch/light_tablet_sim.png")

            # 4. Mobile (375x667)
            page_mob = await browser.new_page(viewport={"width": 375, "height": 667}, color_scheme="light")
            await page_mob.goto("http://localhost:3000", wait_until="networkidle")
            sim_mob = page_mob.locator("#simulator")
            await sim_mob.scroll_into_view_if_needed()
            await page_mob.screenshot(path="scratch/light_mobile_sim.png")

            await browser.close()
            print("Screenshots taken successfully!")
    finally:
        server.terminate()
        server.kill()

if __name__ == "__main__":
    asyncio.run(main())

