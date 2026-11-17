"""
backend/tests/test_t7_1_1_final_security_gap.py
================================================
Targeted verification tests for T7.1.1 residual security gaps:
- Finding 1 (DEF-T7-01): Strict fail-closed verification of caller & session owner in resume_paused_session & resume.
- Finding 2 (DEF-T7-02): Direct SQL and in-memory tenant isolation; legacy memory leak prevention.
- Finding 3 (DEF-T7-03): Final serialized HTTP inspection response credential redaction vs. non-sensitive preservation.
- Finding 4 (DEF-T7-04): Cryptographic session binding preventing cross-session replay on identical plan DAGs.
"""

import asyncio
import json
import re
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from main import app
from core.database import Database
from core.auth_db import AuthDatabase
from orchestration.scheduler import ARDOScheduler, ARDOSession
from orchestration.dag_builder import DAGBuilder, ARDOGraph
from orchestration.checkpoint_manager import ExecutionCheckpointManager
from orchestration.plan_hasher import compute_plan_hash
from knowledge.semantic_memory import SemanticMemoryStore, ExecutionMemoryItem
from knowledge.rag_service import RAGService
from knowledge.embeddings import HybridEmbeddingService, DeterministicLocalEmbeddingProvider
from execution.browser.perception_models import PageSnapshot, PageElement


@pytest.fixture(autouse=True)
def setup_auth_sqlite(monkeypatch):
    """Isolate auth database and scheduler in-memory state."""
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


def _create_user(db: AuthDatabase, email: str) -> tuple[dict, str]:
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
# Finding 1: DEF-T7-01 Fail-Closed Paused Session & Scheduler Resume Invariants
# ─────────────────────────────────────────────────────────────────────────────

def test_def_t7_01_resume_paused_session_missing_caller_identity_fails_closed():
    """Missing caller identity (user_id=None) must fail closed."""
    session_id = "sess_p1_no_caller"
    graph = DAGBuilder.build([{"id": "t1", "type": "default", "data": {"label": "T1"}}], [])
    session = ARDOSession(session_id=session_id, graph=graph, mode="safe", user_id="user_alice")
    ARDOScheduler._sessions[session_id] = session

    assert ARDOScheduler.resume_paused_session(session_id, user_id=None) is False


def test_def_t7_01_resume_paused_session_empty_or_whitespace_caller_fails_closed():
    """Empty and whitespace-only caller identity must fail closed."""
    session_id = "sess_p1_whitespace_caller"
    graph = DAGBuilder.build([{"id": "t1", "type": "default", "data": {"label": "T1"}}], [])
    session = ARDOSession(session_id=session_id, graph=graph, mode="safe", user_id="user_alice")
    ARDOScheduler._sessions[session_id] = session

    assert ARDOScheduler.resume_paused_session(session_id, user_id="") is False
    assert ARDOScheduler.resume_paused_session(session_id, user_id="   ") is False
    assert ARDOScheduler.resume_paused_session(session_id, user_id="\t\n ") is False


def test_def_t7_01_resume_paused_session_missing_or_empty_session_owner_fails_closed():
    """Session whose stored owner is None, empty, or whitespace must never be resumed."""
    graph = DAGBuilder.build([{"id": "t1", "type": "default", "data": {"label": "T1"}}], [])

    # Case A: session.user_id is None
    sess_none = ARDOSession(session_id="sess_owner_none", graph=graph, mode="safe", user_id=None)
    ARDOScheduler._sessions["sess_owner_none"] = sess_none
    assert ARDOScheduler.resume_paused_session("sess_owner_none", user_id="user_alice") is False

    # Case B: session.user_id is empty string
    sess_empty = ARDOSession(session_id="sess_owner_empty", graph=graph, mode="safe", user_id="")
    ARDOScheduler._sessions["sess_owner_empty"] = sess_empty
    assert ARDOScheduler.resume_paused_session("sess_owner_empty", user_id="user_alice") is False

    # Case C: session.user_id is whitespace
    sess_ws = ARDOSession(session_id="sess_owner_ws", graph=graph, mode="safe", user_id="   ")
    ARDOScheduler._sessions["sess_owner_ws"] = sess_ws
    assert ARDOScheduler.resume_paused_session("sess_owner_ws", user_id="user_alice") is False


