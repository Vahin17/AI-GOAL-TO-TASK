"""
test_false_success_remediation.py — Comprehensive Regression Suite for Stage 1B Remediation
=============================================================================================
Thoroughly verifies all 15 scenarios mandated by the Stage 1A Remediation Specification:

Scenario 1:  Unbound task cannot become COMPLETED (fails closed).
Scenario 2:  Missing executor produces TaskState.FAILED with explicit forbidden log.
Scenario 3:  Executor exception cannot produce success.
Scenario 4:  Browser task cannot complete without browser execution.
Scenario 5:  Executed action with failed postcondition cannot become VERIFIED.
Scenario 6:  Missing verification evidence cannot produce VERIFIED goal.
Scenario 7:  Skipped/failed tasks cannot inflate completion percentage.
Scenario 8:  Simulated execution cannot enter real execution metrics.
Scenario 9:  Session completion truthfully reflects incomplete or unverified tasks.
Scenario 10: Goal verification requires every required outcome to pass (weakest-link).
Scenario 11: Duplicate task_completed events cannot inflate metrics (idempotency).
Scenario 12: Unverified results cannot enter semantic memory or playbooks.
Scenario 13: Quarantined records are strictly excluded from retrieval across all stores.
Scenario 14: Non-browser registered tools (calculator) execute via genuine tool contracts.
Scenario 15: Media goal decomposition produces discrete browser tasks with truthful fallback flag.
"""

import asyncio
import hashlib
from unittest.mock import MagicMock, AsyncMock, patch
import pytest

from schemas.models import PlanGraphMetadata
from schemas.ardo_models import ARDOSessionResponse
from orchestration.dag_builder import ARDOGraph, ARDOTaskNode, DAGBuilder
from orchestration.runtime_engine import RuntimeEngine
from orchestration.state_machine import TaskState
from orchestration.execution_monitor import ExecutionMonitor, OrchestrationEvent
from orchestration.safety_schemas import (
    SafetyContext,
    ActionType,
    TargetCategory,
    Scope,
    Environment,
    Reversibility,
    DestructivePotential,
    AmbiguityLevel,
    DataSensitivity,
)
from execution.registry.tool_models import ToolCall as RegistryToolCall, ToolResult
from execution.registry.tool_registry import ToolRegistry
from execution.browser.agent_contracts import (
    BrowserTask,
    BrowserVerification,
    BrowserVerificationStatus,
    BrowserAgentResult,
    BrowserAgentOutcome,
    GoalVerificationResult,
)
from execution.browser.browser_models import BrowserAction, BrowserActionType, BrowserActionResult
from execution.browser.semantic_verifier import SemanticVerifier
from ai.fallback_rules import generate_fallback_plan
from knowledge.semantic_memory import SemanticMemoryStore, ExecutionMemoryItem
from knowledge.playbook_service import PlaybookService
from core.auth_db import AuthDatabase


# ── Scenario 1 & 2: Unbound Task Fails Closed With Explicit Log ───────────────

@pytest.mark.asyncio
async def test_scenario_1_and_2_unbound_task_fails_closed():
    """Unbound task without tool_call or browser_action must transition to FAILED and log simulation forbidden."""
    graph = ARDOGraph()
    node = ARDOTaskNode(
        task_id="t_unbound_1",
        task_name="Generic Generic Step",
        status=TaskState.ACTIVE,
    )
    graph.nodes["t_unbound_1"] = node

    engine = RuntimeEngine(session_id="sess_s1_test", graph=graph)
    success = await engine._attempt_execution(node)

    assert success is False
    assert node.status == TaskState.FAILED
    assert any("Simulated completion forbidden" in log for log in node.execution_logs)


# ── Scenario 3: Executor Exception Cannot Produce Success ─────────────────────

