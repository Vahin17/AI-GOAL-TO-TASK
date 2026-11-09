"""
test_phase4_execution_semantics.py — Unit tests for Phase 4 Execution Semantics
================================================================================
Tests that:
- 1 Agent Action = 1 Explicit Browser Operation
- No hidden side-effect clicks on FILL (no search button auto-click, no save button auto-click)
- CLICK, NAVIGATE, PRESS, SCREENSHOT, SCROLL each execute strictly one operation
- new_tab semantics are preserved
- Structured BrowserActionResult is returned with action-specific metrics
- Existing Phase 1, 2, 3 contracts remain intact
"""

import asyncio
import base64
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from execution.browser.browser_models import (
    BrowserAction,
    BrowserActionResult,
    BrowserActionType,
)
from execution.browser.playwright_executor import _BrowserSession, PlaywrightBrowserExecutor
from execution.browser.browser_task_adapter import BrowserTaskAdapter
from execution.browser.browser_agent import BrowserAgent
from execution.browser.agent_contracts import (
    BrowserTask,
    BrowserObservation,
    BrowserDecision,
    BrowserDecisionType,
    BrowserActionDecision,
    BrowserAgentOutcome,
)
from execution.browser.perception_models import ObservedElement, TabInfo


# ─────────────────────────────────────────────────────────────────────────────
# Test Fixtures & Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_mock_page(url="https://example.com", title="Example Domain"):
    page = AsyncMock()
    page.url = url
    page.title = AsyncMock(return_value=title)
    page.wait_for_selector = AsyncMock(return_value=None)
    page.click = AsyncMock(return_value=None)
    page.fill = AsyncMock(return_value=None)
    page.press = AsyncMock(return_value=None)
    page.screenshot = AsyncMock(return_value=b"fake_png_bytes")
    page.query_selector = AsyncMock(return_value=None)
    page.goto = AsyncMock(return_value=None)

    mouse = AsyncMock()
    mouse.move = AsyncMock(return_value=None)
    mouse.wheel = AsyncMock(return_value=None)
    page.mouse = mouse

    keyboard = AsyncMock()
    keyboard.press = AsyncMock(return_value=None)
    page.keyboard = keyboard

    page.wait_for_timeout = AsyncMock(return_value=None)
    return page


def make_mock_session(page=None, context=None):
    session = _BrowserSession(headless=True, default_timeout_ms=5000)
    session._is_started = True
    session._page = page or make_mock_page()

    if context is None:
        ctx = AsyncMock()
        ctx.pages = [session._page]
        ctx.new_page = AsyncMock(return_value=session._page)
        session._context = ctx
    else:
        session._context = context

    return session


# ─────────────────────────────────────────────────────────────────────────────
# 1. FILL Semantics & Removal of Hidden Side Effects
# ─────────────────────────────────────────────────────────────────────────────

class TestFillExecutionSemantics:
    def test_fill_performs_only_fill(self):
        page = make_mock_page()
        session = make_mock_session(page=page)

        action = BrowserAction(
            action_type=BrowserActionType.FILL,
            selector="input#username",
            value="alice",
        )
        res = asyncio.run(session.execute_action(action))

        assert res.success is True
        assert res.action_type == BrowserActionType.FILL
        page.fill.assert_awaited_once_with("input#username", "alice", timeout=5000)
        # click should NOT have been called on page
        page.click.assert_not_called()

    def test_fill_on_search_selector_does_not_click_search_button(self):
        """CRITICAL: FILL on a search field must NOT click any search button."""
        page = make_mock_page()
        session = make_mock_session(page=page)

        action = BrowserAction(
            action_type=BrowserActionType.FILL,
            selector="input#search-input",
            value="Playwright",
        )
        res = asyncio.run(session.execute_action(action))

        assert res.success is True
        page.fill.assert_awaited_once_with("input#search-input", "Playwright", timeout=5000)
        # query_selector and click must NOT be invoked to look for or click a search button
        page.query_selector.assert_not_called()
        page.click.assert_not_called()

    def test_fill_on_desc_note_metadata_does_not_click_save_button(self):
        """CRITICAL: FILL on description/notes/metadata must NOT click save."""
        page = make_mock_page()
        session = make_mock_session(page=page)

        action = BrowserAction(
            action_type=BrowserActionType.FILL,
            selector="textarea#project-desc",
            value="hello world",
        )
        res = asyncio.run(session.execute_action(action))

        assert res.success is True
        page.fill.assert_awaited_once_with("textarea#project-desc", "hello world", timeout=5000)
        page.query_selector.assert_not_called()
        page.click.assert_not_called()

    def test_fill_missing_selector_returns_failure(self):
        page = make_mock_page()
        session = make_mock_session(page=page)

        action = BrowserAction(
            action_type=BrowserActionType.FILL,
            selector=None,
            value="abc",
        )
        res = asyncio.run(session.execute_action(action))

        assert res.success is False
        assert "Selector parameter required" in (res.error or "")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Individual Browser Action Semantics
