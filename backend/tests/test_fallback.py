import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from ai.fallback_rules import decompose_goal_heuristically, generate_fallback_plan
from ai.gemini_provider import (
    GeminiAuthenticationError,
    GeminiProviderError,
    GeminiQuotaExceededError,
    GeminiRateLimitError,
    GeminiServiceUnavailableError,
)
from ai.input_guard import InputGuard, InputGuardVerdict, InputReviewRequiredError, InputDeniedError
from ai.orchestrator import generate_task_graph
from ai.plan_critic import PlanCritic, CriticIssueSeverity
from execution.browser.semantic_verifier import SemanticVerifier
from execution.evidence.evidence_vault import EvidenceVault
from execution.registry.tool_models import ToolCall
from knowledge.playbook_service import PlaybookService
from orchestration.checkpoint_manager import ExecutionCheckpointManager
from orchestration.dag_builder import DAGBuilder, ARDOGraph, ARDOTaskNode
from orchestration.runtime_engine import RuntimeEngine
from orchestration.scheduler import ARDOScheduler
from orchestration.state_machine import TaskState
from schemas.models import PlanGraphMetadata, PlanGraphResponse, TaskEdge, TaskNode
from core.config import settings


@pytest.fixture(autouse=True)
def isolate_stage2_test_env(monkeypatch):
    """Isolate database, RAG, and credentials so tests never call external APIs."""
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
    monkeypatch.setattr(settings, "POSTGRES_URI", None)
    monkeypatch.delenv("POSTGRES_URI", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr("core.database.Database.connect_db", AsyncMock())
    monkeypatch.setattr("core.database.Database.close_db", AsyncMock())
    monkeypatch.setattr("core.sync_worker.ProfileSyncWorker.start", MagicMock())
    monkeypatch.setattr("orchestration.safety_engine.SafetyEngine.evaluate_plan", AsyncMock(return_value={}))
    from core.auth_db import AuthDatabase
    from core.sync_worker import ProfileSyncWorker
    from knowledge.rag_service import RAGService
    AuthDatabase.reset_instance(force_sqlite=True)
    ProfileSyncWorker.reset_instance()
    RAGService.reset_instance(in_memory=True)
    yield
    ProfileSyncWorker.reset_instance()


@pytest.mark.asyncio
async def test_fallback_generates_valid_schema():
    """
    Simulates a total systemic LLM failure ensuring our React Flow canvas remains highly resilient and safe.
    """
    goal = "Build a rocket"
    fallback_response = await generate_fallback_plan(goal)

    # Instance Assertions
    assert isinstance(fallback_response, PlanGraphResponse)

    # Data length assertions indicating heuristic generic shape
    assert len(fallback_response.nodes) == 3
    assert len(fallback_response.edges) == 2

    # Safety metadata assertions
    assert "Heuristic Fallback Plan" in fallback_response.metadata.intent
    assert fallback_response.metadata.confidence in (0.6, 1.0)
    assert fallback_response.metadata.is_fallback is True

    # Assert nodes format mapping properly into React flow spec
    json_rep = fallback_response.model_dump()
    assert "nodes" in json_rep

    node_labels = [n["data"]["label"] for n in json_rep["nodes"]]
    assert any("Analyze directive" in l for l in node_labels)
    assert any("Execute core operations" in l for l in node_labels)
    assert any("Verify" in l for l in node_labels)


# ─────────────────────────────────────────────────────────────────────────────
# PRIMARY PLANNER TESTS (Scenarios 1–10)
# ─────────────────────────────────────────────────────────────────────────────

def _make_primary_plan(goal: str) -> PlanGraphResponse:
    nodes = [
        TaskNode(
            id="t1",
            type="input",
            position={"x": 250.0, "y": 0.0},
            data={"label": "Navigate to wikipedia.org", "tool_call": {"tool_name": "browser", "action_type": "navigate"}},
        ),
        TaskNode(
            id="t2",
            type="default",
            position={"x": 250.0, "y": 150.0},
            data={"label": "Search for Alan Turing biography", "tool_call": {"tool_name": "browser", "action_type": "fill"}},
        ),
        TaskNode(
            id="t3",
            type="output",
            position={"x": 250.0, "y": 300.0},
            data={"label": "Take verification screenshot of article", "tool_call": {"tool_name": "browser", "action_type": "screenshot"}},
        ),
    ]
    edges = [
        TaskEdge(id="e_1_2", source="t1", target="t2"),
        TaskEdge(id="e_2_3", source="t2", target="t3"),
    ]
    meta = PlanGraphMetadata(
        intent=f"Primary plan for: {goal}",
        confidence=0.92,
        reasoning_summary="Three-step sequential browser plan.",
    )
    return PlanGraphResponse(nodes=nodes, edges=edges, metadata=meta)


@pytest.mark.asyncio
async def test_s2_01_valid_ai_generated_goal_decomposition():
    """Scenario 1: Primary AI planner produces a validated non-fallback plan with explicit primary_ai provenance."""
    mock_provider = MagicMock()
    mock_provider.generate_plan = AsyncMock(return_value=_make_primary_plan("Search Alan Turing on Wikipedia"))

    with patch("ai.orchestrator.get_llm_provider", return_value=mock_provider), \
         patch("ai.orchestrator.get_database", return_value=None):
        plan = await generate_task_graph("Open wikipedia.org and search Alan Turing biography")
        assert plan.metadata.is_fallback is False
        assert plan.metadata.planning_mode == "primary_ai"
        assert plan.metadata.primary_failure_code is None
        assert plan.metadata.validation_passed is True
        assert plan.critic_result["is_valid"] is True
        assert len(plan.nodes) == 3
        assert len(plan.edges) == 2


@pytest.mark.asyncio
async def test_s2_02_multistep_goal_with_dependencies():
    """Scenario 2: Multi-step goal preserves strict sequential dependency ordering through DAGBuilder."""
    plan = _make_primary_plan("Search Alan Turing on Wikipedia")
    graph = DAGBuilder.build(
        [n.model_dump() for n in plan.nodes],
        [e.model_dump() for e in plan.edges],
        goal_text="Search Alan Turing on Wikipedia",
        metadata=plan.metadata.model_dump(),
    )
    assert graph.get_node("t1").dependency_ids == []
    assert graph.get_node("t2").dependency_ids == ["t1"]
    assert graph.get_node("t3").dependency_ids == ["t2"]
    assert graph.successors("t1") == ["t2"]
    assert graph.successors("t2") == ["t3"]


@pytest.mark.asyncio
async def test_s2_03_independent_tasks_parallel_groups():
    """Scenario 3: Independent tasks without mutual dependencies are grouped for parallel execution."""
    fb_plan = await generate_fallback_plan(
        "In parallel: open Wikipedia, open GitHub, then compare results"
    )
    assert len(fb_plan.nodes) == 3
    # First two tasks feed into the third task without serializing each other
    graph = DAGBuilder.build(
        [n.model_dump() for n in fb_plan.nodes],
        [e.model_dump() for e in fb_plan.edges],
        execution_mode="parallel",
        metadata=fb_plan.metadata.model_dump(),
    )
    n0_id, n1_id, n2_id = [n.id for n in fb_plan.nodes]
    assert graph.get_node(n0_id).dependency_ids == []
    assert graph.get_node(n1_id).dependency_ids == []
    assert set(graph.get_node(n2_id).dependency_ids) == {n0_id, n1_id}
    assert any(set(g) == {n0_id, n1_id} for g in graph.parallel_groups)


@pytest.mark.asyncio
async def test_s2_04_ambiguous_goal_requiring_clarification():
    """Scenario 4: Ambiguous pronoun-only directive requires clarification (REVIEW) and never silently executes."""
    for vague_goal in ["Do it", "Fix the thing", "Update this", "Please run that"]:
        res = InputGuard.validate_goal(vague_goal)
        assert res.verdict == InputGuardVerdict.REVIEW
        assert res.requires_review is True
        assert "ambiguous_goal_target" in res.flagged_patterns
        with pytest.raises(InputReviewRequiredError):
            await generate_task_graph(vague_goal)


@pytest.mark.asyncio
async def test_s2_05_empty_ai_response_triggers_fallback_with_code():
    """Scenario 5: Empty AI response triggers fallback with primary_failure_code='empty_response'."""
    mock_provider = MagicMock()
    mock_provider.generate_plan = AsyncMock(
        side_effect=GeminiProviderError("Gemini response contained no candidates.")
    )
    with patch("ai.orchestrator.get_llm_provider", return_value=mock_provider), \
         patch("ai.orchestrator.get_database", return_value=None):
        plan = await generate_task_graph("Navigate to https://example.com")
        assert plan.metadata.is_fallback is True
        assert plan.metadata.planning_mode == "deterministic_fallback"
        assert plan.metadata.primary_failure_code == "empty_response"
        assert "empty response" in (plan.metadata.fallback_reason or "").lower()


@pytest.mark.asyncio
async def test_s2_06_malformed_ai_response_triggers_fallback_with_code():
    """Scenario 6: Malformed AI structured output triggers fallback with primary_failure_code='malformed_output'."""
    mock_provider = MagicMock()
    mock_provider.generate_plan = AsyncMock(
        side_effect=ValueError("Failed to parse PlanGraphResponse from completion: invalid json")
    )
    with patch("ai.orchestrator.get_llm_provider", return_value=mock_provider), \
         patch("ai.orchestrator.get_database", return_value=None):
        plan = await generate_task_graph("Navigate to https://example.com")
        assert plan.metadata.is_fallback is True
        assert plan.metadata.primary_failure_code == "malformed_output"
        assert "malformed" in (plan.metadata.fallback_reason or "").lower()


def test_s2_07_invalid_task_structure_rejected():
    """Scenario 7: Task node with empty label or duplicate ID is rejected by PlanCritic and DAGBuilder."""
    bad_nodes = [
        TaskNode(id="dup_1", type="default", position={"x": 0.0, "y": 0.0}, data={"label": "Open site"}),
        TaskNode(id="dup_1", type="default", position={"x": 0.0, "y": 150.0}, data={"label": "Click button"}),
    ]
    issues = PlanCritic.validate_dag_structure(bad_nodes, [])
    assert any(i.issue_type == "duplicate_task_id" and i.severity == CriticIssueSeverity.CRITICAL for i in issues)

    empty_label_nodes = [
        TaskNode(id="t_empty", type="default", position={"x": 0.0, "y": 0.0}, data={"label": "   "}),
    ]
    issues_empty = PlanCritic.validate_dag_structure(empty_label_nodes, [])
    assert any(i.issue_type == "invalid_task_structure" for i in issues_empty)


@pytest.mark.asyncio
async def test_s2_08_provider_timeout_triggers_fallback_with_code():
    """Scenario 8: Provider timeout triggers fallback with primary_failure_code='provider_timeout'."""
    mock_provider = MagicMock()
    mock_provider.generate_plan = AsyncMock(side_effect=asyncio.TimeoutError("Request timed out after 20s"))
    with patch("ai.orchestrator.get_llm_provider", return_value=mock_provider), \
         patch("ai.orchestrator.get_database", return_value=None):
        plan = await generate_task_graph("Open https://example.com and take screenshot")
        assert plan.metadata.is_fallback is True
        assert plan.metadata.primary_failure_code == "provider_timeout"
        assert "timed out" in (plan.metadata.fallback_reason or "").lower()


@pytest.mark.asyncio
async def test_s2_09_provider_exception_classification():
    """Scenario 9: Quota, rate-limit, auth, and 503 exceptions map to distinct structured failure codes."""
    cases = [
        (GeminiQuotaExceededError("Quota exceeded"), "provider_quota_exhausted"),
        (GeminiRateLimitError("429 rate limit"), "provider_rate_limited"),
        (GeminiAuthenticationError("401 Invalid API key"), "provider_auth_error"),
        (GeminiServiceUnavailableError("503 Service unavailable"), "provider_unavailable"),
    ]
    for exc, expected_code in cases:
        mock_provider = MagicMock()
        mock_provider.generate_plan = AsyncMock(side_effect=exc)
        with patch("ai.orchestrator.get_llm_provider", return_value=mock_provider), \
             patch("ai.orchestrator.get_database", return_value=None):
            plan = await generate_task_graph("Open https://example.com")
            assert plan.metadata.is_fallback is True
            assert plan.metadata.primary_failure_code == expected_code


@pytest.mark.asyncio
async def test_s2_10_provider_plan_failing_dag_validation_falls_back_with_validation_rejected():
    """Scenario 10: When primary AI returns a cyclic/invalid plan on all attempts, fallback engages with 'validation_rejected'."""
    cyclic_nodes = [
        TaskNode(id="a", type="default", position={"x": 0.0, "y": 0.0}, data={"label": "Open site"}),
        TaskNode(id="b", type="default", position={"x": 0.0, "y": 150.0}, data={"label": "Search site"}),
    ]
    cyclic_edges = [
        TaskEdge(id="e1", source="a", target="b"),
        TaskEdge(id="e2", source="b", target="a"),
    ]
    invalid_plan = PlanGraphResponse(
        nodes=cyclic_nodes,
        edges=cyclic_edges,
        metadata=PlanGraphMetadata(intent="cyclic", confidence=0.8, reasoning_summary="cyclic"),
    )
    mock_provider = MagicMock()
    mock_provider.generate_plan = AsyncMock(return_value=invalid_plan)

    with patch("ai.orchestrator.get_llm_provider", return_value=mock_provider), \
         patch("ai.orchestrator.get_database", return_value=None):
        plan = await generate_task_graph("Open https://example.com and search docs")
        assert mock_provider.generate_plan.call_count == 2
        assert plan.metadata.is_fallback is True
        assert plan.metadata.primary_failure_code == "validation_rejected"
        assert "rejected by validation critic" in (plan.metadata.fallback_reason or "").lower()
        assert plan.critic_result["is_valid"] is True


# ─────────────────────────────────────────────────────────────────────────────
# FALLBACK PLANNER TESTS (Scenarios 11–20)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_s2_11_and_12_and_13_fallback_activation_metadata_and_reason():
    """Scenarios 11, 12, 13: Fallback activation populates complete provenance metadata and reason."""
    fb = await generate_fallback_plan(
        "Search for machine learning on Wikipedia",
        is_fallback=True,
        fallback_reason="Primary AI planner timed out after 20s.",
        primary_failure_code="provider_timeout",
    )
    assert fb.metadata.is_fallback is True
    assert fb.metadata.planning_mode == "deterministic_fallback"
    assert fb.metadata.provider_used == "rule_based_fallback"
    assert fb.metadata.primary_failure_code == "provider_timeout"
    assert fb.metadata.fallback_reason == "Primary AI planner timed out after 20s."
    assert fb.metadata.validation_passed is True
    assert len(fb.metadata.limitations) >= 1


@pytest.mark.asyncio
async def test_s2_14_useful_decomposition_for_supported_categories():
    """Scenario 14: Useful, concrete decomposition across media, navigation, search, compound, and calculation goals."""
    # Media goal -> 5 discrete browser steps
    p_media = await generate_fallback_plan("Play Ye Fitoor Mera song on YouTube")
    assert len(p_media.nodes) == 5
    assert all(n.data["tool"] == "browser" for n in p_media.nodes)

    # Calculation goal -> bound to calculator tool
    p_calc = await generate_fallback_plan("Calculate 25 * 4 + 10")
    assert len(p_calc.nodes) == 1
    assert p_calc.nodes[0].data["tool"] == "calculator"
    assert p_calc.nodes[0].data["tool_call"]["arguments"]["expression"] == "25 * 4 + 10"
    assert p_calc.metadata.requires_clarification is False

    # Compound browser goal -> discrete ordered steps
    p_comp = await generate_fallback_plan("Open https://example.com, search for pricing, and take a screenshot")
    assert len(p_comp.nodes) == 3
    assert len(p_comp.edges) == 2


@pytest.mark.asyncio
async def test_s2_15_unsupported_goal_explicit_limitations():
    """Scenario 15: Unsupported abstract goal records explicit limitations and requires_clarification=True."""
    p_abstract = await generate_fallback_plan("Synthesize room-temperature superconductor")
    assert all(n.data["tool"] is None and n.data["is_browser_task"] is False for n in p_abstract.nodes)
    assert p_abstract.metadata.requires_clarification is True
    assert any("does not map to a registered automated executor" in lim for lim in p_abstract.metadata.limitations)


@pytest.mark.asyncio
async def test_s2_16_duplicate_task_prevention():
    """Scenario 16: Repeated identical clauses in a compound goal are deduplicated."""
    steps = decompose_goal_heuristically(
        "Open Wikipedia, open wikipedia, search for Python, search for Python, and take a screenshot"
    )
    assert len(steps) == 3
    assert steps == ["Open Wikipedia", "Search for Python", "Take a screenshot"]


def test_s2_17_dependency_validation_dangling_edge_and_duplicate_id_rejected():
    """Scenario 17: DAGBuilder rejects dangling edges and duplicate task IDs with ValueError."""
    nodes = [{"id": "t1", "data": {"label": "Step 1"}}]
    with pytest.raises(ValueError, match="nonexistent task node"):
        DAGBuilder.build(nodes, [{"source": "t1", "target": "t_missing"}])

    dup_nodes = [
        {"id": "t1", "data": {"label": "Step 1"}},
        {"id": "t1", "data": {"label": "Step 2"}},
    ]
    with pytest.raises(ValueError, match="Duplicate task_id"):
        DAGBuilder.build(dup_nodes, [])


def test_s2_18_cyclic_dependency_rejection():
    """Scenario 18: DAGBuilder and PlanCritic reject cyclic dependency graphs."""
    nodes = [
        {"id": "t1", "data": {"label": "Step 1"}},
        {"id": "t2", "data": {"label": "Step 2"}},
        {"id": "t3", "data": {"label": "Step 3"}},
    ]
    edges = [
        {"source": "t1", "target": "t2"},
        {"source": "t2", "target": "t3"},
        {"source": "t3", "target": "t1"},
    ]
    with pytest.raises(ValueError, match="Cycle detected"):
        DAGBuilder.build(nodes, edges)


@pytest.mark.asyncio
async def test_s2_19_invalid_fallback_plan_rejection():
    """Scenario 19: Empty/whitespace goals or structurally invalid fallback plans are rejected explicitly."""
    with pytest.raises(ValueError, match="cannot be empty"):
        await generate_fallback_plan("   ")

    with pytest.raises(ValueError, match="cannot be empty"):
        decompose_goal_heuristically("")


@pytest.mark.asyncio
async def test_s2_20_no_silent_fallback_to_success_behavior():
    """Scenario 20: Fallback plan never claims execution success and cannot be mistaken for a primary AI plan."""
    mock_provider = MagicMock()
    mock_provider.generate_plan = AsyncMock(side_effect=RuntimeError("Connection refused"))
    with patch("ai.orchestrator.get_llm_provider", return_value=mock_provider), \
         patch("ai.orchestrator.get_database", return_value=None):
        plan = await generate_task_graph("Navigate to https://example.com")
        assert plan.metadata.is_fallback is True
        assert plan.metadata.planning_mode == "deterministic_fallback"
        assert plan.metadata.primary_failure_code == "provider_error"
        assert all(n.data["metadata"]["is_fallback"] is True for n in plan.nodes)


# ─────────────────────────────────────────────────────────────────────────────
# PIPELINE AND API TESTS (Scenarios 21–26)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_s2_21_metadata_survives_dag_construction():
    """Scenario 21: Graph-level planning metadata survives DAGBuilder.build without being overwritten by node metadata."""
    fb_plan = await generate_fallback_plan(
        "Open https://example.com and take screenshot",
        is_fallback=True,
        fallback_reason="Primary AI quota exhausted",
        primary_failure_code="provider_quota_exhausted",
    )
    graph = DAGBuilder.build(
        [n.model_dump() for n in fb_plan.nodes],
        [e.model_dump() for e in fb_plan.edges],
        goal_text="Open https://example.com and take screenshot",
        is_fallback=fb_plan.metadata.is_fallback,
        fallback_reason=fb_plan.metadata.fallback_reason,
        metadata=fb_plan.metadata.model_dump(),
    )
    assert graph.is_fallback is True
    assert graph.is_fallback_plan() is True
    assert graph.fallback_reason == "Primary AI quota exhausted"
    assert graph.metadata["planning_mode"] == "deterministic_fallback"
    assert graph.metadata["primary_failure_code"] == "provider_quota_exhausted"
    assert graph.metadata["provider_used"] == "rule_based_fallback"
    assert len(graph.metadata["limitations"]) >= 1


def test_s2_22_and_24_metadata_survives_api_serialization_and_degradation_reporting():
    """Scenarios 22 & 24: /plan, /orchestrate, and /orchestrate/{id}/state serialize fallback & degradation provenance."""
    from main import app
    from core.auth_db import AuthDatabase
    from api.auth import get_current_user, get_optional_current_user

    client = TestClient(app, raise_server_exceptions=False)
    auth_db = AuthDatabase.get_instance()
    user = auth_db.create_user("stage2_api@example.com", "Stage2Pass!123", "Stage2Tester")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_optional_current_user] = lambda: user
    headers = {}

    mock_provider = MagicMock()
    mock_provider.generate_plan = AsyncMock(side_effect=GeminiQuotaExceededError("Quota exceeded"))

    try:
        with patch("ai.orchestrator.get_llm_provider", return_value=mock_provider), \
             patch("ai.orchestrator.get_database", return_value=None):
            # 1. POST /plan
            res_plan = client.post("/plan", json={"goal": "Calculate 10 * 5"}, headers=headers)
            assert res_plan.status_code == 200
            p_json = res_plan.json()
            assert p_json["metadata"]["is_fallback"] is True
            assert p_json["metadata"]["planning_mode"] == "deterministic_fallback"
            assert p_json["metadata"]["primary_failure_code"] == "provider_quota_exhausted"
            assert len(p_json["metadata"]["limitations"]) >= 1

            # 2. POST /orchestrate
            res_orch = client.post(
                "/orchestrate",
                json={"goal": "Calculate 10 * 5", "mode": "sequential"},
                headers=headers,
            )
            assert res_orch.status_code == 200
            o_json = res_orch.json()
            assert o_json["is_fallback"] is True
            assert o_json["planning_mode"] == "deterministic_fallback"
            assert o_json["primary_failure_code"] == "provider_quota_exhausted"
            assert len(o_json["limitations"]) >= 1
            sid = o_json["session_id"]

            # 3. GET /orchestrate/{session_id}/state
            res_state = client.get(f"/orchestrate/{sid}/state", headers=headers)
            assert res_state.status_code == 200
            s_json = res_state.json()
            assert s_json["is_fallback"] is True
            assert s_json["planning_mode"] == "deterministic_fallback"
            assert s_json["primary_failure_code"] == "provider_quota_exhausted"
            assert len(s_json["limitations"]) >= 1
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_optional_current_user, None)