def test_def_t7_01_resume_paused_session_mismatched_owner_fails_closed():
    """Mismatched caller identity vs. stored session owner must fail closed."""
    session_id = "sess_mismatch_owner"
    graph = DAGBuilder.build([{"id": "t1", "type": "default", "data": {"label": "T1"}}], [])
    session = ARDOSession(session_id=session_id, graph=graph, mode="safe", user_id="user_alice")
    ARDOScheduler._sessions[session_id] = session

    assert ARDOScheduler.resume_paused_session(session_id, user_id="user_bob") is False
    assert ARDOScheduler.resume_paused_session(session_id, user_id="user_alice_impostor") is False


def test_def_t7_01_resume_paused_session_matching_owner_succeeds():
    """Legitimate resume by exact matching owner succeeds."""
    session_id = "sess_matching_owner"
    graph = DAGBuilder.build([{"id": "t1", "type": "default", "data": {"label": "T1"}}], [])
    session = ARDOSession(session_id=session_id, graph=graph, mode="safe", user_id="user_alice")
    ARDOScheduler._sessions[session_id] = session

    # Mock session.resume to return True
    session.resume = MagicMock(return_value=True)
    assert ARDOScheduler.resume_paused_session(session_id, user_id="user_alice") is True
    session.resume.assert_called_once()


@pytest.mark.asyncio
async def test_def_t7_01_checkpoint_resume_all_fail_closed_permutations():
    """ARDOScheduler.resume() rejects missing, empty, whitespace, and ownerless checkpoints."""
    session_id = "sess_chk_failclosed"
    graph = DAGBuilder.build([{"id": "t1", "type": "default", "data": {"label": "T1"}}], [])

    # 1. Missing caller ID -> ValueError
    with pytest.raises(ValueError, match="Authenticated user_id is required"):
        await ARDOScheduler.resume(session_id, user_id=None)
    with pytest.raises(ValueError, match="Authenticated user_id is required"):
        await ARDOScheduler.resume(session_id, user_id="")
    with pytest.raises(ValueError, match="Authenticated user_id is required"):
        await ARDOScheduler.resume(session_id, user_id="   ")

    # 2. Checkpoint with None owner -> PermissionError
    await ExecutionCheckpointManager.save_checkpoint(
        session_id=session_id, graph=graph, trigger="test", user_id=None
    )
    with pytest.raises(PermissionError, match="Cross-user checkpoint resume forbidden"):
        await ARDOScheduler.resume(session_id, user_id="user_alice")

    # 3. Checkpoint with empty/whitespace owner -> PermissionError
    await ExecutionCheckpointManager.save_checkpoint(
        session_id=session_id, graph=graph, trigger="test", user_id="   "
    )
    with pytest.raises(PermissionError, match="Cross-user checkpoint resume forbidden"):
        await ARDOScheduler.resume(session_id, user_id="user_alice")


# ─────────────────────────────────────────────────────────────────────────────
# Finding 2: DEF-T7-02 SQL Tenant Isolation & Legacy Memory Protection
# ─────────────────────────────────────────────────────────────────────────────

