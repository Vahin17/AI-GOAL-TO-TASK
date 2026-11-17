"""
backend/tests/test_t7_1_security_remediation.py
================================================
Targeted regression tests for Task T7.1 Security Remediation:
- DEF-T7-01: Checkpoint scheduler ownership fail-closed validation.
- DEF-T7-02: Semantic memory tenant isolation in planning pipeline.
- DEF-T7-03: Browser inspection credential redaction.
- DEF-T7-04: Approval plan-hash dynamic derivation and fail-closed validation.
"""

import asyncio
import re
import time
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from main import app
from core.database import Database
from core.auth_db import AuthDatabase
from orchestration.scheduler import ARDOScheduler, ARDOSession
from orchestration.dag_builder import DAGBuilder
from orchestration.checkpoint_manager import ExecutionCheckpointManager
from knowledge.semantic_memory import SemanticMemoryStore
from knowledge.rag_service import RAGService
from knowledge.embeddings import HybridEmbeddingService
from execution.browser.environment_perception import EnvironmentPerceptionEngine, _DOM_DISTILLATION_SCRIPT


from unittest.mock import AsyncMock, MagicMock, patch

@pytest.fixture(autouse=True)
def setup_auth_sqlite(monkeypatch):
    """Ensure tests run against an isolated SQLite database and mock out external services."""
    monkeypatch.setenv("AUTH_DB_BACKEND", "sqlite")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-32-bytes-minimum-length-ok")
    monkeypatch.setattr("core.database.Database.connect_db", AsyncMock())
    monkeypatch.setattr("core.database.Database.close_db", AsyncMock())
    ARDOScheduler._sessions.clear()
    ARDOScheduler._session_history.clear()
    HybridEmbeddingService.reset_state()
    db = AuthDatabase.reset_instance(force_sqlite=True)
    yield db
    ARDOScheduler._sessions.clear()
    ARDOScheduler._session_history.clear()
    HybridEmbeddingService.reset_state()


def _create_user(db: AuthDatabase, email: str = "alice_t71@ardo.ai") -> tuple[dict, str]:
    from core.security import generate_session_token, hash_password
    user = db.create_user(
        full_name=f"User {email}",
        email=email,
        password_hash=hash_password("SecurePass123!@#"),
        is_active=True,
    )
    token = generate_session_token()
    db.create_session(user["id"], token)
    return user, token


# ─────────────────────────────────────────────────────────────────────────────
# DEF-T7-01: Checkpoint Scheduler Ownership Tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_def_t7_01_scheduler_resume_fails_closed_without_user_id():
    """
    DEF-T7-01 Remediation:
    ARDOScheduler.resume() must reject missing caller identity (user_id=None or empty).
    """
    session_id = "ardo_t71_resume_no_user"
    nodes = [{"id": "t1", "type": "default", "data": {"label": "Task 1"}}]
    graph = DAGBuilder.build(nodes, [], goal_text="Test Goal")
    graph.session_id = session_id
    graph.user_id = "user_owner_a"

    # Save checkpoint owned by user_owner_a
    await ExecutionCheckpointManager.save_checkpoint(
        session_id=session_id,
        graph=graph,
        trigger="test_save",
        user_id="user_owner_a",
    )

    # Calling resume with user_id=None must raise ValueError (fail closed)
    with pytest.raises(ValueError, match="Authenticated user_id is required"):
        await ARDOScheduler.resume(session_id, user_id=None)

    # Calling resume with empty string must also raise ValueError
    with pytest.raises(ValueError, match="Authenticated user_id is required"):
        await ARDOScheduler.resume(session_id, user_id="")


@pytest.mark.asyncio
async def test_def_t7_01_scheduler_resume_fails_closed_on_unowned_legacy_checkpoint():
    """
    DEF-T7-01 Remediation:
    An unowned/legacy checkpoint (user_id=None) cannot be resumed by any user.
    """
    session_id = "ardo_t71_legacy_checkpoint"
    nodes = [{"id": "t1", "type": "default", "data": {"label": "Legacy Task"}}]
    graph = DAGBuilder.build(nodes, [], goal_text="Legacy Goal")
    graph.session_id = session_id
    graph.user_id = None

    # Save unowned checkpoint
    await ExecutionCheckpointManager.save_checkpoint(
        session_id=session_id,
        graph=graph,
        trigger="legacy_save",
        user_id=None,
    )

    # User B attempting to resume unowned checkpoint must be rejected with PermissionError
    with pytest.raises(PermissionError, match="Cross-user checkpoint resume forbidden"):
        await ARDOScheduler.resume(session_id, user_id="user_b")


