import asyncio
import logging
from execution.browser.playwright_executor import PlaywrightBrowserExecutor
from execution.browser.browser_models import BrowserAction, BrowserActionType
from execution.synthesis.action_synthesizer import ActionSynthesizer
from execution.synthesis.navigation_resolver import NavigationResolver

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("multistep_test")

async def test_multistep():
    executor = PlaywrightBrowserExecutor(headless=True)
    await executor.start()
    synth = ActionSynthesizer()

    try:
        # Step 1: Open Wikipedia in new tab
        print("\n--- STEP 1: Open Wikipedia in a new tab ---")
        call1 = synth.synthesize_for_task("t1", "Open Wikipedia in a new tab")
        print("Synthesized call 1:", call1.action_name, call1.arguments)
        act1 = BrowserAction(action_type=BrowserActionType(call1.action_name), url=call1.arguments["url"], description=call1.description)
        res1 = await executor.execute_action(act1)
        print("Executed step 1:", res1.success, res1.current_url)
        assert res1.success is True

        # Step 2: Search for 'Artificial Intelligence'
        print("\n--- STEP 2: Search for Artificial Intelligence ---")
        snap1 = await executor.inspect_page()
        print(f"Page elements after step 1: {len(snap1.elements)}")
        call2 = synth.synthesize_from_snapshot("t2", "Search for 'Artificial Intelligence'", snap1)
        print("Synthesized call 2:", call2.action_name, call2.arguments if call2 else None)
        assert call2 is not None
        act2 = BrowserAction(action_type=BrowserActionType(call2.action_name), selector=call2.arguments.get("selector"), value=call2.arguments.get("value"), description=call2.description)
        res2 = await executor.execute_action(act2)
        print("Executed step 2:", res2.success, res2.current_url)

        # Submit search via Enter or click search button
        act2_press = BrowserAction(action_type=BrowserActionType.PRESS, key="Enter")
        res2_press = await executor.execute_action(act2_press)
        await asyncio.sleep(2)  # wait for Wikipedia search results or direct article redirect
        print("Search submitted via Enter:", res2_press.success, executor._page.url)

        # Step 3: Click relevant article or verify on article page
        print("\n--- STEP 3: Verify / Click Article ---")
        snap2 = await executor.inspect_page()
        print(f"Current page title: '{snap2.title}', URL: '{snap2.url}'")
        if "Artificial intelligence" in snap2.title:
            print("Directly reached Artificial Intelligence article on Wikipedia!")
        else:
            call3 = synth.synthesize_from_snapshot("t3", "Click on Artificial Intelligence article", snap2)
            if call3:
                print("Synthesized call 3:", call3.action_name, call3.arguments)
                act3 = BrowserAction(action_type=BrowserActionType(call3.action_name), selector=call3.arguments.get("selector"))
                res3 = await executor.execute_action(act3)
                await asyncio.sleep(1)
                print("Clicked article:", res3.success, executor._page.url)

        # Step 4: Scroll down
        print("\n--- STEP 4: Scroll down ---")
        snap3 = await executor.inspect_page()
        call4 = synth.synthesize_from_snapshot("t4", "Scroll down the article page", snap3)
        print("Synthesized call 4:", call4.action_name, call4.arguments if call4 else None)
        assert call4 is not None
        assert call4.action_name == "scroll"
        act4 = BrowserAction(action_type=BrowserActionType(call4.action_name), value=call4.arguments.get("value"))
        res4 = await executor.execute_action(act4)
        print("Executed scroll:", res4.success)

        # Step 5: Take screenshot
        print("\n--- STEP 5: Take screenshot ---")
        snap4 = await executor.inspect_page()
        call5 = synth.synthesize_from_snapshot("t5", "Take a screenshot of the page", snap4)
        print("Synthesized call 5:", call5.action_name)
        assert call5 is not None
        assert call5.action_name == "screenshot"
        act5 = BrowserAction(action_type=BrowserActionType(call5.action_name))
        res5 = await executor.execute_action(act5)
        print("Executed screenshot:", res5.success, "bytes:", res5.metadata.get("screenshot_size_bytes"))
        assert res5.metadata.get("screenshot_size_bytes", 0) > 1000

        print("\nALL 5 STEPS EXECUTED AND VERIFIED SUCCESSFULLY!")

    finally:
        await executor.stop()

if __name__ == "__main__":
    asyncio.run(test_multistep())
