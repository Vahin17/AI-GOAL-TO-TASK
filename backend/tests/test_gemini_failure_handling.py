"""
test_gemini_failure_handling.py — Focused Tests for Gemini Failure Handling & Browser Robustness
================================================================================================
Focused test suite validating:
1. 503 retries at most the configured retry count and raises GeminiServiceUnavailableError.
2. Permanent 429 quota exhaustion causes zero BrowserAgent retry iterations (0 further calls).
3. BrowserAgent stops immediately after permanent quota exhaustion with truthful FAILED outcome.
4. No browser simulation/fake success occurs after Gemini quota exhaustion.
5. Existing successful Gemini decision path remains unchanged.
6. Non-browser tasks are not accidentally affected.
"""

import asyncio
import io
import json
import urllib.error
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from ai.gemini_provider import (
    GeminiProvider,
    GeminiProviderError,
    GeminiAuthenticationError,
    GeminiQuotaExceededError,
    GeminiRateLimitError,
    GeminiServiceUnavailableError,
    _parse_error_body,
)
from execution.browser.agent_contracts import (
    BrowserTask,
    BrowserAgentOutcome,
    BrowserObservation,
    BrowserDecision,
    BrowserDecisionType,
    BrowserActionDecision,
    BrowserVerification,
    BrowserVerificationStatus,
)
from execution.browser.browser_models import BrowserActionType
from execution.browser.browser_agent import BrowserAgent
from execution.browser.perception_models import PageSnapshot, ObservedElement
from orchestration.dag_builder import ARDOTaskNode, ARDOGraph
from orchestration.state_machine import TaskState


def make_http_error(code: int, msg: str = "", body_dict: dict = None, headers: dict = None) -> urllib.error.HTTPError:
    """Helper to construct realistic urllib HTTPError instances."""
    raw_body = json.dumps(body_dict or {}).encode("utf-8") if body_dict else b""
    fp = io.BytesIO(raw_body)
    err = urllib.error.HTTPError(
        url="https://generativelanguage.googleapis.com/v1beta/models/mock:generateContent?key=AIzaSySecret12345",
        code=code,
        msg=msg or f"HTTP {code}",
        hdrs=headers or {},
        fp=fp,
    )
    return err


# ─────────────────────────────────────────────────────────────────────────────
# 1. 503 Retries at most configured retry count
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_1_transient_503_retries_at_most_configured_count():
    """503 retries bounded at max_retries, then raises GeminiServiceUnavailableError."""
    http_503 = make_http_error(
        503,
        body_dict={"error": {"message": "Service unavailable (503) ... high demand", "status": "UNAVAILABLE"}},
    )

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
         patch("urllib.request.urlopen", side_effect=http_503) as mock_urlopen:
        
        provider = GeminiProvider(api_key="test_key", model_name="gemini-3.5-flash", max_retries=2)

        with pytest.raises(GeminiServiceUnavailableError) as exc_info:
            await provider.query("test prompt")

        # 1 initial call + 2 retries = 3 calls total
        assert mock_urlopen.call_count == 3
        # Backoff sleep was called twice
        assert mock_sleep.call_count == 2
        assert "service unavailable (503)" in str(exc_info.value).lower()


# ─────────────────────────────────────────────────────────────────────────────
# 2. Permanent 429 Quota Exhaustion Causes Zero BrowserAgent Retry Iterations
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_2_permanent_429_causes_zero_browser_agent_retry_iterations():
    """Permanent 429 quota exhaustion causes exactly 1 Gemini call and 0 BrowserAgent retries."""
    mock_llm = MagicMock()
    gemini_call_count = 0
    async def mock_query(*args, **kwargs):
        nonlocal gemini_call_count
        gemini_call_count += 1
        raise GeminiQuotaExceededError(
            "Gemini API quota exhausted on model 'gemini-3.5-flash': Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests"
        )
    mock_llm.query = AsyncMock(side_effect=mock_query)

    mock_adapter = MagicMock()
    mock_adapter.ensure_started = AsyncMock()
    mock_perception = MagicMock()
    mock_obs = BrowserObservation(
        session_id="s1",
        task_id="task_001",
        current_url="https://example.com",
        page_title="Test Page",
        page_snapshot=PageSnapshot(title="Test Page", url="https://example.com"),
        observed_elements=[ObservedElement(ref="elem_001", tag="a", text="Click me", css_selector="a#click")],
    )
    mock_perception.observe = AsyncMock(return_value=mock_obs)

    agent = BrowserAgent(llm_provider=mock_llm, adapter=mock_adapter, perception=mock_perception)
    task = BrowserTask(task_id="task_001", objective="Open Wikipedia", session_id="s1", max_actions=10)

    result = await agent.run(task)

    # CRITICAL: Exactly 1 Gemini call was made, zero retry iterations
    assert gemini_call_count == 1
    assert result.outcome == BrowserAgentOutcome.FAILED
    assert "Unrecoverable LLM failure" in result.error
    assert len(result.actions_taken) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 3. BrowserAgent Stops Immediately on Quota Exhaustion
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_3_browser_agent_stops_immediately_on_quota():
    """BrowserAgent immediately returns FAILED result and does not loop through max_actions."""
    events_emitted = []
    
    mock_llm = MagicMock()
    mock_llm.query = AsyncMock(side_effect=GeminiQuotaExceededError("Quota exhausted"))
    mock_adapter = MagicMock()
    mock_adapter.ensure_started = AsyncMock()
    mock_perception = MagicMock()
    mock_obs = BrowserObservation(
        session_id="s1",
        task_id="task_002",
        current_url="about:blank",
        page_title="Blank",
        page_snapshot=PageSnapshot(title="Blank", url="about:blank"),
    )
    mock_perception.observe = AsyncMock(return_value=mock_obs)

    agent = BrowserAgent(
        llm_provider=mock_llm,
        adapter=mock_adapter,
        perception=mock_perception,
        event_callback=lambda evt_type, task, details: events_emitted.append(evt_type),
    )
    task = BrowserTask(task_id="task_002", objective="Search something", session_id="s1", max_actions=5)

    result = await agent.run(task)

    assert result.outcome == BrowserAgentOutcome.FAILED
    assert result.actions_count == 0
    # Emits execution_failed event, NOT recovery_started / recovery_exhausted
    assert "execution_failed" in events_emitted
    assert "recovery_started" not in events_emitted
    assert "recovery_exhausted" not in events_emitted