@pytest.mark.asyncio
async def test_def_t7_01_scheduler_resume_fails_closed_on_mismatched_checkpoint():
    """
    DEF-T7-01 Remediation:
    A checkpoint owned by User A cannot be resumed by User B.
    """
    session_id = "ardo_t71_mismatch_test"
    nodes = [{"id": "t1", "type": "default", "data": {"label": "Alice Task"}}]
    graph = DAGBuilder.build(nodes, [], goal_text="Alice Goal")
    graph.session_id = session_id
    graph.user_id = "user_alice"

    await ExecutionCheckpointManager.save_checkpoint(
        session_id=session_id,
        graph=graph,
        trigger="alice_save",
        user_id="user_alice",
    )

    # Bob attempts to resume Alice's session -> PermissionError
    with pytest.raises(PermissionError, match="Cross-user checkpoint resume forbidden"):
        await ARDOScheduler.resume(session_id, user_id="user_bob")


@pytest.mark.asyncio
async def test_def_t7_01_scheduler_resume_succeeds_for_same_owner():
    """
    DEF-T7-01 Remediation:
    Legitimate same-owner resume succeeds.
    """
    session_id = "ardo_t71_same_owner_test"
    nodes = [{"id": "t1", "type": "default", "data": {"label": "Alice Task"}}]
    graph = DAGBuilder.build(nodes, [], goal_text="Alice Goal")
    graph.session_id = session_id
    graph.user_id = "user_alice"

    await ExecutionCheckpointManager.save_checkpoint(
        session_id=session_id,
        graph=graph,
        trigger="alice_save",
        user_id="user_alice",
    )

    # Alice resumes Alice's session -> succeeds
    resumed_session = await ARDOScheduler.resume(session_id, user_id="user_alice")
    assert resumed_session is not None
    assert resumed_session.user_id == "user_alice"
    assert resumed_session.session_id == session_id


def test_def_t7_01_resume_paused_session_fails_closed_without_user_id():
    """
    DEF-T7-01 Remediation:
    resume_paused_session must fail closed when user_id is missing or None.
    """
    session_id = "ardo_t71_paused_sess"
    nodes = [{"id": "t1", "type": "default", "data": {"label": "Task"}}]
    graph = DAGBuilder.build(nodes, [], goal_text="Paused Goal")
    session = ARDOSession(session_id=session_id, graph=graph, mode="safe", user_id="user_alice")
    ARDOScheduler._sessions[session_id] = session

    # Calling without user_id must return False
    assert ARDOScheduler.resume_paused_session(session_id, user_id=None) is False
    assert ARDOScheduler.resume_paused_session(session_id, user_id="") is False
    # Calling with different user_id must return False
    assert ARDOScheduler.resume_paused_session(session_id, user_id="user_bob") is False


# ─────────────────────────────────────────────────────────────────────────────
# DEF-T7-02: Semantic Memory Isolation in Planning Pipeline Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_def_t7_02_rag_service_omits_memories_when_user_id_is_none():
    """
    DEF-T7-02 Remediation:
    When user_id is None in get_relevant_context_for_goal(), zero memories are retrieved.
    Unauthenticated planning can never trigger an unscoped cross-tenant query.
    """
    rag = RAGService.get_instance()
    # Record a confidential memory
    rag.memory_store.record_memory(
        goal_text="Confidential corporate treasury transfer",
        task_summary="Signed off wire transfer to vendor",
        outcome_status="success",
        user_id="user_corp_treasury",
    )

    # 1. Query with user_id=None (unauthenticated) -> must NOT retrieve the memory
    ctx_unauth, prov_unauth = rag.get_relevant_context_for_goal(
        "corporate treasury transfer",
        user_id=None,
    )
    assert "treasury transfer" not in ctx_unauth.lower() or "wire transfer" not in ctx_unauth.lower()

    # 2. Query with user_id="user_corp_treasury" -> retrieves the memory
    ctx_owner, prov_owner = rag.get_relevant_context_for_goal(
        "corporate treasury transfer",
        user_id="user_corp_treasury",
    )
    assert "wire transfer" in ctx_owner.lower()

    # 3. Query with user_id="user_intruder" -> returns 0 memories
    ctx_intruder, prov_intruder = rag.get_relevant_context_for_goal(
        "corporate treasury transfer",
        user_id="user_intruder",
    )
    assert "wire transfer" not in ctx_intruder.lower()


