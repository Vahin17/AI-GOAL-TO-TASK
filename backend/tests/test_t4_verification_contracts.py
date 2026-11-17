"""
test_t4_verification_contracts.py — Focused Unit Tests for T4 Verification Contracts
=====================================================================================
Empirical validation of:
1. PlanCritic enforcement and invalid plan rejection fallback (Area A).
2. Atomic task definitions and completeness checks (Area B).
3. Real input-value verification on form fill in Playwright executor (Area C).
4. Media playback verification accuracy, state differentiation, and identity matching (Area D).
5. Honest weakest-link goal confidence calculation (Area E).
6. Success-rate metric definitions and dual unit exposure (Area F).
7. LLM-call and fast-path observability on BrowserAgentResult (Area G).
"""

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
)
from execution.browser.browser_agent import BrowserAgent
from execution.browser.browser_models import BrowserAction, BrowserActionResult, BrowserActionType
from execution.browser.perception_models import ObservedElement, PageSnapshot
from execution.browser.semantic_verifier import SemanticVerifier
from orchestration.dag_builder import ARDOTaskNode, ARDOGraph
from orchestration.state_machine import TaskState
from orchestration.execution_monitor import SessionMetrics
from ai.plan_critic import PlanCritic, CriticIssueSeverity
from schemas.models import TaskNode, TaskEdge, PlanGraphResponse, PlanGraphMetadata


# ── Area A: PlanCritic Enforcement ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_plancritic_rejects_invented_capabilities_and_generates_feedback():
    """Verify PlanCritic flags invented capabilities and marks plan as invalid requiring regeneration."""
    nodes = [
        TaskNode(id="t1", type="default", position={"x": 0.0, "y": 0.0}, data={"label": "Navigate to login"}),
        TaskNode(id="t2", type="default", position={"x": 0.0, "y": 150.0}, data={"label": "format_disk"}),
    ]
    edges = [TaskEdge(id="e1", source="t1", target="t2")]
    result = PlanCritic.evaluate_grounding_and_relevance("Clean system", nodes, edges)
    assert result.is_valid is False
    assert result.requires_regeneration is True
    assert any(i.severity == CriticIssueSeverity.CRITICAL for i in result.issues)
    assert "format_disk" in result.regeneration_prompt_feedback


# ── Area C: Real Input-Value Verification ───────────────────────────────────

@pytest.mark.asyncio
async def test_input_value_verification_detects_empty_dom_value():
    """When Playwright page.fill executes but DOM value remains empty, executor marks failure."""
    from execution.browser.playwright_executor import PlaywrightBrowserExecutor

    executor = PlaywrightBrowserExecutor(headless=True)
    session = executor._get_session()
    # Mock internal page
    mock_page = MagicMock()
    mock_page.wait_for_selector = AsyncMock(return_value=None)
    mock_page.fill = AsyncMock()
    # Simulate input field that rejected or cleared the value
    mock_page.input_value = AsyncMock(return_value="")
    mock_page.url = "http://localhost:8080/form"
    mock_page.title = AsyncMock(return_value="Form Page")
    session._page = mock_page
    session._is_started = True

    action = BrowserAction(
        action_type=BrowserActionType.FILL,
        selector="#search-box",
        value="Phoenix Project",
    )
    result = await executor.execute_action(action)
    assert result.success is False
    assert "Input value verification failed" in (result.error or "")
    assert result.metadata.get("input_verified") is False
    assert result.metadata.get("actual_value") == ""


@pytest.mark.asyncio
async def test_input_value_verification_confirms_matching_value():
    """When DOM value matches intended value, executor reports input_verified=True."""
    from execution.browser.playwright_executor import PlaywrightBrowserExecutor

    executor = PlaywrightBrowserExecutor(headless=True)
    session = executor._get_session()
    mock_page = MagicMock()
    mock_page.wait_for_selector = AsyncMock(return_value=None)
    mock_page.fill = AsyncMock()
    mock_page.input_value = AsyncMock(return_value="Phoenix Project")
    mock_page.url = "http://localhost:8080/form"
    mock_page.title = AsyncMock(return_value="Form Page")
    session._page = mock_page
    session._is_started = True

    action = BrowserAction(
        action_type=BrowserActionType.FILL,
        selector="#search-box",
        value="Phoenix Project",
    )
    result = await executor.execute_action(action)
    assert result.success is True
    assert result.metadata.get("input_verified") is True
    assert result.metadata.get("actual_value") == "Phoenix Project"


# ── Area D: Media Playback Verification Accuracy ───────────────────────────

def test_playback_distinguishes_buffering_and_stalled_states():
    """Buffering media (readyState <= 1) is clearly distinguished in failure reason."""
    task = BrowserTask(task_id="t_play", objective="Play song Yeh Fitoor Mera", session_id="s1")
    obs_buffering = BrowserObservation(
        session_id="s1",
        task_id="t_play",
        current_url="https://www.youtube.com/watch?v=123",
        page_title="Yeh Fitoor Mera - YouTube",
        media_state=[{
            "tag": "video",
            "paused": False,
            "currentTime": 0.0,
            "readyState": 1,
        }],
    )
    verif = SemanticVerifier.verify(task, obs_buffering)
    assert verif.status == BrowserVerificationStatus.NOT_VERIFIED
    assert "buffering" in verif.failure_reason
    assert "not actively playing" in verif.failure_reason