def test_def_t7_02_in_memory_tenant_isolation():
    """In-memory store strictly prevents User A from retrieving User B's memories."""
    provider = DeterministicLocalEmbeddingProvider(dim=128)
    store = SemanticMemoryStore(in_memory=True, embedding_provider=provider)

    # Record User A memory
    store.record_memory(
        goal_text="Corporate treasury disbursement authorization",
        task_summary="Signed disbursement batch 101",
        outcome_status="success",
        user_id="user_alice",
    )
    # Record User B memory
    store.record_memory(
        goal_text="Internal confidential audit of project Titan",
        task_summary="Audited secret payroll records",
        outcome_status="success",
        user_id="user_bob",
    )
    # Record legacy unowned memory
    legacy_item = ExecutionMemoryItem(
        memory_id="mem_legacy_unowned",
        goal_text="Legacy deployment workflow",
        task_summary="Executed ancient migration",
        outcome_status="success",
        user_id=None,
        metadata={"embedding_space": provider.embedding_space},
    )
    store._local_memories[legacy_item.memory_id] = legacy_item
    store._local_embeddings[legacy_item.memory_id] = provider.embed_query(legacy_item.goal_text)

    # 1. User Alice retrieves -> gets only Alice's memory, NOT Bob's or legacy
    alice_results = store.retrieve_memories("treasury disbursement", user_id="user_alice")
    assert len(alice_results) == 1
    assert alice_results[0].user_id == "user_alice"

    # Alice queries Bob's domain -> gets 0 results
    alice_bob_results = store.retrieve_memories("audit project Titan payroll", user_id="user_alice")
    assert len(alice_bob_results) == 0

    # 2. User Bob retrieves -> gets only Bob's memory, NOT Alice's or legacy
    bob_results = store.retrieve_memories("audit project Titan payroll", user_id="user_bob")
    assert len(bob_results) == 1
    assert bob_results[0].user_id == "user_bob"

    # 3. Missing/empty/whitespace identity fails closed (0 results)
    assert len(store.retrieve_memories("treasury disbursement", user_id="")) == 0
    assert len(store.retrieve_memories("treasury disbursement", user_id="   ")) == 0


def test_def_t7_02_sql_query_construction_verifies_tenant_isolation():
    """Inspects the exact SQL WHERE clause and parameter binding for PostgreSQL pgvector & tsvector."""
    provider = DeterministicLocalEmbeddingProvider(dim=128)
    store = SemanticMemoryStore(in_memory=True, embedding_provider=provider)
    store._is_pg_connected = True
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    store._pg_conn = mock_conn
    store.db_vector_dim = 128

    # 1. Calling retrieve_memories with user_id="user_tenant_1"
    # Return 1 matching row so the primary pgvector query results are returned without falling back to tsvector
    mock_cursor.fetchall.return_value = [
        ("mem_1", "Deploy cloud", "p1", "s1", "goal", "summary", "success", "{}", "{}", '{"user_id": "user_tenant_1"}', 0.9, None)
    ]
    store.retrieve_memories("Deploy cloud infrastructure", user_id="user_tenant_1")

    assert mock_cursor.execute.called
    sql_executed, params_executed = mock_cursor.execute.call_args[0]

    # Verify SQL strictly includes metadata->>'user_id' = %s
    assert "metadata->>'user_id' = %s" in sql_executed
    assert "metadata->>'embedding_space' = %s" in sql_executed
    assert "embedding IS NOT NULL" in sql_executed

    # Verify user_tenant_1 is in the parameter list
    assert "user_tenant_1" in params_executed

    # 2. Calling with whitespace user_id fails closed before executing SQL
    mock_cursor.reset_mock()
    res = store.retrieve_memories("Deploy cloud infrastructure", user_id="   ")
    assert res == []
    assert not mock_cursor.execute.called

    # 3. tsvector fallback also strictly enforces user_id
    mock_cursor.reset_mock()
    store._has_tsv_column = True
    store._retrieve_memories_tsvector("Deploy cloud", user_id="user_tenant_1")
    assert mock_cursor.execute.called
    ts_sql, ts_params = mock_cursor.execute.call_args[0]
    assert "metadata->>'user_id' = %s" in ts_sql
    assert "user_tenant_1" in ts_params


def test_def_t7_02_planning_pipeline_preserves_authenticated_identity(setup_auth_sqlite):
    """POST /plan extracts authenticated user and forwards it into the planner and RAG context."""
    db = setup_auth_sqlite
    user, token = _create_user(db, "alice_planner_direct@ardo.ai")

    client = TestClient(app)

    with patch("api.router.generate_task_graph", new_callable=AsyncMock) as mock_plan:
        from schemas.models import PlanGraphResponse, PlanGraphMetadata
        mock_plan.return_value = PlanGraphResponse(
            plan_id="p101",
            nodes=[],
            edges=[],
            public_id="p101",
            metadata=PlanGraphMetadata(intent="test", confidence=1.0, reasoning_summary="ok"),
        )

        # 1. Unauthenticated request -> user_id=None
        res_unauth = client.post("/plan", json={"goal": "Unauthenticated goal"})
        assert res_unauth.status_code == 200
        assert mock_plan.call_args.kwargs.get("user_id") is None

        # 2. Authenticated request -> user_id=user["id"]
        res_auth = client.post(
            "/plan",
            json={"goal": "Authenticated goal"},
            cookies={"ardo_session": token},
        )
        assert res_auth.status_code == 200
        assert mock_plan.call_args.kwargs.get("user_id") == user["id"]


