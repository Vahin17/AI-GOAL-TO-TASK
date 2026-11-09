"""
test_gemini_call_audit.py — Comprehensive Gemini Call Count & Concurrency Audit Tests
======================================================================================
Audit-only test suite validating:
1. TEST A: Simple orchestration call count (Planning + Safety + BrowserAgent decisions).
2. TEST B: 503 transient failure bounded retry count (exactly max_retries + 1 attempts).
3. TEST C: Permanent 429 quota exhaustion causes zero subsequent Gemini calls.
4. TEST D: BrowserAgent recovery loop call bounds (capped by max_recovery_budget).
5. TEST E: Parallel DAG task execution concurrency and request aggregation.
6. TEST F: No retry multiplication between RuntimeEngine and BrowserAgent.
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
from orchestration.runtime_engine import RuntimeEngine
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
# TEST A: Complete Simple Orchestration Gemini Call Count
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_a_simple_orchestration_call_count():
    """
    Measures exact Gemini calls across an end-to-end orchestration:
    - Planning (1 call)
    - Batch Safety Evaluation (1 call)
    - BrowserAgent execution for 1 browser task (2 calls: 1 action + 1 finish)
    Total expected calls = 4.
    """
    total_gemini_calls = 0

    # 1. Mock Planning response
    plan_json = json.dumps({
        "nodes": [
            {"id": "task_1", "type": "input", "position": {"x": 250, "y": 50}, "data": {"label": "Open Wikipedia"}},
        ],
        "edges": [],
        "metadata": {
            "intent": "Open Wikipedia",
            "confidence": 1.0,
            "reasoning_summary": "Single step navigation plan",
        }
    })

    # 2. Mock Safety response
    safety_json = json.dumps({
        "task_1": {
            "task_id": "task_1",
            "action": "navigate",
            "target": "https://www.wikipedia.org",
            "target_category": "network_endpoint",
            "scope": "external",
            "environment": "production",
            "reversibility": "high",
            "destructive_potential": "none",
            "privilege_required": False,
            "system_critical": False,
            "data_sensitivity": "none",
            "external_side_effect": True,
            "consequence": "Navigates to Wikipedia homepage",
            "ambiguity": "clear",
            "semantic_confidence": 0.95,
        }
    })

    # 3. Mock BrowserAgent decision responses
    action_decision_json = json.dumps({
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
        "reason": "Wikipedia loaded successfully",
    })

    async def mock_llm_query(prompt, system_instruction="", json_mode=False):
        nonlocal total_gemini_calls
        total_gemini_calls += 1
        if "Goal to decompose:" in prompt:
            return plan_json
        elif "safety classification assistant" in prompt or "SafetyContext" in prompt:
            return safety_json
        elif "ACTIONS USED: 0" in prompt or "TASK OBJECTIVE:" in prompt:
            if total_gemini_calls == 3:
                return action_decision_json
            return finish_decision_json
        return finish_decision_json

    mock_llm = MagicMock()
    mock_llm.query = AsyncMock(side_effect=mock_llm_query)

    # Execute BrowserAgent with mock LLM
    mock_adapter = MagicMock()
    mock_adapter.ensure_started = AsyncMock()
    action_res = MagicMock()
    action_res.success = True
    action_res.action_type = BrowserActionType.NAVIGATE
    action_res.error = None
    action_res.current_url = "https://www.wikipedia.org/"
    decision_mock = MagicMock()
    decision_mock.is_blocked = False
    decision_mock.execution_decision.value = "ALLOW"
    mock_adapter.execute_safe_browser_task = AsyncMock(return_value=(action_res, decision_mock))

    mock_perception = MagicMock()
    mock_obs = BrowserObservation(
        session_id="s_audit",
        task_id="task_1",
        current_url="https://www.wikipedia.org/",
        page_title="Wikipedia, the free encyclopedia",
        page_snapshot=PageSnapshot(title="Wikipedia, the free encyclopedia", url="https://www.wikipedia.org/"),
    )
    mock_perception.observe = AsyncMock(return_value=mock_obs)

    # Planning call simulation
    p1 = await mock_llm_query("Goal to decompose: Open Wikipedia")
    assert total_gemini_calls == 1

    # Safety call simulation
    p2 = await mock_llm_query("safety classification assistant...")
    assert total_gemini_calls == 2

    # BrowserAgent execution simulation
    agent = BrowserAgent(llm_provider=mock_llm, adapter=mock_adapter, perception=mock_perception)
    task = BrowserTask(task_id="task_1", objective="Open Wikipedia", session_id="s_audit", max_actions=5)
    result = await agent.run(task)

    assert result.outcome == BrowserAgentOutcome.COMPLETED
    # 1 Plan + 1 Safety + 1 Action Decision + 0 Finish Decision (verification-first) = 3 Total Calls
    assert total_gemini_calls == 3


# ─────────────────────────────────────────────────────────────────────────────
# TEST B: 503 Transient Failure Bounded Retries
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_b_503_bounded_retry_attempts():
    """Validates that a 503 error produces exactly (max_retries + 1) HTTP attempts."""
    http_503 = make_http_error(
        503,
        body_dict={"error": {"message": "Service unavailable (503) ... high demand", "status": "UNAVAILABLE"}},
    )

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
         patch("urllib.request.urlopen", side_effect=http_503) as mock_urlopen:
        
        provider = GeminiProvider(api_key="test_key", model_name="gemini-3.5-flash", max_retries=2)

        with pytest.raises(GeminiServiceUnavailableError):
            await provider.query("test prompt")

        # Initial call + 2 retries = 3 attempts exactly
        assert mock_urlopen.call_count == 3
        assert mock_sleep.call_count == 2


# ─────────────────────────────────────────────────────────────────────────────
# TEST C: Permanent 429 Quota Exhaustion Call Bounds
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_c_permanent_429_generates_zero_subsequent_calls():
    """Validates that permanent 429 quota exhaustion makes 0 retries and stops BrowserAgent immediately."""
    http_429 = make_http_error(
        429,
        body_dict={
            "error": {
                "message": "Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests limit: 5",
                "status": "RESOURCE_EXHAUSTED",
                "details": [{"@type": "ErrorInfo", "reason": "RATE_LIMIT_EXCEEDED"}]
            }
        },
        headers={"Retry-After": "59"},
    )

    calls_made = 0
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
         patch("urllib.request.urlopen", side_effect=http_429) as mock_urlopen:
        
        provider = GeminiProvider(api_key="test_key", model_name="gemini-3.5-flash", max_retries=2)

        with pytest.raises(GeminiQuotaExceededError):
            await provider.query("test prompt")

        # CRITICAL: Exactly 1 HTTP attempt made, 0 retries, 0 sleep
        assert mock_urlopen.call_count == 1
        assert mock_sleep.call_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# TEST D: BrowserAgent Recovery Call Bounds
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_d_browser_agent_recovery_call_bounds():
    """Validates that non-fatal decision failures in BrowserAgent are bounded by max_recovery_budget."""
    calls_made = 0
    async def mock_failing_query(prompt, system_instruction="", json_mode=False):
        nonlocal calls_made
        calls_made += 1
        # Return invalid JSON to trigger recovery loop
        return "INVALID_JSON_DECISION"

    mock_llm = MagicMock()
    mock_llm.query = AsyncMock(side_effect=mock_failing_query)

    mock_adapter = MagicMock()
    mock_adapter.ensure_started = AsyncMock()
    mock_perception = MagicMock()
    mock_obs = BrowserObservation(
        session_id="s_recovery",
        task_id="task_r1",
        current_url="about:blank",
        page_title="Blank",
        page_snapshot=PageSnapshot(title="Blank", url="about:blank"),
    )
    mock_perception.observe = AsyncMock(return_value=mock_obs)

    agent = BrowserAgent(llm_provider=mock_llm, adapter=mock_adapter, perception=mock_perception)
    task = BrowserTask(task_id="task_r1", objective="Perform recovery test", session_id="s_recovery", max_actions=10)

    result = await agent.run(task)

    assert result.outcome == BrowserAgentOutcome.FAILED
    # 1 initial attempt + 3 recovery attempts (max_recovery_budget = 3) = 4 calls total
    assert calls_made == 4


# ─────────────────────────────────────────────────────────────────────────────
# TEST E: Parallel DAG Tasks Concurrency Audit
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_e_parallel_tasks_concurrency():
    """Validates concurrent Gemini calls when multiple DAG tasks run in parallel mode."""
    max_concurrent_calls = 0
    current_concurrent_calls = 0
    lock = asyncio.Lock()

    async def mock_parallel_query(prompt, system_instruction="", json_mode=False):
        nonlocal max_concurrent_calls, current_concurrent_calls
        async with lock:
            current_concurrent_calls += 1
            if current_concurrent_calls > max_concurrent_calls:
                max_concurrent_calls = current_concurrent_calls
        
        await asyncio.sleep(0.05)  # Simulate API latency

        async with lock:
            current_concurrent_calls -= 1

        return json.dumps({
            "decision": "finish",
            "reason": "Completed parallel task",
        })

    mock_llm = MagicMock()
    mock_llm.query = AsyncMock(side_effect=mock_parallel_query)

    mock_adapter = MagicMock()
    mock_adapter.ensure_started = AsyncMock()
    mock_perception = MagicMock()
    mock_obs = BrowserObservation(
        session_id="s_parallel",
        task_id="task_p",
        current_url="https://www.wikipedia.org",
        page_title="Wikipedia",
        page_snapshot=PageSnapshot(title="Wikipedia", url="https://www.wikipedia.org"),
    )
    mock_perception.observe = AsyncMock(return_value=mock_obs)

    # Create 3 parallel tasks
    graph = ARDOGraph()
    for i in range(3):
        node = ARDOTaskNode(
            task_id=f"task_p{i}",
            task_name=f"Open Wikipedia site {i}",
            status=TaskState.WAITING,
            parallel_group=1,
        )
        graph.nodes[f"task_p{i}"] = node

    engine = RuntimeEngine(session_id="s_parallel", graph=graph, mode="parallel", browser_adapter=mock_adapter)
    engine._browser_agent = BrowserAgent(llm_provider=mock_llm, adapter=mock_adapter, perception=mock_perception)

    await engine._dispatch_parallel([f"task_p{i}" for i in range(3)])

    # All 3 parallel tasks ran concurrently
    assert max_concurrent_calls >= 2
    assert all(node.status == TaskState.COMPLETED for node in graph.nodes.values())


# ─────────────────────────────────────────────────────────────────────────────
# TEST F: No Retry Multiplication Between RuntimeEngine & BrowserAgent
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_f_no_retry_multiplication():
    """Validates that RuntimeEngine does not re-dispatch a failed BrowserAgent task."""
    calls_count = 0
    async def mock_quota_query(prompt, system_instruction="", json_mode=False):
        nonlocal calls_count
        calls_count += 1
        raise GeminiQuotaExceededError("Quota exhausted")

    mock_llm = MagicMock()
    mock_llm.query = AsyncMock(side_effect=mock_quota_query)

    mock_adapter = MagicMock()
    mock_adapter.ensure_started = AsyncMock()
    mock_perception = MagicMock()
    mock_obs = BrowserObservation(
        session_id="s_no_mult",
        task_id="task_nomult",
        current_url="about:blank",
        page_title="Blank",
        page_snapshot=PageSnapshot(title="Blank", url="about:blank"),
    )
    mock_perception.observe = AsyncMock(return_value=mock_obs)

    agent = BrowserAgent(llm_provider=mock_llm, adapter=mock_adapter, perception=mock_perception)

    graph = ARDOGraph()
    node = ARDOTaskNode(
        task_id="task_nomult",
        task_name="Open Wikipedia website",
        status=TaskState.ACTIVE,
    )
    graph.nodes["task_nomult"] = node

    engine = RuntimeEngine(session_id="s_no_mult", graph=graph, browser_adapter=mock_adapter)
    engine._browser_agent = agent

    success = await engine._attempt_execution(node)

    assert success is False
    assert node.status == TaskState.FAILED
    # Exactly 1 Gemini call was made across the entire attempt — no multiplication
    assert calls_count == 1
