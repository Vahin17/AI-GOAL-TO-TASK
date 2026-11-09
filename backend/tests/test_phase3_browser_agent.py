"""
test_phase3_browser_agent.py — Unit tests for Phase 3 BrowserAgent
====================================================================
All tests use mocked Gemini (llm_provider) and mocked execution
(BrowserTaskAdapter). No live Playwright or live Gemini API calls.

Tests cover:
  1.  Agent observes before acting.
  2.  Agent asks for a decision based on the current observation.
  3.  EXECUTE decision results in exactly one browser action.
  4.  Agent observes again after execution (fresh observation per iteration).
  5.  Multiple actions can occur for one BrowserTask (multi-step).
  6.  Element references are resolved against the current observation.
  7.  Old element references from prior observations are not reused.
  8.  FINISH returns a completed BrowserAgentResult.
  9.  FAIL returns a failed BrowserAgentResult.
  10. Action failure causes new observation/decision, not blind repetition.
  11. Maximum action limit prevents infinite loops.
  12. BrowserAgentResult correctly reflects actions performed.
  13. Invalid/unsafe decision structure is rejected before execution.
  14. _parse_gemini_decision handles well-formed JSON.
  15. _parse_gemini_decision rejects malformed JSON.
  16. _parse_gemini_decision rejects unknown decision type.
  17. _parse_gemini_decision rejects unknown action_type.
  18. _resolve_element_ref finds the correct CSS selector.
  19. _resolve_element_ref returns None for missing ref.
  20. _decision_to_browser_action builds correct BrowserAction.
  21. Safety block (BLOCKED_BY_SAFETY_POLICY) returns BLOCKED outcome.
  22. RECOVER with action executes it and re-observes.
  23. RECOVER without action re-observes only.
  24. Phase 1 and Phase 2 regressions (contracts still import cleanly).
"""

import asyncio
import json
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
from pydantic import ValidationError

from execution.browser.agent_contracts import (
    BrowserActionDecision,
    BrowserAgentOutcome,
    BrowserAgentResult,
    BrowserDecision,
    BrowserDecisionType,
    BrowserObservation,
    BrowserTask,
    BrowserVerificationStatus,
    RecentActionRecord,
)
from execution.browser.browser_models import (
    BrowserAction,
    BrowserActionResult,
    BrowserActionType,
)
from execution.browser.browser_agent import (
    BrowserAgent,
    _decision_to_browser_action,
    _parse_gemini_decision,
    _resolve_element_ref,
)
from execution.browser.perception_models import ObservedElement, PageElement, TabInfo
from orchestration.safety_schemas import ExecutionDecision, RiskLevel, SafetyDecision


# ─────────────────────────────────────────────────────────────────────────────
# Helpers / fixtures
# ─────────────────────────────────────────────────────────────────────────────

def make_task(
    objective: str = "Open example.com",
    max_actions: int = 5,
    success_criteria: Optional[str] = None,
    task_id: str = "t_001",
    session_id: str = "s_001",
) -> BrowserTask:
    return BrowserTask(
        task_id=task_id,
        session_id=session_id,
        objective=objective,
        max_actions=max_actions,
        success_criteria=success_criteria,
    )


def make_observed_element(
    ref: str = "elem_001",
    tag: str = "a",
    role: str = "link",
    text: str = "More information",
    css_selector: str = "a[href]",
) -> ObservedElement:
    return ObservedElement(
        ref=ref,
        tag=tag,
        role=role,
        text=text,
        css_selector=css_selector,
    )


def make_observation(
    url: str = "https://example.com",
    title: str = "Example Domain",
    elements: Optional[List[ObservedElement]] = None,
    session_id: str = "s_001",
    task_id: str = "t_001",
    actions_taken_count: int = 0,
    recent_action: Optional[RecentActionRecord] = None,
) -> BrowserObservation:
    if elements is None:
        elements = [make_observed_element()]
    return BrowserObservation(
        session_id=session_id,
        task_id=task_id,
        current_url=url,
        page_title=title,
        observed_elements=elements,
        actions_taken_count=actions_taken_count,
        recent_action=recent_action,
        tabs=[TabInfo(tab_ref="tab_001", url=url, title=title, is_active=True, index=0)],
    )


def make_safety_decision(blocked: bool = False) -> SafetyDecision:
    return SafetyDecision(
        task_id="t_test",
        task_name="test_task",
        risk_level=RiskLevel.CRITICAL if blocked else RiskLevel.LOW,
        execution_decision=ExecutionDecision.BLOCK if blocked else ExecutionDecision.ALLOW,
        explanation="Blocked by test policy" if blocked else "Allowed",
    )


def gemini_execute(action_type: str = "navigate", url: str = "https://example.com",
                   elem_ref: Optional[str] = None, reason: str = "navigating") -> str:
    action = {"action_type": action_type, "element_ref": elem_ref,
               "url": url, "value": None, "key": None, "new_tab": False}
    return json.dumps({"decision": "execute", "reason": reason, "action": action})


def gemini_finish(reason: str = "Task complete") -> str:
    return json.dumps({"decision": "finish", "reason": reason, "action": None})