# ─────────────────────────────────────────────────────────────────────────────
# 4. No Browser Simulation / Fake Success Occurs
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_4_no_browser_simulation_after_quota_exhaustion():
    """When Gemini is quota-exhausted, BrowserAgent never fabricates success or simulates actions."""
    from orchestration.runtime_engine import RuntimeEngine

    mock_llm = MagicMock()
    mock_llm.query = AsyncMock(side_effect=GeminiQuotaExceededError("Quota exhausted"))

    mock_adapter = MagicMock()
    mock_adapter.ensure_started = AsyncMock()
    mock_adapter.execute_safe_browser_task = AsyncMock()
    mock_perception = MagicMock()
    mock_obs = BrowserObservation(
        session_id="s_test",
        task_id="task_b1",
        current_url="https://wikipedia.org",
        page_title="Wikipedia",
        page_snapshot=PageSnapshot(title="Wikipedia", url="https://wikipedia.org"),
    )
    mock_perception.observe = AsyncMock(return_value=mock_obs)

    agent = BrowserAgent(llm_provider=mock_llm, adapter=mock_adapter, perception=mock_perception)

    graph = ARDOGraph()
    node = ARDOTaskNode(
        task_id="task_b1",
        task_name="Open Wikipedia in a new tab",
        status=TaskState.ACTIVE,
    )
    graph.nodes["task_b1"] = node

    engine = RuntimeEngine(session_id="s_test", graph=graph, browser_adapter=mock_adapter)
    engine._browser_agent = agent

    success = await engine._attempt_execution(node)

    assert success is False
    assert node.status == TaskState.FAILED
    assert node.browser_result is not None
    assert node.browser_result.outcome == BrowserAgentOutcome.FAILED
    # Ensure no fake browser action was executed
    assert mock_adapter.execute_safe_browser_task.call_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# 5. Existing Successful Gemini Decision Path Remains Unchanged
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_5_successful_gemini_path_unchanged():
    """When Gemini returns a valid JSON decision, BrowserAgent executes it faithfully."""
    valid_decision_json = json.dumps({
        "decision": "execute",
        "reason": "Navigate to Wikipedia",
        "action": {
            "action_type": "navigate",
            "url": "https://www.wikipedia.org",
            "new_tab": False,
        }
    })

    finish_decision_json = json.dumps({
        "decision": "finish",
        "reason": "Objective reached",
    })

    responses = [valid_decision_json, finish_decision_json]
    
    mock_llm = MagicMock()
    mock_llm.query = AsyncMock(side_effect=responses)

    mock_adapter = MagicMock()
    mock_adapter.ensure_started = AsyncMock()
    
    # Mock action execution
    action_res = MagicMock()
    action_res.success = True
    action_res.action_type = BrowserActionType.NAVIGATE
    action_res.error = None
    action_res.current_url = "https://www.wikipedia.org/"
    decision_mock = MagicMock()
    decision_mock.is_blocked = False
    decision_mock.execution_decision.value = "ALLOW"
    mock_adapter.execute_safe_browser_task = AsyncMock(return_value=(action_res, decision_mock))

    mock_obs = BrowserObservation(
        session_id="s1",
        task_id="task_s1",
        current_url="https://www.wikipedia.org/",
        page_title="Wikipedia, the free encyclopedia",
        page_snapshot=PageSnapshot(title="Wikipedia, the free encyclopedia", url="https://www.wikipedia.org/"),
    )
    mock_perception = MagicMock()
    mock_perception.observe = AsyncMock(return_value=mock_obs)

    agent = BrowserAgent(llm_provider=mock_llm, adapter=mock_adapter, perception=mock_perception)
    task = BrowserTask(task_id="task_s1", objective="Open Wikipedia", session_id="s1", max_actions=5)

    result = await agent.run(task)

    assert result.outcome == BrowserAgentOutcome.COMPLETED
    assert len(result.actions_taken) == 1
    assert result.actions_taken[0].action_type == BrowserActionType.NAVIGATE
    assert result.actions_taken[0].url == "https://www.wikipedia.org"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Non-Browser Tasks Not Accidentally Affected
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_6_non_browser_tasks_not_affected():
    """Non-browser tasks execute through tool registry or simulation normally."""
    from orchestration.runtime_engine import RuntimeEngine

    graph = ARDOGraph()
    node = ARDOTaskNode(
        task_id="task_generic_1",
        task_name="Process Data Computation",
        status=TaskState.ACTIVE,
    )
    graph.nodes["task_generic_1"] = node

    engine = RuntimeEngine(session_id="s_test", graph=graph)

    with patch("random.random", return_value=0.99):  # Will not trigger SIM_FAILURE_RATE (0.15)
        success = await engine._attempt_execution(node)

    assert success is True
    assert node.status == TaskState.COMPLETED