# ─────────────────────────────────────────────────────────────────────────────
# Finding 3: DEF-T7-03 Final Serialized Credential Redaction Verification
# ─────────────────────────────────────────────────────────────────────────────

def test_def_t7_03_serialized_inspect_response_redacts_all_sensitive_categories(setup_auth_sqlite):
    """
    Directly tests the final HTTP serialized response of GET /orchestrate/{session_id}/inspect:
    - Passwords, API keys, access/refresh tokens, auth/credential fields, CVV/CVC, card numbers, SSNs, autocomplete metadata.
    - Confirms non-sensitive fields (username, search query, item count, notes) are PRESERVED.
    """
    db = setup_auth_sqlite
    user, token = _create_user(db, "alice_security_audit@ardo.ai")

    session_id = "ardo_inspect_final_audit"
    graph = DAGBuilder.build([{"id": "t1", "type": "default", "data": {"label": "Audit Task"}}], [])
    session = ARDOSession(session_id=session_id, graph=graph, mode="safe", user_id=user["id"])
    mock_adapter = MagicMock()
    mock_adapter.executor._page = MagicMock()
    session.browser_adapter = mock_adapter
    ARDOScheduler._sessions[session_id] = session

    # Construct comprehensive test snapshot containing both sensitive and non-sensitive elements
    elements = [
        # Non-sensitive (must be preserved)
        PageElement(tag="input", element_id="user-name", name="username", input_type="text", value="alice_lead_dev", css_selector="#user-name"),
        PageElement(tag="input", element_id="search-input", name="q", input_type="text", value="quantum cryptography", css_selector="#search-input"),
        PageElement(tag="input", element_id="item-qty", name="quantity", input_type="number", value="42", css_selector="#item-qty"),
        PageElement(tag="textarea", element_id="order-notes", name="notes", value="Deliver after 5pm", css_selector="#order-notes"),

        # 1. Passwords (type=password, name=pwd)
        PageElement(tag="input", element_id="pwd-field", name="password", input_type="password", value="P@ssw0rd999!", css_selector="#pwd-field"),
        PageElement(tag="input", element_id="user-pin", name="pin", input_type="text", value="1234", css_selector="#user-pin"),

        # 2. API Keys
        PageElement(tag="input", element_id="gemini-key", name="api_key", input_type="text", value="AIzaSyA1234567890", css_selector="#gemini-key"),
        PageElement(tag="input", element_id="openai-key", name="apiKey", input_type="text", value="sk-live-0987654321", css_selector="#openai-key"),

        # 3. Access & Refresh Tokens / Bearer
        PageElement(tag="input", element_id="access-token", name="access_token", input_type="text", value="eyJh...access", css_selector="#access-token"),
        PageElement(tag="input", element_id="refresh-token", name="refresh_token", input_type="text", value="eyJh...refresh", css_selector="#refresh-token"),
        PageElement(tag="input", element_id="bearer-tok", name="bearer", input_type="text", value="bearer_xyz_token", css_selector="#bearer-tok"),

        # 4. Authorization & Credentials
        PageElement(tag="input", element_id="auth-hdr", name="auth_header", input_type="text", value="Basic YWxpY2U6c2VjcmV0", css_selector="#auth-hdr"),
        PageElement(tag="input", element_id="priv-key", name="private_key", input_type="text", value="-----BEGIN RSA PRIVATE KEY-----", css_selector="#priv-key"),

        # 5. CVV / CVC
        PageElement(tag="input", element_id="cvv-input", name="cvv", input_type="text", value="888", css_selector="#cvv-input"),
        PageElement(tag="input", element_id="cvc-input", name="cvc", input_type="text", value="999", css_selector="#cvc-input"),

        # 6. Card numbers & Autocomplete
        PageElement(tag="input", element_id="cc-num", name="cardnumber", input_type="text", value="4111222233334444", css_selector="#cc-num"),
        PageElement(tag="input", element_id="auto-cc", name="card", autocomplete="cc-number", input_type="text", value="5500000000000004", css_selector="#auto-cc"),

        # 7. SSNs
        PageElement(tag="input", element_id="ssn-field", name="ssn", input_type="text", value="000-12-3456", css_selector="#ssn-field"),
        PageElement(tag="input", element_id="social-sec", name="social_security", input_type="text", value="999-99-9999", css_selector="#social-sec"),
    ]

    snapshot = PageSnapshot(url="http://127.0.0.1:8000/checkout", title="Checkout", elements=elements)

    with patch("execution.browser.environment_perception.EnvironmentPerceptionEngine.inspect", new_callable=AsyncMock) as mock_inspect:
        mock_inspect.return_value = snapshot

        client = TestClient(app)
        res = client.get(
            f"/orchestrate/{session_id}/inspect",
            cookies={"ardo_session": token},
        )
        assert res.status_code == 200
        raw_json_text = res.text
        data = res.json()
        elem_map = {e["element_id"]: e for e in data["snapshot"]["elements"]}

        # ── Verification A: Non-sensitive elements are PRESERVED ──
        assert elem_map["user-name"]["value"] == "alice_lead_dev"
        assert elem_map["search-input"]["value"] == "quantum cryptography"
        assert elem_map["item-qty"]["value"] == "42"
        assert elem_map["order-notes"]["value"] == "Deliver after 5pm"

        # ── Verification B: All sensitive elements are REDACTED ──
        sensitive_ids = [
            "pwd-field", "user-pin",
            "gemini-key", "openai-key",
            "access-token", "refresh-token", "bearer-tok",
            "auth-hdr", "priv-key",
            "cvv-input", "cvc-input",
            "cc-num", "auto-cc",
            "ssn-field", "social-sec",
        ]
        for sid in sensitive_ids:
            assert elem_map[sid]["value"] == "[REDACTED]", f"Field {sid} was not redacted!"

        # ── Verification C: Plaintext secrets NEVER appear in HTTP response body ──
        forbidden_plaintexts = [
            "P@ssw0rd999!", "1234", "AIzaSyA1234567890", "sk-live-0987654321",
            "eyJh...access", "eyJh...refresh", "bearer_xyz_token",
            "Basic YWxpY2U6c2VjcmV0", "-----BEGIN RSA PRIVATE KEY-----",
            "888", "999", "4111222233334444", "5500000000000004",
            "000-12-3456", "999-99-9999",
        ]
        for secret in forbidden_plaintexts:
            assert secret not in raw_json_text, f"Plaintext secret '{secret}' leaked in serialized JSON response!"