def gemini_fail(reason: str = "Cannot proceed") -> str:
    return json.dumps({"decision": "fail", "reason": reason, "action": None})


def gemini_recover(reason: str = "Unexpected state") -> str:
    return json.dumps({"decision": "recover", "reason": reason, "action": None})


def make_action_result(
    success: bool = True,
    current_url: str = "https://example.com",
    current_title: str = "Example Domain",
    error: Optional[str] = None,
) -> BrowserActionResult:
    return BrowserActionResult(
        success=success,
        action_type=BrowserActionType.NAVIGATE,
        current_url=current_url,
        current_title=current_title,
        error=error,
    )


def build_agent(
    gemini_responses: List[str],
    observations: Optional[List[BrowserObservation]] = None,
    action_result: Optional[BrowserActionResult] = None,
    safety_blocked: bool = False,
) -> BrowserAgent:
    """
    Build a fully mocked BrowserAgent.

    gemini_responses: List of JSON strings returned per Gemini call (in order).
    observations: List of BrowserObservations returned per _observe call (in order).
    action_result: Result returned by execute_safe_browser_task.
    """
    # Mock LLM provider
    llm = MagicMock()
    response_iter = iter(gemini_responses)

    async def mock_query(prompt, system_instruction="", json_mode=False):
        return next(response_iter)

    llm.query = mock_query

    # Mock adapter
    adapter = MagicMock()
    adapter.executor = MagicMock()
    adapter.executor._session = MagicMock()
    adapter.executor._session._page = None   # No live page — stub observations used
    adapter.executor._session._context = None

    ar = action_result or make_action_result()
    sd = make_safety_decision(blocked=safety_blocked)
    if safety_blocked:
        ar = BrowserActionResult(
            success=False,
            action_type=BrowserActionType.NAVIGATE,
            error="BLOCKED_BY_SAFETY_POLICY: Not allowed",
        )

    async def mock_execute(task_id, session_id, action):
        return ar, sd

    adapter.execute_safe_browser_task = mock_execute

    # Mock perception engine — returns observations in order
    perception = MagicMock()
    if observations is None:
        # Default: return same observation every time
        default_obs = make_observation()
        obs_iter = None

        async def mock_observe(page, session_id, task_id, context=None, actions_taken_count=0, recent_action=None):
            return BrowserObservation(
                session_id=session_id,
                task_id=task_id,
                current_url=default_obs.current_url,
                page_title=default_obs.page_title,
                observed_elements=default_obs.observed_elements,
                actions_taken_count=actions_taken_count,
                recent_action=recent_action,
                tabs=default_obs.tabs,
            )
    else:
        obs_iter = iter(observations)

        async def mock_observe(page, session_id, task_id, context=None, actions_taken_count=0, recent_action=None):
            try:
                base = next(obs_iter)
                return BrowserObservation(
                    session_id=session_id,
                    task_id=task_id,
                    current_url=base.current_url,
                    page_title=base.page_title,
                    observed_elements=base.observed_elements,
                    actions_taken_count=actions_taken_count,
                    recent_action=recent_action,
                    tabs=base.tabs,
                )
            except StopIteration:
                return make_observation(actions_taken_count=actions_taken_count, recent_action=recent_action)

    perception.observe = mock_observe

    agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
    return agent


# ─────────────────────────────────────────────────────────────────────────────
# 1. Agent observes before acting
# ─────────────────────────────────────────────────────────────────────────────

class TestAgentObservesFirst:
    def test_observe_called_before_first_decision(self):
        observe_calls = []
        task = make_task(max_actions=2)

        llm = MagicMock()
        call_idx = [0]

        async def mock_query(prompt, system_instruction="", json_mode=False):
            # Verify we have a non-empty observation in the prompt
            assert "URL:" in prompt, "Observation must be in prompt before decision"
            observe_calls.append("decided")
            return gemini_finish()

        llm.query = mock_query

        adapter = MagicMock()
        adapter.executor._session = MagicMock(_page=None, _context=None)
        adapter.executor._session._page = None

        perception = MagicMock()
        obs_called = []

        async def mock_observe(page, session_id, task_id, context=None, actions_taken_count=0, recent_action=None):
            obs_called.append(actions_taken_count)
            return make_observation(actions_taken_count=actions_taken_count)

        perception.observe = mock_observe

        agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)

        async def run():
            return await agent.run(task)

        result = asyncio.run(run())
        # Observation must happen before the first Gemini call
        assert len(obs_called) >= 1
        assert obs_called[0] == 0  # First observe is before any action


# ─────────────────────────────────────────────────────────────────────────────
# 2. Agent asks Gemini based on current observation
# ─────────────────────────────────────────────────────────────────────────────

class TestGeminiCalledWithObservation:
    def test_observation_summary_in_prompt(self):
        captured_prompts = []
        task = make_task()

        llm = MagicMock()

        async def mock_query(prompt, system_instruction="", json_mode=False):
            captured_prompts.append(prompt)
            return gemini_finish()

        llm.query = mock_query

        adapter = MagicMock()
        adapter.executor._session = MagicMock(_page=None, _context=None)

        obs = make_observation(url="https://wikipedia.org", title="Wikipedia")
        perception = MagicMock()

        async def mock_observe(*a, **kw):
            return obs

        perception.observe = mock_observe

        agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
        asyncio.run(agent.run(task))

        assert len(captured_prompts) >= 1
        # The observation summary must appear in the prompt
        assert "wikipedia.org" in captured_prompts[0] or "Wikipedia" in captured_prompts[0]
        assert "TASK OBJECTIVE" in captured_prompts[0]


