"""
test_phase15_perception.py — Test Suite for Phase 15 Environment Perception & JIT Action Grounding
==================================================================================================
Tests:
1. Live Playwright page perception & element extraction.
2. Natural-language search grounding without explicit CSS selectors.
3. Natural-language click grounding without explicit CSS selectors.
4. Dynamic environment perception reflecting live DOM state changes.
5. Safety governance blocking synthesized credential targets.
6. Ambiguity protection rejecting equally-scored elements.
7. Schema validation and error containment.
8. ToolRegistry dispatch of grounded semantic ToolCalls.
9. Legacy browser_action backward compatibility.
10. Generic task simulation fallback.
11. Full end-to-end multi-step natural language execution on real Chromium.
12. Diagnostic inspection API endpoint.
"""

import os
import pytest
from pathlib import Path
from starlette.testclient import TestClient

from execution.browser.browser_models import BrowserAction, BrowserActionType, BrowserActionResult
from execution.browser.browser_tool import BrowserTool
from execution.browser.environment_perception import EnvironmentPerceptionEngine
from execution.browser.perception_models import PageElement, PageSnapshot
from execution.browser.playwright_executor import PlaywrightBrowserExecutor
from execution.registry.base_tool import BaseTool
from execution.registry.tool_models import ToolCall, ToolCapability, ToolResult
from execution.registry.tool_registry import ToolRegistry
from execution.synthesis.action_synthesizer import ActionSynthesizer
from orchestration.dag_builder import DAGBuilder
from orchestration.runtime_engine import RuntimeEngine
from orchestration.safety_policy import SafetyPolicyEngine
from orchestration.safety_schemas import (
    ActionType,
    ExecutionDecision,
    RiskLevel,
    SafetyContext,
    TargetCategory,
)
from orchestration.state_machine import TaskState, TaskStateMachine
from main import app


@pytest.fixture
def dashboard_fixture_url():
    """File URL pointing to deterministic local dashboard fixture."""
    fixture_path = Path(__file__).parent / "fixtures" / "internal_dashboard.html"
    assert fixture_path.exists(), f"Fixture not found at {fixture_path}"
    return fixture_path.resolve().as_uri()


@pytest.fixture(autouse=True)
def reset_registry():
    """Reset the singleton registry before and after every test."""
    ToolRegistry.reset_instance()
    yield
    ToolRegistry.reset_instance()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Page Perception
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_15_1_page_perception(dashboard_fixture_url):
    """
    Test 1: Live Playwright page perception.
    Given internal_dashboard.html, EnvironmentPerceptionEngine must extract
    both input and button elements with their selectors, tags, and placeholders.
    """
    async with PlaywrightBrowserExecutor(headless=True) as executor:
        await executor.execute_action(
            BrowserAction(action_type=BrowserActionType.NAVIGATE, url=dashboard_fixture_url)
        )
        engine = EnvironmentPerceptionEngine()
        snapshot = await engine.inspect(executor._page)

        assert isinstance(snapshot, PageSnapshot)
        assert snapshot.url.startswith("file://")
        assert "Internal Project Ops Dashboard" in snapshot.title
        assert len(snapshot.elements) >= 4

        # Verify search input is discovered
        search_input = snapshot.find_element_by_selector("#project-search-input")
        assert search_input is not None
        assert search_input.tag == "input"
        assert "Search projects" in (search_input.placeholder or "")
        assert search_input.visible is True

        # Verify search button is discovered
        search_btn = snapshot.find_element_by_selector("#project-search-btn")
        assert search_btn is not None
        assert search_btn.tag == "button"
        assert "Search" in (search_btn.text or "")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Natural Language Search Grounding (Level 1 Semantic)
# ─────────────────────────────────────────────────────────────────────────────

def test_15_2_natural_language_search_grounding():
    """
    Test 2: Natural-language search grounding.
    Task: 'Search for project Phoenix'
    Contains NO '#selector' in the label.
    Synthesizer must map to '#project-search-input' with value 'Phoenix'.
    """
    snapshot = PageSnapshot(
        url="http://localhost:8080/dashboard",
        title="Dashboard",
        elements=[
            PageElement(tag="input", element_id="project-search-input", placeholder="Search projects...", css_selector="#project-search-input"),
            PageElement(tag="button", element_id="project-search-btn", text="Search Project", css_selector="#project-search-btn"),
        ],
    )

    synthesizer = ActionSynthesizer()
    call = synthesizer.synthesize_from_snapshot(
        task_id="t2",
        task_label="Search for project Phoenix",
        snapshot=snapshot,
    )

    assert call is not None
    assert call.tool_name == "browser"
    assert call.action_name == "fill"
    assert call.arguments["selector"] == "#project-search-input"
    assert call.arguments["value"] == "Phoenix"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Natural Language Click Grounding
