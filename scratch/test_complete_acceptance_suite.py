import asyncio
import logging
import uuid
from typing import List, Dict, Any
from core.config import settings
from orchestration.scheduler import ARDOScheduler
from execution.registry.tool_registry import ToolRegistry
from execution.browser.browser_tool import BrowserTool
from execution.browser.playwright_executor import PlaywrightBrowserExecutor
from ai.orchestrator import generate_task_graph

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("acceptance_suite")

ACCEPTANCE_GOALS = [
    "open wikipedia in the new tab",
    "open wikipedia in the new tab and give me a screenshot",
    "open https://httpbin.org/forms/post and fill customer name with Test User",
    "open https://example.com and take a screenshot",
]

async def run_acceptance_suite():
    logger.info("=" * 80)
    logger.info("STARTING PHASE 15 END-TO-END ACCEPTANCE SUITE")
    logger.info("=" * 80)
    logger.info(f"[LLM-CONFIG] provider={settings.LLM_PROVIDER} model={settings.MODEL_NAME} api_key_configured={bool(settings.GEMINI_API_KEY)}")
    logger.info(f"[EMBEDDING-CONFIG] provider={'openai' if bool(settings.OPENAI_API_KEY) else 'local'}")
    logger.info(f"[BROWSER-CONFIG] headless={settings.HEADLESS_BROWSER} (headed visible browser)")
    logger.info(f"[SERVER-CONFIG] host={settings.BACKEND_HOST} port={settings.BACKEND_PORT}")
    logger.info("=" * 80)

    # Initialize headed browser executor as configured
    executor = PlaywrightBrowserExecutor(headless=settings.HEADLESS_BROWSER)
    await executor.start()

    registry = ToolRegistry.get_instance()
    browser_tool = BrowserTool(executor=executor)
    registry.register(browser_tool)

    results = []

    try:
        for idx, goal in enumerate(ACCEPTANCE_GOALS, 1):
            logger.info(f"\n" + "-" * 70)
            logger.info(f"RUNNING GOAL {idx}/{len(ACCEPTANCE_GOALS)}: '{goal}'")
            logger.info("-" * 70)

            tabs_before = len(executor._context.pages) if executor._context else 1
            url_before = executor._page.url if executor._page else "about:blank"

            # Step 1: Real AI Goal-To-Task Planning
            plan = await generate_task_graph(goal)
            planned_labels = [n.data.get("label") for n in plan.nodes]
            logger.info(f"[PLAN-OUTPUT] intent='{plan.metadata.intent}' tasks={planned_labels}")

            # Step 2: Launch through ARDOScheduler
            session_id = f"ardo_{uuid.uuid4().hex[:8]}"
            session = await ARDOScheduler.start(
                plan_nodes=[n.model_dump() for n in plan.nodes],
                plan_edges=[e.model_dump() for e in plan.edges],
                goal_id=goal,
                session_id=session_id,
                mode="sequential",
                safety_decisions=plan.safety_decisions,
            )

            # Step 3: Wait for session completion
            await session._task

            tabs_after = len(executor._context.pages) if executor._context else tabs_before
            url_after = executor._page.url if executor._page else "none"
            metrics = session.engine.monitor.metrics

            evidence = session.get_evidence()
            screenshot_bytes = None
            for item in evidence.get("evidence", []):
                meta = item.get("result", {}).get("metadata", {})
                if meta and "screenshot_size_bytes" in meta:
                    screenshot_bytes = meta["screenshot_size_bytes"]

            is_pass = (
                metrics.tasks_total > 0
                and metrics.tasks_completed == metrics.tasks_total
                and metrics.tasks_failed == 0
                and metrics.success_rate == 1.0
            )

            results.append({
                "index": idx,
                "goal": goal,
                "session_id": session_id,
                "tasks_planned": planned_labels,
                "tasks_completed": f"{metrics.tasks_completed}/{metrics.tasks_total}",
                "success_rate": metrics.success_rate,
                "tabs_before": tabs_before,
                "tabs_after": tabs_after,
                "url_before": url_before,
                "url_after": url_after,
                "screenshot_bytes": screenshot_bytes,
                "status": "PASS" if is_pass else "FAIL",
                "headed_visible": not executor.headless,
            })

    finally:
        await executor.stop()

    print("\n" + "=" * 90)
    print("PHASE 15 END-TO-END ACCEPTANCE RESULTS SUMMARY:")
    print("=" * 90)
    for r in results:
        print(f"[{r['status']}] GOAL {r['index']}: '{r['goal']}'")
        print(f"  Session: {r['session_id']} | Tasks: {r['tasks_completed']} | Success Rate: {r['success_rate']}")
        print(f"  Tabs: {r['tabs_before']} -> {r['tabs_after']} | URL: '{r['url_before']}' -> '{r['url_after']}'")
        print(f"  Headed Visible: {r['headed_visible']}")
        if r['screenshot_bytes']:
            print(f"  Screenshot Size: {r['screenshot_bytes']} bytes")
        print("-" * 90)
    print("=" * 90)

    # Final overall assertion
    all_passed = all(r["status"] == "PASS" for r in results)
    assert all_passed, "One or more acceptance goals failed!"
    print("\n>>> ALL 4 ACCEPTANCE CRITERIA EMPIRICALLY VERIFIED AND PASSED! <<<")

if __name__ == "__main__":
    asyncio.run(run_acceptance_suite())