@pytest.mark.asyncio
async def test_def_t7_02_orchestrator_threads_user_id_into_planning():
    """
    DEF-T7-02 Remediation:
    generate_task_graph(goal, user_id=...) forwards user_id into RAGService.
    """
    from ai.orchestrator import generate_task_graph

    rag = RAGService.get_instance()
    rag.memory_store.record_memory(
        goal_text="Internal confidential audit of Alpha project",
        task_summary="Audited secret Alpha ledger",
        outcome_status="success",
        user_id="user_alpha_lead",
    )

    # Mock provider to inspect passed rag_context
    with patch("ai.orchestrator.get_llm_provider") as mock_get_provider:
        mock_provider = MagicMock()
        from schemas.models import PlanGraphResponse, PlanGraphMetadata
        mock_provider.generate_plan = MagicMock()
        mock_provider.generate_plan.return_value = asyncio.Future()
        mock_provider.generate_plan.return_value.set_result(
            PlanGraphResponse(
                plan_id="plan_test_01",
                nodes=[],
                edges=[],
                public_id="plan_test_01",
                metadata=PlanGraphMetadata(
                    intent="test",
                    confidence=1.0,
                    reasoning_summary="test reasoning",
                ),
            )
        )
        mock_get_provider.return_value = mock_provider

        # A. Planning with user_id=None -> rag_context must NOT contain Alice's secret ledger
        await generate_task_graph("Internal audit of Alpha project", user_id=None)
        call_kwargs_unauth = mock_provider.generate_plan.call_args.kwargs
        rag_ctx_unauth = call_kwargs_unauth.get("rag_context", "")
        assert "secret alpha ledger" not in rag_ctx_unauth.lower()

        # B. Planning with user_id="user_alpha_lead" -> rag_context contains Alice's memory
        await generate_task_graph("Internal audit of Alpha project", user_id="user_alpha_lead")
        call_kwargs_owner = mock_provider.generate_plan.call_args.kwargs
        rag_ctx_owner = call_kwargs_owner.get("rag_context", "")
        assert "secret alpha ledger" in rag_ctx_owner.lower()


def test_def_t7_02_api_plan_endpoint_passes_authenticated_user_id(setup_auth_sqlite):
    """
    DEF-T7-02 Remediation:
    POST /plan extracts authenticated user from session cookie when present.
    """
    db = setup_auth_sqlite
    user, token = _create_user(db, "alice_planner@ardo.ai")

    rag = RAGService.get_instance()
    rag.memory_store.record_memory(
        goal_text="Alice confidential deployment playbook",
        task_summary="Deploy Alice cluster",
        outcome_status="success",
        user_id=user["id"],
    )

    client = TestClient(app)

    with patch("api.router.generate_task_graph", new_callable=AsyncMock) as mock_gen:
        from schemas.models import PlanGraphResponse, PlanGraphMetadata
        mock_gen.return_value = PlanGraphResponse(
            plan_id="p1",
            nodes=[],
            edges=[],
            public_id="p1",
            metadata=PlanGraphMetadata(
                intent="test",
                confidence=1.0,
                reasoning_summary="test reasoning",
            ),
        )

        # 1. Unauthenticated call -> user_id=None passed to generate_task_graph
        client.post("/plan", json={"goal": "Alice deployment playbook"})
        assert mock_gen.call_args.kwargs.get("user_id") is None

        # 2. Authenticated call -> user["id"] passed to generate_task_graph
        client.post(
            "/plan",
            json={"goal": "Alice deployment playbook"},
            cookies={"ardo_session": token},
        )
        assert mock_gen.call_args.kwargs.get("user_id") == user["id"]