# ─────────────────────────────────────────────────────────────────────────────

def test_15_3_natural_language_click_grounding():
    """
    Test 3: Natural-language click grounding.
    Task: 'Click the Search Project button'
    Contains NO '#selector' in the label.
    Synthesizer must map to '#project-search-btn' with action 'click'.
    """
    snapshot = PageSnapshot(
        url="http://localhost:8080/dashboard",
        title="Dashboard",
        elements=[
            PageElement(tag="input", element_id="project-search-input", placeholder="Search projects...", css_selector="#project-search-input"),
            PageElement(tag="button", element_id="project-search-btn", text="Search Project", css_selector="#project-search-btn"),
        ],
    )

    synthesizer = ActionSynthesizer()
    call = synthesizer.synthesize_from_snapshot(
        task_id="t3",
        task_label="Click the Search Project button",
        snapshot=snapshot,
    )

    assert call is not None
    assert call.tool_name == "browser"
    assert call.action_name == "click"
    assert call.arguments["selector"] == "#project-search-btn"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Dynamic Environment Perception (State Change Across Tasks)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_15_4_dynamic_environment_state_change(dashboard_fixture_url):
    """
    Test 4: Dynamic environment state reflection.
    Executes a search action in Chromium, then re-inspects the page.
    The new PageSnapshot must reflect the live DOM mutation without reloading.
    """
    async with PlaywrightBrowserExecutor(headless=True) as executor:
        await executor.execute_action(
            BrowserAction(action_type=BrowserActionType.NAVIGATE, url=dashboard_fixture_url)
        )
        await executor.execute_action(
            BrowserAction(action_type=BrowserActionType.FILL, selector="#project-search-input", value="Apollo")
        )
        await executor.execute_action(
            BrowserAction(action_type=BrowserActionType.CLICK, selector="#project-search-btn")
        )

        # Re-inspect page after search execution
        snapshot = await executor.inspect_page()
        assert snapshot is not None

        # Verify table name cell updated to include [Apollo]
        extract_res = await executor.execute_action(
            BrowserAction(action_type=BrowserActionType.EXTRACT_TEXT, selector="#project-name-cell")
        )
        assert "[Apollo]" in (extract_res.text_extracted or "")


# ─────────────────────────────────────────────────────────────────────────────
# 5. Safety Blocking on Synthesized Credential Target
# ─────────────────────────────────────────────────────────────────────────────

def test_15_5_safety_blocking_on_synthesized_credential_target():
    """
    Test 5: Safety governance blocking credential target.
    A natural-language task 'Enter vault master password with secret123'
    grounds to '#admin-credential-reset-input'.
    SafetyPolicyEngine MUST evaluate concrete parameters and BLOCK execution.
    """
    snapshot = PageSnapshot(
        url="http://localhost:8080/dashboard",
        title="Dashboard",
        elements=[
            PageElement(
                tag="input",
                element_id="admin-credential-reset-input",
                input_type="password",
                placeholder="Enter root security vault master password...",
                css_selector="#admin-credential-reset-input"
            ),
        ],
    )

    synthesizer = ActionSynthesizer()
    call = synthesizer.synthesize_from_snapshot(
        task_id="t_danger",
        task_label="Enter root security vault master password with secret123",
        snapshot=snapshot,
    )

    assert call is not None
    assert call.arguments["selector"] == "#admin-credential-reset-input"

    # Evaluate concrete safety context
    b_tool = BrowserTool()
    s_ctx = b_tool.infer_safety_context("t_danger", call)
    assert s_ctx.target_category == TargetCategory.CREDENTIAL

    # Evaluate policy
    policy_engine = SafetyPolicyEngine()
    decision = policy_engine.evaluate(s_ctx, call.description or "Enter password")
    assert decision.is_blocked is True
    assert "credential" in decision.explanation.lower() or "password" in decision.explanation.lower()


# ─────────────────────────────────────────────────────────────────────────────
# 6. Ambiguity Protection (Reject Equal Candidates)
# ─────────────────────────────────────────────────────────────────────────────