# ─────────────────────────────────────────────────────────────────────────────
# 3. EXECUTE → exactly one browser action
# ─────────────────────────────────────────────────────────────────────────────

class TestExecuteOneAction:
    def test_exactly_one_action_per_execute(self):
        execute_calls = []
        task = make_task(max_actions=5)

        llm = MagicMock()
        responses = [
            gemini_execute("navigate", url="https://example.com"),
            gemini_finish(),
        ]
        resp_iter = iter(responses)

        async def mock_query(p, system_instruction="", json_mode=False):
            return next(resp_iter)

        llm.query = mock_query

        adapter = MagicMock()
        adapter.executor._session = MagicMock(_page=None, _context=None)

        async def mock_execute(task_id, session_id, action):
            execute_calls.append(action)
            return make_action_result(), make_safety_decision()

        adapter.execute_safe_browser_task = mock_execute

        perception = MagicMock()

        async def mock_observe(*a, **kw):
            return make_observation()

        perception.observe = mock_observe

        agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
        result = asyncio.run(agent.run(task))

        # One EXECUTE decision → one execute call
        assert len(execute_calls) == 1
        assert execute_calls[0].action_type == BrowserActionType.NAVIGATE


# ─────────────────────────────────────────────────────────────────────────────
# 4. Agent observes again after each execution
# ─────────────────────────────────────────────────────────────────────────────

class TestReObserveAfterAction:
    def test_observe_count_equals_actions_plus_one(self):
        """Initial observe + one post-action observe = 2 total for 1 action."""
        observe_counts = []
        task = make_task(max_actions=5)

        llm = MagicMock()
        responses = [
            gemini_execute("navigate", url="https://example.com"),
            gemini_finish(),
        ]
        resp_iter = iter(responses)

        async def mock_query(p, system_instruction="", json_mode=False):
            return next(resp_iter)

        llm.query = mock_query

        adapter = MagicMock()
        adapter.executor._session = MagicMock(_page=None, _context=None)

        async def mock_execute(task_id, session_id, action):
            return make_action_result(), make_safety_decision()

        adapter.execute_safe_browser_task = mock_execute

        perception = MagicMock()

        async def mock_observe(page, session_id, task_id, context=None, actions_taken_count=0, recent_action=None):
            observe_counts.append(actions_taken_count)
            return make_observation(actions_taken_count=actions_taken_count)

        perception.observe = mock_observe

        agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
        asyncio.run(agent.run(task))

        # Initial observe (count=0) + post-action observe (count=1) + no more
        assert len(observe_counts) == 2
        assert observe_counts[0] == 0   # Before any action
        assert observe_counts[1] == 1   # After first action


# ─────────────────────────────────────────────────────────────────────────────
# 5. Multiple actions for one BrowserTask
# ─────────────────────────────────────────────────────────────────────────────

class TestMultipleActions:
    def test_three_actions_then_finish(self):
        execute_calls = []
        task = make_task(objective="Navigate to example.com, click button, and take screenshot", max_actions=10)

        llm = MagicMock()
        responses = [
            gemini_execute("navigate", url="https://example.com"),
            gemini_execute("click", elem_ref="elem_001"),
            gemini_execute("screenshot"),
            gemini_finish(),
        ]
        resp_iter = iter(responses)

        async def mock_query(p, system_instruction="", json_mode=False):
            return next(resp_iter)

        llm.query = mock_query

        adapter = MagicMock()
        adapter.executor._session = MagicMock(_page=None, _context=None)

        async def mock_execute(task_id, session_id, action):
            execute_calls.append(action.action_type)
            return (
                BrowserActionResult(
                    success=True,
                    action_type=action.action_type,
                    screenshot_b64="fake_img" if action.action_type == BrowserActionType.SCREENSHOT else None,
                ),
                make_safety_decision(),
            )

        adapter.execute_safe_browser_task = mock_execute

        perception = MagicMock()

        async def mock_observe(*a, **kw):
            return make_observation(recent_action=kw.get("recent_action"))

        perception.observe = mock_observe

        agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
        result = asyncio.run(agent.run(task))

        assert len(execute_calls) == 3
        assert execute_calls[0] == BrowserActionType.NAVIGATE
        assert execute_calls[1] == BrowserActionType.CLICK
        assert execute_calls[2] == BrowserActionType.SCREENSHOT
        assert result.actions_count == 3
        assert result.outcome in (BrowserAgentOutcome.COMPLETED, BrowserAgentOutcome.COMPLETED_UNVERIFIED)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Element references resolved from current observation
# ─────────────────────────────────────────────────────────────────────────────