@pytest.mark.asyncio
async def test_scenario_3_executor_exception_fails_closed():
    """If a tool raises an unhandled exception during dispatch, task must fail closed."""
    registry = ToolRegistry.get_instance()
    mock_tool = MagicMock()
    mock_tool.name = "exploding_tool"
    mock_tool.execute = AsyncMock(side_effect=RuntimeError("Hardware explosion!"))
    s_ctx = SafetyContext(
        task_id="t_err_1",
        action=ActionType.READ,
        target="exploding_tool",
        target_category=TargetCategory.APPLICATION_DATA,
        scope=Scope.APPLICATION,
        environment=Environment.LOCAL,
        reversibility=Reversibility.HIGH,
        destructive_potential=DestructivePotential.NONE,
        semantic_confidence=1.0,
    )
    mock_tool.infer_safety_context = MagicMock(return_value=s_ctx)
    registry.register(mock_tool)

    graph = ARDOGraph()
    node = ARDOTaskNode(
        task_id="t_err_1",
        task_name="Dangerous Operation",
        status=TaskState.ACTIVE,
        max_retries=0,
        tool_call=RegistryToolCall(
            call_id="call_err",
            tool_name="exploding_tool",
            action_name="explode",
            arguments={},
        ),
    )
    graph.nodes["t_err_1"] = node

    engine = RuntimeEngine(session_id="sess_s3_test", graph=graph)
    success = await engine._attempt_execution(node)

    assert success is False
    assert node.status == TaskState.FAILED


# ── Scenario 4: Browser Task Cannot Complete Without Browser Execution ────────

@pytest.mark.asyncio
async def test_scenario_4_browser_task_requires_browser_execution():
    """A browser task without an adapter or whose browser execution fails cannot report success."""
    graph = ARDOGraph()
    node = ARDOTaskNode(
        task_id="t_browser_1",
        task_name="Open YouTube and search",
        status=TaskState.ACTIVE,
        max_retries=0,
        browser_action=BrowserAction(action_type=BrowserActionType.NAVIGATE, url="https://youtube.com"),
    )
    graph.nodes["t_browser_1"] = node

    # Engine without browser adapter
    engine = RuntimeEngine(session_id="sess_s4_test", graph=graph, browser_adapter=None)
    success = await engine._attempt_execution(node)

    assert success is False
    assert node.status == TaskState.FAILED
    assert any("failed" in log.lower() for log in node.execution_logs)


# ── Scenario 5: Executed Action With Failed Postcondition Cannot Become Verified ─

@pytest.mark.asyncio
async def test_scenario_5_failed_postcondition_rejects_verification():
    """An action whose DOM state fails postcondition cannot receive VERIFIED status."""
    task = BrowserTask(
        task_id="t_post_1",
        objective="Verify song is playing",
        session_id="sess_s5",
        success_criteria="playback status is PLAYING",
    )

    # Observation shows PAUSED (failed postcondition)
    obs = MagicMock()
    obs.current_url = "https://youtube.com/watch?v=123"
    obs.page_title = "Ye Fitoor Mera"
    obs.recent_action = BrowserActionResult(
        success=False,
        action_type=BrowserActionType.EXTRACT_TEXT,
        text_extracted="PAUSED",
        error="Expected PLAYING, observed PAUSED",
    )
    obs.page_snapshot = MagicMock(page_text="Status: PAUSED")

    verif = SemanticVerifier.verify(task, obs)
    assert verif.status == BrowserVerificationStatus.NOT_VERIFIED
    assert "failed" in (verif.failure_reason or "").lower()


# ── Scenario 6: Missing Verification Evidence Cannot Produce Verified Goal ─────

@pytest.mark.asyncio
async def test_scenario_6_missing_evidence_yields_unverified_goal():
    """If all tasks claim completed but lack verifiable evidence, goal must be 'unverified'."""
    graph = ARDOGraph()
    for i in range(3):
        nid = f"task_{i}"
        node = ARDOTaskNode(task_id=nid, task_name=f"Step {i}", status=TaskState.COMPLETED)
        graph.nodes[nid] = node

    outcome = SemanticVerifier.verify_goal_outcome(
        goal_text="Play song",
        graph=graph,
        session_id="sess_s6",
        evidence_items=[],
    )

    assert outcome.overall_status == "unverified"
    assert outcome.confidence_score == 0.0
    assert outcome.verified_tasks == 0
    assert "lack empirical verification evidence" in outcome.explanation


# ── Scenario 7: Skipped/Failed Tasks Cannot Inflate Completion Percentage ──────