# ─────────────────────────────────────────────────────────────────────────────

class TestIndividualActionSemantics:
    def test_click_performs_one_click(self):
        page = make_mock_page()
        session = make_mock_session(page=page)

        action = BrowserAction(
            action_type=BrowserActionType.CLICK,
            selector="button#submit",
        )
        res = asyncio.run(session.execute_action(action))

        assert res.success is True
        assert res.action_type == BrowserActionType.CLICK
        page.click.assert_awaited_once_with("button#submit", timeout=5000)

    def test_navigate_performs_one_goto(self):
        page = make_mock_page(url="https://example.com/target", title="Target Page")
        session = make_mock_session(page=page)

        action = BrowserAction(
            action_type=BrowserActionType.NAVIGATE,
            url="https://example.com/target",
        )
        res = asyncio.run(session.execute_action(action))

        assert res.success is True
        assert res.action_type == BrowserActionType.NAVIGATE
        page.goto.assert_awaited_once_with(
            "https://example.com/target", timeout=10000, wait_until="domcontentloaded"
        )
        assert res.current_url == "https://example.com/target"

    def test_press_performs_one_key_press(self):
        page = make_mock_page()
        session = make_mock_session(page=page)

        action = BrowserAction(
            action_type=BrowserActionType.PRESS,
            key="Enter",
        )
        res = asyncio.run(session.execute_action(action))

        assert res.success is True
        page.keyboard.press.assert_awaited_once_with("Enter")

    def test_press_on_selector_performs_press(self):
        page = make_mock_page()
        session = make_mock_session(page=page)

        action = BrowserAction(
            action_type=BrowserActionType.PRESS,
            selector="input#search",
            key="Enter",
        )
        res = asyncio.run(session.execute_action(action))

        assert res.success is True
        page.press.assert_awaited_once_with("input#search", "Enter", timeout=5000)

    def test_screenshot_produces_base64_artifact(self):
        page = make_mock_page()
        page.screenshot = AsyncMock(return_value=b"png_image_data")
        session = make_mock_session(page=page)

        action = BrowserAction(action_type=BrowserActionType.SCREENSHOT)
        res = asyncio.run(session.execute_action(action))

        assert res.success is True
        assert res.action_type == BrowserActionType.SCREENSHOT
        assert res.metadata.get("screenshot_size_bytes") == len(b"png_image_data")
        assert res.metadata.get("screenshot_base64") == base64.b64encode(b"png_image_data").decode("utf-8")

    def test_scroll_performs_mouse_wheel(self):
        page = make_mock_page()
        session = make_mock_session(page=page)

        action = BrowserAction(
            action_type=BrowserActionType.SCROLL,
            value="800",
        )
        res = asyncio.run(session.execute_action(action))

        assert res.success is True
        page.mouse.wheel.assert_awaited_once_with(0, 800)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Tab & New-Tab Behavior Preservation
# ─────────────────────────────────────────────────────────────────────────────