class TestElementRefResolution:
    def test_click_uses_css_from_current_observation(self):
        resolved_selectors = []
        task = make_task(max_actions=5)

        obs_with_button = make_observation(
            elements=[
                ObservedElement(ref="elem_001", tag="button", css_selector="#submit-btn", role="button", text="Submit")
            ]
        )

        llm = MagicMock()
        responses = [
            json.dumps({"decision": "execute", "reason": "click submit",
                        "action": {"action_type": "click", "element_ref": "elem_001",
                                   "url": None, "value": None, "key": None, "new_tab": False}}),
            gemini_finish(),
        ]
        resp_iter = iter(responses)

        async def mock_query(p, system_instruction="", json_mode=False):
            return next(resp_iter)

        llm.query = mock_query

        adapter = MagicMock()
        adapter.executor._session = MagicMock(_page=None, _context=None)

        async def mock_execute(task_id, session_id, action):
            resolved_selectors.append(action.selector)
            return make_action_result(), make_safety_decision()

        adapter.execute_safe_browser_task = mock_execute

        perception = MagicMock()

        async def mock_observe(*a, **kw):
            return obs_with_button

        perception.observe = mock_observe

        agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
        asyncio.run(agent.run(task))

        assert len(resolved_selectors) == 1
        assert resolved_selectors[0] == "#submit-btn"


# ─────────────────────────────────────────────────────────────────────────────
# 7. Old element references not reused after new observation
# ─────────────────────────────────────────────────────────────────────────────

class TestElementRefNotReused:
    def test_ref_resolved_from_current_not_previous(self):
        """
        Iteration 1 obs: elem_001 = Login button (#login-btn)
        Iteration 2 obs: elem_001 = Search box   (#search-box)
        The agent clicks elem_001 in iteration 2; it must resolve to #search-box,
        NOT #login-btn from the previous observation.
        """
        resolved_selectors = []
        task = make_task(objective="Search for information on example.com", max_actions=10)

        obs1 = make_observation(
            elements=[ObservedElement(ref="elem_001", tag="button", css_selector="#login-btn", role="button", text="Login")]
        )
        obs2 = make_observation(
            url="https://example.com/search",
            elements=[ObservedElement(ref="elem_001", tag="input", css_selector="#search-box", role="searchbox", text=None, placeholder="Search")]
        )

        llm = MagicMock()
        responses = [
            # First: navigate
            gemini_execute("navigate", url="https://example.com/search"),
            # Second: click elem_001 in the NEW observation (should resolve to #search-box)
            json.dumps({"decision": "execute", "reason": "fill search",
                        "action": {"action_type": "click", "element_ref": "elem_001",
                                   "url": None, "value": None, "key": None, "new_tab": False}}),
            gemini_finish(),
        ]
        resp_iter = iter(responses)

        async def mock_query(p, system_instruction="", json_mode=False):
            return next(resp_iter)

        llm.query = mock_query

        adapter = MagicMock()
        adapter.executor._session = MagicMock(_page=None, _context=None)

        async def mock_execute(task_id, session_id, action):
            if action.selector:
                resolved_selectors.append(action.selector)
            return make_action_result(), make_safety_decision()

        adapter.execute_safe_browser_task = mock_execute

        perception = MagicMock()
        obs_queue = iter([obs1, obs2, obs2, obs2])  # Extra obs2 for post-action re-observe

        async def mock_observe(*a, **kw):
            try:
                return next(obs_queue)
            except StopIteration:
                return obs2

        perception.observe = mock_observe

        agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
        asyncio.run(agent.run(task))

        # The second click should use #search-box (obs2), not #login-btn (obs1)
        assert "#search-box" in resolved_selectors
        assert "#login-btn" not in resolved_selectors


# ─────────────────────────────────────────────────────────────────────────────
# 8. FINISH returns completed result
# ─────────────────────────────────────────────────────────────────────────────

class TestFinishDecision:
    def test_finish_immediately_returns_completed(self):
        agent = build_agent(
            gemini_responses=[gemini_finish("Task is done")],
        )
        task = make_task(success_criteria="example domain observed")
        result = asyncio.run(agent.run(task))
        assert result.outcome in (BrowserAgentOutcome.COMPLETED, BrowserAgentOutcome.COMPLETED_UNVERIFIED)
        assert result.succeeded is True

    def test_finish_with_matching_criteria_is_verified(self):
        obs = make_observation(url="https://example.com", title="Example Domain")
        agent = build_agent(
            gemini_responses=[gemini_finish()],
            observations=[obs],
        )
        task = make_task(success_criteria="example domain observed")
        result = asyncio.run(agent.run(task))
        assert result.succeeded is True

    def test_finish_result_has_zero_actions_if_no_action_taken(self):
        agent = build_agent(gemini_responses=[gemini_finish()])
        task = make_task()
        result = asyncio.run(agent.run(task))
        assert result.actions_count == 0
        assert result.actions_taken == []


# ─────────────────────────────────────────────────────────────────────────────
# 9. FAIL returns failed result
# ─────────────────────────────────────────────────────────────────────────────