# ─────────────────────────────────────────────────────────────────────────────
# DEF-T7-03: Browser Inspection Credential Redaction Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_def_t7_03_perception_script_detects_and_redacts_sensitive_inputs():
    """
    DEF-T7-03 Remediation:
    _DOM_DISTILLATION_SCRIPT contains strict redaction logic for password and sensitive inputs.
    """
    # Verify script contains redaction for password and sensitive terms
    assert "inputType === 'password'" in _DOM_DISTILLATION_SCRIPT
    assert "[REDACTED]" in _DOM_DISTILLATION_SCRIPT
    assert "password|secret|token|apikey|api_key|auth|credential|cvv|cvc|cardnumber" in _DOM_DISTILLATION_SCRIPT


def test_def_t7_03_api_inspect_redacts_credentials_before_serialization(setup_auth_sqlite):
    """
    DEF-T7-03 Remediation:
    GET /orchestrate/{session_id}/inspect redacts password and secret fields in the returned snapshot.
    """
    db = setup_auth_sqlite
    user, token = _create_user(db, "alice_inspect@ardo.ai")

    session_id = "ardo_inspect_redaction_test"
    nodes = [{"id": "t1", "type": "default", "data": {"label": "Inspect Task"}}]
    graph = DAGBuilder.build(nodes, [], goal_text="Inspect Goal")
    session = ARDOSession(session_id=session_id, graph=graph, mode="safe", user_id=user["id"])

    # Mock active browser adapter with page containing sensitive inputs
    mock_adapter = MagicMock()
    mock_adapter.executor._page = MagicMock()
    session.browser_adapter = mock_adapter

    ARDOScheduler._sessions[session_id] = session

    # Mock EnvironmentPerceptionEngine to return a snapshot with both normal and sensitive inputs
    from execution.browser.perception_models import PageSnapshot, PageElement
    fake_snapshot = PageSnapshot(
        url="http://127.0.0.1:8000/login",
        title="Login Page",
        elements=[
            PageElement(
                tag="input",
                element_id="username-field",
                name="username",
                input_type="text",
                value="alice_admin",
                css_selector="#username-field",
            ),
            PageElement(
                tag="input",
                element_id="password-field",
                name="password",
                input_type="password",
                value="SuperSecretPassword123!",
                css_selector="#password-field",
            ),
            PageElement(
                tag="input",
                element_id="api-token-field",
                name="api_token",
                input_type="text",
                value="sk-antigravity-99999",
                css_selector="#api-token-field",
            ),
        ],
    )

    with patch("execution.browser.environment_perception.EnvironmentPerceptionEngine.inspect", new_callable=AsyncMock) as mock_inspect:
        mock_inspect.return_value = fake_snapshot

        client = TestClient(app)
        res = client.get(
            f"/orchestrate/{session_id}/inspect",
            cookies={"ardo_session": token},
        )
        assert res.status_code == 200
        data = res.json()
        elements = data["snapshot"]["elements"]

        elem_by_id = {e["element_id"]: e for e in elements}

        # 1. Non-sensitive username value is preserved
        assert elem_by_id["username-field"]["value"] == "alice_admin"

        # 2. Password input value is REDACTED
        assert elem_by_id["password-field"]["value"] == "[REDACTED]"
        assert "SuperSecretPassword123!" not in res.text

        # 3. API token input value is REDACTED
        assert elem_by_id["api-token-field"]["value"] == "[REDACTED]"
        assert "sk-antigravity-99999" not in res.text


# ─────────────────────────────────────────────────────────────────────────────
# DEF-T7-04: Approval Plan-Hash Validation Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_def_t7_04_approve_task_rejects_missing_empty_and_null_hash(setup_auth_sqlite):
    """
    DEF-T7-04 Remediation:
    POST /orchestrate/{session_id}/approve/{task_id} rejects missing, null, or empty plan_hash with HTTP 400.
    """
    db = setup_auth_sqlite
    user, token = _create_user(db, "alice_approval@ardo.ai")

    session_id = "ardo_appr_hash_test_1"
    nodes = [{"id": "t1", "type": "default", "data": {"label": "Approval Task"}}]
    graph = DAGBuilder.build(nodes, [], goal_text="Approval Goal")
    session = ARDOSession(session_id=session_id, graph=graph, mode="safe", user_id=user["id"])
    ARDOScheduler._sessions[session_id] = session

    client = TestClient(app)

    # 1. Missing plan_hash
    res1 = client.post(
        f"/orchestrate/{session_id}/approve/t1",
        json={"approved": True},
        cookies={"ardo_session": token},
    )
    assert res1.status_code == 400
    assert "Missing plan_hash" in res1.json()["detail"]

    # 2. Empty string plan_hash
    res2 = client.post(
        f"/orchestrate/{session_id}/approve/t1",
        json={"approved": True, "plan_hash": ""},
        cookies={"ardo_session": token},
    )
    assert res2.status_code == 400
    assert "Missing plan_hash" in res2.json()["detail"]

    # 3. Whitespace only plan_hash
    res3 = client.post(
        f"/orchestrate/{session_id}/approve/t1",
        json={"approved": True, "plan_hash": "   "},
        cookies={"ardo_session": token},
    )
    assert res3.status_code == 400
    assert "Missing plan_hash" in res3.json()["detail"]