@pytest.mark.asyncio
async def test_s2_23_metadata_survives_checkpoint_persistence():
    """Scenario 23: Fallback provenance survives ExecutionCheckpointManager.save_checkpoint and load_latest_checkpoint."""
    fb = await generate_fallback_plan(
        "Calculate 12 + 34",
        is_fallback=True,
        fallback_reason="Primary AI offline",
        primary_failure_code="provider_unavailable",
    )
    graph = DAGBuilder.build(
        [n.model_dump() for n in fb.nodes],
        [e.model_dump() for e in fb.edges],
        session_id="sess_chk_s2",
        user_id="usr_chk_s2",
        is_fallback=True,
        fallback_reason=fb.metadata.fallback_reason,
        metadata=fb.metadata.model_dump(),
    )
    await ExecutionCheckpointManager.save_checkpoint(
        session_id="sess_chk_s2",
        graph=graph,
        trigger="stage2_test",
        db=None,
        user_id="usr_chk_s2",
    )
    chk = await ExecutionCheckpointManager.load_latest_checkpoint("sess_chk_s2", db=None)
    assert chk is not None
    assert chk["is_fallback"] is True
    assert chk["fallback_reason"] == "Primary AI offline"
    assert chk["graph_metadata"]["planning_mode"] == "deterministic_fallback"
    assert chk["graph_metadata"]["primary_failure_code"] == "provider_unavailable"


