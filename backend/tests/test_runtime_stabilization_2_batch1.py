"""
test_runtime_stabilization_2_batch1.py — Batch 1 Stabilization Tests
=====================================================================
Covers:
- Priority 0 Issue A: Shared process-wide Gemini API rate limiter & multi-session concurrency.
- Priority 0 Issue B: Correct structured Gemini HTTP 429 classification (transient vs permanent quota).
- Priority 0 Issue C: Deterministic navigation & search execution saving Gemini calls (0 LLM calls).
- Priority 0 Issue D: PyMongo Database truthiness regression test using real MongoClient (no mocks).
"""

import asyncio
import time
import pytest
import pymongo
from unittest.mock import AsyncMock, MagicMock, patch

from core.config import settings
from ai.gemini_rate_limiter import GeminiSharedRateLimiter, GeminiRateLimitError
from ai.gemini_provider import (
    GeminiProvider,
    _parse_error_body,
    GeminiQuotaExceededError,
)
from execution.browser.agent_contracts import (
    BrowserAgentOutcome,
    BrowserDecisionType,
    BrowserObservation,
    BrowserTask,
    BrowserVerification,
    BrowserVerificationStatus,
    RecentActionRecord,
)
from execution.browser.browser_agent import BrowserAgent
from execution.browser.browser_models import BrowserAction, BrowserActionResult, BrowserActionType
from execution.browser.browser_task_adapter import BrowserTaskAdapter
from execution.browser.environment_perception import EnvironmentPerceptionEngine
from execution.browser.perception_models import ObservedElement, PageElement
from orchestration.dag_builder import DAGBuilder, ARDOTaskNode, ARDOGraph
from orchestration.runtime_engine import RuntimeEngine
from orchestration.state_machine import TaskState


# ─────────────────────────────────────────────────────────────────────────────
# 1. ISSUE A: Shared Process-Wide Rate Limiter Tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_issue_a_shared_limiter_process_singleton():
    """Verify GeminiSharedRateLimiter maintains a single process-wide singleton instance."""
    limiter1 = GeminiSharedRateLimiter.get_instance()
    limiter2 = GeminiSharedRateLimiter.get_instance()
    assert limiter1 is limiter2


@pytest.mark.asyncio
async def test_issue_a_shared_limiter_concurrency_multi_session():
    """
    Prove parallel sessions share the same process-wide request budget.
    With RPM=4, 5 concurrent tasks requesting slots within <1s window:
    - 4 acquire immediately.
    - The 5th is throttled or must wait for the sliding window.
    """
    # Create test limiter with RPM=4 and a 2.0s rolling window for fast testing
    limiter = GeminiSharedRateLimiter(rpm=4, window_seconds=2.0)
    limiter.reset()

    acquired_timestamps = []

    async def session_request(session_id: str):
        t_wait = await limiter.acquire(timeout=5.0)
        acquired_timestamps.append((session_id, time.monotonic(), t_wait))

    # Launch 4 requests in parallel (should all succeed immediately)
    tasks = [session_request(f"session_{i}") for i in range(4)]
    await asyncio.gather(*tasks)

    assert len(acquired_timestamps) == 4
    for _, _, t_wait in acquired_timestamps:
        assert t_wait == 0.0

    # 5th request from a DIFFERENT session must be throttled because capacity (4/4) is exhausted
    status = limiter.get_status()
    assert status["current_window_requests"] == 4
    assert status["remaining_capacity"] == 0

    # Now verify 5th request raises timeout error if budget is too small
    with pytest.raises(GeminiRateLimitError) as exc_info:
        await limiter.acquire(timeout=0.05)
    assert "exceeds timeout" in str(exc_info.value)
    assert exc_info.value.is_transient is True


@pytest.mark.asyncio
async def test_issue_a_shared_limiter_block_for_provider_backoff():
    """
    Verify block_for(seconds) blocks all sessions from acquiring slots
    until provider-mandated backoff has elapsed.
    """
    limiter = GeminiSharedRateLimiter(rpm=10, window_seconds=60.0)
    limiter.reset()

    # Provider issues 0.3s backoff
    limiter.block_for(0.3)
    status = limiter.get_status()
    assert status["is_blocked"] is True
    assert status["blocked_for_seconds"] > 0

    t0 = time.monotonic()
    waited = await limiter.acquire(timeout=2.0)
    t1 = time.monotonic()

    assert waited >= 0.25
    assert (t1 - t0) >= 0.25
    assert limiter.get_status()["is_blocked"] is False