def test_scenario_7_monitor_metrics_exclude_failed_tasks():
    """Failed and incomplete tasks do not increment completed task count."""
    monitor = ExecutionMonitor(session_id="sess_s7")
    monitor.metrics.tasks_total = 4
    monitor.on_task_completed("t1", "Step 1")
    monitor.on_task_failed("t2", "Step 2", "reason")
    monitor.on_task_blocked("t3", "Step 3", "cause")

    metrics = monitor.get_metrics()
    assert metrics["tasks_completed"] == 1
    assert metrics["tasks_failed"] == 1
    assert metrics["tasks_blocked"] == 1
    assert metrics["success_rate"] == 0.25  # 1/4 = 25%


# ── Scenario 8: Simulated Execution Cannot Enter Real Metrics ──────────────────

@pytest.mark.asyncio
async def test_scenario_8_unbound_task_records_failed_metric():
    """Unbound task failure is truthfully registered in the execution monitor."""
    graph = ARDOGraph()
    node = ARDOTaskNode(task_id="t_sim_1", task_name="Fake Step", status=TaskState.ACTIVE)
    graph.nodes["t_sim_1"] = node

    engine = RuntimeEngine(session_id="sess_s8", graph=graph)
    engine.monitor.metrics.tasks_total = 1
    await engine._attempt_execution(node)

    metrics = engine.monitor.get_metrics()
    assert metrics["tasks_completed"] == 0
    assert node.status == TaskState.FAILED


# ── Scenario 9: Session Completion Truthfully Reflects Incomplete Tasks ────────

def test_scenario_9_weakest_link_goal_verification():
    """If 2 tasks succeed with verification but 1 task fails, goal overall status is failed."""
    graph = ARDOGraph()
    node1 = ARDOTaskNode(task_id="t1", task_name="Step 1", status=TaskState.COMPLETED)
    node1.tool_result = ToolResult(call_id="c1", tool_name="calculator", action_name="evaluate", success=True, output_data={"result": 1})
    node2 = ARDOTaskNode(task_id="t2", task_name="Step 2", status=TaskState.COMPLETED)
    node2.tool_result = ToolResult(call_id="c2", tool_name="calculator", action_name="evaluate", success=True, output_data={"result": 2})
    node3 = ARDOTaskNode(task_id="t3", task_name="Step 3", status=TaskState.FAILED)

    graph.nodes["t1"] = node1
    graph.nodes["t2"] = node2
    graph.nodes["t3"] = node3

    outcome = SemanticVerifier.verify_goal_outcome(
        goal_text="Three step workflow",
        graph=graph,
        session_id="sess_s9",
    )

    assert outcome.overall_status == "failed"
    assert outcome.confidence_score == 0.0
    assert outcome.failed_tasks == 1


# ── Scenario 10: Weakest-Link Confidence Bounded by Minimum Confidence Task ───

def test_scenario_10_weakest_link_confidence():
    """A high confidence task (0.99) cannot mask a weak task (0.50)."""
    graph = ARDOGraph()
    node1 = ARDOTaskNode(task_id="t1", task_name="Step 1", status=TaskState.COMPLETED)
    node1.browser_result = BrowserAgentResult(
        task_id="t1", session_id="sess_s10", outcome=BrowserAgentOutcome.COMPLETED, objective="Step 1",
        verification=BrowserVerification(task_id="t1", status=BrowserVerificationStatus.VERIFIED, confidence_score=0.99),
    )
    node2 = ARDOTaskNode(task_id="t2", task_name="Step 2", status=TaskState.COMPLETED)
    node2.browser_result = BrowserAgentResult(
        task_id="t2", session_id="sess_s10", outcome=BrowserAgentOutcome.COMPLETED, objective="Step 2",
        verification=BrowserVerification(task_id="t2", status=BrowserVerificationStatus.VERIFIED, confidence_score=0.50),
    )
    graph.nodes["t1"] = node1
    graph.nodes["t2"] = node2

    outcome = SemanticVerifier.verify_goal_outcome(
        goal_text="Multi-step workflow",
        graph=graph,
        session_id="sess_s10",
    )

    assert outcome.overall_status == "verified"
    assert outcome.confidence_score <= 0.50, f"Expected <= 0.50, got {outcome.confidence_score}"


# ── Scenario 11: Duplicate Events Cannot Inflate Metrics (Idempotency) ────────