def test_s2_25_frontend_transparency_contract_verification():
    """Scenario 25: Frontend store, API client, ChatPanel, and ExecutionControl wire fallback & limitation fields."""
    repo_root = Path(__file__).resolve().parents[2]
    api_ts = (repo_root / "frontend" / "src" / "lib" / "ardo-api.ts").read_text(encoding="utf-8")
    store_ts = (repo_root / "frontend" / "src" / "store" / "useARDOStore.ts").read_text(encoding="utf-8")
    chat_tsx = (repo_root / "frontend" / "src" / "components" / "panels" / "ChatPanel.tsx").read_text(encoding="utf-8")
    exec_tsx = (repo_root / "frontend" / "src" / "components" / "panels" / "ExecutionControl.tsx").read_text(encoding="utf-8")

    assert "planning_mode?:" in api_ts
    assert "primary_failure_code?:" in api_ts
    assert "limitations?:" in api_ts
    assert "planningLimitations" in store_ts
    assert "primaryFailureCode" in store_ts
    assert "Fallback planning was used because the primary AI planning provider was unavailable" in chat_tsx
    assert "Heuristic Fallback Plan" in exec_tsx
    assert "planningLimitations" in exec_tsx


@pytest.mark.asyncio
async def test_s2_26_user_approval_remains_distinct_from_execution():
    """Scenario 26: Approving a gated task transitions it out of waiting-for-approval but does NOT mark it verified without execution."""
    nodes = [
        {
            "id": "t_gate",
            "data": {"label": "Delete production cache"},
        }
    ]
    graph = DAGBuilder.build(nodes, [], risk_threshold=0.5, session_id="sess_s2_gate", user_id="usr_s2_gate")
    node = graph.get_node("t_gate")
    assert node.approval_needed is True
    assert node.status == TaskState.PENDING

    engine = RuntimeEngine(graph=graph, session_id="sess_s2_gate", user_id="usr_s2_gate")
    engine.approve_task("t_gate")
    # Approval alone never marks the task COMPLETED or VERIFIED
    assert node.status != TaskState.COMPLETED
    gv = engine.get_goal_verification()
    assert gv.overall_status != "verified"


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 1 REGRESSION CONFIRMATION (Scenarios 27–31)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_s2_27_no_generic_simulated_success():
    """Scenario 27: Unbound tasks fail closed immediately without generic simulated completion."""
    fb = await generate_fallback_plan("Build a rocket")
    graph = DAGBuilder.build(
        [n.model_dump() for n in fb.nodes],
        [e.model_dump() for e in fb.edges],
        execution_mode="sequential",
        session_id="sess_s2_27",
        user_id="usr_s2_27",
        is_fallback=True,
        metadata=fb.metadata.model_dump(),
    )
    engine = RuntimeEngine(graph=graph, session_id="sess_s2_27", user_id="usr_s2_27")
    async for _ in engine.run():
        pass
    first_node = list(graph.nodes.values())[0]
    assert first_node.status == TaskState.FAILED
    assert engine.get_goal_verification().overall_status == "failed"


