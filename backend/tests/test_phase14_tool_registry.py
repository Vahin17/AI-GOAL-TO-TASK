"""
test_phase14_tool_registry.py — Phase 14: Autonomous Tool Registry & Action Synthesis Tests
==========================================================================================
Validates:
    14.1 Tool registration in ToolRegistry
    14.2 Tool retrieval by exact name
    14.3 Capability-based tool lookup
    14.4 Missing tool handling (clean error, no crash)
    14.5 Input schema validation (rejection of invalid arguments)
    14.6 Tool dispatch and standardized ToolResult formatting
    14.7 BrowserTool conformance to BaseTool
    14.8 Heuristic synthesis: NAVIGATE action
    14.9 Heuristic synthesis: CLICK action
    14.10 Heuristic synthesis: FILL action
    14.11 Heuristic synthesis: EXTRACT_TEXT action
    14.12 Heuristic synthesis: SCREENSHOT action
    14.13 Pre-execution safety evaluation of synthesized actions
    14.14 Credential / protected target blocking on synthesized parameters
    14.15 Generic task simulation fallback when synthesis returns None
    14.16 Legacy manual browser_action backward compatibility
    14.17 End-to-end synthesized browser execution against local fixture
    14.18 Diagnostic GET /tools endpoint
"""

import asyncio
from pathlib import Path
import time
from typing import Any, Dict, List, Optional
import pytest
from pydantic import BaseModel, Field

from execution.registry.base_tool import BaseTool
from execution.registry.tool_models import ToolCapability, ToolCall, ToolResult
from execution.registry.tool_registry import ToolRegistry
from execution.browser.browser_models import BrowserAction, BrowserActionResult, BrowserActionType
from execution.browser.browser_tool import BrowserTool
from execution.browser.playwright_executor import PlaywrightBrowserExecutor
from execution.browser.browser_task_adapter import BrowserTaskAdapter
from execution.synthesis.action_synthesizer import ActionSynthesizer
from orchestration.dag_builder import ARDOGraph, ARDOTaskNode, DAGBuilder
from orchestration.runtime_engine import RuntimeEngine
from orchestration.state_machine import TaskState
from orchestration.safety_policy import SafetyPolicyEngine
from orchestration.safety_schemas import ActionType, ExecutionDecision, RiskLevel, SafetyContext, TargetCategory


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def local_html_fixture_url() -> str:
    fixture_path = Path(__file__).parent / "fixtures" / "browser_test_app.html"
    assert fixture_path.exists(), f"Fixture not found at {fixture_path}"
    return fixture_path.as_uri()


@pytest.fixture
def dashboard_fixture_url() -> str:
    fixture_path = Path(__file__).parent / "fixtures" / "internal_dashboard.html"
    assert fixture_path.exists(), f"Fixture not found at {fixture_path}"
    return fixture_path.as_uri()


@pytest.fixture(autouse=True)
def clean_tool_registry():
    """Ensure a fresh ToolRegistry for each test."""
    reg = ToolRegistry.reset_instance()
    reg.register(BrowserTool())
    yield reg


# Dummy Custom Tool for generic tests
class DummyInput(BaseModel):
    query: str = Field(..., description="Query string")
    limit: int = Field(default=10, description="Max results")

class DummyOutput(BaseModel):
    count: int

class DummySearchTool(BaseTool):
    name = "dummy_search"
    description = "Mock search tool for testing"
    capability = ToolCapability.DATA_SEARCH
    input_schema = DummyInput
    output_schema = DummyOutput

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        return ToolResult(
            call_id=tool_call.call_id,
            tool_name=self.name,
            action_name=tool_call.action_name,
            success=True,
            output_data={"count": 42},
            duration_ms=5.0,
        )

    def infer_safety_context(self, task_id: str, tool_call: ToolCall) -> SafetyContext:
        return SafetyContext.low_risk(task_id=task_id)


# ─────────────────────────────────────────────────────────────────────────────
# 14.1 - 14.6 Tool Registry Unit Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_14_1_tool_registration():
    registry = ToolRegistry.get_instance()
    dummy = DummySearchTool()
    registry.register(dummy)
    assert registry.get_tool("dummy_search") is dummy


