import asyncio
import functools
import http.server
import json
import logging
import os
import re
import threading
from pathlib import Path
from unittest.mock import patch
import pytest

from core.database import get_database
from orchestration.scheduler import ARDOScheduler
from orchestration.state_machine import TaskState
from ai.fallback_rules import generate_fallback_plan
from ai.gemini_provider import GeminiProvider
from execution.registry.tool_registry import ToolRegistry
from execution.browser.network_guard import NetworkGuard
from execution.browser.browser_models import BrowserActionType

logger = logging.getLogger(__name__)


def _find_ref(prompt: str, pattern: str):
    m = re.search(r"(elem_\d+).*?" + pattern, prompt, re.IGNORECASE)
    if m:
        return m.group(1)
    m2 = re.search(pattern + r".*?(elem_\d+)", prompt, re.IGNORECASE)
    if m2:
        return m2.group(1)
    return None


async def _mock_gemini_query(self, prompt: str, system_instruction: str = "", json_mode: bool = False, **kwargs) -> str:
    """
    Deterministic, realistic Gemini provider mock that returns structured decisions
    based on the current browser observation provided in the prompt.
    """
    p_lower = prompt.lower()

    # Task 2: Search for Phoenix
    if "search for phoenix" in p_lower:
        search_input_ref = _find_ref(prompt, r"Search projects") or "elem_001"
        search_btn_ref = _find_ref(prompt, r"Search Project") or "elem_002"

        if f"click on {search_btn_ref}" in p_lower:
            return json.dumps({
                "decision": "finish",
                "reason": "Search completed for Phoenix",
            })
        elif f"fill on {search_input_ref}" in p_lower or "press" in p_lower or f"value='phoenix'" in p_lower or f'value="phoenix"' in p_lower:
            return json.dumps({
                "decision": "execute",
                "action": {
                    "action_type": "click",
                    "element_ref": search_btn_ref,
                },
                "reason": "Click search button to filter projects for Phoenix",
            })
        else:
            return json.dumps({
                "decision": "execute",
                "action": {
                    "action_type": "fill",
                    "element_ref": search_input_ref,
                    "value": "Phoenix",
                },
                "reason": "Fill search input with Phoenix",
            })

    # Task 3: Open the matching project
    if "open the matching project" in p_lower:
        project_link_ref = _find_ref(prompt, r"AI Goal-to-Task") or _find_ref(prompt, r"link") or "elem_003"
        if f"recent action: click on {project_link_ref}" in p_lower or "recent action: click" in p_lower:
            return json.dumps({
                "decision": "finish",
                "reason": "Matching project opened",
            })
        else:
            return json.dumps({
                "decision": "execute",
                "action": {
                    "action_type": "click",
                    "element_ref": project_link_ref,
                },
                "reason": "Click matching project link to open details",
            })

    # Task 4: Update its description
    if "update its description" in p_lower:
        desc_input_ref = _find_ref(prompt, r"project notes") or "elem_004"
        save_btn_ref = _find_ref(prompt, r"Update Metadata") or "elem_005"

        if f"click on {save_btn_ref}" in p_lower:
            return json.dumps({
                "decision": "finish",
                "reason": "Project description updated successfully",
            })
        elif f"fill on {desc_input_ref}" in p_lower or "recent action: fill" in p_lower:
            return json.dumps({
                "decision": "execute",
                "action": {
                    "action_type": "click",
                    "element_ref": save_btn_ref,
                },
                "reason": "Click Update Metadata button to save updated notes",
            })
        else:
            return json.dumps({
                "decision": "execute",
                "action": {
                    "action_type": "fill",
                    "element_ref": desc_input_ref,
                    "value": "Phoenix project description updated",
                },
                "reason": "Enter updated description for Phoenix project",
            })

    # Task 5: Take a screenshot
    if "take a screenshot" in p_lower or "screenshot" in p_lower:
        if "recent action: screenshot" in p_lower:
            return json.dumps({
                "decision": "finish",
                "reason": "Screenshot captured",
            })
        else:
            return json.dumps({
                "decision": "execute",
                "action": {
                    "action_type": "screenshot",
                },
                "reason": "Take a screenshot of the updated dashboard",
            })

    return json.dumps({
        "decision": "finish",
        "reason": "Default finish",
    })