# ─────────────────────────────────────────────────────────────────────────────
# Finding 4: DEF-T7-04 Cross-Session Approval Replay & Session Binding
# ─────────────────────────────────────────────────────────────────────────────

def test_def_t7_04_cross_session_replay_blocked_for_identical_plans(setup_auth_sqlite):
    """
    CRITICAL DEF-T7-04 SCENARIO:
    Session A and Session B have 100% IDENTICAL plan structures (same nodes, edges, and goal).
    1. Verify that Session A and Session B produce distinct canonical plan hashes bound to their session.
    2. Submitting Session A's approval hash to Session B must be strictly REJECTED with HTTP 400.
    """
    db = setup_auth_sqlite
    user, token = _create_user(db, "alice_operator@ardo.ai")

    nodes = [
        {"id": "t1", "type": "default", "data": {"label": "Query financial database"}},
        {"id": "t2", "type": "default", "data": {"label": "Authorize wire transfer"}},
    ]
    edges = [{"source": "t1", "target": "t2"}]
    goal = "Authorize financial batch transfer"

    # Create Session A
    session_id_a = "ardo_session_alpha"
    graph_a = DAGBuilder.build(nodes, edges, goal_text=goal, session_id=session_id_a, user_id=user["id"])
    session_a = ARDOSession(session_id=session_id_a, graph=graph_a, mode="safe", goal_id=goal, user_id=user["id"])
    ARDOScheduler._sessions[session_id_a] = session_a

    # Create Session B with IDENTICAL nodes, edges, goal
    session_id_b = "ardo_session_beta"
    graph_b = DAGBuilder.build(nodes, edges, goal_text=goal, session_id=session_id_b, user_id=user["id"])
    session_b = ARDOSession(session_id=session_id_b, graph=graph_b, mode="safe", goal_id=goal, user_id=user["id"])
    ARDOScheduler._sessions[session_id_b] = session_b

    # 1. Derive recomputed plan hashes
    hash_a = session_a.graph.recompute_plan_hash(goal_text=goal, session_id=session_id_a, user_id=user["id"])
    hash_b = session_b.graph.recompute_plan_hash(goal_text=goal, session_id=session_id_b, user_id=user["id"])

    # Invariant: Because hashes are bound to session identity, identical plans MUST yield distinct hashes
    assert hash_a != hash_b, "Hashes for two distinct sessions must differ even with identical plans!"

    client = TestClient(app)
    cookies = {"ardo_session": token}

    # 2. Legitimate approval of Session A with Session A's hash -> 200 OK
    res_a = client.post(
        f"/orchestrate/{session_id_a}/approve/t2",
        json={"approved": True, "plan_hash": hash_a},
        cookies=cookies,
    )
    assert res_a.status_code == 200
    assert res_a.json()["status"] == "approved"
    assert res_a.json()["plan_hash"] == hash_a

    # 3. REPLAY ATTACK: Attempt to approve Session B using Session A's hash -> MUST FAIL WITH 400
    res_b_replay = client.post(
        f"/orchestrate/{session_id_b}/approve/t2",
        json={"approved": True, "plan_hash": hash_a},  # Submitting Hash A to Session B
        cookies=cookies,
    )
    assert res_b_replay.status_code == 400
    assert "Plan hash mismatch" in res_b_replay.json()["detail"]

    # 4. Approving Session B with its OWN valid hash -> 200 OK
    res_b_valid = client.post(
        f"/orchestrate/{session_id_b}/approve/t2",
        json={"approved": True, "plan_hash": hash_b},
        cookies=cookies,
    )
    assert res_b_valid.status_code == 200
    assert res_b_valid.json()["status"] == "approved"
    assert res_b_valid.json()["plan_hash"] == hash_b


