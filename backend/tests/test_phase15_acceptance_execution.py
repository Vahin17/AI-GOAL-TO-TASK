import asyncio
import os
import pytest
from pathlib import Path
from core.database import get_database
from orchestration.scheduler import ARDOScheduler
from orchestration.state_machine import TaskState
from ai.fallback_rules import generate_fallback_plan
from execution.registry.tool_registry import ToolRegistry


@pytest.fixture
def dashboard_fixture_url() -> str:
    fixture_path = Path(__file__).parent / "fixtures" / "internal_dashboard.html"
    assert fixture_path.exists(), f"Fixture missing: {fixture_path}"
    return fixture_path.as_uri()


@pytest.mark.asyncio
async def test_real_world_acceptance_execution(dashboard_fixture_url):
    """
    Acceptance Test:
    "Open the internal dashboard, search for Phoenix, open the matching project, update its description, and take a screenshot."

    Verifies:
    1. Deterministic goal decomposition produces actionable tasks.
    2. Real Playwright Chromium browser opens internal_dashboard.html.
    3. JIT perception discovers #project-search-input and fills 'Phoenix'.
    4. JIT perception discovers matching project link and clicks it.
    5. JIT perception discovers #project-desc-input, fills and saves metadata.
    6. Verifies DOM is updated on the live page.
    7. Playwright captures a real screenshot stored in task evidence.
    8. All DAG tasks transition to COMPLETED (not stuck at 0/3).
    """
    goal_text = (
        "Open the internal dashboard, search for Phoenix, open the matching project, "
        "update its description, and take a screenshot."
    )

    # Step 1: Generate plan (simulating unconfigured external LLM key fallback)
    plan_response = await generate_fallback_plan(goal_text)
    assert len(plan_response.nodes) == 5, f"Expected 5 decomposed tasks, got {len(plan_response.nodes)}"

    nodes_data = [n.dict() for n in plan_response.nodes]
    edges_data = [e.dict() for e in plan_response.edges]

    session_id = "session_acceptance_p15"

    # Step 2: Start ARDO session via scheduler
    session = await ARDOScheduler.start(
        plan_nodes=nodes_data,
        plan_edges=edges_data,
        goal_id=goal_text,
        session_id=session_id,
        mode="standard",
        db=None,
    )

    # Step 3: Stream and collect execution events until DAG completes
    events = []
    subscriber = session.subscribe()

    async def collect_events():
        while True:
            ev = await subscriber.get()
            events.append(ev)
            if ev.event_type == "session_complete":
                break

    try:
        await asyncio.wait_for(collect_events(), timeout=60.0)
    finally:
        session.unsubscribe(subscriber)

    # Step 4: Verify task completions in DAG
    completed_nodes = [
        node for node in session.graph.nodes.values()
        if node.status == TaskState.COMPLETED
    ]
    assert len(completed_nodes) == 5, (
        f"Expected all 5 tasks to complete, but only {len(completed_nodes)} completed: "
        f"{[(n.task_id, n.task_name, n.status.value) for n in session.graph.nodes.values()]}"
    )

    # Step 5: Verify browser state & live DOM update
    registry = ToolRegistry.get_instance()
    browser_tool = registry.get_tool("browser")
    assert browser_tool is not None, "Browser tool must be registered"
    executor = browser_tool.executor
    assert executor is not None and executor.is_running, "Browser executor must be running"

    # Print diagnostic logs
    for node in session.graph.nodes.values():
        print(f"\n--- Node: {node.task_id} ({node.task_name}) Status: {node.status.value} ---")
        if node.tool_call:
            print(f"  ToolCall: {node.tool_call.tool_name}.{node.tool_call.action_name} args={node.tool_call.arguments}")
        for log in node.execution_logs:
            print(f"  Log: {log}")

    # Verify DOM contains the updated Phoenix search and metadata notes
    page = executor._page
    name_cell_text = await page.inner_text("#project-name-cell")
    assert "Phoenix" in name_cell_text, f"Expected 'Phoenix' in project name, got: '{name_cell_text}'"

    banner_text = await page.inner_text("#status-banner")
    assert "Metadata successfully updated" in banner_text, f"Expected status banner to be updated, got: '{banner_text}'"

    note_tag_text = await page.inner_text("#active-note-tag")
    assert "Phoenix" in note_tag_text or "Production" in note_tag_text, (
        f"Expected active note to contain update, got: '{note_tag_text}'"
    )

    # Step 6: Verify screenshot evidence
    evidence = session.get_evidence()
    assert "evidence_items" in evidence
    assert evidence["evidence_count"] >= 1
    screenshot_evidence = [
        item for item in evidence["evidence_items"]
        if item.get("action", {}).get("action_type") in ("screenshot", "BrowserActionType.SCREENSHOT")
        or (item.get("result") and item["result"].get("action_name") == "screenshot")
    ]
    assert len(screenshot_evidence) > 0, f"Expected screenshot evidence in {evidence}"

    # Clean up browser executor
    await executor.stop()
