"""
test_offline_chromium_acceptance.py — Real Offline Chromium Acceptance Trials
=============================================================================
Validates genuine, evidence-backed browser execution and goal verification on a local
HTML fixture (mock_youtube.html) without consuming Gemini quota or accessing external networks.

Includes:
1. Direct PlaywrightBrowserExecutor + SemanticVerifier acceptance trial.
2. Negative control: Completed tasks without empirical evidence yield 'unverified' (0.0 confidence).
3. DEF-1C-08 Full Orchestration Pipeline Acceptance Trial:
   Goal -> generate_fallback_plan -> DAGBuilder -> ARDOScheduler / RuntimeEngine ->
   BrowserAgent -> PlaywrightBrowserExecutor -> mock_youtube.html ->
   EnvironmentPerceptionEngine -> SemanticVerifier -> EvidenceVault -> GoalVerification.
4. DEF-1C-08 Negative Pipeline Controls:
   - DOM postcondition failure (video remains paused) fails closed through BrowserAgent & RuntimeEngine.
   - Browser launch failure fails closed and blocks downstream tasks.
"""

import hashlib
import json
import re
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from execution.browser.browser_models import BrowserAction, BrowserActionType, BrowserActionResult
from execution.browser.browser_tool import BrowserTool
from execution.browser. browser_agent import BrowserAgent
from execution.browser.agent_contracts import (
    BrowserTask,
    BrowserVerification,
    BrowserVerificationStatus,
    BrowserAgentResult,
    BrowserAgentOutcome,
)
from execution.browser.playwright_executor import PlaywrightBrowserExecutor
from execution.browser.semantic_verifier import SemanticVerifier
from execution.evidence.evidence_vault import EvidenceVault
from orchestration.dag_builder import DAGBuilder, ARDOGraph
from orchestration.runtime_engine import RuntimeEngine
from orchestration.scheduler import ARDOScheduler, ARDOSession
from orchestration.state_machine import TaskState
from ai.fallback_rules import generate_fallback_plan


@pytest.fixture
def mock_youtube_url() -> str:
    """Returns local file:// URI for the mock YouTube HTML fixture and registers test origin."""
    from execution.browser.network_guard import NetworkGuard
    fixture_path = Path(__file__).parent / "fixtures" / "mock_youtube.html"
    assert fixture_path.exists(), f"Fixture not found at {fixture_path}"
    url = fixture_path.resolve().as_uri()
    NetworkGuard.register_test_allowed_origin(url)
    yield url
    NetworkGuard.unregister_test_allowed_origin(url)


class OfflineLocalDecisionProvider:
    """
    Deterministic offline decision responder for local acceptance testing.
    Makes zero external network calls while parsing the live BrowserObservation
    summary produced by EnvironmentPerceptionEngine from real Chromium DOM state.
    """

    def __init__(self) -> None:
        self.calls = []

    async def query(self, prompt: str, system_instruction: str = "", json_mode: bool = True) -> str:
        self.calls.append(prompt)
        prompt_lower = prompt.lower()

        # Extract the element ref for #video-card-1 from the live observation summary in prompt
        # Format in observation summary: [elem_003] div "Ye Fitoor Mera (Official Song)..." selector=#video-card-1
        video_card_ref = "elem_003"
        m = re.search(r"\[(elem_\d+)\][^\n]*#video-card-1", prompt)
        if m:
            video_card_ref = m.group(1)
        else:
            m2 = re.search(r"\[(elem_\d+)\][^\n]*ye fitoor mera \(official song\)", prompt, re.IGNORECASE)
            if m2:
                video_card_ref = m2.group(1)

        # If task objective is to identify the result and search results are visible on page
        if "objective: identify the appropriate result" in prompt_lower:
            if "ye fitoor mera" in prompt_lower:
                return json.dumps({
                    "decision": "finish",
                    "reason": f"Identified matching video result '{video_card_ref}' (#video-card-1) in live DOM.",
                })

        # If task objective is to play the selected result
        if "objective: play the selected result" in prompt_lower:
            # Check if video is already actively playing in the live DOM observation
            if "watch?v=yefitoormera" in prompt_lower or "paused=false" in prompt_lower or "active_playback" in prompt_lower:
                return json.dumps({
                    "decision": "finish",
                    "reason": "Video card clicked and active HTML5 video playback observed in DOM.",
                })
            return json.dumps({
                "decision": "execute",
                "reason": f"Click video result card {video_card_ref} (#video-card-1) to start playback.",
                "action": {
                    "action_type": "click",
                    "element_ref": video_card_ref,
                },
            })

        # If task objective is to verify the observable playback outcome
        if "objective: verify the observable playback outcome" in prompt_lower:
            if "actions taken: 0/" in prompt_lower:
                return json.dumps({
                    "decision": "execute",
                    "reason": "Capture verification screenshot of active video playback.",
                    "action": {
                        "action_type": "screenshot",
                    },
                })
            return json.dumps({
                "decision": "finish",
                "reason": "Observable playback outcome and screenshot verified.",
            })

        return json.dumps({
            "decision": "finish",
            "reason": "Step satisfied based on live DOM observation.",
        })