def test_scenario_11_duplicate_events_idempotent():
    """Duplicate task_completed events for the same task ID do not inflate task completion counts."""
    monitor = ExecutionMonitor(session_id="sess_s11")
    monitor.metrics.tasks_total = 2
    monitor.on_task_completed("t1", "Step 1")
    monitor.on_task_completed("t1", "Step 1")
    monitor.on_task_completed("t1", "Step 1")

    metrics = monitor.get_metrics()
    assert metrics["tasks_completed"] == 1, f"Expected 1 completed task, got {metrics['tasks_completed']}"


# ── Scenario 12: Unverified Results Cannot Enter Memory or Playbooks ────────────

@pytest.mark.asyncio
async def test_scenario_12_unverified_playbook_generation_rejected():
    """Candidate playbook generation is rejected when goal outcome is not verified or evidence is missing."""
    service = PlaybookService()
    graph = ARDOGraph()
    node = ARDOTaskNode(task_id="t1", task_name="Unverified Step", status=TaskState.COMPLETED)
    graph.nodes["t1"] = node

    goal_verif = GoalVerificationResult(
        session_id="s12",
        goal_text="Test unverified candidate",
        overall_status="unverified",
        confidence_score=0.0,
    )

    pb = service.generate_candidate_playbook(
        user_id="user_test_12",
        session_id="s12",
        goal_text="Test unverified candidate",
        graph=graph,
        goal_verification=goal_verif,
        evidence_records=[],
    )
    assert pb is None, "PlaybookService must return None for unverified goal outcomes"


# ── Scenario 13 (DEF-1C-04): Quarantined Records Strictly Excluded From Retrieval ─

def test_scenario_13_quarantined_records_excluded():
    """Quarantined playbooks and memories are excluded from all normal retrieval paths, including explicit status filters."""
    auth_db = AuthDatabase.get_instance()
    target_owner = "usr_e72e16a21169"

    # Default retrieval for the actual owner of pb_f8b7ab912370 and pb_3ed0edabd366
    user_pbs = auth_db.get_playbooks_for_user(target_owner)
    for pb in user_pbs:
        assert pb.get("status") != "quarantined", f"Found quarantined playbook in user list: {pb}"
        assert pb.get("id") not in ("pb_f8b7ab912370", "pb_3ed0edabd366")

    # Explicit status='quarantined' filter must also return empty list
    assert auth_db.get_playbooks_for_user(target_owner, status="quarantined") == []
    assert PlaybookService.get_playbooks_for_user(target_owner, status="quarantined") == []

    # Direct lookup by ID must not expose quarantined playbooks
    assert auth_db.get_playbook_by_id("pb_f8b7ab912370", user_id=target_owner) is None
    assert auth_db.get_playbook_by_id("pb_3ed0edabd366", user_id=target_owner) is None
    assert PlaybookService.get_playbook_by_id("pb_f8b7ab912370", user_id=target_owner) is None

    # SemanticMemoryStore query for actual owner
    mem_store = SemanticMemoryStore()
    memories = mem_store.retrieve_memories("play ye fitoor mera song on youtube", top_k=10, user_id=target_owner)
    for mem in memories:
        assert mem.outcome_status != "quarantined", f"Found quarantined memory: {mem}"
        assert mem.memory_id not in ("mem_859b0e4920a5", "mem_651c95e43ab8")


# ── Scenario 14: Calculator Tool Completes via Evidence-Backed Contract ────────

@pytest.mark.asyncio
async def test_scenario_14_calculator_tool_execution():
    """Calculator tool safely evaluates mathematical expressions and records real tool results."""
    from execution.registry.calculation_tool import CalculationTool
    calc = CalculationTool()
    call = RegistryToolCall(
        call_id="call_calc_test",
        tool_name="calculator",
        action_name="evaluate",
        arguments={"expression": "sqrt(144) + 8 * 2"},
    )
    result = await calc.execute(call)
    assert result.success is True
    assert result.output_data["result"] == 28.0
    assert result.metadata["evaluator"] == "ast_safe_eval"


# ── Scenario 15: Media Decomposition Produces 5 Discrete Browser Tasks ─────────

