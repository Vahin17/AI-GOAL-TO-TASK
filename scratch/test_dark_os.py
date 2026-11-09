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
            # Emulate an OS in dark mode!
            page = await browser.new_page(color_scheme="dark")
            await page.goto("http://localhost:3000", wait_until="networkidle")

            # Check heading color when page is in light theme
            heading_color = await page.evaluate("""() => {
                const h = document.querySelector('#architecture h3');
                return window.getComputedStyle(h).color;
            }""")
            print(f"OS dark mode, App light mode -> Heading color: {heading_color}")

            # Check node 1 text
            node_color = await page.evaluate("""() => {
                const el = document.querySelector('.space-y-2\\\\.5 span');
                return el ? window.getComputedStyle(el).color : 'not found';
            }""")
            print(f"OS dark mode, App light mode -> Node color: {node_color}")

            await browser.close()
    finally:
        server.terminate()
        server.kill()

if __name__ == "__main__":
    asyncio.run(main())