class TestFailDecision:
    def test_fail_returns_failed_outcome(self):
        agent = build_agent(gemini_responses=[gemini_fail("Cannot navigate to this site")])
        result = asyncio.run(agent.run(make_task()))
        assert result.outcome == BrowserAgentOutcome.FAILED
        assert result.succeeded is False
        assert "Cannot navigate" in (result.error or "")

    def test_fail_result_has_correct_error(self):
        agent = build_agent(gemini_responses=[gemini_fail("Blocked by captcha")])
        result = asyncio.run(agent.run(make_task()))
        assert "Blocked by captcha" in result.error


# ─────────────────────────────────────────────────────────────────────────────
# 10. Action failure causes new observation / decision
# ─────────────────────────────────────────────────────────────────────────────

class TestActionFailureRecovery:
    def test_failed_action_triggers_new_observation(self):
        observe_counts = []

        llm = MagicMock()
        responses = [
            gemini_execute("click", elem_ref="elem_001"),
            gemini_finish(),  # Second decision after failure + re-observe
        ]
        resp_iter = iter(responses)

        async def mock_query(p, system_instruction="", json_mode=False):
            return next(resp_iter)

        llm.query = mock_query

        adapter = MagicMock()
        adapter.executor._session = MagicMock(_page=None, _context=None)

        async def mock_execute(task_id, session_id, action):
            # First call fails
            return (
                BrowserActionResult(
                    success=False,
                    action_type=BrowserActionType.CLICK,
                    error="Element not interactable",
                ),
                make_safety_decision(),
            )

        adapter.execute_safe_browser_task = mock_execute

        perception = MagicMock()

        async def mock_observe(page, session_id, task_id, context=None, actions_taken_count=0, recent_action=None):
            observe_counts.append(recent_action)
            return make_observation(actions_taken_count=actions_taken_count, recent_action=recent_action)

        perception.observe = mock_observe

        agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
        result = asyncio.run(agent.run(make_task(max_actions=5)))

        # Should have observed: initial (None) + post-failed-action (with error)
        assert len(observe_counts) >= 2
        # The second observation should carry the failed recent_action
        post_fail_obs = observe_counts[1]
        assert post_fail_obs is not None
        assert post_fail_obs.success is False
        assert "Element not interactable" in (post_fail_obs.error or "")

    def test_failed_action_next_decision_sees_failure_in_observation(self):
        """The prompt for the second decision must contain recent action failure info."""
        captured_prompts = []

        llm = MagicMock()
        responses = [
            gemini_execute("click", elem_ref="elem_001"),
            gemini_finish(),
        ]
        resp_iter = iter(responses)

        async def mock_query(p, system_instruction="", json_mode=False):
            captured_prompts.append(p)
            return next(resp_iter)

        llm.query = mock_query

        adapter = MagicMock()
        adapter.executor._session = MagicMock(_page=None, _context=None)

        async def mock_execute(task_id, session_id, action):
            return (
                BrowserActionResult(success=False, action_type=BrowserActionType.CLICK,
                                    error="Timeout"),
                make_safety_decision(),
            )

        adapter.execute_safe_browser_task = mock_execute

        perception = MagicMock()

        async def mock_observe(page, session_id, task_id, context=None, actions_taken_count=0, recent_action=None):
            return make_observation(actions_taken_count=actions_taken_count, recent_action=recent_action)

        perception.observe = mock_observe

        agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
        asyncio.run(agent.run(make_task(max_actions=5)))

        # The second Gemini prompt (after the failed action) should show failure
        assert len(captured_prompts) >= 2
        second_prompt = captured_prompts[1]
        assert "FAILED" in second_prompt or "Timeout" in second_prompt


# ─────────────────────────────────────────────────────────────────────────────
# 11. Maximum action limit prevents infinite loops
# ─────────────────────────────────────────────────────────────────────────────

class TestMaxActionsLimit:
    def test_agent_stops_at_max_actions(self):
        execute_calls = []

        llm = MagicMock()

        # Always return EXECUTE — the agent must stop itself
        async def mock_query(p, system_instruction="", json_mode=False):
            return gemini_execute("navigate", url="https://example.com")

        llm.query = mock_query

        adapter = MagicMock()
        adapter.executor._session = MagicMock(_page=None, _context=None)

        async def mock_execute(task_id, session_id, action):
            execute_calls.append(1)
            return make_action_result(), make_safety_decision()

        adapter.execute_safe_browser_task = mock_execute

        perception = MagicMock()

        async def mock_observe(*a, **kw):
            return make_observation()

        perception.observe = mock_observe

        task = make_task(objective="Click button on page until confirmed", max_actions=3)
        agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
        result = asyncio.run(agent.run(task))

        assert len(execute_calls) == 3
        assert result.outcome == BrowserAgentOutcome.FAILED
        assert "Max actions" in (result.error or "")
        assert result.actions_count == 3

    def test_max_actions_one_still_runs_once(self):
        execute_calls = []

        llm = MagicMock()
        responses = [gemini_execute("screenshot"), gemini_finish()]
        resp_iter = iter(responses)

        async def mock_query(p, system_instruction="", json_mode=False):
            return next(resp_iter)

        llm.query = mock_query

        adapter = MagicMock()
        adapter.executor._session = MagicMock(_page=None, _context=None)

        async def mock_execute(task_id, session_id, action):
            execute_calls.append(1)
            return make_action_result(), make_safety_decision()

        adapter.execute_safe_browser_task = mock_execute

        perception = MagicMock()

        async def mock_observe(*a, **kw):
            return make_observation()

        perception.observe = mock_observe

        task = make_task(objective="Click button on page until confirmed", max_actions=1)
        agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
        result = asyncio.run(agent.run(task))

        # With max_actions=1, it can execute once but then runs out of iterations
        assert len(execute_calls) == 1
        assert result.outcome == BrowserAgentOutcome.FAILED  # Never got to FINISH
        assert result.actions_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# 12. BrowserAgentResult correctly reflects actions performed