@pytest.fixture(scope="module")
def fixture_http_server():
    """
    Spawns a lightweight local HTTP server bound strictly to 127.0.0.1 on an ephemeral port
    to serve backend/tests/fixtures without violating SSRF or requiring file:// permissions.
    """
    fixtures_dir = Path(__file__).parent / "fixtures"
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(fixtures_dir))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    host, port = server.server_address
    origin = f"http://127.0.0.1:{port}"

    NetworkGuard.register_test_allowed_origin(origin)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        yield origin
    finally:
        NetworkGuard.unregister_test_allowed_origin(origin)
        server.shutdown()
        server.server_close()


@pytest.fixture
def dashboard_fixture_url(fixture_http_server) -> str:
    return f"{fixture_http_server}/internal_dashboard.html"


@pytest.mark.live_browser
@pytest.mark.asyncio
async def test_real_world_acceptance_execution(dashboard_fixture_url):
    """
    Acceptance Test:
    "Open the internal dashboard, search for Phoenix, open the matching project, update its description, and take a screenshot."

    Verifies:
    1. Deterministic goal decomposition produces actionable tasks.
    2. Real Playwright Chromium browser opens internal_dashboard.html via local fixture server.
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

    # Explicitly supply the fixture destination URL to task 0 metadata
    if len(nodes_data) > 0:
        nodes_data[0].setdefault("metadata", {})["expected_url"] = dashboard_fixture_url
        nodes_data[0]["metadata"]["destination_url"] = dashboard_fixture_url

    session_id = "session_acceptance_p15"
    session = None
    subscriber = None

    with patch.object(GeminiProvider, "query", new=_mock_gemini_query):
        try:
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
                    completed = [n for n in session.graph.nodes.values() if n.status == TaskState.COMPLETED]
                    if len(completed) == 5:
                        break
                    if ev.event_type == "session_complete":
                        break
                    if hasattr(session, "engine") and session.engine and session.engine._all_terminal():
                        break

            await asyncio.wait_for(collect_events(), timeout=30.0)

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
            browser_tool = session.tool_registry.get_tool("browser") if hasattr(session, "tool_registry") else None
            if not browser_tool:
                registry = ToolRegistry.get_instance()
                browser_tool = registry.get_tool("browser")
            assert browser_tool is not None, "Browser tool must be registered"
            executor = browser_tool.executor or getattr(session.browser_adapter, "executor", None)
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
                or any(
                    str(a.get("action_type", "")).lower() in ("screenshot", "browseractiontype.screenshot")
                    for a in item.get("result", {}).get("actions_taken", [])
                )
            ]
            assert len(screenshot_evidence) > 0, f"Expected screenshot evidence in {evidence}"

        finally:
            # Guaranteed idempotent cleanup of all session, executor, and driver resources
            if session is not None:
                if subscriber is not None:
                    try:
                        session.unsubscribe(subscriber)
                    except Exception:
                        pass
                try:
                    await session.cleanup()
                except Exception as e:
                    logger.warning(f"Error during session.cleanup: {e}")

                executor = getattr(session.browser_adapter, "executor", None)
                if executor is None and hasattr(session, "tool_registry") and session.tool_registry:
                    b_tool = session.tool_registry.get_tool("browser")
                    executor = getattr(b_tool, "executor", None)
                if executor:
                    if hasattr(executor, "stop"):
                        try:
                            await executor.stop()
                        except Exception as e:
                            logger.warning(f"Error stopping executor: {e}")
                    if hasattr(executor, "force_terminate") and getattr(executor, "is_running", False):
                        try:
                            executor.force_terminate()
                        except Exception as e:
                            logger.warning(f"Error force-terminating executor: {e}")

                ARDOScheduler._sessions.pop(session_id, None)