def test_14_2_tool_retrieval():
    registry = ToolRegistry.get_instance()
    browser = registry.get_tool("browser")
    assert browser is not None
    assert browser.name == "browser"
    assert browser.capability == ToolCapability.BROWSER_AUTOMATION


def test_14_3_capability_lookup():
    registry = ToolRegistry.get_instance()
    registry.register(DummySearchTool())
    search_tools = registry.find_by_capability(ToolCapability.DATA_SEARCH)
    assert len(search_tools) == 1
    assert search_tools[0].name == "dummy_search"

    browser_tools = registry.find_by_capability(ToolCapability.BROWSER_AUTOMATION)
    assert len(browser_tools) == 1
    assert browser_tools[0].name == "browser"


@pytest.mark.asyncio
async def test_14_4_missing_tool_handling():
    registry = ToolRegistry.get_instance()
    call = ToolCall(
        call_id="c_missing_1",
        tool_name="non_existent_tool",
        action_name="do_something",
        arguments={},
    )
    result = await registry.dispatch(call)
    assert result.success is False
    assert "TOOL_NOT_FOUND" in (result.error or "")


@pytest.mark.asyncio
async def test_14_5_schema_validation_rejection():
    registry = ToolRegistry.get_instance()
    registry.register(DummySearchTool())
    # DummyInput requires query: str. Pass invalid arguments.
    call = ToolCall(
        call_id="c_invalid_1",
        tool_name="dummy_search",
        action_name="search",
        arguments={},  # missing 'query'
    )
    result = await registry.dispatch(call)
    assert result.success is False
    assert "VALIDATION_ERROR" in (result.error or "")


@pytest.mark.asyncio
async def test_14_6_tool_dispatch():
    registry = ToolRegistry.get_instance()
    registry.register(DummySearchTool())
    call = ToolCall(
        call_id="c_valid_1",
        tool_name="dummy_search",
        action_name="search",
        arguments={"query": "test query", "limit": 5},
    )
    result = await registry.dispatch(call)
    assert result.success is True
    assert result.tool_name == "dummy_search"
    assert result.output_data == {"count": 42}
    assert result.duration_ms >= 0


# ─────────────────────────────────────────────────────────────────────────────
# 14.7 BrowserTool Conformance
# ─────────────────────────────────────────────────────────────────────────────

def test_14_7_browser_tool_conformance():
    b_tool = BrowserTool()
    assert b_tool.name == "browser"
    assert b_tool.capability == ToolCapability.BROWSER_AUTOMATION
    assert b_tool.input_schema == BrowserAction
    assert b_tool.output_schema == BrowserActionResult

    # Check safe infer_safety_context
    call = ToolCall(
        call_id="c_b_1",
        tool_name="browser",
        action_name="navigate",
        arguments={"url": "https://example.com"},
    )
    s_ctx = b_tool.infer_safety_context("task_1", call)
    assert s_ctx.action in (ActionType.NAVIGATE, ActionType.READ)
    assert s_ctx.target_category == TargetCategory.NETWORK_ENDPOINT


# ─────────────────────────────────────────────────────────────────────────────
# 14.8 - 14.12 Action Synthesis Unit Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_14_8_navigate_synthesis():
    synthesizer = ActionSynthesizer()
    call = synthesizer.synthesize_for_task(
        task_id="t1",
        task_label="Navigate to http://localhost:8080/dashboard",
        goal_text="Check dashboard",
    )
    assert call is not None
    assert call.tool_name == "browser"
    assert call.action_name == "navigate"
    assert call.arguments.get("url") == "http://localhost:8080/dashboard"


def test_14_9_click_synthesis():
    synthesizer = ActionSynthesizer()
    call = synthesizer.synthesize_for_task(
        task_id="t2",
        task_label="Click #project-search-btn to start query",
    )
    assert call is not None
    assert call.tool_name == "browser"
    assert call.action_name == "click"
    assert call.arguments.get("selector") == "#project-search-btn"