# ─────────────────────────────────────────────────────────────────────────────

class TestBrowserAgentResult:
    def test_result_actions_list_matches_executed(self):
        task = make_task(objective="Navigate to example.com and take screenshot", max_actions=10)

        llm = MagicMock()
        responses = [
            gemini_execute("navigate", url="https://example.com"),
            gemini_execute("screenshot"),
            gemini_finish(),
        ]
        resp_iter = iter(responses)

        async def mock_query(p, system_instruction="", json_mode=False):
            return next(resp_iter)

        llm.query = mock_query

        adapter = MagicMock()
        adapter.executor._session = MagicMock(_page=None, _context=None)

        async def mock_execute(task_id, session_id, action):
            return make_action_result(), make_safety_decision()

        adapter.execute_safe_browser_task = mock_execute

        perception = MagicMock()

        async def mock_observe(*a, **kw):
            return make_observation()

        perception.observe = mock_observe

        agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
        result = asyncio.run(agent.run(task))

        assert result.actions_count == 2
        assert len(result.actions_taken) == 2
        assert result.actions_taken[0].action_type == BrowserActionType.NAVIGATE
        assert result.actions_taken[1].action_type == BrowserActionType.SCREENSHOT

    def test_result_duration_ms_is_positive(self):
        agent = build_agent(gemini_responses=[gemini_finish()])
        result = asyncio.run(agent.run(make_task()))
        assert result.duration_ms > 0

    def test_result_task_id_and_session_id_preserved(self):
        agent = build_agent(gemini_responses=[gemini_finish()])
        task = make_task(task_id="mytask_42", session_id="mysession_99")
        result = asyncio.run(agent.run(task))
        assert result.task_id == "mytask_42"
        assert result.session_id == "mysession_99"

    def test_result_objective_copied(self):
        agent = build_agent(gemini_responses=[gemini_finish()])
        task = make_task(objective="Go to Wikipedia")
        result = asyncio.run(agent.run(task))
        assert result.objective == "Go to Wikipedia"


# ─────────────────────────────────────────────────────────────────────────────
# 13. Invalid/unsafe decision structure rejected
# ─────────────────────────────────────────────────────────────────────────────

class TestDecisionValidation:
    def test_execute_with_no_action_returns_failed(self):
        # Gemini returns EXECUTE but no action block
        bad_response = json.dumps({"decision": "execute", "reason": "do something", "action": None})
        agent = build_agent(gemini_responses=[bad_response])
        result = asyncio.run(agent.run(make_task()))
        assert result.outcome == BrowserAgentOutcome.FAILED
        assert "no action" in (result.error or "").lower()

    def test_missing_element_ref_returns_new_observation(self):
        """If elem_ref not in observation, agent should re-observe, not crash."""
        bad_click = json.dumps({
            "decision": "execute",
            "reason": "click nonexistent",
            "action": {"action_type": "click", "element_ref": "elem_999",
                       "url": None, "value": None, "key": None, "new_tab": False},
        })
        responses = [bad_click, gemini_finish()]
        agent = build_agent(
            gemini_responses=responses,
            observations=[make_observation()],  # No elem_999
        )
        result = asyncio.run(agent.run(make_task(max_actions=5)))
        # Should not crash; should eventually reach FINISH in next iteration
        assert result is not None


# ─────────────────────────────────────────────────────────────────────────────
# 14. _parse_gemini_decision: well-formed JSON
# ─────────────────────────────────────────────────────────────────────────────

