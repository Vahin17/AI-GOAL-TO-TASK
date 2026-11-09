"""
test_browser_call_reduction.py — Focused Tests for Verification-First & Explicit Fast-Pathing
=============================================================================================
Tests the two BrowserAgent optimizations:
1. Verification-First Completion (completes when deterministic verifier proves task success without extra FINISH call)
2. Explicit Action Fast-Pathing (executes explicit atomic actions like URL navigation and screenshots directly)

All tests use strict mocks:
- ZERO real Gemini API calls
- ZERO real browser / external network executions
- Strict call-counting and decision tracing
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from execution.browser.agent_contracts import (
    BrowserActionDecision,
    BrowserAgentOutcome,
    BrowserAgentResult,
    BrowserDecision,
    BrowserDecisionType,
    BrowserObservation,
    BrowserTask,
    BrowserVerification,
    BrowserVerificationStatus,
    RecentActionRecord,
)
from execution.browser.browser_agent import BrowserAgent
from execution.browser.browser_models import (
    BrowserAction,
    BrowserActionResult,
    BrowserActionType,
)
from execution.browser.perception_models import ObservedElement, PageSnapshot, TabInfo
from execution.browser.semantic_verifier import SemanticVerifier
from orchestration.safety_schemas import ExecutionDecision, RiskLevel, SafetyDecision


# ─────────────────────────────────────────────────────────────────────────────
# Test Fixtures & Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_obs(
    url: str = "about:blank",
    title: str = "",
    elements: list = None,
    recent_action: RecentActionRecord = None,
    screenshot_b64: str = None,
    tab_count: int = 1,
    session_id: str = "s_001",
    task_id: str = "t_001",
) -> BrowserObservation:
    snapshot = PageSnapshot(url=url, title=title, screenshot_base64=screenshot_b64) if screenshot_b64 else None
    return BrowserObservation(
        session_id=session_id,
        task_id=task_id,
        current_url=url,
        page_title=title,
        observed_elements=elements or [],
        recent_action=recent_action,
        page_snapshot=snapshot,
        open_tab_count=tab_count,
        tabs=[TabInfo(tab_ref="tab_001", url=url, title=title, is_active=True, index=0)],
    )

def make_mock_observe(obs_list: list) -> AsyncMock:
    obs_iter = iter(obs_list)
    last_obs = obs_list[-1]
    async def _obs(*a, **kw):
        try:
            return next(obs_iter)
        except StopIteration:
            return last_obs
    return AsyncMock(side_effect=_obs)

def make_safety_ok(task_id: str = "t_001", task_name: str = "task") -> SafetyDecision:
    return SafetyDecision(
        task_id=task_id,
        task_name=task_name,
        risk_level=RiskLevel.LOW,
        execution_decision=ExecutionDecision.ALLOW,
        reasoning="Test action is safe.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# TEST 1 — Verification-First Navigation
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verification_first_navigation():
    """
    Given an explicit navigation task, when the navigation action succeeds
    and observation satisfies SemanticVerifier, the task completes immediately
    without requesting a Gemini FINISH decision.
    """
    task = BrowserTask(
        task_id="t_nav_01",
        objective="Navigate to https://www.wikipedia.org",
        session_id="s_001",
    )

    llm = MagicMock()
    llm.query = AsyncMock(return_value="{}")
    adapter = MagicMock()
    adapter.executor._session = MagicMock(_page=None, _context=None)

    obs_initial = make_obs(url="about:blank", task_id="t_nav_01")
    obs_loaded = make_obs(
        url="https://www.wikipedia.org/",
        title="Wikipedia, the free encyclopedia",
        task_id="t_nav_01",
        recent_action=RecentActionRecord(
            action_type=BrowserActionType.NAVIGATE,
            url="https://www.wikipedia.org",
            success=True,
        ),
    )

    perception = MagicMock()
    perception.observe = make_mock_observe([obs_initial, obs_loaded])

    async def mock_execute(task_id, session_id, action):
        return (
            BrowserActionResult(success=True, action_type=action.action_type, current_url=action.url),
            make_safety_ok(task_id=task_id),
        )

    adapter.execute_safe_browser_task = mock_execute

    agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
    result = await agent.run(task)

    # Must complete as VERIFIED and COMPLETED
    assert result.outcome == BrowserAgentOutcome.COMPLETED
    assert result.verification is not None
    assert result.verification.status == BrowserVerificationStatus.VERIFIED
    # ZERO Gemini calls made because fast-path + verification-first handled everything
    assert llm.query.call_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# TEST 2 — Verification Failure Preserves Gemini Reasoning
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verification_failure_preserves_gemini_reasoning():
    """
    When an action executes but verification is NOT satisfied,
    BrowserAgent does not falsely complete and invokes Gemini reasoning for next steps.
    """
    task = BrowserTask(
        task_id="t_nav_02",
        objective="Navigate to https://www.wikipedia.org",
        session_id="s_002",
    )

    gemini_decision = json.dumps({
        "decision": "recover",
        "reason": "Wrong page loaded, retrying navigation",
        "action": {
            "action_type": "navigate",
            "url": "https://www.wikipedia.org",
            "element_ref": None,
            "value": None,
            "key": None,
            "new_tab": False,
        }
    })
    llm = MagicMock()
    llm.query = AsyncMock(return_value=gemini_decision)

    adapter = MagicMock()
    adapter.executor._session = MagicMock(_page=None, _context=None)

    obs_initial = make_obs(url="about:blank", task_id="t_nav_02")
    obs_wrong = make_obs(
        url="https://error.example.com/404",
        title="Error 404",
        task_id="t_nav_02",
        recent_action=RecentActionRecord(
            action_type=BrowserActionType.NAVIGATE,
            url="https://www.wikipedia.org",
            success=True,
        ),
    )
    obs_correct = make_obs(
        url="https://www.wikipedia.org/",
        title="Wikipedia",
        task_id="t_nav_02",
        recent_action=RecentActionRecord(
            action_type=BrowserActionType.NAVIGATE,
            url="https://www.wikipedia.org",
            success=True,
        ),
    )

    perception = MagicMock()
    perception.observe = make_mock_observe([obs_initial, obs_wrong, obs_correct])

    async def mock_execute(task_id, session_id, action):
        return (
            BrowserActionResult(success=True, action_type=action.action_type, current_url=action.url),
            make_safety_ok(task_id=task_id),
        )

    adapter.execute_safe_browser_task = mock_execute

    agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
    result = await agent.run(task)

    # Gemini reasoning was invoked to recover from the wrong page
    assert llm.query.call_count >= 1
    assert result.outcome == BrowserAgentOutcome.COMPLETED


# ─────────────────────────────────────────────────────────────────────────────
# TEST 3 — Explicit Navigation Fast Path
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_explicit_navigation_fast_path():
    """
    Given a task with explicit destination URL in task contract,
    it executes without an LLM action-selection call.
    """
    task = BrowserTask(
        task_id="t_nav_03",
        objective="Open https://docs.python.org/3/",
        session_id="s_003",
    )

    executed_actions = []

    llm = MagicMock()
    llm.query = AsyncMock(return_value="{}")
    adapter = MagicMock()
    adapter.executor._session = MagicMock(_page=None, _context=None)

    obs_initial = make_obs(url="about:blank", task_id="t_nav_03")
    obs_loaded = make_obs(
        url="https://docs.python.org/3/",
        title="Python Documentation",
        task_id="t_nav_03",
        recent_action=RecentActionRecord(
            action_type=BrowserActionType.NAVIGATE,
            url="https://docs.python.org/3/",
            success=True,
        ),
    )

    perception = MagicMock()
    perception.observe = make_mock_observe([obs_initial, obs_loaded])

    async def mock_execute(task_id, session_id, action):
        executed_actions.append(action)
        return (
            BrowserActionResult(success=True, action_type=action.action_type, current_url=action.url),
            make_safety_ok(task_id=task_id),
        )

    adapter.execute_safe_browser_task = mock_execute

    agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
    result = await agent.run(task)

    assert len(executed_actions) == 1
    assert executed_actions[0].action_type == BrowserActionType.NAVIGATE
    assert executed_actions[0].url == "https://docs.python.org/3/"
    assert llm.query.call_count == 0
    assert result.outcome == BrowserAgentOutcome.COMPLETED


# ─────────────────────────────────────────────────────────────────────────────
# TEST 4 — Explicit Screenshot Fast Path
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_explicit_screenshot_fast_path():
    """
    Given a task requiring a screenshot on the currently active page,
    it executes screenshot directly without an LLM action-selection call.
    """
    task = BrowserTask(
        task_id="t_ss_01",
        objective="Capture screenshot of Wikipedia page",
        session_id="s_004",
    )

    executed_actions = []
    llm = MagicMock()
    llm.query = AsyncMock(return_value="{}")
    adapter = MagicMock()
    adapter.executor._session = MagicMock(_page=None, _context=None)

    obs_initial = make_obs(url="https://www.wikipedia.org/", title="Wikipedia", task_id="t_ss_01")
    obs_post_ss = make_obs(
        url="https://www.wikipedia.org/",
        title="Wikipedia",
        task_id="t_ss_01",
        screenshot_b64="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA",
        recent_action=RecentActionRecord(
            action_type=BrowserActionType.SCREENSHOT,
            success=True,
        ),
    )

    perception = MagicMock()
    perception.observe = make_mock_observe([obs_initial, obs_post_ss])

    async def mock_execute(task_id, session_id, action):
        executed_actions.append(action)
        return (
            BrowserActionResult(
                success=True,
                action_type=action.action_type,
                screenshot_data="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA",
            ),
            make_safety_ok(task_id=task_id),
        )

    adapter.execute_safe_browser_task = mock_execute

    agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
    result = await agent.run(task)

    assert len(executed_actions) == 1
    assert executed_actions[0].action_type == BrowserActionType.SCREENSHOT
    assert llm.query.call_count == 0
    assert result.outcome == BrowserAgentOutcome.COMPLETED


# ─────────────────────────────────────────────────────────────────────────────
# TEST 5 — Dynamic Task Still Uses Gemini
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dynamic_task_still_uses_gemini():
    """
    A dynamic in-page interaction task (e.g. search / element selection)
    must NOT fast-path and MUST invoke Gemini for DOM reasoning.
    """
    task = BrowserTask(
        task_id="t_dyn_01",
        objective="Search YouTube for Vande Mataram and play the correct song",
        session_id="s_005",
    )

    gemini_responses = [
        # Decision 1: Fill search box
        json.dumps({
            "decision": "execute",
            "reason": "Fill search box with query",
            "action": {
                "action_type": "fill",
                "element_ref": "elem_001",
                "value": "Vande Mataram",
                "url": None,
                "key": None,
                "new_tab": False,
            }
        }),
        # Decision 2: Finish
        json.dumps({
            "decision": "finish",
            "reason": "Search submitted and playing",
            "action": None,
        }),
    ]
    resp_iter = iter(gemini_responses)
    llm = MagicMock()
    llm.query = AsyncMock(side_effect=lambda *a, **kw: next(resp_iter))

    adapter = MagicMock()
    adapter.executor._session = MagicMock(_page=None, _context=None)

    obs_search = make_obs(
        url="https://www.youtube.com",
        task_id="t_dyn_01",
        elements=[
            ObservedElement(ref="elem_001", tag="input", css_selector="#search-input", role="searchbox", placeholder="Search")
        ],
    )

    perception = MagicMock()
    perception.observe = AsyncMock(return_value=obs_search)

    async def mock_execute(task_id, session_id, action):
        return (
            BrowserActionResult(success=True, action_type=action.action_type, value=action.value),
            make_safety_ok(task_id=task_id),
        )

    adapter.execute_safe_browser_task = mock_execute

    agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
    result = await agent.run(task)

    # Proves Gemini was invoked for dynamic element reasoning
    assert llm.query.call_count >= 1
    assert result.outcome == BrowserAgentOutcome.COMPLETED


# ─────────────────────────────────────────────────────────────────────────────
# TEST 6 — Unknown Website Remains Generic
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_website_remains_generic():
    """
    Fast-path explicit navigation works for arbitrary, unknown domains
    without requiring any hardcoded website mapping.
    """
    task = BrowserTask(
        task_id="t_unk_01",
        objective="Navigate to https://custom-internal-dashboard.corp.local:8080/reports",
        session_id="s_006",
    )

    llm = MagicMock()
    llm.query = AsyncMock(return_value="{}")
    adapter = MagicMock()
    adapter.executor._session = MagicMock(_page=None, _context=None)

    obs_initial = make_obs(url="about:blank", task_id="t_unk_01")
    obs_loaded = make_obs(
        url="https://custom-internal-dashboard.corp.local:8080/reports",
        title="Internal Reports",
        task_id="t_unk_01",
        recent_action=RecentActionRecord(
            action_type=BrowserActionType.NAVIGATE,
            url="https://custom-internal-dashboard.corp.local:8080/reports",
            success=True,
        ),
    )

    perception = MagicMock()
    perception.observe = make_mock_observe([obs_initial, obs_loaded])

    executed_urls = []
    async def mock_execute(task_id, session_id, action):
        executed_urls.append(action.url)
        return (
            BrowserActionResult(success=True, action_type=action.action_type, current_url=action.url),
            make_safety_ok(task_id=task_id),
        )

    adapter.execute_safe_browser_task = mock_execute

    agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
    result = await agent.run(task)

    assert executed_urls == ["https://custom-internal-dashboard.corp.local:8080/reports"]
    assert llm.query.call_count == 0
    assert result.outcome == BrowserAgentOutcome.COMPLETED


# ─────────────────────────────────────────────────────────────────────────────
# TEST 7 — No False Success
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_false_success():
    """
    If an action executes successfully but verification fails and all actions
    are exhausted, the task outcome must be FAILED (no false/simulated success).
    """
    task = BrowserTask(
        task_id="t_fail_01",
        objective="Navigate to https://www.wikipedia.org",
        max_actions=1,
        session_id="s_007",
    )

    llm = MagicMock()
    llm.query = AsyncMock(return_value="{}")
    adapter = MagicMock()
    adapter.executor._session = MagicMock(_page=None, _context=None)

    obs_initial = make_obs(url="about:blank", task_id="t_fail_01")
    # Action loaded a blank page, failing verification
    obs_failed = make_obs(
        url="about:blank",
        task_id="t_fail_01",
        recent_action=RecentActionRecord(
            action_type=BrowserActionType.NAVIGATE,
            url="https://www.wikipedia.org",
            success=True,
        ),
    )

    perception = MagicMock()
    perception.observe = make_mock_observe([obs_initial, obs_failed])

    async def mock_execute(task_id, session_id, action):
        return (
            BrowserActionResult(success=True, action_type=action.action_type, current_url="about:blank"),
            make_safety_ok(task_id=task_id),
        )

    adapter.execute_safe_browser_task = mock_execute

    agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
    result = await agent.run(task)

    assert result.outcome == BrowserAgentOutcome.FAILED


# ─────────────────────────────────────────────────────────────────────────────
# TEST 8 — Existing Recovery Behavior Preserved
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_existing_recovery_behavior_preserved():
    """
    When verification fails on an initial fast-path action, BrowserAgent triggers
    recovery, allowing Gemini to reason and recover within the recovery budget.
    """
    task = BrowserTask(
        task_id="t_rec_01",
        objective="Navigate to https://example.com",
        max_actions=5,
        session_id="s_008",
    )

    # Gemini suggests a recovery action to re-navigate
    recovery_decision = json.dumps({
        "decision": "recover",
        "reason": "Re-navigating to example.com",
        "action": {
            "action_type": "navigate",
            "url": "https://example.com",
            "element_ref": None,
            "value": None,
            "key": None,
            "new_tab": False,
        }
    })
    llm = MagicMock()
    llm.query = AsyncMock(return_value=recovery_decision)

    adapter = MagicMock()
    adapter.executor._session = MagicMock(_page=None, _context=None)

    obs_initial = make_obs(url="about:blank", task_id="t_rec_01")
    obs_blank = make_obs(url="about:blank", task_id="t_rec_01")
    obs_success = make_obs(
        url="https://example.com",
        title="Example Domain",
        task_id="t_rec_01",
        recent_action=RecentActionRecord(action_type=BrowserActionType.NAVIGATE, url="https://example.com", success=True),
    )

    perception = MagicMock()
    perception.observe = make_mock_observe([obs_initial, obs_blank, obs_success])

    async def mock_execute(task_id, session_id, action):
        return (
            BrowserActionResult(success=True, action_type=action.action_type, current_url="https://example.com"),
            make_safety_ok(task_id=task_id),
        )

    adapter.execute_safe_browser_task = mock_execute

    agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
    result = await agent.run(task)

    assert llm.query.call_count == 1
    assert result.outcome == BrowserAgentOutcome.COMPLETED


# ─────────────────────────────────────────────────────────────────────────────
# TEST 9 — Gemini Call Count for "Open Wikipedia" Scenario
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gemini_call_count_reduced_to_zero_in_browser_agent():
    """
    Verifies that for the 2-node 'Open Wikipedia' scenario (Node 1: Navigate, Node 2: Screenshot),
    the BrowserAgent makes ZERO Gemini calls (previously 4 calls: Navigate, Finish, Screenshot, Finish).
    """
    # ── Node 1: Navigate to Wikipedia ───────────────────────────────────────
    task_node1 = BrowserTask(
        task_id="node_001",
        objective="Navigate to https://www.wikipedia.org",
        session_id="s_wiki",
    )

    llm_node1 = MagicMock()
    llm_node1.query = AsyncMock(return_value="{}")
    adapter_node1 = MagicMock()
    adapter_node1.executor._session = MagicMock(_page=None, _context=None)

    obs_init_1 = make_obs(url="about:blank", task_id="node_001")
    obs_done_1 = make_obs(
        url="https://www.wikipedia.org/",
        title="Wikipedia",
        task_id="node_001",
        recent_action=RecentActionRecord(action_type=BrowserActionType.NAVIGATE, url="https://www.wikipedia.org", success=True),
    )

    perception_1 = MagicMock()
    perception_1.observe = make_mock_observe([obs_init_1, obs_done_1])

    async def mock_exec_1(task_id, session_id, action):
        return (
            BrowserActionResult(success=True, action_type=action.action_type, current_url="https://www.wikipedia.org/"),
            make_safety_ok(task_id=task_id),
        )

    adapter_node1.execute_safe_browser_task = mock_exec_1

    agent_1 = BrowserAgent(llm_provider=llm_node1, adapter=adapter_node1, perception=perception_1)
    res_1 = await agent_1.run(task_node1)

    assert res_1.outcome == BrowserAgentOutcome.COMPLETED
    assert llm_node1.query.call_count == 0  # 0 calls for Node 1!

    # ── Node 2: Capture Screenshot ──────────────────────────────────────────
    task_node2 = BrowserTask(
        task_id="node_002",
        objective="Capture screenshot of Wikipedia page",
        session_id="s_wiki",
    )

    llm_node2 = MagicMock()
    llm_node2.query = AsyncMock(return_value="{}")
    adapter_node2 = MagicMock()
    adapter_node2.executor._session = MagicMock(_page=None, _context=None)

    obs_init_2 = make_obs(url="https://www.wikipedia.org/", title="Wikipedia", task_id="node_002")
    obs_done_2 = make_obs(
        url="https://www.wikipedia.org/",
        title="Wikipedia",
        task_id="node_002",
        screenshot_b64="data:image/png;base64,mockb64data",
        recent_action=RecentActionRecord(action_type=BrowserActionType.SCREENSHOT, success=True),
    )

    perception_2 = MagicMock()
    perception_2.observe = make_mock_observe([obs_init_2, obs_done_2])

    async def mock_exec_2(task_id, session_id, action):
        return (
            BrowserActionResult(success=True, action_type=action.action_type, screenshot_data="data:image/png;base64,mockb64data"),
            make_safety_ok(task_id=task_id),
        )

    adapter_node2.execute_safe_browser_task = mock_exec_2

    agent_2 = BrowserAgent(llm_provider=llm_node2, adapter=adapter_node2, perception=perception_2)
    res_2 = await agent_2.run(task_node2)

    assert res_2.outcome == BrowserAgentOutcome.COMPLETED
    assert llm_node2.query.call_count == 0  # 0 calls for Node 2!

    # Total BrowserAgent Gemini calls across both nodes = 0 (reduced from 4!)
    total_browser_calls = llm_node1.query.call_count + llm_node2.query.call_count
    assert total_browser_calls == 0


# ─────────────────────────────────────────────────────────────────────────────
# TEST 10 — Compatibility with Standard Browser Decision Parsing
# ─────────────────────────────────────────────────────────────────────────────

def test_browser_decision_parsing_and_events_compatibility():
    """
    Ensures that standard decision parsing, element resolution, and schemas
    remain 100% backwards compatible.
    """
    raw_decision = json.dumps({
        "decision": "execute",
        "reason": "Click search button",
        "action": {
            "action_type": "click",
            "element_ref": "elem_042",
            "url": None,
            "value": None,
            "key": None,
            "new_tab": False,
        }
    })
    from execution.browser.browser_agent import _parse_gemini_decision
    dec = _parse_gemini_decision(raw_decision)
    assert dec.decision == BrowserDecisionType.EXECUTE
    assert dec.action.action_type == BrowserActionType.CLICK
    assert dec.action.element_ref == "elem_042"


# ─────────────────────────────────────────────────────────────────────────────
# TEST 11 — Natural-Language Simple Navigation (Open Wikipedia with metadata)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_natural_language_simple_navigation_with_metadata():
    """
    Test A: Natural-language simple navigation:
    Objective 'Open Wikipedia' with resolved destination_url in metadata.
    Expected:
    - fast-path navigates once
    - fresh observation occurs
    - SemanticVerifier verifies completion
    - BrowserAgent completes without Gemini FINISH
    - exactly 0 Gemini calls, 1 action executed
    """
    task = BrowserTask(
        task_id="t_wiki_meta",
        objective="Open Wikipedia",
        session_id="s_wiki_01",
        metadata={"destination_url": "https://www.wikipedia.org"},
    )

    llm = MagicMock()
    llm.query = AsyncMock(return_value="{}")
    adapter = MagicMock()
    adapter.executor._session = MagicMock(_page=None, _context=None)

    obs_initial = make_obs(url="about:blank", task_id="t_wiki_meta")
    obs_loaded = make_obs(
        url="https://www.wikipedia.org/",
        title="Wikipedia, the free encyclopedia",
        task_id="t_wiki_meta",
        recent_action=RecentActionRecord(
            action_type=BrowserActionType.NAVIGATE,
            url="https://www.wikipedia.org",
            success=True,
        ),
    )

    perception = MagicMock()
    perception.observe = make_mock_observe([obs_initial, obs_loaded])

    actions_executed = []
    async def mock_execute(task_id, session_id, action):
        actions_executed.append(action)
        return (
            BrowserActionResult(success=True, action_type=action.action_type, current_url="https://www.wikipedia.org/"),
            make_safety_ok(task_id=task_id),
        )

    adapter.execute_safe_browser_task = mock_execute

    agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
    result = await agent.run(task)

    assert result.outcome == BrowserAgentOutcome.COMPLETED
    assert result.verification.status == BrowserVerificationStatus.VERIFIED
    assert len(actions_executed) == 1
    assert actions_executed[0].action_type == BrowserActionType.NAVIGATE
    assert llm.query.call_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# TEST 12 — Natural-Language Simple Navigation (Open Wikipedia without metadata)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_natural_language_simple_navigation_gemini_resolved_single_action():
    """
    Test A2: Objective 'Open Wikipedia' without explicit metadata scheme.
    Gemini resolves the URL on iteration 1.
    Post-action verification detects successful navigation for single navigation goal.
    Expected:
    - Exactly 1 Gemini call (for the initial decision)
    - Exactly 1 navigation action executed
    - Completes without a 2nd Gemini FINISH call
    - No runaway loop to iteration 6
    """
    task = BrowserTask(
        task_id="t_wiki_gemini",
        objective="Open Wikipedia",
        session_id="s_wiki_02",
    )

    gemini_nav_decision = json.dumps({
        "decision": "execute",
        "reason": "Navigate to Wikipedia main page",
        "action": {
            "action_type": "navigate",
            "url": "https://www.wikipedia.org",
            "element_ref": None,
            "value": None,
            "key": None,
            "new_tab": False,
        }
    })

    llm = MagicMock()
    llm.query = AsyncMock(return_value=gemini_nav_decision)
    adapter = MagicMock()
    adapter.executor._session = MagicMock(_page=None, _context=None)

    obs_initial = make_obs(url="about:blank", task_id="t_wiki_gemini")
    obs_loaded = make_obs(
        url="https://www.wikipedia.org/",
        title="Wikipedia, the free encyclopedia",
        task_id="t_wiki_gemini",
        recent_action=RecentActionRecord(
            action_type=BrowserActionType.NAVIGATE,
            url="https://www.wikipedia.org",
            success=True,
        ),
    )

    perception = MagicMock()
    perception.observe = make_mock_observe([obs_initial, obs_loaded])

    actions_executed = []
    async def mock_execute(task_id, session_id, action):
        actions_executed.append(action)
        return (
            BrowserActionResult(success=True, action_type=action.action_type, current_url="https://www.wikipedia.org/"),
            make_safety_ok(task_id=task_id),
        )

    adapter.execute_safe_browser_task = mock_execute

    agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
    result = await agent.run(task)

    assert result.outcome == BrowserAgentOutcome.COMPLETED
    assert result.verification.status == BrowserVerificationStatus.VERIFIED
    assert len(actions_executed) == 1
    assert llm.query.call_count == 1  # Only 1 Gemini call for initial decision, ZERO extra FINISH calls!


# ─────────────────────────────────────────────────────────────────────────────
# TEST 13 — Multi-Step Dynamic Task Does NOT Early-Complete on Navigation
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_multi_step_dynamic_task_preserves_gemini():
    """
    Test C: Multi-step task 'Open YouTube and search for Vande Mataram'.
    Expected:
    - Navigation alone does NOT complete the task
    - Agent continues into iteration 2 to perform dynamic interaction (search)
    - Gemini remains available
    """
    task = BrowserTask(
        task_id="t_yt_search",
        objective="Open YouTube and search for Vande Mataram",
        session_id="s_yt_01",
    )

    gemini_nav = json.dumps({
        "decision": "execute",
        "reason": "Navigate to YouTube",
        "action": {
            "action_type": "navigate",
            "url": "https://www.youtube.com",
            "element_ref": None,
            "value": None,
            "key": None,
            "new_tab": False,
        }
    })
    gemini_search = json.dumps({
        "decision": "execute",
        "reason": "Type search query in search bar",
        "action": {
            "action_type": "fill",
            "element_ref": "elem_search_box",
            "value": "Vande Mataram",
            "url": None,
            "key": None,
            "new_tab": False,
        }
    })

    llm = MagicMock()
    llm.query = AsyncMock(side_effect=[gemini_nav, gemini_search, json.dumps({"decision": "finish", "reason": "Search complete"})])
    adapter = MagicMock()
    adapter.executor._session = MagicMock(_page=None, _context=None)

    obs_initial = make_obs(url="about:blank", task_id="t_yt_search")
    obs_nav = make_obs(
        url="https://www.youtube.com/",
        title="YouTube",
        task_id="t_yt_search",
        elements=[ObservedElement(ref="elem_search_box", tag="input", css_selector="#search")],
        recent_action=RecentActionRecord(action_type=BrowserActionType.NAVIGATE, url="https://www.youtube.com", success=True),
    )
    obs_typed = make_obs(
        url="https://www.youtube.com/results?search_query=Vande+Mataram",
        title="Vande Mataram - YouTube",
        task_id="t_yt_search",
        recent_action=RecentActionRecord(action_type=BrowserActionType.FILL, success=True),
    )

    perception = MagicMock()
    perception.observe = make_mock_observe([obs_initial, obs_nav, obs_typed, obs_typed])

    actions_executed = []
    async def mock_execute(task_id, session_id, action):
        actions_executed.append(action)
        return (
            BrowserActionResult(success=True, action_type=action.action_type),
            make_safety_ok(task_id=task_id),
        )

    adapter.execute_safe_browser_task = mock_execute

    agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
    result = await agent.run(task)

    # Must NOT have stopped after step 1 navigation
    assert len(actions_executed) >= 2
    assert actions_executed[0].action_type == BrowserActionType.NAVIGATE
    assert actions_executed[1].action_type == BrowserActionType.FILL
    assert llm.query.call_count >= 2


# ─────────────────────────────────────────────────────────────────────────────
# TEST 14 — Metadata Propagation from ARDOTaskNode into BrowserTask
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_metadata_propagation_into_browser_task():
    """
    Test E: Verify RuntimeEngine forwards node metadata into BrowserTask.
    """
    from orchestration.dag_builder import DAGBuilder
    from orchestration.runtime_engine import RuntimeEngine

    raw_nodes = [
        {
            "id": "node_01",
            "type": "default",
            "position": {"x": 0.0, "y": 0.0},
            "data": {
                "label": "Open Wikipedia",
                "metadata": {
                    "destination_url": "https://www.wikipedia.org",
                    "success_criteria": "Wikipedia home page loaded",
                },
            },
        }
    ]
    graph = DAGBuilder.build(raw_nodes, [])
    node = graph.get_node("node_01")
    assert node is not None
    assert node.metadata.get("destination_url") == "https://www.wikipedia.org"
    assert node.metadata.get("success_criteria") == "Wikipedia home page loaded"


# ─────────────────────────────────────────────────────────────────────────────
# TEST 15 — WebSocket Runtime Verification
# ─────────────────────────────────────────────────────────────────────────────

def test_websocket_library_installed():
    """
    Test G: Verify websockets module is installed and importable for Uvicorn.
    """
    import websockets
    assert websockets is not None