def test_15_6_ambiguous_element_rejection():
    """
    Test 6: Ambiguous element handling.
    If multiple candidate inputs have identical search semantics and cannot be
    disambiguated, the synthesizer must return None to prevent arbitrary execution.
    """
    snapshot = PageSnapshot(
        url="http://localhost:8080/dashboard",
        title="Dashboard",
        elements=[
            PageElement(tag="input", element_id="search-box-a", placeholder="Search items...", css_selector="#search-box-a"),
            PageElement(tag="input", element_id="search-box-b", placeholder="Search items...", css_selector="#search-box-b"),
        ],
    )

    synthesizer = ActionSynthesizer()
    call = synthesizer.synthesize_from_snapshot(
        task_id="t_ambig",
        task_label="Search for items",
        snapshot=snapshot,
    )

    # Must be None due to ambiguity detection
    assert call is None, "Ambiguous elements should return None rather than guessing!"


# ─────────────────────────────────────────────────────────────────────────────
# 7. Schema Validation Rejection
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_15_7_schema_validation_rejection():
    """
    Test 7: Schema validation.
    A ToolCall with missing required arguments (e.g. click without selector)
    is rejected cleanly by ToolRegistry before reaching Playwright.
    """
    registry = ToolRegistry.get_instance()
    b_tool = BrowserTool()
    registry.register(b_tool)

    malformed_call = ToolCall(
        call_id="call_bad",
        tool_name="browser",
        action_name="click",
        arguments={},  # missing selector
    )

    result = await registry.dispatch(malformed_call)
    assert result.success is False
    assert "VALIDATION_ERROR" in (result.error or "") or "Selector parameter required" in (result.error or "")


# ─────────────────────────────────────────────────────────────────────────────
# 8. ToolRegistry Dispatch of Semantic ToolCall
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_15_8_registry_dispatch(dashboard_fixture_url):
    """
    Test 8: Complete dispatch path:
    ToolCall -> ToolRegistry -> BrowserTool -> Playwright.
    """
    async with PlaywrightBrowserExecutor(headless=True) as executor:
        b_tool = BrowserTool(executor=executor)
        registry = ToolRegistry.get_instance()
        registry.register(b_tool)

        # 1. Navigate via ToolRegistry
        nav_call = ToolCall(
            call_id="c_nav",
            tool_name="browser",
            action_name="navigate",
            arguments={"url": dashboard_fixture_url},
        )
        res_nav = await registry.dispatch(nav_call)
        assert res_nav.success is True

        # 2. Inspect page via ToolRegistry action
        inspect_call = ToolCall(
            call_id="c_insp",
            tool_name="browser",
            action_name="inspect_page",
            arguments={},
        )
        res_insp = await registry.dispatch(inspect_call)
        assert res_insp.success is True
        assert "snapshot" in res_insp.output_data
        assert res_insp.output_data["snapshot"]["url"].startswith("file://")


# ─────────────────────────────────────────────────────────────────────────────
# 9. Legacy browser_action Compatibility
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_15_9_legacy_browser_action_compatibility(dashboard_fixture_url):
    """
    Test 9: Backward compatibility with Phase 10/11 manual browser_action.
    """
    nodes_data = [
        {
            "id": "t1",
            "data": {"label": "Manual Navigation"},
            "browser_action": {
                "action_type": "navigate",
                "url": dashboard_fixture_url,
            },
        },
    ]
    graph = DAGBuilder.build(nodes_data, [], execution_mode="sequential")

    async with PlaywrightBrowserExecutor(headless=True) as executor:
        b_tool = BrowserTool(executor=executor)
        engine = RuntimeEngine(
            graph=graph,
            session_id="sess_p15_legacy",
            mode="safe",
            browser_adapter=b_tool.adapter,
        )

        async for _ in engine.run():
            pass

        node = graph.get_node("t1")
        assert node.status == TaskState.COMPLETED
        assert node.browser_result is not None
        assert node.browser_result.success is True