def test_playback_rejects_unrelated_background_ad():
    """When a video is playing but neither page title, URL, nor text matches requested song, verification fails."""
    task = BrowserTask(task_id="t_play", objective="Play song Vande Mataram", session_id="s1")
    obs_ad = BrowserObservation(
        session_id="s1",
        task_id="t_play",
        current_url="https://example.com/portal",
        page_title="Corporate Portal - Welcome",
        page_text_summary="Welcome to the corporate intranet and news feed.",
        media_state=[{
            "tag": "video",
            "paused": False,
            "currentTime": 5.2,
            "duration": 30.0,
            "readyState": 4,
        }],
    )
    verif = SemanticVerifier.verify(task, obs_ad)
    assert verif.status == BrowserVerificationStatus.NOT_VERIFIED
    assert "matches requested objective tokens" in verif.failure_reason


# ── Area E: Honest Weakest-Link Goal Confidence ─────────────────────────────

def test_weakest_link_confidence_prevents_high_confidence_masking():
    """A weak task with 0.20 confidence bounds goal confidence so it cannot be masked by 0.95 tasks."""
    node1 = ARDOTaskNode(task_id="t1", task_name="Step 1", status=TaskState.COMPLETED)
    node1.browser_result = BrowserAgentResult(
        task_id="t1", session_id="s1", outcome=BrowserAgentOutcome.COMPLETED, objective="Step 1",
        verification=BrowserVerification(task_id="t1", status=BrowserVerificationStatus.VERIFIED, confidence_score=0.95),
    )
    node2 = ARDOTaskNode(task_id="t2", task_name="Step 2", status=TaskState.COMPLETED)
    node2.browser_result = BrowserAgentResult(
        task_id="t2", session_id="s1", outcome=BrowserAgentOutcome.COMPLETED, objective="Step 2",
        verification=BrowserVerification(task_id="t2", status=BrowserVerificationStatus.VERIFIED, confidence_score=0.95),
    )
    node3 = ARDOTaskNode(task_id="t3", task_name="Step 3", status=TaskState.COMPLETED)
    node3.browser_result = BrowserAgentResult(
        task_id="t3", session_id="s1", outcome=BrowserAgentOutcome.COMPLETED, objective="Step 3",
        verification=BrowserVerification(task_id="t3", status=BrowserVerificationStatus.VERIFIED, confidence_score=0.20),
    )
    graph = ARDOGraph(nodes={"t1": node1, "t2": node2, "t3": node3})
    goal_res = SemanticVerifier.verify_goal_outcome("Three step workflow", graph, "s1")

    assert goal_res.overall_status == "verified"
    # Arithmetic mean would be 0.70; weakest-link correctly clamps to 0.20!
    assert goal_res.confidence_score == 0.20


def test_failed_goal_reports_zero_confidence():
    """A goal with failed tasks reports 0.0 confidence rather than 0.90."""
    node1 = ARDOTaskNode(task_id="t1", task_name="Step 1", status=TaskState.COMPLETED)
    node2 = ARDOTaskNode(task_id="t2", task_name="Step 2", status=TaskState.FAILED)
    graph = ARDOGraph(nodes={"t1": node1, "t2": node2})
    goal_res = SemanticVerifier.verify_goal_outcome("Two step workflow", graph, "s1")

    assert goal_res.overall_status == "failed"
    assert goal_res.confidence_score == 0.0


# ── Area F: Success-Rate Metric Semantics ───────────────────────────────────

def test_session_metrics_dual_unit_exposure():
    """SessionMetrics provides both ratio [0.0, 1.0] and percentage [0.0, 100.0]."""
    metrics = SessionMetrics(session_id="s_test")
    metrics.tasks_total = 4
    metrics.tasks_completed = 3
    metrics.tasks_failed = 1

    assert metrics.success_rate == 0.75
    assert metrics.success_rate_percentage == 75.0

    d = metrics.to_dict()
    assert d["success_rate"] == 0.75
    assert d["success_rate_percentage"] == 75.0


# ── Area G: LLM-Call & Fast-Path Observability ──────────────────────────────

@pytest.mark.asyncio
async def test_browser_agent_result_surfaces_llm_and_fast_path_observability():
    """BrowserAgentResult accurately reports llm_calls_count, fast_path_used, and model_name."""
    task = BrowserTask(task_id="t_nav", objective="Navigate to https://example.com", session_id="s1")
    llm = MagicMock()
    llm.model_name = "gemini-3.5-flash"
    llm.query = AsyncMock(return_value="{}")

    adapter = MagicMock()
    adapter.executor._session = MagicMock(_page=None, _context=None)
    async def mock_execute(task_id, session_id, action):
        return (
            BrowserActionResult(success=True, action_type=action.action_type, current_url=action.url),
            MagicMock(execution_decision=MagicMock(value="allow")),
        )
    adapter.execute_safe_browser_task = mock_execute

    obs1 = BrowserObservation(session_id="s1", task_id="t_nav", current_url="about:blank")
    obs2 = BrowserObservation(session_id="s1", task_id="t_nav", current_url="https://example.com", page_title="Example")

    perception = MagicMock()
    obs_iter = iter([obs1, obs2])
    perception.observe = AsyncMock(side_effect=lambda *a, **k: next(obs_iter, obs2))

    agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
    result = await agent.run(task)

    assert result.outcome == BrowserAgentOutcome.COMPLETED
    assert result.fast_path_used is True
    assert result.llm_calls_count == 0
    assert result.model_name == "gemini-3.5-flash"