@pytest.mark.asyncio
async def test_scenario_15_media_decomposition():
    """Goal 'play ye fitoor mera song on youtube' generates 5 discrete browser tasks with fallback flag."""
    plan = await generate_fallback_plan("play ye fitoor mera song on youtube")
    assert len(plan.nodes) == 5
    descriptions = [n.data.get("label", "").lower() for n in plan.nodes]
    assert any("open youtube" in d for d in descriptions)
    assert any("search for 'ye fitoor mera'" in d for d in descriptions)
    assert any("identify" in d for d in descriptions)
    assert any("play" in d for d in descriptions)
    assert any("verify" in d for d in descriptions)

    # Truthful fallback metadata
    assert plan.metadata.is_fallback is True
    assert plan.metadata.fallback_reason is not None
    for node in plan.nodes:
        assert node.data.get("tool") == "browser"
        assert node.data.get("is_browser_task") is True


# ── DEF-1C-01: Fallback Plan Memory & Playbook Promotion Guard ────────────────

@pytest.mark.asyncio
async def test_def_1c_01_fallback_plan_cannot_promote_to_memory_or_playbook():
    """Fallback-generated plans must never promote into 'success' semantic memories or candidate playbooks."""
    plan = await generate_fallback_plan("Play Ye Fitoor Mera on YouTube")
    nodes_data = [n.model_dump() for n in plan.nodes]
    edges_data = [e.model_dump() for e in plan.edges]
    graph = DAGBuilder.build(nodes_data, edges_data, session_id="sess_fb_guard", user_id="usr_fb_test")

    assert graph.is_fallback is True
    assert graph.is_fallback_plan() is True

    # Even if every node is marked completed with a verified tool result:
    for nid, node in graph.nodes.items():
        node.status = TaskState.COMPLETED
        node.tool_result = ToolResult(
            call_id=f"c_{nid}",
            tool_name="calculator",
            action_name="evaluate",
            success=True,
            output_data={"result": 1.0, "expression": "1"},
        )

    engine = RuntimeEngine(graph=graph, session_id="sess_fb_guard", user_id="usr_fb_test")
    assert engine._is_fallback_graph() is True

    mock_rag = MagicMock()
    with patch("knowledge.rag_service.RAGService.get_instance", return_value=mock_rag):
        engine._persist_semantic_memory()
        assert mock_rag.record_plan_outcome.called
        assert mock_rag.record_plan_outcome.call_args[1]["outcome_status"] == "unverified"

    # Direct PlaybookService call must also reject fallback graphs
    goal_verif = GoalVerificationResult(
        session_id="sess_fb_guard",
        goal_text="Play Ye Fitoor Mera on YouTube",
        overall_status="verified",
        confidence_score=0.95,
    )
    pb = PlaybookService.generate_candidate_playbook(
        user_id="usr_fb_test",
        session_id="sess_fb_guard",
        goal_text="Play Ye Fitoor Mera on YouTube",
        graph=graph,
        goal_verification=goal_verif,
        evidence_records=[{
            "id": "ev_1",
            "session_id": "sess_fb_guard",
            "user_id": "usr_fb_test",
            "task_id": list(graph.nodes.keys())[0],
            "verification_status": "verified",
            "artifact_hash": hashlib.sha256(b"fb_content").hexdigest(),
            "content_payload": "fb_content",
        }],
    )
    assert pb is None


# ── DEF-1C-02: Evidence Session/Task/Tenant/PlanHash Binding & Legacy Removal ─