# ─────────────────────────────────────────────────────────────────────────────
# 2. ISSUE B: Structured Gemini 429 Classification Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_issue_b_transient_429_with_structured_retry_info():
    """
    Captured sample format:
    Status 429 with Google RPC RetryInfo (retryDelay: "51.236756038s")
    must be classified as transient rate limit (is_permanent_quota=False).
    """
    body = {
        "error": {
            "code": 429,
            "message": "Resource has been exhausted (e.g. check quota). Please retry in 51.236756038s.",
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": "51.236756038s"
                },
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": "RATE_LIMIT_EXCEEDED",
                    "domain": "googleapis.com",
                    "metadata": {
                        "quota_metric": "generativelanguage.googleapis.com/generate_content_requests",
                        "quota_limit": "5"
                    }
                }
            ]
        }
    }
    import json
    raw_bytes = json.dumps(body).encode("utf-8")
    err_msg, err_status, retry_after, is_permanent_quota = _parse_error_body(
        status_code=429,
        headers={"Content-Type": "application/json"},
        raw_bytes=raw_bytes,
    )

    assert err_status == "RESOURCE_EXHAUSTED"
    assert retry_after == pytest.approx(51.2367, abs=0.01)
    assert is_permanent_quota is False, "Must NOT classify transient RPM limit as permanent quota"


def test_issue_b_conservative_text_regex_retry_fallback():
    """
    When structured details are absent or incomplete, conservative parsing
    extracts retry delay from 'retry in Ns' text and marks as transient.
    """
    body = {
        "error": {
            "code": 429,
            "message": "Quota exceeded. Please retry in 30.5s.",
            "status": "RESOURCE_EXHAUSTED",
            "details": []
        }
    }
    import json
    raw_bytes = json.dumps(body).encode("utf-8")
    err_msg, err_status, retry_after, is_permanent_quota = _parse_error_body(
        status_code=429,
        headers={},
        raw_bytes=raw_bytes,
    )
    assert retry_after == pytest.approx(30.5, abs=0.1)
    assert is_permanent_quota is False


def test_issue_b_permanent_daily_or_billing_exhaustion():
    """
    Exhaustion indicating daily quota or billing required must be classified as permanent.
    """
    body = {
        "error": {
            "code": 429,
            "message": "Resource has been exhausted. Check your plan and billing details.",
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": "BILLING_DISABLED",
                    "metadata": {
                        "quota_metric": "per_day_requests",
                    }
                }
            ]
        }
    }
    import json
    raw_bytes = json.dumps(body).encode("utf-8")
    err_msg, err_status, retry_after, is_permanent_quota = _parse_error_body(
        status_code=429,
        headers={},
        raw_bytes=raw_bytes,
    )
    assert is_permanent_quota is True


def test_issue_b_header_retry_after_extraction():
    """Retry-After header takes precedence and marks as transient."""
    err_msg, err_status, retry_after, is_permanent = _parse_error_body(
        status_code=429,
        headers={"Retry-After": "45"},
        raw_bytes=b"Too Many Requests",
    )
    assert retry_after == 45.0
    assert is_permanent is False


# ─────────────────────────────────────────────────────────────────────────────
# 3. ISSUE C: Deterministic Navigation & Search (0 LLM Calls)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_issue_c_deterministic_navigation_zero_llm_calls():
    """
    When a validated plan provides a destination URL (or explicit domain),
    BrowserAgent navigates directly via Playwright without calling Gemini.
    """
    mock_llm = AsyncMock()
    mock_adapter = MagicMock()
    safety_ok = MagicMock()
    safety_ok.execution_decision = MagicMock()
    safety_ok.execution_decision.value = "allow"

    async def mock_execute(task_id, session_id, action):
        return (
            BrowserActionResult(success=True, action_type=action.action_type, current_url=action.url or "https://www.youtube.com/"),
            safety_ok,
        )

    mock_adapter.execute_safe_browser_task = mock_execute

    mock_perception = MagicMock()
    # Observation before navigation: blank
    obs_initial = BrowserObservation(
        session_id="s_test",
        task_id="t_nav",
        current_url="about:blank",
        page_title="",
        observed_elements=[],
    )
    # Observation after navigation: target reached
    obs_post = BrowserObservation(
        session_id="s_test",
        task_id="t_nav",
        current_url="https://www.youtube.com/",
        page_title="YouTube",
        observed_elements=[],
    )
    mock_perception.observe = AsyncMock(side_effect=[obs_initial, obs_post])

    agent = BrowserAgent(llm_provider=mock_llm, adapter=mock_adapter, perception=mock_perception)

    task = BrowserTask(
        task_id="t_nav",
        session_id="s_test",
        objective="Navigate to https://www.youtube.com",
        metadata={"expected_url": "https://www.youtube.com"},
    )

    result = await agent.run(task)

    assert result.outcome == BrowserAgentOutcome.COMPLETED
    assert result.error is None
    # CRITICAL: 0 LLM calls must have been made!
    assert agent._llm_call_count == 0
    assert mock_llm.query.call_count == 0
    # Verifies direct navigation action was executed
    assert len(result.actions_taken) == 1
    assert result.actions_taken[0].action_type == BrowserActionType.NAVIGATE
    assert result.actions_taken[0].url == "https://www.youtube.com"