def test_14_10_fill_synthesis():
    synthesizer = ActionSynthesizer()
    call = synthesizer.synthesize_for_task(
        task_id="t3",
        task_label="Fill #project-search-input with 'Phoenix Engine'",
    )
    assert call is not None
    assert call.tool_name == "browser"
    assert call.action_name == "fill"
    assert call.arguments.get("selector") == "#project-search-input"
    assert call.arguments.get("value") == "Phoenix Engine"


def test_14_11_extract_text_synthesis():
    synthesizer = ActionSynthesizer()
    call = synthesizer.synthesize_for_task(
        task_id="t4",
        task_label="Extract text from #project-name-cell",
    )
    assert call is not None
    assert call.tool_name == "browser"
    assert call.action_name == "extract_text"
    assert call.arguments.get("selector") == "#project-name-cell"


def test_14_12_screenshot_synthesis():
    synthesizer = ActionSynthesizer()
    call = synthesizer.synthesize_for_task(
        task_id="t5",
        task_label="Take screenshot of the resulting dashboard page",
    )
    assert call is not None
    assert call.tool_name == "browser"
    assert call.action_name == "screenshot"


# ─────────────────────────────────────────────────────────────────────────────
# 14.13 - 14.14 Safety Verification on Synthesized Parameters
# ─────────────────────────────────────────────────────────────────────────────

def test_14_13_safety_evaluation_allows_harmless_synthesized_action():
    nodes = [
        {"id": "n_safe", "data": {"label": "Click #search-btn to view results"}},
    ]
    graph = DAGBuilder.build(nodes, [], execution_mode="safe")
    node = graph.get_node("n_safe")
    assert node is not None
    assert node.tool_call is not None
    assert node.status != TaskState.BLOCKED
    assert node.risk_score < 0.7


def test_14_14_credential_target_blocking_on_harmless_label():
    """
    CRITICAL INVARIANT TEST:
    A harmless label like 'Verify login configuration' that targets a password/credential field
    MUST BE BLOCKED by policy at build time!
    """
    nodes = [
        {"id": "n_cred", "data": {"label": "Fill #admin-password with 'secret123'"}},
    ]
    graph = DAGBuilder.build(nodes, [], execution_mode="safe")
    node = graph.get_node("n_cred")
    assert node is not None
    assert node.tool_call is not None
    assert node.tool_call.arguments.get("selector") == "#admin-password"

    # Must be BLOCKED at build time because target is CREDENTIAL
    assert node.status == TaskState.BLOCKED
    assert any("BLOCKED" in log for log in node.execution_logs)


# ─────────────────────────────────────────────────────────────────────────────
# 14.15 Generic Task Simulation Fallback
# ─────────────────────────────────────────────────────────────────────────────

def test_14_15_generic_task_simulation_fallback():
    """
    Conceptual or non-actionable tasks return None from synthesizer
    and retain the verified simulation execution path.
    """
    synthesizer = ActionSynthesizer()
    call = synthesizer.synthesize_for_task(
        task_id="t_gen",
        task_label="Formulate architectural hypothesis for distributed ledger",
    )
    assert call is None

    # When built in DAG, tool_call is None and browser_action is None
    nodes = [{"id": "n_gen", "data": {"label": "Formulate architectural hypothesis"}}]
    graph = DAGBuilder.build(nodes, [])
    node = graph.get_node("n_gen")
    assert node.tool_call is None
    assert node.browser_action is None


# ─────────────────────────────────────────────────────────────────────────────
# 14.16 Legacy Manual Browser Action Backward Compatibility
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_14_16_legacy_manual_browser_action_compatibility(local_html_fixture_url):
    """
    Ensures that tasks created with manual `node.browser_action = BrowserAction(...)`
    (as in Phase 10 & 11 tests) execute seamlessly without regressions.
    """
    action = BrowserAction(
        action_type=BrowserActionType.NAVIGATE,
        url=local_html_fixture_url,
        description="Legacy manual navigate",
    )
    node = ARDOTaskNode(
        task_id="legacy_1",
        task_name="Manual Navigation",
        browser_action=action,
    )
    graph = ARDOGraph(nodes={"legacy_1": node})

    async with PlaywrightBrowserExecutor(headless=True) as executor:
        adapter = BrowserTaskAdapter(executor=executor)
        engine = RuntimeEngine(
            graph=graph,
            session_id="sess_legacy_compat",
            mode="safe",
            browser_adapter=adapter,
        )

        events = []
        async for ev in engine.run():
            events.append(ev)

        assert node.status == TaskState.COMPLETED
        assert node.browser_result is not None
        assert node.browser_result.success is True