def test_def_1c_02_evidence_binding_and_scheduler_truthfulness():
    """
    Verifies all 7 evidence integrity cases in SemanticVerifier.verify_goal_outcome,
    EvidenceVault empty-content rejection, and ARDOSession.get_evidence() truthfulness.
    """
    from execution.evidence.evidence_vault import EvidenceVault
    from orchestration.scheduler import ARDOSession

    session_id = "sess_ev_bind_01"
    user_id = "usr_owner_01"
    plan_hash = "ph_authoritative_hash_001"

    graph = ARDOGraph(session_id=session_id, user_id=user_id, plan_hash=plan_hash)
    node = ARDOTaskNode(task_id="t1", task_name="Step 1", status=TaskState.COMPLETED)
    graph.nodes["t1"] = node

    # Case 1: Same-session, same-task, same-user, matching plan_hash valid evidence -> VERIFIED
    content_valid = "Empirical verification proof for task t1"
    valid_hash = hashlib.sha256(content_valid.encode("utf-8")).hexdigest()
    valid_ev = [{
        "id": "ev_valid",
        "session_id": session_id,
        "user_id": user_id,
        "plan_hash": plan_hash,
        "task_id": "t1",
        "verification_status": "verified",
        "confidence_score": 0.94,
        "artifact_hash": valid_hash,
        "content_payload": content_valid,
    }]
    res_valid = SemanticVerifier.verify_goal_outcome("Test Goal", graph, session_id, evidence_items=valid_ev, user_id=user_id)
    assert res_valid.overall_status == "verified"
    assert res_valid.confidence_score == 0.94
    assert res_valid.supporting_evidence_hashes == [valid_hash]

    # Case 2: Foreign-session evidence -> REJECTED (unverified)
    foreign_sess_ev = [{**valid_ev[0], "session_id": "sess_foreign_999"}]
    res_fs = SemanticVerifier.verify_goal_outcome("Test Goal", graph, session_id, evidence_items=foreign_sess_ev, user_id=user_id)
    assert res_fs.overall_status == "unverified"
    assert res_fs.confidence_score == 0.0
    assert res_fs.supporting_evidence_hashes == []

    # Case 3: Foreign-task evidence -> REJECTED (unverified)
    foreign_task_ev = [{**valid_ev[0], "task_id": "t_other_999"}]
    res_ft = SemanticVerifier.verify_goal_outcome("Test Goal", graph, session_id, evidence_items=foreign_task_ev, user_id=user_id)
    assert res_ft.overall_status == "unverified"
    assert res_ft.supporting_evidence_hashes == []

    # Case 4: Foreign-user evidence -> REJECTED (unverified)
    foreign_user_ev = [{**valid_ev[0], "user_id": "usr_attacker_999"}]
    res_fu = SemanticVerifier.verify_goal_outcome("Test Goal", graph, session_id, evidence_items=foreign_user_ev, user_id=user_id)
    assert res_fu.overall_status == "unverified"
    assert res_fu.supporting_evidence_hashes == []

    # Case 5: Missing evidence -> REJECTED (unverified)
    res_missing = SemanticVerifier.verify_goal_outcome("Test Goal", graph, session_id, evidence_items=[], user_id=user_id)
    assert res_missing.overall_status == "unverified"
    assert res_missing.confidence_score == 0.0

    # Case 6: Failed evidence -> REJECTED (unverified, hash excluded from supporting_evidence_hashes)
    failed_hash = hashlib.sha256(b"failed payload").hexdigest()
    failed_ev = [{**valid_ev[0], "verification_status": "failed", "artifact_hash": failed_hash, "content_payload": "failed payload"}]
    res_failed = SemanticVerifier.verify_goal_outcome("Test Goal", graph, session_id, evidence_items=failed_ev, user_id=user_id)
    assert res_failed.overall_status == "unverified"
    assert res_failed.supporting_evidence_hashes == []

    # Case 7: Stale / mismatched plan_hash evidence -> REJECTED (unverified)
    stale_ev = [{**valid_ev[0], "plan_hash": "ph_stale_old_plan_hash"}]
    res_stale = SemanticVerifier.verify_goal_outcome("Test Goal", graph, session_id, evidence_items=stale_ev, user_id=user_id)
    assert res_stale.overall_status == "unverified"
    assert res_stale.supporting_evidence_hashes == []

    # EvidenceVault empty content guard: cannot record 'verified' when raw_content is empty/None
    rec_empty = EvidenceVault.record_evidence(
        user_id=user_id,
        session_id="sess_empty_ev_guard",
        plan_id="p1",
        plan_hash=plan_hash,
        task_id="t1",
        task_name="Step 1",
        evidence_type="task_execution",
        raw_content=None,
        verification_status="verified",
        confidence_score=0.95,
    )
    assert rec_empty.verification_status == "unverified"
    assert rec_empty.confidence_score == 0.0
    assert rec_empty.artifact_hash == "unverified"

    # ARDOSession.get_evidence() must not infer 'verified' or 1.0 confidence from node.status == COMPLETED
    node.browser_action = BrowserAction(action_type=BrowserActionType.NAVIGATE, url="https://example.com")
    node.metadata = {}
    ardo_sess = ARDOSession(session_id="sess_no_vault_ev", graph=graph, mode="safe", user_id=user_id)
    ev_payload = ardo_sess.get_evidence()
    assert len(ev_payload["evidence_items"]) == 1
    assert ev_payload["evidence_items"][0]["verification_status"] == "unverified"
    assert ev_payload["evidence_items"][0]["confidence_score"] == 0.0