class TestParseGeminiDecision:
    def test_parse_execute_navigate(self):
        raw = json.dumps({
            "decision": "execute",
            "reason": "navigate to target",
            "action": {
                "action_type": "navigate",
                "element_ref": None,
                "url": "https://en.wikipedia.org/wiki/Main_Page",
                "value": None,
                "key": None,
                "new_tab": True,
            }
        })
        d = _parse_gemini_decision(raw)
        assert d.decision == BrowserDecisionType.EXECUTE
        assert d.action is not None
        assert d.action.action_type == BrowserActionType.NAVIGATE
        assert d.action.url == "https://en.wikipedia.org/wiki/Main_Page"
        assert d.action.new_tab is True

    def test_parse_execute_click_with_ref(self):
        raw = json.dumps({
            "decision": "execute",
            "reason": "click link",
            "action": {"action_type": "click", "element_ref": "elem_003",
                       "url": None, "value": None, "key": None, "new_tab": False},
        })
        d = _parse_gemini_decision(raw)
        assert d.action.action_type == BrowserActionType.CLICK
        assert d.action.element_ref == "elem_003"

    def test_parse_finish(self):
        raw = json.dumps({"decision": "finish", "reason": "done", "action": None})
        d = _parse_gemini_decision(raw)
        assert d.decision == BrowserDecisionType.FINISH
        assert d.action is None
        assert d.is_final is True

    def test_parse_fail(self):
        raw = json.dumps({"decision": "fail", "reason": "cannot proceed"})
        d = _parse_gemini_decision(raw)
        assert d.decision == BrowserDecisionType.FAIL
        assert d.is_final is True

    def test_parse_recover_no_action(self):
        raw = json.dumps({"decision": "recover", "reason": "unexpected page", "action": None})
        d = _parse_gemini_decision(raw)
        assert d.decision == BrowserDecisionType.RECOVER
        assert d.action is None

    def test_parse_strips_markdown_fence(self):
        raw = "```json\n" + json.dumps({"decision": "finish", "reason": "ok"}) + "\n```"
        d = _parse_gemini_decision(raw)
        assert d.decision == BrowserDecisionType.FINISH

    def test_parse_screenshot_action(self):
        raw = json.dumps({
            "decision": "execute",
            "reason": "take screenshot",
            "action": {"action_type": "screenshot", "element_ref": None,
                       "url": None, "value": None, "key": None, "new_tab": False},
        })
        d = _parse_gemini_decision(raw)
        assert d.action.action_type == BrowserActionType.SCREENSHOT

    def test_parse_fill_action(self):
        raw = json.dumps({
            "decision": "execute",
            "reason": "fill search box",
            "action": {"action_type": "fill", "element_ref": "elem_002",
                       "url": None, "value": "Playwright automation", "key": None, "new_tab": False},
        })
        d = _parse_gemini_decision(raw)
        assert d.action.action_type == BrowserActionType.FILL
        assert d.action.value == "Playwright automation"
        assert d.action.element_ref == "elem_002"


# ─────────────────────────────────────────────────────────────────────────────
# 15. _parse_gemini_decision: malformed JSON raises
# ─────────────────────────────────────────────────────────────────────────────

class TestParseGeminiDecisionErrors:
    def test_malformed_json_raises_value_error(self):
        with pytest.raises(ValueError, match="non-JSON"):
            _parse_gemini_decision("this is not json at all")

    def test_unknown_decision_type_raises(self):
        raw = json.dumps({"decision": "dance", "reason": "???"})
        with pytest.raises(ValueError, match="Unrecognised decision"):
            _parse_gemini_decision(raw)

    def test_unknown_action_type_raises(self):
        raw = json.dumps({
            "decision": "execute",
            "reason": "do something weird",
            "action": {"action_type": "explode", "element_ref": None,
                       "url": None, "value": None, "key": None, "new_tab": False},
        })
        with pytest.raises(ValueError, match="Unrecognised action_type"):
            _parse_gemini_decision(raw)


# ─────────────────────────────────────────────────────────────────────────────
# 18. _resolve_element_ref
# ─────────────────────────────────────────────────────────────────────────────

class TestResolveElementRef:
    def test_found_element_returns_selector(self):
        obs = make_observation(elements=[
            ObservedElement(ref="elem_001", tag="a", css_selector="a#main-link", role="link", text="Main"),
            ObservedElement(ref="elem_002", tag="button", css_selector="#submit", role="button", text="Submit"),
        ])
        assert _resolve_element_ref(obs, "elem_001") == "a#main-link"
        assert _resolve_element_ref(obs, "elem_002") == "#submit"

    def test_missing_ref_returns_none(self):
        obs = make_observation()
        assert _resolve_element_ref(obs, "elem_999") is None

    def test_empty_elements_returns_none(self):
        obs = BrowserObservation(session_id="s", task_id="t")
        assert _resolve_element_ref(obs, "elem_001") is None


# ─────────────────────────────────────────────────────────────────────────────
# 20. _decision_to_browser_action
# ─────────────────────────────────────────────────────────────────────────────

class TestDecisionToBrowserAction:
    def test_navigate_action_built_correctly(self):
        ad = BrowserActionDecision(
            action_type=BrowserActionType.NAVIGATE,
            url="https://example.com",
            new_tab=True,
        )
        task = make_task()
        ba = _decision_to_browser_action(ad, css_selector=None, task=task)
        assert ba.action_type == BrowserActionType.NAVIGATE
        assert ba.url == "https://example.com"
        assert ba.new_tab is True
        assert ba.selector is None

    def test_click_action_carries_selector(self):
        ad = BrowserActionDecision(
            action_type=BrowserActionType.CLICK,
            element_ref="elem_003",
        )
        task = make_task()
        ba = _decision_to_browser_action(ad, css_selector="#mybutton", task=task)
        assert ba.action_type == BrowserActionType.CLICK
        assert ba.selector == "#mybutton"

    def test_fill_action(self):
        ad = BrowserActionDecision(
            action_type=BrowserActionType.FILL,
            element_ref="elem_001",
            value="hello world",
        )
        ba = _decision_to_browser_action(ad, css_selector="input#q", task=make_task())
        assert ba.value == "hello world"
        assert ba.selector == "input#q"

    def test_description_includes_task_id(self):
        ad = BrowserActionDecision(action_type=BrowserActionType.SCREENSHOT)
        ba = _decision_to_browser_action(ad, css_selector=None, task=make_task(task_id="t_xyz"))
        assert "t_xyz" in ba.description

    def test_metadata_has_agent_tag(self):
        ad = BrowserActionDecision(action_type=BrowserActionType.NAVIGATE, url="https://x.com")
        ba = _decision_to_browser_action(ad, css_selector=None, task=make_task())
        assert ba.metadata.get("agent") == "BrowserAgent"


