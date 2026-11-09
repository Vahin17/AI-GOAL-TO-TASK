import asyncio
from playwright.async_api import async_playwright
import subprocess
import time

async def main():
    server = subprocess.Popen(["npm", "--prefix", "frontend", "start"], shell=True)
    time.sleep(3)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(color_scheme="light")
            await page.goto("http://localhost:3000", wait_until="networkidle")

            # Check theme state
            root_class = await page.evaluate("() => document.querySelector('.landing-root')?.className")
            html_class = await page.evaluate("() => document.documentElement.className")
            print(f"html class: {html_class}")
            print(f"landing-root class: {root_class}")

            # Elements to inspect
            queries = [
                ("Hero H1", "h1"),
                ("Hero Subtitle", "p.max-w-2xl"),
                ("Hero Nav Links", "nav a"),
                ("Stage 01 Heading", "#architecture h3"),
                ("Stage 01 Description", "#architecture p"),
                ("Feedback Loop text", ".max-w-4xl.mx-auto"),
                ("Supporting Infra Heading", "#architecture h3.text-2xl"),
                ("Supporting Infra p", "#architecture div.text-center > p"),
                ("Workflow preview node 1 text", ".lg\\:col-span-5 span.text-slate-800"),
                ("Simulator Preset buttons", "#simulator button.rounded-xl"),
                ("Simulator Custom Goal Label", "#simulator label"),
            ]

            for name, sel in queries:
                info = await page.evaluate(f"""() => {{
                    const el = document.querySelector("{sel}");
                    if (!el) return "NOT FOUND";
                    const s = window.getComputedStyle(el);
                    return {{
                        text: el.innerText.substring(0, 40),
                        color: s.color,
                        bgColor: s.backgroundColor,
                        opacity: s.opacity,
                        classes: el.className
                    }};
                }}""")
                print(f"\n{name} ({sel}):")
                print(f"  {info}")

            await browser.close()
    finally:
        server.terminate()
        server.kill()

if __name__ == "__main__":
    asyncio.run(main())

