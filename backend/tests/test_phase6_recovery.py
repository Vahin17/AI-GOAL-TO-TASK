"""
test_phase6_recovery.py — Unit tests for Phase 6 BrowserAgent Recovery & Replanning
===================================================================================
Tests that:
- Action failure triggers fresh observation with failure context followed by replanning
- Stale element reference is rejected against current observation and prompts replanning
- Wrong page state or failed verification triggers replanning when recovery budget permits
- Recovery budget exhaustion terminates with a clear FAILED outcome (no infinite loops)
- Recovery actions do not bypass SemanticVerifier
- RECOVER decision without action purely re-observes fresh state
- Successful recovery followed by verified state produces COMPLETED
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from execution.browser.agent_contracts import (
    BrowserAgentOutcome,
    BrowserAgentResult,
    BrowserDecision,
    BrowserDecisionType,
    BrowserActionDecision,
    BrowserObservation,
    BrowserTask,
    BrowserVerification,
    BrowserVerificationStatus,
    RecentActionRecord,
)
from execution.browser.browser_models import (
    BrowserAction,
    BrowserActionResult,
    BrowserActionType,
)
from execution.browser.browser_agent import BrowserAgent
from execution.browser.perception_models import ObservedElement, TabInfo


# ─────────────────────────────────────────────────────────────────────────────
# Test Fixtures & Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_obs(
    url: str = "https://example.com",
    title: str = "Example Domain",
    text_summary: str = "Example Domain content",
    elements: list = None,
    recent_action: RecentActionRecord = None,
) -> BrowserObservation:
    return BrowserObservation(
        session_id="s_test",
        task_id="t_test",
        current_url=url,
        page_title=title,
        page_text_summary=text_summary,
        open_tab_count=1,
        tabs=[TabInfo(tab_ref="tab_001", url=url, title=title, is_active=True, index=0)],
        recent_action=recent_action,
        observed_elements=elements or [],
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Action Failure & Replanning Flow
# ─────────────────────────────────────────────────────────────────────────────

class TestActionFailureReplanning:
    def test_failed_action_reobserves_and_replans_with_different_action(self):
        """
        Action 1: Click elem_001 -> execution fails (e.g. timeout / element covered)
        Agent re-observes and sees recent action failure
        Action 2: Gemini replans to navigate directly to target URL
        Action 3: FINISH on verified target URL -> COMPLETED
        """
        gemini_decisions = [
            # Decision 1: Try clicking button
            json.dumps({
                "decision": "execute",
                "reason": "Click link to target page",
                "action": {"action_type": "click", "element_ref": "elem_001", "url": None, "value": None, "key": None, "new_tab": False},
            }),
            # Decision 2: Replanning after failure: Navigate directly instead
            json.dumps({
                "decision": "execute",
                "reason": "Click failed, replanning by navigating directly to destination",
                "action": {"action_type": "navigate", "element_ref": None, "url": "https://example.com/target", "value": None, "key": None, "new_tab": False},
            }),
            # Decision 3: Finish
            json.dumps({
                "decision": "finish",
                "reason": "Successfully reached target page",
                "action": None,
            }),
        ]
        resp_iter = iter(gemini_decisions)

        llm = MagicMock()
        prompts_received = []

        async def mock_query(prompt, system_instruction="", json_mode=False):
            prompts_received.append(prompt)
            return next(resp_iter)

        llm.query = mock_query

        # Observations sequence
        obs1 = make_obs(
            url="https://example.com",
            elements=[ObservedElement(ref="elem_001", tag="button", css_selector="button#submit")],
        )
        obs2 = make_obs(
            url="https://example.com",
            elements=[ObservedElement(ref="elem_001", tag="button", css_selector="button#submit")],
        )
        obs3 = make_obs(url="https://example.com/target", title="Target Page")

        observations = [obs1, obs2, obs3]
        obs_iter = iter(observations)

        perception = MagicMock()
        async def mock_observe(*args, **kwargs):
            try:
                base = next(obs_iter)
            except StopIteration:
                base = obs3
            return BrowserObservation(
                session_id="s_test",
                task_id="t_test",
                current_url=base.current_url,
                page_title=base.page_title,
                page_text_summary=base.page_text_summary,
                observed_elements=base.observed_elements,
                tabs=base.tabs,
                actions_taken_count=kwargs.get("actions_taken_count", 0),
                recent_action=kwargs.get("recent_action"),
            )
        perception.observe = mock_observe

        # Adapter execution results: 1st fails, 2nd succeeds
        adapter = MagicMock()
        adapter.executor = MagicMock()
        exec_count = 0

        async def mock_execute(task_id, session_id, action):
            nonlocal exec_count
            exec_count += 1
            if exec_count == 1:
                return BrowserActionResult(
                    success=False,
                    action_type=action.action_type,
                    error="ElementClickInterceptedException: element covered by modal",
                ), MagicMock(is_blocked=False)
            else:
                return BrowserActionResult(
                    success=True,
                    action_type=action.action_type,
                    current_url="https://example.com/target",
                ), MagicMock(is_blocked=False)

        adapter.execute_safe_browser_task = mock_execute

        agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
        task = BrowserTask(
            task_id="t_recover",
            objective="Navigate to https://example.com/target",
            session_id="s_test",
        )

        result = asyncio.run(agent.run(task))

        assert result.outcome == BrowserAgentOutcome.COMPLETED
        assert result.verification.status == BrowserVerificationStatus.VERIFIED
        # Ensure 2nd prompt received by Gemini contained the failure record
        assert "FAILED" in prompts_received[1]
        assert "ElementClickInterceptedException" in prompts_received[1]


# ─────────────────────────────────────────────────────────────────────────────
# 2. Stale Element Reference Prevention & Recovery
# ─────────────────────────────────────────────────────────────────────────────

class TestStaleElementReferenceRecovery:
    def test_stale_ref_rejected_and_recovered_with_valid_ref(self):
        """
        Gemini references 'elem_999' which does not exist in current observation.
        Agent records stale ref failure in recent_action, re-observes, and prompts Gemini.
        Gemini selects valid 'elem_002', then completes task.
        """
        gemini_decisions = [
            # Decision 1: Bad element ref
            json.dumps({
                "decision": "execute",
                "reason": "Click stale reference",
                "action": {"action_type": "click", "element_ref": "elem_999", "url": None, "value": None, "key": None, "new_tab": False},
            }),
            # Decision 2: Replan with valid ref
            json.dumps({
                "decision": "execute",
                "reason": "Click valid button from fresh observation",
                "action": {"action_type": "click", "element_ref": "elem_002", "url": None, "value": None, "key": None, "new_tab": False},
            }),
            # Decision 3: Finish
            json.dumps({
                "decision": "finish",
                "reason": "Task complete",
                "action": None,
            }),
        ]
        resp_iter = iter(gemini_decisions)

        llm = MagicMock()
        prompts = []
        async def mock_query(prompt, system_instruction="", json_mode=False):
            prompts.append(prompt)
            return next(resp_iter)
        llm.query = mock_query

        # Only elem_002 exists in observation
        obs = make_obs(
            url="https://example.com",
            elements=[ObservedElement(ref="elem_002", tag="button", css_selector="button#valid-btn")],
        )
        perception = MagicMock()
        async def mock_observe(*args, **kwargs):
            return BrowserObservation(
                session_id="s_test",
                task_id="t_test",
                current_url=obs.current_url,
                page_title=obs.page_title,
                observed_elements=obs.observed_elements,
                tabs=obs.tabs,
                actions_taken_count=kwargs.get("actions_taken_count", 0),
                recent_action=kwargs.get("recent_action"),
            )
        perception.observe = mock_observe

        adapter = MagicMock()
        adapter.executor = MagicMock()
        executed_actions = []

        async def mock_execute(task_id, session_id, action):
            executed_actions.append(action)
            return BrowserActionResult(
                success=True,
                action_type=action.action_type,
                current_url="https://example.com",
            ), MagicMock(is_blocked=False)
        adapter.execute_safe_browser_task = mock_execute

        agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
        task = BrowserTask(task_id="t_stale", objective="Click button on https://example.com", session_id="s_test")

        result = asyncio.run(agent.run(task))

        assert result.outcome == BrowserAgentOutcome.COMPLETED
        # Exactly one action executed (the valid elem_002 click; elem_999 was rejected before adapter dispatch)
        assert len(executed_actions) == 1
        assert executed_actions[0].selector == "button#valid-btn"
        # Second prompt informed Gemini of the missing ref
        assert "elem_999" in prompts[1]
        assert "not found" in prompts[1]


# ─────────────────────────────────────────────────────────────────────────────
# 3. Verification Failure Triggering Replanning
# ─────────────────────────────────────────────────────────────────────────────

class TestVerificationFailureReplanning:
    def test_finish_on_wrong_page_triggers_replan_and_recovery(self):
        """
        Gemini prematurely calls FINISH on wrong page (google.com instead of example.com).
        Verification returns NOT_VERIFIED.
        Agent feeds verification failure to Gemini, allowing replanning.
        Gemini navigates to example.com and then calls FINISH -> COMPLETED.
        """
        gemini_decisions = [
            # Decision 1: Premature FINISH on wrong page
            json.dumps({"decision": "finish", "reason": "I think I am done", "action": None}),
            # Decision 2: Replanning navigation to correct target
            json.dumps({
                "decision": "execute",
                "reason": "Verification failed, navigating to correct target",
                "action": {"action_type": "navigate", "element_ref": None, "url": "https://example.com", "value": None, "key": None, "new_tab": False},
            }),
            # Decision 3: Final verified finish
            json.dumps({"decision": "finish", "reason": "Now on verified page", "action": None}),
        ]
        resp_iter = iter(gemini_decisions)

        llm = MagicMock()
        prompts = []
        async def mock_query(prompt, system_instruction="", json_mode=False):
            prompts.append(prompt)
            return next(resp_iter)
        llm.query = mock_query

        # Starts on wrong page, then moves to correct page
        obs_wrong = make_obs(url="https://google.com/", title="Google")
        obs_correct = make_obs(url="https://example.com/", title="Example Domain")

        current_obs = obs_wrong
        perception = MagicMock()
        async def mock_observe(*args, **kwargs):
            return BrowserObservation(
                session_id="s_test",
                task_id="t_test",
                current_url=current_obs.current_url,
                page_title=current_obs.page_title,
                observed_elements=current_obs.observed_elements,
                tabs=current_obs.tabs,
                actions_taken_count=kwargs.get("actions_taken_count", 0),
                recent_action=kwargs.get("recent_action"),
            )
        perception.observe = mock_observe

        adapter = MagicMock()
        adapter.executor = MagicMock()
        async def mock_execute(task_id, session_id, action):
            nonlocal current_obs
            current_obs = obs_correct
            return BrowserActionResult(
                success=True,
                action_type=action.action_type,
                current_url="https://example.com/",
            ), MagicMock(is_blocked=False)
        adapter.execute_safe_browser_task = mock_execute

        agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
        task = BrowserTask(task_id="t_verify_replan", objective="Find and open the target portal", session_id="s_test", success_criteria="URL must be https://example.com")

        result = asyncio.run(agent.run(task))

        assert result.outcome == BrowserAgentOutcome.COMPLETED
        assert result.verification.status == BrowserVerificationStatus.VERIFIED
        # Ensure 2nd prompt received the verification failure
        assert "verification failed" in prompts[1].lower()


# ─────────────────────────────────────────────────────────────────────────────
# 4. Recovery Budget & Infinite Loop Prevention
# ─────────────────────────────────────────────────────────────────────────────

class TestRecoveryBudgetLimits:
    def test_exhausted_recovery_budget_terminates_with_failed(self):
        """
        Agent encounters repeated unrecoverable failures (e.g. repeated bad clicks).
        Once max_recovery_budget (3) is exceeded, agent halts with FAILED outcome.
        """
        repeated_bad_action = json.dumps({
            "decision": "execute",
            "reason": "Attempting action",
            "action": {"action_type": "click", "element_ref": "elem_001", "url": None, "value": None, "key": None, "new_tab": False},
        })

        llm = MagicMock()
        llm.query = AsyncMock(return_value=repeated_bad_action)

        perception = MagicMock()
        obs = make_obs(url="https://example.com", elements=[ObservedElement(ref="elem_001", tag="button", css_selector="#btn")])
        perception.observe = AsyncMock(return_value=obs)

        adapter = MagicMock()
        adapter.executor = MagicMock()
        # Always fail execution
        adapter.execute_safe_browser_task = AsyncMock(return_value=(
            BrowserActionResult(success=False, action_type=BrowserActionType.CLICK, error="Unrecoverable network error"),
            MagicMock(is_blocked=False),
        ))

        agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
        task = BrowserTask(task_id="t_budget", objective="Test recovery budget", max_actions=10, session_id="s_test")

        result = asyncio.run(agent.run(task))

        assert result.outcome == BrowserAgentOutcome.FAILED
        assert "Recovery budget exhausted" in (result.error or "")

    def test_pure_recover_decision_without_action_reobserves(self):
        """RECOVER without action simply requests fresh observation and replans."""
        gemini_decisions = [
            json.dumps({"decision": "recover", "reason": "Wait for page to settle", "action": None}),
            json.dumps({"decision": "finish", "reason": "Page settled, done", "action": None}),
        ]
        resp_iter = iter(gemini_decisions)
        llm = MagicMock()
        llm.query = AsyncMock(side_effect=lambda *args, **kwargs: next(resp_iter))

        obs = make_obs(url="https://example.com/")
        perception = MagicMock()
        observe_count = 0
        async def mock_observe(*args, **kwargs):
            nonlocal observe_count
            observe_count += 1
            return obs
        perception.observe = mock_observe

        adapter = MagicMock()
        adapter.executor = MagicMock()
        adapter.execute_safe_browser_task = AsyncMock()

        agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
        task = BrowserTask(task_id="t_pure_recover", objective="Navigate to https://example.com", session_id="s_test")

        result = asyncio.run(agent.run(task))

        assert result.outcome == BrowserAgentOutcome.COMPLETED
        # Adapter execute was not called for pure RECOVER
        adapter.execute_safe_browser_task.assert_not_called()
        # Initial observe + re-observe after RECOVER
        assert observe_count >= 2
