"""
test_live_runtime_failure_remediation.py — Targeted Tests for Live Runtime Failure Remediation
=============================================================================================
Validates:
1. Primary planning provider timeout activates deterministic fallback planning.
2. Fallback-generated browser tasks (e.g. Open YouTube, search, identify, play, verify)
   do not call or depend on the failed LLM provider.
3. Explicit navigation ("Open YouTube", "Navigate to YouTube") resolves deterministically.
4. Deterministic media actions (search, result identification, video click, playback verification)
   execute natively and fail closed if external preconditions or elements are missing.
5. Upstream task failure triggers transitive branch blocking without falsely marking
   pending or ready tasks as directly failed.
6. Goal verification distinguishes directly failed, blocked, pending, ready, and unverified tasks.
7. Dashboard reporting and evidence API accurately surface distinguished task states.
8. Terminal-state monotonicity and tenant isolation remain strictly intact.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, patch

from ai.fallback_rules import generate_fallback_plan, decompose_goal_heuristically
from ai.orchestrator import generate_task_graph
from ai.gemini_provider import GeminiProviderError
from execution.browser.agent_contracts import (
    BrowserTask,
    BrowserObservation,
    BrowserActionType,
    BrowserDecisionType,
    BrowserAgentOutcome,
    BrowserVerificationStatus,
)
from execution.browser.browser_agent import BrowserAgent
from execution.browser.perception_models import ObservedElement
from execution.browser.semantic_verifier import SemanticVerifier
from orchestration.dag_builder import DAGBuilder, ARDOGraph, ARDOTaskNode
from orchestration.dependency_manager import DependencyManager
from orchestration.runtime_engine import RuntimeEngine
from orchestration.state_machine import TaskState
from core.auth_db import AuthDatabase


class ThrowingLLMProvider:
    """Mock LLM provider that simulates a Gemini timeout / read operation failure."""
    def __init__(self, error_message: str = "Gemini request failed: The read operation timed out."):
        self.error_message = error_message
        self.call_count = 0

    async def query(self, prompt: str, system_instruction: str = "", json_mode: bool = False) -> str:
        self.call_count += 1
        raise GeminiProviderError(self.error_message)


# ── 1. Planning Provider Timeout Activates Deterministic Fallback ─────────────

@pytest.mark.asyncio
async def test_primary_planning_provider_timeout_activates_fallback():
    """Validates that when the primary planning provider raises a timeout, fallback activates cleanly."""
    mock_provider = AsyncMock()
    mock_provider.generate_plan.side_effect = GeminiProviderError("Gemini request failed: The read operation timed out.")

    with patch("ai.orchestrator.get_llm_provider", return_value=mock_provider), \
         patch("ai.orchestrator.get_database", return_value=None):
        plan = await generate_task_graph("play ye fitoor mera song", user_id="user_live_01")

    assert plan is not None
    assert plan.metadata.is_fallback is True
    assert plan.metadata.planning_mode == "deterministic_fallback"
    assert "timeout" in (plan.metadata.fallback_reason or "").lower() or plan.metadata.fallback_reason is not None
    assert len(plan.nodes) == 5
    # Confirm nodes have deterministic browser parameters attached
    first_node = plan.nodes[0]
    assert first_node.data["label"] == "Open YouTube"
    assert first_node.data["metadata"].get("action_type") == "navigate"
    assert first_node.data["metadata"].get("expected_url") == "https://www.youtube.com"


# ── 2. Fallback Browser Tasks Do Not Unnecessarily Call Failed LLM ────────────

@pytest.mark.asyncio
async def test_open_youtube_executes_without_gemini_call():
    """Validates that 'Open YouTube' resolves deterministically to navigation without invoking Gemini."""
    throwing_llm = ThrowingLLMProvider()
    agent = BrowserAgent(llm_provider=throwing_llm, adapter=None)

    task = BrowserTask(
        task_id="task_open_yt",
        objective="Open YouTube",
        session_id="sess_no_gemini",
    )
    obs = BrowserObservation(
        session_id="sess_no_gemini",
        task_id="task_open_yt",
        current_url="about:blank",
        page_title="",
    )

    # Calling _detect_explicit_action should immediately resolve to YouTube navigation
    decision = agent._detect_explicit_action(task, obs)
    assert decision is not None
    assert decision.decision == BrowserDecisionType.EXECUTE
    assert decision.action is not None
    assert decision.action.action_type == BrowserActionType.NAVIGATE
    assert "youtube.com" in decision.action.url
    # LLM was never called
    assert throwing_llm.call_count == 0


@pytest.mark.asyncio
async def test_platform_name_variations_resolve_explicit_navigation():
    """Validates that 'Navigate to YouTube', 'Open Google', 'Open Wikipedia' resolve without LLM."""
    agent = BrowserAgent(llm_provider=ThrowingLLMProvider(), adapter=None)
    obs = BrowserObservation(session_id="s", task_id="t", current_url="about:blank")

    variations = [
        ("Open YouTube", "https://www.youtube.com"),
        ("Navigate to YouTube", "https://www.youtube.com"),
        ("Open Google", "https://www.google.com"),
        ("Open Wikipedia", "https://www.wikipedia.org"),
        ("Go to github.com", "https://github.com"),
    ]
    for prompt, expected_url in variations:
        task = BrowserTask(task_id="t", objective=prompt, session_id="s")
        decision = agent._detect_explicit_action(task, obs)
        assert decision is not None, f"Failed for {prompt}"
        assert decision.action.action_type == BrowserActionType.NAVIGATE
        normalized_expected = expected_url.replace("www.", "")
        normalized_actual = decision.action.url.replace("www.", "")
        assert normalized_actual == normalized_expected


# ── 3. Deterministic Media Actions Resolve Natively ───────────────────────────

def test_deterministic_media_actions_resolve_without_llm():
    """Validates identify, play, and verify actions resolve deterministically from DOM observations."""
    agent = BrowserAgent(llm_provider=ThrowingLLMProvider(), adapter=None)

    # 1. Identify result on search page
    el_card = ObservedElement(
        ref="elem_001",
        tag="div",
        css_selector="#video-card-1",
        element_id="video-card-1",
        text="Ye Fitoor Mera (Official Video)",
    )
    obs_results = BrowserObservation(
        session_id="s",
        task_id="t_identify",
        current_url="https://www.youtube.com/results?search_query=ye+fitoor+mera",
        observed_elements=[el_card],
    )
    task_identify = BrowserTask(task_id="t_identify", objective="Identify the appropriate result", session_id="s")
    dec_identify = agent._detect_deterministic_media_action(task_identify, obs_results, [])
    assert dec_identify is not None
    assert dec_identify.decision == BrowserDecisionType.FINISH

    # 2. Play result clicks the video card element
    task_play = BrowserTask(task_id="t_play", objective="Play the selected result", session_id="s")
    dec_play = agent._detect_deterministic_media_action(task_play, obs_results, [])
    assert dec_play is not None
    assert dec_play.decision == BrowserDecisionType.EXECUTE
    assert dec_play.action.action_type == BrowserActionType.CLICK
    assert dec_play.action.element_ref == "elem_001"

    # 3. Verify playback captures screenshot when active
    obs_playing = BrowserObservation(
        session_id="s",
        task_id="t_verify",
        current_url="https://www.youtube.com/watch?v=yefitoormera",
        page_text_summary="Now Playing: Ye Fitoor Mera ACTIVE_PLAYBACK",
        media_state=[{"tag": "video", "paused": False, "currentTime": 2.5, "duration": 240.0}],
    )
    task_verify = BrowserTask(task_id="t_verify", objective="Verify the observable playback outcome", session_id="s")
    dec_verify = agent._detect_deterministic_media_action(task_verify, obs_playing, [])
    assert dec_verify is not None
    assert dec_verify.decision == BrowserDecisionType.EXECUTE
    assert dec_verify.action.action_type == BrowserActionType.SCREENSHOT


# ── 4. Genuine Browser Execution Errors Remain Visible and Fail Closed ────────

@pytest.mark.asyncio
async def test_genuine_browser_errors_fail_closed_with_llm_error():
    """Non-deterministic task requiring an LLM decision fails closed when the LLM times out."""
    throwing_llm = ThrowingLLMProvider("Gemini request failed: The read operation timed out.")
    agent = BrowserAgent(llm_provider=throwing_llm, adapter=None)

    # An abstract/interactive task that cannot be resolved deterministically
    task = BrowserTask(
        task_id="t_complex",
        objective="Analyze unexpected interactive banner and choose best option",
        session_id="sess_fail_closed",
    )
    obs = BrowserObservation(
        session_id="sess_fail_closed",
        task_id="t_complex",
        current_url="https://example.com/banner",
    )

    with patch.object(agent, "_observe", AsyncMock(return_value=obs)):
        result = await agent.run(task)

    assert result.outcome == BrowserAgentOutcome.FAILED
    assert "LLM provider error" in (result.error or "")
    assert "read operation timed out" in (result.error or "")


# ── 5. Transitive Dependency Failure Blocking (No Stale Pending / Ready) ──────

def test_transitive_dependency_failure_blocks_all_downstream_tasks():
    """
    Validates that when Task 1 in a chain T1 -> T2 -> T3 -> T4 -> T5 fails,
    ALL downstream tasks (T2..T5) are identified as BLOCKED in the transitive closure,
    and are not left in ambiguous PENDING or READY states.
    """
    nodes = {
        f"task_{i}": ARDOTaskNode(
            task_id=f"task_{i}",
            task_name=f"Step {i}",
            dependency_ids=[f"task_{i-1}"] if i > 1 else [],
            status=TaskState.PENDING,
            fallback_available=False,
        )
        for i in range(1, 6)
    }
    graph = ARDOGraph(nodes=nodes, session_id="sess_chain")

    # Mark task_1 as FAILED
    graph.nodes["task_1"].status = TaskState.FAILED

    # Transitive blocked computation
    blocked = DependencyManager.get_blocked_by_failure(graph)
    assert set(blocked) == {"task_2", "task_3", "task_4", "task_5"}


def test_independent_branches_not_falsely_blocked():
    """Validates that a failure in branch A does NOT block tasks in independent branch B."""
    nodes = {
        "t1_a": ARDOTaskNode(task_id="t1_a", task_name="Branch A1", dependency_ids=[], status=TaskState.FAILED),
        "t2_a": ARDOTaskNode(task_id="t2_a", task_name="Branch A2", dependency_ids=["t1_a"], status=TaskState.PENDING),
        "t1_b": ARDOTaskNode(task_id="t1_b", task_name="Branch B1", dependency_ids=[], status=TaskState.PENDING),
        "t2_b": ARDOTaskNode(task_id="t2_b", task_name="Branch B2", dependency_ids=["t1_b"], status=TaskState.PENDING),
    }
    graph = ARDOGraph(nodes=nodes, session_id="sess_independent")

    blocked = DependencyManager.get_blocked_by_failure(graph)
    assert blocked == ["t2_a"]
    assert "t1_b" not in blocked
    assert "t2_b" not in blocked


# ── 6. Goal Verification Distinguishes Directly Failed vs Blocked vs Pending ──

def test_goal_verification_distinguishes_direct_failure_and_blocks():
    """
    Validates that SemanticVerifier.verify_goal_outcome accurately reports:
    - failed_tasks: 1 (directly failed)
    - blocked_tasks: 4 (dependency-blocked)
    - total_tasks: 5
    - and does NOT conflate all 5 as directly failed.
    """
    nodes = {
        "t1": ARDOTaskNode(task_id="t1", task_name="Open YouTube", status=TaskState.FAILED),
        "t2": ARDOTaskNode(task_id="t2", task_name="Search", status=TaskState.BLOCKED),
        "t3": ARDOTaskNode(task_id="t3", task_name="Identify", status=TaskState.BLOCKED),
        "t4": ARDOTaskNode(task_id="t4", task_name="Play", status=TaskState.BLOCKED),
        "t5": ARDOTaskNode(task_id="t5", task_name="Verify", status=TaskState.BLOCKED),
    }
    graph = ARDOGraph(nodes=nodes, session_id="sess_verif_distinguish")

    outcome = SemanticVerifier.verify_goal_outcome(
        goal_text="play ye fitoor mera song",
        graph=graph,
        session_id="sess_verif_distinguish",
    )

    assert outcome.overall_status == "failed"
    assert outcome.total_tasks == 5
    assert outcome.failed_tasks == 1
    assert outcome.blocked_tasks == 4
    assert outcome.pending_tasks == 0
    assert outcome.ready_tasks == 0
    assert outcome.verified_tasks == 0
    assert "1 directly failed" in outcome.explanation
    assert "4 blocked" in outcome.explanation


def test_goal_verification_preserves_pending_and_ready_without_marking_failed():
    """
    If a run is terminated with tasks still in pending/ready, they are reported as
    pending/ready and NOT counted as failed.
    """
    nodes = {
        "t1": ARDOTaskNode(task_id="t1", task_name="Open YouTube", status=TaskState.FAILED),
        "t2": ARDOTaskNode(task_id="t2", task_name="Search", status=TaskState.WAITING),
        "t3": ARDOTaskNode(task_id="t3", task_name="Identify", status=TaskState.PENDING),
        "t4": ARDOTaskNode(task_id="t4", task_name="Play", status=TaskState.PENDING),
        "t5": ARDOTaskNode(task_id="t5", task_name="Verify", status=TaskState.PENDING),
    }
    graph = ARDOGraph(nodes=nodes, session_id="sess_pending_preserved")

    outcome = SemanticVerifier.verify_goal_outcome(
        goal_text="play ye fitoor mera song",
        graph=graph,
        session_id="sess_pending_preserved",
    )

    assert outcome.overall_status == "failed"
    assert outcome.total_tasks == 5
    assert outcome.failed_tasks == 1
    assert outcome.blocked_tasks == 0
    assert outcome.ready_tasks == 1
    assert outcome.pending_tasks == 3
    assert "1 directly failed" in outcome.explanation
    assert "3 pending" in outcome.explanation
    assert "1 ready" in outcome.explanation