@pytest.mark.asyncio
async def test_genuine_offline_chromium_workflow(mock_youtube_url: str):
    """
    Executes the 5 discrete fallback tasks for 'Play Ye Fitoor Mera on YouTube'
    against the local HTML fixture using real Playwright Chromium.
    Verifies observable DOM mutations and empirical proof generation.
    """
    goal = "Play Ye Fitoor Mera on YouTube"
    plan = await generate_fallback_plan(goal)
    assert len(plan.nodes) == 5, f"Expected 5 tasks in media plan, got {len(plan.nodes)}"

    nodes_data = [n.model_dump() for n in plan.nodes]
    edges_data = [e.model_dump() for e in plan.edges]
    graph: ARDOGraph = DAGBuilder.build(nodes_data, edges_data, execution_mode="sequential")

    session_id = "sess_offline_accept_001"
    evidence_vault = []

    async with PlaywrightBrowserExecutor(headless=True) as executor:
        assert executor.is_running, "Playwright Chromium failed to launch."

        ordered_nodes = list(graph.nodes.values())
        t1_node, t2_node, t3_node, t4_node, t5_node = ordered_nodes

        # Step 1: Open YouTube (Fixture navigation)
        res1 = await executor.execute_action(
            BrowserAction(action_type=BrowserActionType.NAVIGATE, url=mock_youtube_url)
        )
        assert res1.success is True, f"T1 Navigation failed: {res1.error}"
        assert "Mock YouTube" in (res1.current_title or "")
        t1_node.status = TaskState.COMPLETED
        t1_node.browser_result = BrowserAgentResult(
            task_id=t1_node.task_id,
            session_id=session_id,
            outcome=BrowserAgentOutcome.COMPLETED,
            objective=t1_node.task_name,
            verification=BrowserVerification(
                task_id=t1_node.task_id,
                status=BrowserVerificationStatus.VERIFIED,
                confidence_score=0.98,
                observed_url=res1.current_url,
            ),
        )

        # Step 2: Search for 'Ye Fitoor Mera'
        res2_fill = await executor.execute_action(
            BrowserAction(action_type=BrowserActionType.FILL, selector="#search", text="Ye Fitoor Mera")
        )
        assert res2_fill.success is True, f"T2 Fill failed: {res2_fill.error}"
        res2_click = await executor.execute_action(
            BrowserAction(action_type=BrowserActionType.CLICK, selector="#search-icon-legacy")
        )
        assert res2_click.success is True, f"T2 Click failed: {res2_click.error}"
        t2_node.status = TaskState.COMPLETED
        t2_node.browser_result = BrowserAgentResult(
            task_id=t2_node.task_id,
            session_id=session_id,
            outcome=BrowserAgentOutcome.COMPLETED,
            objective=t2_node.task_name,
            verification=BrowserVerification(
                task_id=t2_node.task_id,
                status=BrowserVerificationStatus.VERIFIED,
                confidence_score=0.95,
            ),
        )

        # Step 3: Identify the appropriate result
        res3 = await executor.execute_action(
            BrowserAction(action_type=BrowserActionType.EXTRACT_TEXT, selector="#video-title-1")
        )
        assert res3.success is True, f"T3 Extract failed: {res3.error}"
        assert "Ye Fitoor Mera" in (res3.text_extracted or "")
        t3_node.status = TaskState.COMPLETED
        t3_node.browser_result = BrowserAgentResult(
            task_id=t3_node.task_id,
            session_id=session_id,
            outcome=BrowserAgentOutcome.COMPLETED,
            objective=t3_node.task_name,
            verification=BrowserVerification(
                task_id=t3_node.task_id,
                status=BrowserVerificationStatus.VERIFIED,
                confidence_score=0.96,
            ),
        )

        # Step 4: Play the selected result
        res4 = await executor.execute_action(
            BrowserAction(action_type=BrowserActionType.CLICK, selector="#video-card-1")
        )
        assert res4.success is True, f"T4 Click failed: {res4.error}"
        t4_node.status = TaskState.COMPLETED
        t4_node.browser_result = BrowserAgentResult(
            task_id=t4_node.task_id,
            session_id=session_id,
            outcome=BrowserAgentOutcome.COMPLETED,
            objective=t4_node.task_name,
            verification=BrowserVerification(
                task_id=t4_node.task_id,
                status=BrowserVerificationStatus.VERIFIED,
                confidence_score=0.95,
            ),
        )

        # Step 5: Verify the observable playback outcome
        res5_text = await executor.execute_action(
            BrowserAction(action_type=BrowserActionType.EXTRACT_TEXT, selector="#playback-status")
        )
        assert res5_text.success is True, f"T5 Status check failed: {res5_text.error}"
        assert res5_text.text_extracted == "PLAYING", f"Expected 'PLAYING', got '{res5_text.text_extracted}'"

        # Capture empirical screenshot evidence
        res5_shot = await executor.execute_action(
            BrowserAction(action_type=BrowserActionType.SCREENSHOT)
        )
        assert res5_shot.success is True, f"T5 Screenshot failed: {res5_shot.error}"
        shot_b64 = res5_shot.metadata.get("screenshot_base64", "")
        assert len(shot_b64) > 100, "Screenshot base64 was empty"
        shot_hash = hashlib.sha256(shot_b64.encode("utf-8")).hexdigest()

        evidence_item = {
            "session_id": session_id,
            "task_id": t5_node.task_id,
            "verification_status": "verified",
            "confidence_score": 0.98,
            "artifact_hash": shot_hash,
            "url": res5_shot.current_url,
            "dom_text": res5_text.text_extracted,
            "content_payload": shot_b64,
        }
        evidence_vault.append(evidence_item)

        t5_node.status = TaskState.COMPLETED
        t5_node.browser_result = BrowserAgentResult(
            task_id=t5_node.task_id,
            session_id=session_id,
            outcome=BrowserAgentOutcome.COMPLETED,
            objective=t5_node.task_name,
            verification=BrowserVerification(
                task_id=t5_node.task_id,
                status=BrowserVerificationStatus.VERIFIED,
                confidence_score=0.98,
                observed_url=res5_shot.current_url,
            ),
        )

    outcome = SemanticVerifier.verify_goal_outcome(
        goal_text=goal,
        graph=graph,
        session_id=session_id,
        evidence_items=evidence_vault,
    )

    assert outcome.overall_status == "verified"
    assert outcome.total_tasks == 5
    assert outcome.verified_tasks == 5
    assert outcome.failed_tasks == 0
    assert outcome.confidence_score >= 0.90
    assert shot_hash in outcome.supporting_evidence_hashes
    assert "Goal fully verified" in outcome.explanation