# ─────────────────────────────────────────────────────────────────────────────
# 14.17 End-to-End Synthesized Browser Execution
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_14_17_end_to_end_synthesized_browser_execution(dashboard_fixture_url):
    """
    FULL END-TO-END VERIFICATION:
    1. Plan with high-level strings:
       - 'Navigate to <dashboard_url>'
       - 'Fill #project-search-input with "Phoenix"'
       - 'Click #project-search-btn'
       - 'Extract text from #project-name-cell'
       - 'Take screenshot of dashboard'
    2. DAGBuilder synthesizes ToolCalls for all 5 tasks.
    3. ARDORuntimeEngine dispatches each ToolCall through ToolRegistry -> BrowserTool -> Playwright.
    4. Proves real Chromium automation executes without manual action binding!
    """
    nodes_data = [
        {"id": "t1", "data": {"label": f"Navigate to {dashboard_fixture_url}"}},
        {"id": "t2", "data": {"label": "Fill #project-search-input with 'Phoenix'"}},
        {"id": "t3", "data": {"label": "Click #project-search-btn"}},
        {"id": "t4", "data": {"label": "Extract text from #project-name-cell"}},
        {"id": "t5", "data": {"label": "Take screenshot of dashboard"}},
    ]
    edges_data = [
        {"source": "t1", "target": "t2"},
        {"source": "t2", "target": "t3"},
        {"source": "t3", "target": "t4"},
        {"source": "t4", "target": "t5"},
    ]

    # Build DAG with automatic action synthesis
    graph = DAGBuilder.build(nodes_data, edges_data, execution_mode="sequential")

    # Verify all nodes have synthesized tool_calls
    for nid in ["t1", "t2", "t3", "t4", "t5"]:
        n = graph.get_node(nid)
        assert n.tool_call is not None, f"Node {nid} failed tool synthesis!"
        assert n.tool_call.tool_name == "browser"

    # Launch real Playwright executor wrapped in BrowserTool
    async with PlaywrightBrowserExecutor(headless=True) as executor:
        b_tool = BrowserTool(executor=executor)
        registry = ToolRegistry.get_instance()
        registry.register(b_tool)

        engine = RuntimeEngine(
            graph=graph,
            session_id="sess_p14_e2e_001",
            mode="sequential",
            browser_adapter=b_tool.adapter,
        )

        events = []
        async for ev in engine.run():
            events.append(ev)

        # Assert all tasks completed
        for nid in ["t1", "t2", "t3", "t4", "t5"]:
            node = graph.get_node(nid)
            assert node.status == TaskState.COMPLETED, f"Task {nid} did not complete! Logs: {node.execution_logs}"
            assert node.tool_result is not None
            assert node.tool_result.success is True

        # Assert text extraction succeeded
        t4_node = graph.get_node("t4")
        assert "AI Goal-to-Task Execution Engine" in (t4_node.tool_result.extracted_text or "")

        # Assert screenshot artifact captured
        t5_node = graph.get_node("t5")
        assert "screenshot" in t5_node.tool_result.artifacts


# ─────────────────────────────────────────────────────────────────────────────
# 14.18 Diagnostic GET /tools Endpoint
# ─────────────────────────────────────────────────────────────────────────────

def test_14_18_tools_endpoint():
    from fastapi.testclient import TestClient
    from main import app

    client = TestClient(app)
    res = client.get("/tools")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["total_tools"] >= 1
    tool_names = [t["name"] for t in data["tools"]]
    assert "browser" in tool_names
