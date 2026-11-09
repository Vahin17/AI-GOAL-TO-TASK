import asyncio
import logging
import uuid
import pytest
from orchestration.scheduler import ARDOScheduler
from execution.registry.tool_registry import ToolRegistry
from execution.browser.browser_tool import BrowserTool
from execution.browser.playwright_executor import PlaywrightBrowserExecutor
from execution.synthesis.action_synthesizer import ActionSynthesizer
from execution.synthesis.navigation_resolver import NavigationResolver
from ai.orchestrator import generate_task_graph

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("phase15_chain")

async def test_full_chain():
    logger.info("=== STARTING FULL END-TO-END VERIFICATION CHAIN ===")
    goal = "open wikipedia in the new tab"

    # Step 1: LLM Intent & Decomposition
    logger.info(f"\n1. Submitting goal: '{goal}'")
    plan = await generate_task_graph(goal)
    logger.info(f"Plan generated with {len(plan.nodes)} nodes: {[n.data.get('label') for n in plan.nodes]}")

    # Step 2: Tool Registry & Real Playwright Browser
    registry = ToolRegistry.get_instance()
    executor = PlaywrightBrowserExecutor(headless=True)
    await executor.start()
    browser_tool = BrowserTool(executor=executor)
    registry.register(browser_tool)

    scheduler = ARDOScheduler()

    collected_events = []

    try:
        session = await ARDOScheduler.start(
            plan_nodes=[n.model_dump() for n in plan.nodes],
            plan_edges=[e.model_dump() for e in plan.edges],
            goal_id=goal,
            session_id=f"ardo_{uuid.uuid4().hex[:8]}",
            mode="sequential",
            safety_decisions=plan.safety_decisions,
        )

        logger.info(f"\n2. Session started: {session.session_id}")

        await session._task

        logger.info("\n=== EVALUATING POST-EXECUTION INVARIANTS ===")
        metrics = session.engine.monitor.metrics

        print("\n" + "=" * 80)
        print("EXACT CHAIN EXECUTION SUMMARY:")
        print("=" * 80)
        print(f"Goal: '{goal}'")
        print(f"Session ID: {session.session_id}")
        print(f"Total Tasks Planned: {metrics.tasks_total}")
        print(f"Tasks Completed: {metrics.tasks_completed}")
        print(f"Tasks Failed: {metrics.tasks_failed}")
        print(f"Success Rate: {metrics.success_rate}")
        print(f"Tabs in Browser: {len(executor._context.pages)}")
        print(f"Current Browser URL: {executor._page.url if executor._page else 'None'}")
        print("=" * 80)

        # Assertions
        assert metrics.tasks_total > 0, "No tasks were planned!"
        assert metrics.tasks_completed == metrics.tasks_total, f"Incomplete tasks! {metrics.tasks_completed}/{metrics.tasks_total}"
        assert metrics.tasks_failed == 0, f"Some tasks failed! {metrics.tasks_failed}"
        assert metrics.success_rate == 1.0, f"Success rate not 1.0! {metrics.success_rate}"
        assert len(executor._context.pages) >= 2, f"New tab was not created! Pages: {len(executor._context.pages)}"
        assert "wikipedia.org" in (executor._page.url or ""), f"Not on wikipedia! URL: {executor._page.url}"

        print("\nALL INVARIANTS EMPIRICALLY SATISFIED AND VERIFIED!")

    finally:
        await executor.stop()

if __name__ == "__main__":
    asyncio.run(test_full_chain())