# ─────────────────────────────────────────────────────────────────────────────
# 10. Generic Simulation Fallback
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_15_10_generic_simulation_fallback():
    """
    Test 10: Tasks that have no tool grounding cleanly fall back to simulation.
    """
    nodes_data = [
        {"id": "t_generic", "data": {"label": "Analyze architectural invariants"}},
    ]
    graph = DAGBuilder.build(nodes_data, [], execution_mode="sequential")

    node = graph.get_node("t_generic")
    assert node.tool_call is None
    assert node.browser_action is None

    engine = RuntimeEngine(
        graph=graph,
        session_id="sess_p15_sim",
        mode="safe",
    )

    async for _ in engine.run():
        pass

    assert node.status in (TaskState.COMPLETED, TaskState.FAILED)


# ─────────────────────────────────────────────────────────────────────────────
# 11. Full End-to-End Multi-Step Natural Language Execution (Real Chromium)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_15_11_end_to_end_natural_language_workflow(dashboard_fixture_url):
    """
    Test 11: FULL JIT ENVIRONMENT GROUNDING ON REAL CHROMIUM:
    Tasks use PURE NATURAL LANGUAGE (no explicit selectors for search):
    1. 'Navigate to <dashboard_url>'
    2. 'Search for project Phoenix' (JIT grounded to #project-search-input)
    3. 'Click Search Project' (JIT grounded to #project-search-btn)
    4. 'Extract text from #project-name-cell'
    5. 'Take screenshot of dashboard'

    Verifies that real Chromium executes all 5 steps without human selector binding!
    """
    nodes_data = [
        {"id": "t1", "data": {"label": f"Navigate to {dashboard_fixture_url}"}},
        {"id": "t2", "data": {"label": "Search for project Phoenix"}},
        {"id": "t3", "data": {"label": "Click Search Project"}},
        {"id": "t4", "data": {"label": "Extract text from #project-name-cell"}},
        {"id": "t5", "data": {"label": "Take screenshot of dashboard"}},
    ]
    edges_data = [
        {"source": "t1", "target": "t2"},
        {"source": "t2", "target": "t3"},
        {"source": "t3", "target": "t4"},
        {"source": "t4", "target": "t5"},
    ]

    graph = DAGBuilder.build(nodes_data, edges_data, execution_mode="sequential")

    # Before execution, t2 and t3 should have NO tool_call (deferred to JIT)
    assert graph.get_node("t2").tool_call is None
    assert graph.get_node("t3").tool_call is None

    async with PlaywrightBrowserExecutor(headless=True) as executor:
        b_tool = BrowserTool(executor=executor)
        registry = ToolRegistry.get_instance()
        registry.register(b_tool)

        engine = RuntimeEngine(
            graph=graph,
            session_id="sess_p15_e2e",
            mode="sequential",
            browser_adapter=b_tool.adapter,
        )

        async for _ in engine.run():
            pass

        # Verify all tasks completed
        for nid in ["t1", "t2", "t3", "t4", "t5"]:
            node = graph.get_node(nid)
            assert node.status == TaskState.COMPLETED, f"Task {nid} failed! Logs: {node.execution_logs}"

        # Verify t2 was JIT grounded to #project-search-input
        t2_node = graph.get_node("t2")
        assert t2_node.tool_call is not None
        assert t2_node.tool_call.arguments.get("selector") == "#project-search-input"
        assert t2_node.tool_call.arguments.get("value") == "Phoenix"

        # Verify t3 was JIT grounded to #project-search-btn
        t3_node = graph.get_node("t3")
        assert t3_node.tool_call is not None
        assert t3_node.tool_call.arguments.get("selector") == "#project-search-btn"

        # Verify t4 extracted the updated project text
        t4_node = graph.get_node("t4")
        assert "[Phoenix]" in (t4_node.browser_result.extracted_text or "")


# ─────────────────────────────────────────────────────────────────────────────
# 12. Diagnostic Inspection API Endpoint
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_15_12_inspect_endpoint(dashboard_fixture_url):
    """
    Test 12: GET /orchestrate/{session_id}/inspect returns live PageSnapshot.
    """
    async with PlaywrightBrowserExecutor(headless=True) as executor:
        b_tool = BrowserTool(executor=executor)
        registry = ToolRegistry.get_instance()
        registry.register(b_tool)

        # Navigate first so browser is running
        await executor.execute_action(
            BrowserAction(action_type=BrowserActionType.NAVIGATE, url=dashboard_fixture_url)
        )

        from api.router import inspect_session_page
        data = await inspect_session_page("sess_test_p15")
        assert data["status"] == "active"
        assert "snapshot" in data
        assert "elements" in data["snapshot"]
        assert len(data["snapshot"]["elements"]) >= 4