@pytest.mark.asyncio
async def test_issue_c_deterministic_search_zero_llm_calls():
    """
    For a recognized search task:
    1. Locates search input.
    2. Fills query deterministically.
    3. Submits via Enter deterministically.
    4. Verifies results state and completes.
    Total Gemini calls = 0.
    """
    mock_llm = AsyncMock()
    mock_adapter = MagicMock()
    safety_ok = MagicMock()
    safety_ok.execution_decision = MagicMock()
    safety_ok.execution_decision.value = "allow"

    async def mock_execute(task_id, session_id, action):
        curr_url = "https://www.youtube.com"
        if action.action_type == BrowserActionType.PRESS:
            curr_url = "https://www.youtube.com/results?search_query=Yeh+Fitoor+Mera"
        return (
            BrowserActionResult(success=True, action_type=action.action_type, current_url=curr_url),
            safety_ok,
        )

    mock_adapter.execute_safe_browser_task = mock_execute

    search_input = ObservedElement(
        ref="elem_001",
        tag="input",
        name="search_query",
        role="searchbox",
        placeholder="Search",
        css_selector="input#search",
        value="",
    )

    # State 1: on youtube.com homepage, search input empty
    obs1 = BrowserObservation(
        session_id="s_test",
        task_id="t_search",
        current_url="https://www.youtube.com",
        page_title="YouTube",
        observed_elements=[search_input],
    )

    # State 2: query filled in search input
    filled_input = search_input.model_copy(update={"value": "Yeh Fitoor Mera"})
    obs2 = BrowserObservation(
        session_id="s_test",
        task_id="t_search",
        current_url="https://www.youtube.com",
        page_title="YouTube",
        observed_elements=[filled_input],
    )

    # State 3: results page reached
    obs3 = BrowserObservation(
        session_id="s_test",
        task_id="t_search",
        current_url="https://www.youtube.com/results?search_query=Yeh+Fitoor+Mera",
        page_title="Yeh Fitoor Mera - YouTube",
        page_text_summary="Results for Yeh Fitoor Mera song",
        observed_elements=[],
    )

    mock_perception = MagicMock()
    mock_perception.observe = AsyncMock(side_effect=[obs1, obs2, obs3, obs3])

    agent = BrowserAgent(llm_provider=mock_llm, adapter=mock_adapter, perception=mock_perception)

    task = BrowserTask(
        task_id="t_search",
        session_id="s_test",
        objective="Search for Yeh Fitoor Mera on YouTube",
    )

    result = await agent.run(task)

    assert result.outcome == BrowserAgentOutcome.COMPLETED
    assert result.error is None
    # 0 LLM calls!
    assert agent._llm_call_count == 0
    assert mock_llm.query.call_count == 0
    # Actions taken: FILL followed by PRESS Enter
    assert len(result.actions_taken) >= 2
    assert result.actions_taken[0].action_type == BrowserActionType.FILL
    assert result.actions_taken[0].value == "Yeh Fitoor Mera"
    assert result.actions_taken[1].action_type == BrowserActionType.PRESS
    assert result.actions_taken[1].key == "Enter"


# ─────────────────────────────────────────────────────────────────────────────
# 4. ISSUE D: Real PyMongo Database Truthiness Regression Test
# ─────────────────────────────────────────────────────────────────────────────

def test_issue_d_real_pymongo_database_bool_raises_not_implemented():
    """
    Requirement (b): Use a REAL pymongo Database object (MongoClient is lazy,
    so MongoClient('mongodb://localhost:1')['test'] needs no server).
    Assert that bool(db) raises NotImplementedError.
    Do NOT rely on mocks.
    """
    client = pymongo.MongoClient("mongodb://localhost:1", serverSelectionTimeoutMS=100)
    db = client["test_ardo_truthiness"]

    # 1. Assert that bool(db) strictly raises NotImplementedError
    with pytest.raises(NotImplementedError) as exc_info:
        _ = bool(db)

    err_text = str(exc_info.value)
    assert "Database objects do not implement truth value testing or bool()" in err_text
    assert "Please compare with None instead: database is not None" in err_text


@pytest.mark.asyncio
async def test_issue_d_runtime_engine_handles_real_pymongo_db_without_error(caplog):
    """
    Verify RuntimeEngine._persist_semantic_memory() handles a real PyMongo Database
    object without raising NotImplementedError or logging a warning about bool(db).
    """
    client = pymongo.MongoClient("mongodb://localhost:1", serverSelectionTimeoutMS=100)
    real_db = client["test_truthiness_db"]

    graph = ARDOGraph()
    node = ARDOTaskNode(
        task_id="t1",
        task_name="Navigate to https://example.com",
        status=TaskState.COMPLETED,
    )
    graph.nodes[node.task_id] = node

    engine = RuntimeEngine(graph=graph, session_id="s_truthiness_test")
    # Attach real PyMongo Database object to engine
    engine.db = real_db
    engine.goal_id = "goal_test_001"

    # Must execute cleanly without raising NotImplementedError
    import logging
    with caplog.at_level(logging.WARNING):
        engine._persist_semantic_memory()

    # Verify no truthiness warnings in captured logs
    for record in caplog.records:
        assert "Database objects do not implement truth value testing or bool()" not in record.message