class TestNewTabSemantics:
    def test_navigate_with_new_tab_creates_new_page(self):
        initial_page = make_mock_page(url="https://example.com/first")
        new_page = make_mock_page(url="https://example.com/second", title="Second Page")

        context = AsyncMock()
        context.pages = [initial_page]

        async def mock_new_page():
            context.pages.append(new_page)
            return new_page

        context.new_page = mock_new_page

        session = make_mock_session(page=initial_page, context=context)

        action = BrowserAction(
            action_type=BrowserActionType.NAVIGATE,
            url="https://example.com/second",
            new_tab=True,
        )
        res = asyncio.run(session.execute_action(action))

        assert res.success is True
        assert res.metadata.get("new_tab_created") is True
        assert res.metadata.get("tabs_before") == 1
        assert res.metadata.get("tabs_after") == 2
        new_page.goto.assert_awaited_once_with(
            "https://example.com/second", timeout=10000, wait_until="domcontentloaded"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Error & Timeout Handling
# ─────────────────────────────────────────────────────────────────────────────

class TestActionFailureHandling:
    def test_playwright_exception_returns_clean_failure_result(self):
        page = make_mock_page()
        page.click = AsyncMock(side_effect=RuntimeError("Element not interactable"))
        session = make_mock_session(page=page)

        action = BrowserAction(
            action_type=BrowserActionType.CLICK,
            selector="button#broken",
        )
        res = asyncio.run(session.execute_action(action))

        assert res.success is False
        assert "Element not interactable" in (res.error or "")
        assert res.action_type == BrowserActionType.CLICK

    def test_custom_action_timeout_respected(self):
        page = make_mock_page()
        session = make_mock_session(page=page)

        action = BrowserAction(
            action_type=BrowserActionType.CLICK,
            selector="button#fast",
            timeout_ms=1234,
        )
        asyncio.run(session.execute_action(action))

        page.click.assert_awaited_once_with("button#fast", timeout=1234)


# ─────────────────────────────────────────────────────────────────────────────
# 5. End-to-End BrowserAgent Interaction with Clean Execution Semantics
# ─────────────────────────────────────────────────────────────────────────────

class TestBrowserAgentFillDecisionsAreIsolated:
    def test_agent_fill_search_only_executes_fill_action(self):
        """
        Verify the complete chain:
        Agent decides: FILL search box with 'Playwright'
        Executor runs: only FILL
        No second CLICK action is automatically generated or executed.
        """
        fill_resp = json.dumps({
            "decision": "execute",
            "reason": "Enter search query into search box",
            "action": {
                "action_type": "fill",
                "element_ref": "elem_001",
                "value": "Playwright",
                "url": None,
                "key": None,
                "new_tab": False,
            }
        })
        finish_resp = json.dumps({
            "decision": "finish",
            "reason": "Search query typed successfully",
            "action": None,
        })

        responses = [fill_resp, finish_resp]
        resp_iter = iter(responses)

        llm = MagicMock()
        async def mock_query(prompt, system_instruction="", json_mode=False):
            return next(resp_iter)
        llm.query = mock_query

        # Mock adapter and track all dispatched actions
        dispatched_actions = []
        adapter = MagicMock()
        adapter.executor = MagicMock()
        adapter.executor._session = MagicMock()

        async def mock_execute(task_id, session_id, action):
            dispatched_actions.append(action)
            return BrowserActionResult(
                success=True,
                action_type=action.action_type,
                current_url="https://example.com/search",
            ), MagicMock(is_blocked=False)

        adapter.execute_safe_browser_task = mock_execute

        # Mock perception
        perception = MagicMock()
        obs = BrowserObservation(
            session_id="s1",
            task_id="t1",
            current_url="https://example.com/search",
            page_title="Search Engine",
            observed_elements=[
                ObservedElement(ref="elem_001", tag="input", css_selector="input[name='q']"),
                ObservedElement(ref="elem_002", tag="button", css_selector="button#search-btn"),
            ],
            tabs=[TabInfo(tab_ref="tab_001", url="https://example.com/search", is_active=True, index=0)],
        )

        async def mock_observe(*args, **kwargs):
            return obs
        perception.observe = mock_observe

        agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
        task = BrowserTask(
            task_id="t1",
            objective="Fill search field with Playwright",
            session_id="s1",
        )

        result = asyncio.run(agent.run(task))

        assert result.outcome == BrowserAgentOutcome.COMPLETED
        # Exactly 1 action executed (FILL)
        assert len(dispatched_actions) == 1
        assert dispatched_actions[0].action_type == BrowserActionType.FILL
        assert dispatched_actions[0].selector == "input[name='q']"
        assert dispatched_actions[0].value == "Playwright"

    def test_agent_fill_description_only_executes_fill_action(self):
        """
        Verify that FILL description executes only FILL and not a subsequent save button click.
        """
        fill_resp = json.dumps({
            "decision": "execute",
            "reason": "Enter description text",
            "action": {
                "action_type": "fill",
                "element_ref": "elem_001",
                "value": "hello",
                "url": None,
                "key": None,
                "new_tab": False,
            }
        })
        finish_resp = json.dumps({
            "decision": "finish",
            "reason": "Description filled",
            "action": None,
        })

        responses = [fill_resp, finish_resp]
        resp_iter = iter(responses)

        llm = MagicMock()
        async def mock_query(prompt, system_instruction="", json_mode=False):
            return next(resp_iter)
        llm.query = mock_query

        dispatched_actions = []
        adapter = MagicMock()
        adapter.executor = MagicMock()
        adapter.executor._session = MagicMock()

        async def mock_execute(task_id, session_id, action):
            dispatched_actions.append(action)
            return BrowserActionResult(
                success=True,
                action_type=action.action_type,
                current_url="https://example.com/edit",
            ), MagicMock(is_blocked=False)

        adapter.execute_safe_browser_task = mock_execute

        perception = MagicMock()
        obs = BrowserObservation(
            session_id="s1",
            task_id="t1",
            current_url="https://example.com/edit",
            page_title="Edit Project",
            observed_elements=[
                ObservedElement(ref="elem_001", tag="textarea", css_selector="textarea#project-desc"),
                ObservedElement(ref="elem_002", tag="button", css_selector="button#save-btn"),
            ],
            tabs=[TabInfo(tab_ref="tab_001", url="https://example.com/edit", is_active=True, index=0)],
        )

        async def mock_observe(*args, **kwargs):
            return obs
        perception.observe = mock_observe

        agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
        task = BrowserTask(
            task_id="t1",
            objective="Fill description with hello",
            session_id="s1",
        )

        result = asyncio.run(agent.run(task))

        assert result.outcome == BrowserAgentOutcome.COMPLETED
        assert len(dispatched_actions) == 1
        assert dispatched_actions[0].action_type == BrowserActionType.FILL
        assert dispatched_actions[0].selector == "textarea#project-desc"
        assert dispatched_actions[0].value == "hello"