# ── DEF-1C-05: Goal Classification Matrix ──────────────────────────────────────

@pytest.mark.asyncio
async def test_def_1c_05_goal_classification_matrix():
    """
    Verifies fallback classification across:
    1. Abstract reasoning goals -> non-browser
    2. Browser navigation goals -> browser
    3. Search goals -> browser
    4. Media goals -> browser
    5. Unsupported goals (including substring words 'display' and 'table') -> non-browser, fail closed
    """
    # 1. Abstract reasoning goal
    p_abstract = await generate_fallback_plan("Analyze architectural invariants")
    assert all(n.data.get("is_browser_task") is False and n.data.get("tool") is None for n in p_abstract.nodes)

    # 2. Browser navigation goal
    p_nav = await generate_fallback_plan("Navigate to https://example.com")
    assert all(n.data.get("is_browser_task") is True and n.data.get("tool") == "browser" for n in p_nav.nodes)

    # 3. Search goal
    p_search = await generate_fallback_plan("Search for quantum computing on Wikipedia")
    assert all(n.data.get("is_browser_task") is True and n.data.get("tool") == "browser" for n in p_search.nodes)

    # 4. Media goal
    p_media = await generate_fallback_plan("Play Ye Fitoor Mera on YouTube")
    assert len(p_media.nodes) == 5
    assert all(n.data.get("is_browser_task") is True and n.data.get("tool") == "browser" for n in p_media.nodes)

    # 5. Unsupported / abstract goal with substring words ('display' contains 'play', 'table' contains 'tab')
    p_unsup = await generate_fallback_plan("Display database table schema")
    assert all(n.data.get("is_browser_task") is False and n.data.get("tool") is None for n in p_unsup.nodes)

    # Executing the unsupported goal through RuntimeEngine fails honestly (no simulation)
    g_unsup = DAGBuilder.build(
        [n.model_dump() for n in p_unsup.nodes],
        [e.model_dump() for e in p_unsup.edges],
        execution_mode="sequential",
        session_id="sess_unsup_fail",
    )
    eng_unsup = RuntimeEngine(graph=g_unsup, session_id="sess_unsup_fail")
    async for _ in eng_unsup.run():
        pass
    first_node = list(g_unsup.nodes.values())[0]
    assert first_node.status == TaskState.FAILED
    assert eng_unsup.get_goal_verification().overall_status == "failed"


# ── DEF-1C-07: ToolResult output_data Persistence & Malformed Output Guard ─────

@pytest.mark.asyncio
async def test_def_1c_07_tool_result_output_data_and_malformed_rejection():
    """
    Verifies that:
    - Successful tool results persist structured output_data in EvidenceVault.
    - Empty/malformed tool outputs cannot become verified.
    - Failed tool results remain failed.
    """
    from execution.evidence.evidence_vault import EvidenceVault

    graph = ARDOGraph(session_id="sess_tr_07", user_id="usr_tr_07", plan_hash="ph_tr_07")
    node_ok = ARDOTaskNode(
        task_id="t_ok",
        task_name="Valid Calc",
        status=TaskState.COMPLETED,
        tool_result=ToolResult(
            call_id="c_ok",
            tool_name="calculator",
            action_name="evaluate",
            success=True,
            output_data={"expression": "12 * 12", "result": 144.0},
        ),
    )
    node_empty = ARDOTaskNode(
        task_id="t_empty",
        task_name="Empty Output Tool",
        status=TaskState.COMPLETED,
        tool_result=ToolResult(
            call_id="c_empty",
            tool_name="custom_empty",
            action_name="noop",
            success=True,
            output_data={},
            extracted_text=None,
            artifacts={},
        ),
    )
    graph.nodes["t_ok"] = node_ok
    graph.nodes["t_empty"] = node_empty

    engine = RuntimeEngine(graph=graph, session_id="sess_tr_07", user_id="usr_tr_07")
    await engine._handle_success(node_ok)
    await engine._handle_success(node_empty)

    assert node_ok.metadata["verification_status"] == "verified"
    assert node_ok.metadata["confidence_score"] == 1.0
    assert node_empty.metadata["verification_status"] == "unverified"
    assert node_empty.metadata["confidence_score"] == 0.0

    ev_list = EvidenceVault.get_session_evidence("sess_tr_07", "usr_tr_07")
    ev_by_tid = {e["task_id"]: e for e in ev_list}
    assert '"result": 144.0' in (ev_by_tid["t_ok"]["content_payload"] or "")
    assert ev_by_tid["t_empty"]["verification_status"] == "unverified"

    # Goal verification must report 'unverified' because t_empty produced no valid output
    gv = SemanticVerifier.verify_goal_outcome("Tool Goal", graph, "sess_tr_07", evidence_items=ev_list, user_id="usr_tr_07")
    assert gv.overall_status == "unverified"
    assert gv.verified_tasks == 1
    assert gv.confidence_score == 0.0