@pytest.mark.asyncio
async def test_offline_chromium_rejects_unverified_if_evidence_missing(mock_youtube_url: str):
    """
    Negative control: If tasks execute and claim completion but lack empirical proof,
    the goal status MUST be reported as 'unverified' and confidence capped at 0.0.
    """
    goal = "Play Ye Fitoor Mera on YouTube"
    plan = await generate_fallback_plan(goal)
    nodes_data = [n.model_dump() for n in plan.nodes]
    edges_data = [e.model_dump() for e in plan.edges]
    graph: ARDOGraph = DAGBuilder.build(nodes_data, edges_data, execution_mode="sequential")

    for nid, node in graph.nodes.items():
        node.status = TaskState.COMPLETED
        node.browser_result = None
        node.tool_result = None

    outcome = SemanticVerifier.verify_goal_outcome(
        goal_text=goal,
        graph=graph,
        session_id="sess_neg_001",
        evidence_items=[],
    )

    assert outcome.overall_status == "unverified"
    assert outcome.confidence_score == 0.0
    assert outcome.verified_tasks == 0
    assert "lack empirical verification evidence" in outcome.explanation


@pytest.mark.asyncio
async def test_def_1c_08_full_orchestration_pipeline_chromium_acceptance(mock_youtube_url: str):
    """
    DEF-1C-08 Full Application Orchestration Pipeline Acceptance Test:
    Routes 'Play Ye Fitoor Mera on YouTube' through the complete ARDO runtime pipeline:
    Goal submission -> fallback planning -> DAGBuilder -> ARDOScheduler / RuntimeEngine ->
    BrowserAgent -> PlaywrightBrowserExecutor (real headless Chromium) ->
    mock_youtube.html -> EnvironmentPerceptionEngine -> SemanticVerifier ->
    EvidenceVault -> GoalVerification.
    """
    goal = "Play Ye Fitoor Mera on YouTube"
    session_id = "ardo_full_pipeline_001"
    user_id = "usr_pipeline_owner_01"

    # 1. Planning: Decompose goal via fallback planner
    plan = await generate_fallback_plan(goal)
    assert len(plan.nodes) == 5
    assert plan.metadata.is_fallback is True

    nodes_data = [n.model_dump() for n in plan.nodes]
    edges_data = [e.model_dump() for e in plan.edges]
    # Point Step 1 ('Open YouTube') to the local offline mock_youtube.html fixture URL
    nodes_data[0]["data"]["metadata"]["expected_url"] = mock_youtube_url
    nodes_data[0]["data"]["expected_url"] = mock_youtube_url

    # 2. DAG Construction
    graph: ARDOGraph = DAGBuilder.build(
        nodes_data=nodes_data,
        edges_data=edges_data,
        execution_mode="sequential",
        goal_text=goal,
        session_id=session_id,
        user_id=user_id,
        is_fallback=plan.metadata.is_fallback,
        fallback_reason=plan.metadata.fallback_reason,
    )
    assert graph.is_fallback_plan() is True

    # 3. Real Playwright Chromium + BrowserTool + BrowserAgent + RuntimeEngine execution
    async with PlaywrightBrowserExecutor(headless=True) as executor:
        assert executor.is_running, "Real Chromium browser must be running."
        b_tool = BrowserTool(executor=executor)
        local_llm = OfflineLocalDecisionProvider()

        engine = RuntimeEngine(
            graph=graph,
            session_id=session_id,
            mode="sequential",
            goal_id=goal,
            user_id=user_id,
            browser_adapter=b_tool.adapter,
        )
        # Inject BrowserAgent wired to real BrowserTaskAdapter and offline local decision provider
        agent = BrowserAgent(
            llm_provider=local_llm,
            adapter=b_tool.adapter,
            event_callback=engine._handle_browser_agent_event,
        )
        engine._browser_agent = agent

        # Register session in ARDOScheduler for full scheduler/API contract verification
        ardo_session = ARDOSession(
            session_id=session_id,
            graph=graph,
            mode="sequential",
            goal_id=goal,
            user_id=user_id,
        )
        ardo_session.engine = engine
        ARDOScheduler._sessions[session_id] = ardo_session

        try:
            events = []
            async for evt in engine.run():
                events.append(evt)

            # 4. Verify all 5 tasks executed through BrowserAgent and completed with VERIFIED status
            ordered_nodes = list(graph.nodes.values())
            assert len(ordered_nodes) == 5
            for node in ordered_nodes:
                assert node.status == TaskState.COMPLETED, f"Task {node.task_id} ({node.task_name}) failed: {node.execution_logs}"
                assert node.browser_result is not None, f"Task {node.task_id} missing BrowserAgentResult"
                assert node.browser_result.task_id == node.task_id
                assert node.browser_result.session_id == session_id
                assert node.browser_result.outcome == BrowserAgentOutcome.COMPLETED
                assert node.browser_result.verification is not None
                assert node.browser_result.verification.status == BrowserVerificationStatus.VERIFIED

            # Verify Task 1 & Task 2 used BrowserAgent's zero-LLM deterministic fast paths
            assert ordered_nodes[0].browser_result.fast_path_used is True
            assert ordered_nodes[1].browser_result.fast_path_used is True

            # Verify live Chromium DOM state at end of pipeline run
            live_snapshot = await executor.inspect_page()
            assert "watch?v=yefitoormera" in (live_snapshot.url or "")
            assert "Ye Fitoor Mera" in (live_snapshot.title or "")
            media_els = live_snapshot.metadata.get("media_elements", [])
            assert len(media_els) >= 1
            assert media_els[0]["paused"] is False
            assert media_els[0]["currentTime"] > 0.0

            # 5. Verify EvidenceVault persistence and session/user ownership
            vault_items = EvidenceVault.get_session_evidence(session_id, user_id)
            assert len(vault_items) == 5
            for item in vault_items:
                assert item["session_id"] == session_id
                assert item["user_id"] == user_id
                assert item["task_id"] in graph.nodes
                assert item["verification_status"] == "verified"
                assert item["artifact_hash"] and item["artifact_hash"] != "unverified"

            # Verify scheduler evidence retrieval & tenant isolation
            sched_ev = ARDOScheduler.get_evidence(session_id, user_id=user_id)
            assert sched_ev is not None
            assert sched_ev["evidence_count"] == 5
            assert all(e["verification_status"] == "verified" for e in sched_ev["evidence_items"])
            assert ARDOScheduler.get_evidence(session_id, user_id="foreign_attacker_user") is None

            # 6. Verify final GoalVerification outcome
            goal_verif = ARDOScheduler.get_goal_verification(session_id, user_id=user_id)
            assert goal_verif is not None
            assert goal_verif["overall_status"] == "verified"
            assert goal_verif["total_tasks"] == 5
            assert goal_verif["verified_tasks"] == 5
            assert goal_verif["failed_tasks"] == 0
            assert goal_verif["confidence_score"] >= 0.70
            assert len(goal_verif["supporting_evidence_hashes"]) == 5

            # Verify session_complete event emitted with status in ('completed', 'verified')
            complete_events = [e for e in events if e.event_type == "session_complete"]
            assert len(complete_events) == 1
            assert complete_events[0].status in ("completed", "verified")
        finally:
            ARDOScheduler._sessions.pop(session_id, None)