def test_def_t7_04_approve_task_rejects_old_hash_after_plan_mutation(setup_auth_sqlite):
    """
    DEF-T7-04 Remediation:
    When a plan is mutated after generation, approving with the old (stale) hash
    is rejected with HTTP 400 because the authoritative hash is dynamically recomputed.
    """
    db = setup_auth_sqlite
    user, token = _create_user(db, "alice_mutation@ardo.ai")

    session_id = "ardo_mutation_test"
    nodes = [
        {"id": "t1", "type": "default", "data": {"label": "Initial Task 1"}},
        {"id": "t2", "type": "default", "data": {"label": "Initial Task 2"}},
    ]
    edges = [{"source": "t1", "target": "t2"}]
    graph = DAGBuilder.build(nodes, edges, goal_text="Initial Goal")
    initial_hash = graph.plan_hash

    session = ARDOSession(session_id=session_id, graph=graph, mode="safe", user_id=user["id"])
    ARDOScheduler._sessions[session_id] = session

    # Mutate the plan: alter a node's task_name
    graph.nodes["t2"].task_name = "Tampered Malicious Task"
    new_hash = graph.recompute_plan_hash()
    assert new_hash != initial_hash

    client = TestClient(app)

    # 1. Attempting to approve using the OLD initial_hash must fail with HTTP 400
    res_stale = client.post(
        f"/orchestrate/{session_id}/approve/t1",
        json={"approved": True, "plan_hash": initial_hash},
        cookies={"ardo_session": token},
    )
    assert res_stale.status_code == 400
    assert "Plan hash mismatch" in res_stale.json()["detail"]

    # 2. Approving using the NEW recomputed hash must succeed with HTTP 200
    res_valid = client.post(
        f"/orchestrate/{session_id}/approve/t1",
        json={"approved": True, "plan_hash": new_hash},
        cookies={"ardo_session": token},
    )
    assert res_valid.status_code == 200
    assert res_valid.json()["status"] == "approved"
    assert res_valid.json()["plan_hash"] == new_hash


def test_def_t7_04_approve_task_rejects_cross_session_hash(setup_auth_sqlite):
    """
    DEF-T7-04 Remediation:
    Submitting a valid hash from a different session is rejected with HTTP 400.
    """
    db = setup_auth_sqlite
    user, token = _create_user(db, "alice_cross@ardo.ai")

    # Session 1
    nodes1 = [{"id": "t1", "type": "default", "data": {"label": "Session 1 Task"}}]
    graph1 = DAGBuilder.build(nodes1, [], goal_text="Goal 1")
    session1 = ARDOSession(session_id="sess_01", graph=graph1, mode="safe", user_id=user["id"])
    ARDOScheduler._sessions["sess_01"] = session1

    # Session 2
    nodes2 = [{"id": "t1", "type": "default", "data": {"label": "Session 2 Task"}}]
    graph2 = DAGBuilder.build(nodes2, [], goal_text="Goal 2")
    session2 = ARDOSession(session_id="sess_02", graph=graph2, mode="safe", user_id=user["id"])
    ARDOScheduler._sessions["sess_02"] = session2

    client = TestClient(app)

    # Submit Session 2's hash to Session 1's approval endpoint -> 400 Mismatch
    res = client.post(
        "/orchestrate/sess_01/approve/t1",
        json={"approved": True, "plan_hash": graph2.plan_hash},
        cookies={"ardo_session": token},
    )
    assert res.status_code == 400
    assert "Plan hash mismatch" in res.json()["detail"]