# ── Stage 1D Deadlock Fix: INSPECT_PAGE Lock Ownership & Serialization ────────

@pytest.mark.asyncio
async def test_stage1d_inspect_page_action_no_deadlock_and_serialized_concurrency(monkeypatch):
    """
    Proves that:
    1. _BrowserSession._inspect_page_unlocked raises RuntimeError if called without _op_lock held.
    2. execute_action(BrowserAction(action_type=BrowserActionType.INSPECT_PAGE)) completes
       without deadlocking on _op_lock.
    3. Concurrent execute_action(INSPECT_PAGE) and inspect_page() calls are strictly serialized
       under _op_lock without overlap or deadlock.
    """
    from execution.browser.playwright_executor import _BrowserSession
    from execution.browser.browser_models import BrowserAction, BrowserActionType
    from execution.browser.environment_perception import EnvironmentPerceptionEngine, PageSnapshot

    session = _BrowserSession(headless=True, default_timeout_ms=2000)
    session._is_started = True
    session._page = MagicMock()
    session._page.url = "https://example.com"

    # 1. Calling _inspect_page_unlocked without holding _op_lock must fail closed
    with pytest.raises(RuntimeError, match="_inspect_page_unlocked called without holding _op_lock"):
        await session._inspect_page_unlocked()

    # Track concurrency overlap inside EnvironmentPerceptionEngine.inspect
    active_count = 0
    max_concurrent = 0
    call_order = []

    async def _fake_inspect(self_eng, page):
        nonlocal active_count, max_concurrent
        active_count += 1
        max_concurrent = max(max_concurrent, active_count)
        call_order.append("enter")
        await asyncio.sleep(0.02)
        call_order.append("exit")
        active_count -= 1
        return PageSnapshot(
            url="https://example.com",
            title="Example",
            timestamp=1700000000.0,
            elements=[],
        )

    monkeypatch.setattr(EnvironmentPerceptionEngine, "inspect", _fake_inspect)

    # 2. Single INSPECT_PAGE action via execute_action completes without deadlock
    action = BrowserAction(action_type=BrowserActionType.INSPECT_PAGE)
    res = await asyncio.wait_for(session.execute_action(action), timeout=2.0)
    assert res.success is True
    assert res.action_type == BrowserActionType.INSPECT_PAGE
    assert res.current_url == "https://example.com"

    # 3. Concurrent execute_action(INSPECT_PAGE) and inspect_page() calls serialize cleanly
    results = await asyncio.wait_for(
        asyncio.gather(
            session.execute_action(BrowserAction(action_type=BrowserActionType.INSPECT_PAGE)),
            session.inspect_page(),
            session.execute_action(BrowserAction(action_type=BrowserActionType.INSPECT_PAGE)),
        ),
        timeout=3.0,
    )
    assert len(results) == 3
    assert results[0].success is True
    assert isinstance(results[1], PageSnapshot)
    assert results[2].success is True
    assert max_concurrent == 1
    assert call_order == ["enter", "exit", "enter", "exit", "enter", "exit", "enter", "exit"]


