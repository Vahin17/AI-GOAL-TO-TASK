import asyncio
import sys
from playwright.async_api import async_playwright

async def main():
    print("Connecting to running server on http://localhost:3000...", flush=True)
    async with async_playwright() as p:
        # Launch Chromium in a completely clean, isolated context (no extensions, no autofill profiles)
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        console_logs = []
        page.on("console", lambda msg: console_logs.append({
            "type": msg.type,
            "text": msg.text,
            "location": msg.location
        }))

        print("\n==========================================", flush=True)
        print("TEST 1: Navigating to http://localhost:3000/app", flush=True)
        print("==========================================", flush=True)
        await page.goto("http://localhost:3000/app", wait_until="networkidle")
        await asyncio.sleep(2)

        # 1. Search DOM for fdprocessedid
        fd_elements = await page.evaluate("""
            () => {
                const results = [];
                const all = document.querySelectorAll('*');
                for (const el of all) {
                    for (const attr of el.attributes) {
                        if (attr.name.toLowerCase().includes('fdprocessedid')) {
                            results.push({
                                tag: el.tagName,
                                attr: attr.name,
                                value: attr.value,
                                outerHtml: el.outerHTML.slice(0, 150)
                            });
                        }
                    }
                }
                return results;
            }
        """)
        print(f"Elements in DOM with 'fdprocessedid': {len(fd_elements)}", flush=True)
        if fd_elements:
            for el in fd_elements:
                print(f"  FOUND: {el}", flush=True)
        else:
            print("  --> ZERO elements in DOM have 'fdprocessedid' in clean browser context!", flush=True)

        # 2. Inspect buttons inside ExecutionControl
        buttons = await page.evaluate("""
            () => {
                const btns = Array.from(document.querySelectorAll('button'));
                return btns.map(b => ({
                    text: b.innerText.trim().slice(0, 30),
                    attributes: Array.from(b.attributes).map(a => a.name)
                }));
            }
        """)
        print(f"\nInspected {len(buttons)} total buttons on /app:", flush=True)
        for b in buttons[:10]:
            print(f"  Button: '{b['text']}' -> Attributes: {b['attributes']}", flush=True)

        # 3. Check for any React hydration mismatch errors or warnings
        hydration_messages = [
            m for m in console_logs
            if "hydration" in m["text"].lower() or "mismatch" in m["text"].lower() or "did not match" in m["text"].lower() or "fdprocessedid" in m["text"].lower()
        ]
        print(f"\nHydration-related console messages on /app: {len(hydration_messages)}", flush=True)
        for m in hydration_messages:
            print(f"  [{m['type'].upper()}] {m['text']}", flush=True)

        # 4. Check all console errors and warnings
        errors_and_warnings = [m for m in console_logs if m["type"] in ("error", "warning")]
        print(f"\nAll console errors/warnings on /app: {len(errors_and_warnings)}", flush=True)
        for m in errors_and_warnings:
            print(f"  [{m['type'].upper()}] {m['text']}", flush=True)

        print("\n==========================================", flush=True)
        print("TEST 2: Navigating to http://localhost:3000 (Landing Page)", flush=True)
        print("==========================================", flush=True)
        console_logs.clear()
        await page.goto("http://localhost:3000", wait_until="networkidle")
        await asyncio.sleep(2)

        landing_hydration = [
            m for m in console_logs
            if "hydration" in m["text"].lower() or "mismatch" in m["text"].lower() or "did not match" in m["text"].lower()
        ]
        print(f"Hydration-related console messages on /: {len(landing_hydration)}", flush=True)
        for m in landing_hydration:
            print(f"  [{m['type'].upper()}] {m['text']}", flush=True)

        await browser.close()
        print("\nClean browser verification completed successfully.", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