def test_def_t7_04_tampered_plan_after_approval_fails_runtime_verification(setup_auth_sqlite):
    """
    RuntimeEngine cryptographic verification verifies plan has not been tampered
    prior to executing an approval-gated node.
    """
    db = setup_auth_sqlite
    user, _ = _create_user(db, "alice_runtime@ardo.ai")

    nodes = [
        {"id": "t1", "type": "default", "data": {"label": "High risk trade execution"}},
    ]
    goal = "Execute automated trade"
    session_id = "ardo_trade_session"

    graph = DAGBuilder.build(nodes, [], goal_text=goal, session_id=session_id, user_id=user["id"])
    session = ARDOSession(session_id=session_id, graph=graph, mode="safe", goal_id=goal, user_id=user["id"])
    ARDOScheduler._sessions[session_id] = session

    initial_hash = session.graph.recompute_plan_hash(goal_text=goal, session_id=session_id, user_id=user["id"])

    # Record valid approval for initial_hash
    db.record_plan_approval(
        session_id=session_id,
        user_id=user["id"],
        plan_hash=initial_hash,
        status="approved",
        task_id="t1",
    )
    assert db.is_plan_approved(session_id, initial_hash, task_id="t1") is True

    # Mutate the node after approval (e.g. inject malicious tool call)
    session.graph.nodes["t1"].task_name = "Malicious unauthorized trade dump"
    mutated_hash = session.graph.recompute_plan_hash(goal_text=goal, session_id=session_id, user_id=user["id"])
    assert mutated_hash != initial_hash

    # RuntimeEngine check with mutated graph MUST fail verification
    assert db.is_plan_approved(session_id, mutated_hash, task_id="t1") is False