@pytest.mark.asyncio
async def test_s2_28_and_31_no_evidence_free_verification_or_status_bypass():
    """Scenarios 28 & 31: Completed tasks without empirical evidence yield 'unverified' goal status and 0.0 confidence."""
    graph = ARDOGraph(session_id="sess_s2_28", user_id="usr_s2_28", plan_hash="ph_s2_28")
    graph.nodes["t1"] = ARDOTaskNode(task_id="t1", task_name="Step 1", status=TaskState.COMPLETED)
    gv = SemanticVerifier.verify_goal_outcome("Test Goal", graph, "sess_s2_28", evidence_items=[], user_id="usr_s2_28")
    assert gv.overall_status == "unverified"
    assert gv.verified_tasks == 0
    assert gv.confidence_score == 0.0


@pytest.mark.asyncio
async def test_s2_29_and_30_fallback_tasks_and_memory_playbook_protection():
    """Scenarios 29 & 30: Fallback plans even with valid calculator tool execution never promote to playbooks or 'success' memory."""
    fb = await generate_fallback_plan("Calculate 6 * 7")
    graph = DAGBuilder.build(
        [n.model_dump() for n in fb.nodes],
        [e.model_dump() for e in fb.edges],
        execution_mode="sequential",
        session_id="sess_s2_30",
        user_id="usr_s2_30",
        is_fallback=True,
        fallback_reason=fb.metadata.fallback_reason,
        metadata=fb.metadata.model_dump(),
    )
    engine = RuntimeEngine(graph=graph, session_id="sess_s2_30", user_id="usr_s2_30")
    async for _ in engine.run():
        pass

    calc_node = list(graph.nodes.values())[0]
    assert calc_node.status == TaskState.COMPLETED
    assert calc_node.tool_result.output_data["result"] == 42

    # Candidate playbook generation must return None for fallback graph
    ev_items = EvidenceVault.get_session_evidence("sess_s2_30", "usr_s2_30")
    pb_id = PlaybookService.generate_candidate_playbook(
        user_id="usr_s2_30",
        session_id="sess_s2_30",
        goal_text="Calculate 6 * 7",
        graph=graph,
        goal_verification=engine.get_goal_verification(),
        evidence_records=ev_items,
    )
    assert pb_id is None