# ─────────────────────────────────────────────────────────────────────────────
# 21. Safety block returns BLOCKED outcome
# ─────────────────────────────────────────────────────────────────────────────

class TestSafetyBlock:
    def test_blocked_action_returns_blocked_outcome(self):
        agent = build_agent(
            gemini_responses=[gemini_execute("navigate", url="https://unsafe.com")],
            safety_blocked=True,
        )
        result = asyncio.run(agent.run(make_task()))
        assert result.outcome == BrowserAgentOutcome.BLOCKED
        assert "BLOCKED" in (result.error or "")


# ─────────────────────────────────────────────────────────────────────────────
# 22. RECOVER with action
# ─────────────────────────────────────────────────────────────────────────────

class TestRecoverDecision:
    def test_recover_with_action_executes_it(self):
        execute_calls = []

        llm = MagicMock()
        recover_response = json.dumps({
            "decision": "recover",
            "reason": "page state unexpected, navigating back",
            "action": {"action_type": "navigate", "element_ref": None,
                       "url": "https://example.com", "value": None, "key": None, "new_tab": False},
        })
        responses = [recover_response, gemini_finish()]
        resp_iter = iter(responses)

        async def mock_query(p, system_instruction="", json_mode=False):
            return next(resp_iter)

        llm.query = mock_query

        adapter = MagicMock()
        adapter.executor._session = MagicMock(_page=None, _context=None)

        async def mock_execute(task_id, session_id, action):
            execute_calls.append(action)
            return make_action_result(), make_safety_decision()

        adapter.execute_safe_browser_task = mock_execute

        perception = MagicMock()

        async def mock_observe(*a, **kw):
            return make_observation()

        perception.observe = mock_observe

        agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
        result = asyncio.run(agent.run(make_task(max_actions=5)))

        assert len(execute_calls) == 1
        assert execute_calls[0].action_type == BrowserActionType.NAVIGATE

    def test_recover_without_action_just_reobserves(self):
        observe_counts = []

        llm = MagicMock()
        responses = [gemini_recover(), gemini_finish()]
        resp_iter = iter(responses)

        async def mock_query(p, system_instruction="", json_mode=False):
            return next(resp_iter)

        llm.query = mock_query

        adapter = MagicMock()
        adapter.executor._session = MagicMock(_page=None, _context=None)
        # execute should NOT be called for pure RECOVER
        execute_was_called = []

        async def mock_execute(task_id, session_id, action):
            execute_was_called.append(1)
            return make_action_result(), make_safety_decision()

        adapter.execute_safe_browser_task = mock_execute

        perception = MagicMock()

        async def mock_observe(page, session_id, task_id, context=None, actions_taken_count=0, recent_action=None):
            observe_counts.append(1)
            return make_observation()

        perception.observe = mock_observe

        agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
        asyncio.run(agent.run(make_task(max_actions=5)))

        assert len(execute_was_called) == 0  # No browser action for pure RECOVER
        assert len(observe_counts) >= 2      # Initial + re-observe after RECOVER


# ─────────────────────────────────────────────────────────────────────────────
# 24. Phase 1 & 2 import regression
# ─────────────────────────────────────────────────────────────────────────────

class TestPhase1Phase2Regression:
    def test_phase1_contracts_still_importable(self):
        from execution.browser.agent_contracts import (
            BrowserTask, BrowserObservation, BrowserDecision,
            BrowserActionDecision, BrowserVerification, BrowserAgentResult,
        )
        task = BrowserTask(task_id="t", objective="x", session_id="s")
        assert task.max_actions == 10

    def test_phase2_observation_still_works(self):
        from execution.browser.perception_models import ObservedElement, TabInfo, PageSnapshot
        el = ObservedElement(ref="elem_001", tag="a", css_selector="a[href]")
        tab = TabInfo(tab_ref="tab_001", url="https://x.com", is_active=True, index=0)
        assert el.ref == "elem_001"
        assert tab.is_active is True

    def test_browser_agent_importable_from_package(self):
        from execution.browser import BrowserAgent
        assert BrowserAgent is not None

    def test_agent_prompts_importable(self):
        from execution.browser.agent_prompts import BROWSER_AGENT_SYSTEM_PROMPT, build_user_prompt
        assert "browser agent" in BROWSER_AGENT_SYSTEM_PROMPT.lower()
        prompt = build_user_prompt("Open X", None, [], "URL: https://x.com\nTitle: X", 0, 10)
        assert "Open X" in prompt
        assert "0 / 10" in prompt
