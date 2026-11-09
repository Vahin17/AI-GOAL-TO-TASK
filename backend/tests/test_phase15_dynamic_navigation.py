"""
test_phase15_dynamic_navigation.py — Phase 15 Dynamic Navigation & Real Playwright Tests
========================================================================================
Validates:
1. Dynamic destination resolution decoupled from current DOM.
2. New tab semantics (tabs_before, tabs_after, new_tab_created).
3. Screenshot semantics.
4. Preserved DOM interaction for in-page actions (e.g. click help button).
"""

import pytest
import asyncio
from unittest.mock import patch

from execution.synthesis.navigation_resolver import NavigationResolver, NavigationIntent
from execution.synthesis.action_synthesizer import ActionSynthesizer
from execution.browser.environment_perception import PageSnapshot, PageElement
from execution.browser.browser_models import BrowserAction, BrowserActionType
from execution.browser.playwright_executor import PlaywrightBrowserExecutor
from execution.registry.tool_registry import ToolRegistry
from execution.browser.browser_tool import BrowserTool


@pytest.mark.asyncio
async def test_scenario_a_open_wikipedia_in_new_tab():
    """A. open wikipedia in the new tab: NAVIGATE intent, dynamic URL, new_tab=True, real Playwright execution."""
    synth = ActionSynthesizer()
    call = synth.synthesize_for_task("task_wiki", "open wikipedia in the new tab")

    assert call is not None
    assert call.tool_name == "browser"
    assert call.action_name == "navigate"
    assert "wikipedia.org" in call.arguments.get("url")

    # Real Playwright Execution
    executor = PlaywrightBrowserExecutor(headless=True)
    await executor.start()
    try:
        action = BrowserAction(
            action_type=BrowserActionType.NAVIGATE,
            url=call.arguments["url"],
            description=call.description,
        )
        res = await executor.execute_action(action)
        assert res.success is True
        assert "wikipedia" in res.current_url.lower()
        # Verify new tab metadata
        assert res.metadata.get("new_tab_requested") is True
        assert res.metadata.get("new_tab_created") is True
        assert res.metadata.get("tabs_after") >= 2
    finally:
        await executor.stop()


@pytest.mark.asyncio
async def test_scenario_b_open_gmail_in_new_tab():
    """B. open gmail in a new tab: dynamic navigation, new_tab=True, real browser execution."""
    synth = ActionSynthesizer()
    call = synth.synthesize_for_task("task_gmail", "open gmail in a new tab")

    assert call is not None
    assert call.tool_name == "browser"
    assert call.action_name == "navigate"
    assert any(d in call.arguments.get("url") for d in ("gmail.com", "mail.google.com"))

    executor = PlaywrightBrowserExecutor(headless=True)
    await executor.start()
    try:
        action = BrowserAction(
            action_type=BrowserActionType.NAVIGATE,
            url=call.arguments["url"],
            description=call.description,
        )
        res = await executor.execute_action(action)
        assert res.success is True
        assert "google.com" in res.current_url.lower()
        assert res.metadata.get("new_tab_created") is True
    finally:
        await executor.stop()


@pytest.mark.asyncio
async def test_scenario_c_explicit_url_in_new_tab():
    """C. open https://example.com in a new tab: explicit URL preserved, new_tab=True."""
    synth = ActionSynthesizer()
    call = synth.synthesize_for_task("task_ex", "open https://example.com in a new tab")

    assert call is not None
    assert call.action_name == "navigate"
    assert call.arguments.get("url") == "https://example.com"

    executor = PlaywrightBrowserExecutor(headless=True)
    await executor.start()
    try:
        action = BrowserAction(
            action_type=BrowserActionType.NAVIGATE,
            url=call.arguments["url"],
            description=call.description,
        )
        res = await executor.execute_action(action)
        assert res.success is True
        assert "example.com" in res.current_url.lower()
        assert res.metadata.get("new_tab_created") is True
    finally:
        await executor.stop()


@pytest.mark.asyncio
async def test_scenario_d_open_wikipedia_and_screenshot():
    """D. open wikipedia and take a screenshot: navigate then screenshot, real Playwright, both verified."""
    synth = ActionSynthesizer()
    nav_call = synth.synthesize_for_task("t_nav", "open wikipedia")
    ss_call = synth.synthesize_for_task("t_ss", "take a screenshot")

    assert nav_call.action_name == "navigate"
    assert ss_call.action_name == "screenshot"

    executor = PlaywrightBrowserExecutor(headless=True)
    await executor.start()
    try:
        # 1. Navigate
        act_nav = BrowserAction(action_type=BrowserActionType.NAVIGATE, url=nav_call.arguments["url"], description=nav_call.description)
        res_nav = await executor.execute_action(act_nav)
        assert res_nav.success is True
        assert "wikipedia" in res_nav.current_url.lower()

        # 2. Screenshot
        act_ss = BrowserAction(action_type=BrowserActionType.SCREENSHOT, description=ss_call.description)
        res_ss = await executor.execute_action(act_ss)
        assert res_ss.success is True
        assert res_ss.metadata.get("screenshot_size_bytes", 0) > 0
        assert len(res_ss.metadata.get("screenshot_base64", "")) > 100
    finally:
        await executor.stop()


@pytest.mark.asyncio
async def test_scenario_e_click_help_button_uses_dom_grounding():
    """E. click the help button: uses DOM/perception grounding, does NOT use navigation fast path."""
    nav = NavigationResolver.detect_navigation_intent("click the help button")
    assert nav is None  # Must NOT be detected as navigation!

    synth = ActionSynthesizer()
    help_el = PageElement(
        element_id="el_help",
        tag="button",
        role="button",
        text_content="Help",
        css_selector="#help-button",
        is_interactive=True,
    )
    snap = PageSnapshot(url="https://app.example.com", title="App", elements=[help_el])
    call = synth.synthesize_from_snapshot("t_click", "click the help button", snap)

    assert call is not None
    assert call.tool_name == "browser"
    assert call.action_name == "click"
    assert call.arguments.get("selector") == "#help-button"


@pytest.mark.asyncio
async def test_scenario_f_multistep_plan_generation():
    """F. Complex multi-step instruction planning."""
    from ai.fallback_rules import decompose_goal_heuristically
    goal = "open the website, search for Phoenix, click submit, and take a screenshot"
    subtasks = decompose_goal_heuristically(goal)

    assert len(subtasks) >= 3
    assert any("open" in s.lower() for s in subtasks)
    assert any("search" in s.lower() for s in subtasks)
    assert any("screenshot" in s.lower() for s in subtasks)