@pytest.mark.asyncio
async def test_def_1c_08_full_pipeline_fails_closed_on_postcondition_or_launch_failure(mock_youtube_url: str):
    """
    DEF-1C-08 Requirement 8:
    Verifies that when routing through the full RuntimeEngine -> BrowserAgent -> Playwright pipeline:
    (A) If a playback task finishes without starting the video (DOM postcondition absent: video paused),
        SemanticVerifier rejects verification, BrowserAgent fails the task, and the session fails closed.
    (B) If Chromium launch fails, BrowserAgent surfaces the failure and the session fails closed.
    """
    # Part A: Postcondition failure on real Chromium (video remains paused)
    session_a = "ardo_pipeline_neg_post"
    user_id = "usr_pipeline_owner_01"
    nodes_a = [
        {
            "id": "t_open",
            "data": {
                "label": "Open YouTube",
                "tool": "browser",
                "is_browser_task": True,
                "expected_url": mock_youtube_url,
                "metadata": {"tool": "browser", "is_browser_task": True, "expected_url": mock_youtube_url},
            },
        },
        {
            "id": "t_play_fail",
            "data": {
                "label": "Play Ye Fitoor Mera song on YouTube",
                "tool": "browser",
                "is_browser_task": True,
                "metadata": {"tool": "browser", "is_browser_task": True},
            },
        },
    ]
    edges_a = [{"source": "t_open", "target": "t_play_fail"}]
    graph_a = DAGBuilder.build(nodes_a, edges_a, execution_mode="sequential", session_id=session_a, user_id=user_id)

    # LLM prematurely declares FINISH without clicking the video card -> video is still paused in DOM!
    premature_llm = MagicMock()
    async def _premature_finish(*args, **kwargs):
        return json.dumps({"decision": "finish", "reason": "Claiming playback without clicking play"})
    premature_llm.query = _premature_finish

    async with PlaywrightBrowserExecutor(headless=True) as executor:
        b_tool = BrowserTool(executor=executor)
        engine_a = RuntimeEngine(
            graph=graph_a,
            session_id=session_a,
            mode="sequential",
            goal_id="Play Ye Fitoor Mera song on YouTube",
            user_id=user_id,
            browser_adapter=b_tool.adapter,
        )
        engine_a._browser_agent = BrowserAgent(
            llm_provider=premature_llm,
            adapter=b_tool.adapter,
            event_callback=engine_a._handle_browser_agent_event,
        )
        events_a = [evt async for evt in engine_a.run()]

        assert graph_a.get_node("t_open").status == TaskState.COMPLETED
        assert graph_a.get_node("t_play_fail").status == TaskState.FAILED
        gv_a = engine_a.get_goal_verification()
        assert gv_a.overall_status == "failed"
        assert gv_a.confidence_score == 0.0
        comp_a = [e for e in events_a if e.event_type == "session_complete"]
        assert len(comp_a) == 1
        assert comp_a[0].status == "failed"

    # Part B: Browser launch failure fails closed and blocks dependent tasks
    session_b = "ardo_pipeline_neg_launch"
    graph_b = DAGBuilder.build(nodes_a, edges_a, execution_mode="sequential", session_id=session_b, user_id=user_id)
    broken_adapter = MagicMock()
    async def _fail_launch():
        raise RuntimeError("Chromium browser process failed to launch")
    broken_adapter.ensure_started = _fail_launch

    engine_b = RuntimeEngine(
        graph=graph_b,
        session_id=session_b,
        mode="sequential",
        goal_id="Play Ye Fitoor Mera song on YouTube",
        user_id=user_id,
        browser_adapter=broken_adapter,
    )
    engine_b._browser_agent = BrowserAgent(
        llm_provider=premature_llm,
        adapter=broken_adapter,
        event_callback=engine_b._handle_browser_agent_event,
    )
    events_b = [evt async for evt in engine_b.run()]

    assert graph_b.get_node("t_open").status == TaskState.FAILED
    assert graph_b.get_node("t_play_fail").status == TaskState.BLOCKED
    gv_b = engine_b.get_goal_verification()
    assert gv_b.overall_status == "failed"
    assert gv_b.confidence_score == 0.0
    comp_b = [e for e in events_b if e.event_type == "session_complete"]
    assert len(comp_b) == 1
    assert comp_b[0].status == "failed"
