"""
test_t7_1_2_final_approval_integrity.py — Task T7.1.2 Final Approval Integrity & Session Access Suite
======================================================================================================
Validates all requirements specified in OWNER AUTHORIZATION: T7.1.2:
1. DEF-T7-04: Removal of stale cached-hash approval fallback:
   - Live graph authoritative recomputation exclusively used.
   - Stale approvals for old hashes cannot authorize mutated plans.
   - Mutated task/edge/parameters are quarantined and never executed.
2. Session lookup identity enforcement in ARDOScheduler.get_session():
   - Missing identity (None) fails closed on protected session access.
   - Missing, empty, or whitespace-only session owner fails closed.
   - Mismatched owner fails closed.
   - Correct authenticated owner retains normal access.
   - Internal system lookup (get_session_internal) preserved for system background sweeps.
   - Unauthenticated API caller cannot read tenant session state.
3. Mandatory plan-hash identity at security-sensitive boundaries:
   - Approval issuance requires non-empty session_id and user_id (fails closed on omission).
   - Runtime dispatch verification requires non-empty session_id and user_id (quarantines on omission).
   - Non-security generic plan hashing without session/user is preserved for structural uses.
4. Live PostgreSQL/pgvector tenant isolation verification:
   - Direct execution against live PostgreSQL (when available).
   - User A memory insertion cannot be retrieved by User B or unauthenticated queries.
"""

import time
import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient

from core.auth_db import AuthDatabase
from core.security import generate_session_token
from orchestration.dag_builder import DAGBuilder
from orchestration.state_machine import TaskState
from orchestration.scheduler import ARDOScheduler, ARDOSession
from orchestration.runtime_engine import RuntimeEngine
from orchestration.plan_hasher import compute_plan_hash
from main import app


@pytest.fixture(autouse=True)
def setup_auth_sqlite(monkeypatch):
    """Hermetic SQLite auth database and clean in-memory session scheduler."""
    monkeypatch.setenv("AUTH_DB_BACKEND", "sqlite")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-32-bytes-minimum-length-ok")
    monkeypatch.setattr("core.database.Database.connect_db", AsyncMock())
    monkeypatch.setattr("core.database.Database.close_db", AsyncMock())
    ARDOScheduler._sessions.clear()
    db = AuthDatabase.reset_instance(force_sqlite=True)
    yield db
    ARDOScheduler._sessions.clear()


def _create_user(db: AuthDatabase, email: str = "operator@ardo.ai"):
    user = db.create_user(
        full_name="Operator User",
        email=email,
        password_hash="test_hash",
        is_active=True,
        is_verified=True,
    )
    token = generate_session_token()
    db.create_session(user["id"], token)
    return user, token


# ── Area 1: DEF-T7-04 Stale Cached-Hash Fallback Removal & Tamper Rejection ───

@pytest.mark.asyncio
async def test_mutated_plan_with_stale_approval_is_quarantined_and_not_executed(setup_auth_sqlite):
    """
    Regression Test:
    1. Create a plan with a safety-gated task.
    2. Approve the original plan hash in AuthDB.
    3. Mutate a task parameter / label after approval.
    4. Verify that approval against the old hash is rejected (no fallback to cached graph.plan_hash).
    5. Verify that the task is quarantined with TaskState.FAILED and not executed.
    """
    db = setup_auth_sqlite
    user, token = _create_user(db, "owner@ardo.ai")
    session_id = "ardo_t712_tamper_sess"

    nodes = [
        {"id": "t_transfer", "type": "default", "data": {"label": "Transfer $100 to Alice", "tool_call": {"amount": 100}}}
    ]
    edges = []
    goal = "Execute funds transfer"

    # 1. Build original plan and compute canonical hash
    graph = DAGBuilder.build(nodes, edges, goal_text=goal, session_id=session_id, user_id=user["id"])
    node = graph.get_node("t_transfer")
    node.approval_needed = True
    original_hash = graph.plan_hash
    assert original_hash is not None

    # 2. Record authoritative approval for the ORIGINAL plan hash
    db.record_plan_approval(
        session_id=session_id,
        user_id=user["id"],
        plan_hash=original_hash,
        task_id="t_transfer",
        status="approved",
    )
    assert db.is_plan_approved(session_id, original_hash, task_id="t_transfer") is True

    # 3. Maliciously mutate the task after approval (e.g. increase amount to $1,000,000)
    node.task_name = "Transfer $1,000,000 to Attacker"
    node.tool_call = {"amount": 1000000, "recipient": "attacker"}

    # 4. Recompute current live hash — must differ from original hash
    mutated_hash = graph.recompute_plan_hash(goal, session_id=session_id, user_id=user["id"])
    assert mutated_hash != original_hash

    # Note: graph.plan_hash still holds the OLD cached hash:
    assert graph.plan_hash == original_hash

    # 5. Execute via RuntimeEngine — must NOT fall back to graph.plan_hash
    engine = RuntimeEngine(
        graph=graph,
        session_id=session_id,
        mode="safe",
        goal_id=goal,
        user_id=user["id"],
    )
    engine.approve_task("t_transfer")  # Operator flag set, but live cryptographic hash doesn't match approval

    events = []
    async for evt in engine.run():
        events.append(evt)

    # 6. Task must be quarantined and NOT completed
    assert node.status == TaskState.FAILED
    assert any("Cryptographic approval verification failed" in log for log in node.execution_logs)
    assert any(evt.event_type == "task_approval_invalidated" for evt in events)


# ── Area 2: Session Lookup Identity Enforcement (ARDOScheduler.get_session) ───

def test_session_lookup_fails_closed_without_identity(setup_auth_sqlite):
    """get_session() must fail closed when caller user_id is None, empty, or whitespace."""
    db = setup_auth_sqlite
    user, _ = _create_user(db, "alice@ardo.ai")
    session_id = "ardo_tenant_sess_01"

    nodes = [{"id": "t1", "type": "default", "data": {"label": "Task 1"}}]
    graph = DAGBuilder.build(nodes, [], session_id=session_id, user_id=user["id"])
    session = ARDOSession(session_id=session_id, graph=graph, mode="safe", user_id=user["id"])
    ARDOScheduler._sessions[session_id] = session

    # 1. Missing identity (None) -> None
    assert ARDOScheduler.get_session(session_id, user_id=None) is None

    # 2. Empty string identity -> None
    assert ARDOScheduler.get_session(session_id, user_id="") is None

    # 3. Whitespace identity -> None
    assert ARDOScheduler.get_session(session_id, user_id="   ") is None


def test_session_lookup_fails_closed_on_unowned_session(setup_auth_sqlite):
    """get_session() must fail closed when the session itself lacks a valid owner."""
    db = setup_auth_sqlite
    user, _ = _create_user(db, "bob@ardo.ai")
    session_id = "ardo_unowned_sess_02"

    nodes = [{"id": "t1", "type": "default", "data": {"label": "Task 1"}}]
    graph = DAGBuilder.build(nodes, [], session_id=session_id)
    # Session owner is None
    session_unowned = ARDOSession(session_id=session_id, graph=graph, mode="safe", user_id=None)
    ARDOScheduler._sessions[session_id] = session_unowned

    assert ARDOScheduler.get_session(session_id, user_id=user["id"]) is None
    assert ARDOScheduler.get_session(session_id, user_id=None) is None

    # Session owner is whitespace
    session_unowned.user_id = "   "
    assert ARDOScheduler.get_session(session_id, user_id=user["id"]) is None


def test_session_lookup_mismatched_and_matching_owner(setup_auth_sqlite):
    """get_session() strictly rejects mismatched owners and permits legitimate matching owners."""
    db = setup_auth_sqlite
    owner, _ = _create_user(db, "owner@ardo.ai")
    intruder, _ = _create_user(db, "intruder@ardo.ai")
    session_id = "ardo_isolation_sess_03"

    nodes = [{"id": "t1", "type": "default", "data": {"label": "Task 1"}}]
    graph = DAGBuilder.build(nodes, [], session_id=session_id, user_id=owner["id"])
    session = ARDOSession(session_id=session_id, graph=graph, mode="safe", user_id=owner["id"])
    ARDOScheduler._sessions[session_id] = session

    # Mismatched owner -> None
    assert ARDOScheduler.get_session(session_id, user_id=intruder["id"]) is None

    # Matching owner -> returns session
    matched = ARDOScheduler.get_session(session_id, user_id=owner["id"])
    assert matched is not None
    assert matched.session_id == session_id
    assert matched.user_id == owner["id"]


def test_trusted_internal_session_lookup_preserved(setup_auth_sqlite):
    """Trusted internal system path get_session_internal() is preserved for background sweeps."""
    db = setup_auth_sqlite
    owner, _ = _create_user(db, "owner@ardo.ai")
    session_id = "ardo_internal_sess_04"

    nodes = [{"id": "t1", "type": "default", "data": {"label": "Task 1"}}]
    graph = DAGBuilder.build(nodes, [], session_id=session_id, user_id=owner["id"])
    session = ARDOSession(session_id=session_id, graph=graph, mode="safe", user_id=owner["id"])
    ARDOScheduler._sessions[session_id] = session

    # Internal system path retrieves session without caller identity
    internal_sess = ARDOScheduler.get_session_internal(session_id)
    assert internal_sess is session


def test_api_state_endpoint_fails_closed_for_unauthenticated_caller(setup_auth_sqlite):
    """GET /orchestrate/{session_id}/state fails closed (404/denied) for unauthenticated callers."""
    db = setup_auth_sqlite
    owner, token = _create_user(db, "owner@ardo.ai")
    session_id = "ardo_api_state_sess_05"

    nodes = [{"id": "t1", "type": "default", "data": {"label": "Secret Task"}}]
    graph = DAGBuilder.build(nodes, [], session_id=session_id, user_id=owner["id"])
    session = ARDOSession(session_id=session_id, graph=graph, mode="safe", user_id=owner["id"])
    ARDOScheduler._sessions[session_id] = session

    client = TestClient(app)

    # 1. Unauthenticated request (no session cookie) -> 401 or 404 (session state not exposed)
    res_unauth = client.get(f"/orchestrate/{session_id}/state")
    assert res_unauth.status_code in (401, 403, 404)

    # 2. Authenticated request as correct owner -> 200 OK with state
    res_auth = client.get(f"/orchestrate/{session_id}/state", cookies={"ardo_session": token})
    assert res_auth.status_code == 200
    assert res_auth.json()["session_id"] == session_id


# ── Area 3: Mandatory Plan-Hash Identity at Security Boundaries ───────────────

def test_approval_issuance_rejects_missing_session_or_user_identity(setup_auth_sqlite):
    """POST /orchestrate/{session_id}/approve/{task_id} requires session_id and user_id."""
    db = setup_auth_sqlite
    user, token = _create_user(db, "approver@ardo.ai")
    session_id = "ardo_mandatory_id_sess"

    nodes = [{"id": "t1", "type": "default", "data": {"label": "Safety Gated"}}]
    graph = DAGBuilder.build(nodes, [], goal_text="Operation", session_id=session_id, user_id=user["id"])
    session = ARDOSession(session_id=session_id, graph=graph, mode="safe", user_id=user["id"], goal_id="Operation")
    ARDOScheduler._sessions[session_id] = session

    client = TestClient(app)
    cookies = {"ardo_session": token}

    canonical_hash = session.graph.recompute_plan_hash("Operation", session_id=session_id, user_id=user["id"])

    # 1. Valid issuance with both identities -> 200 OK
    res_valid = client.post(
        f"/orchestrate/{session_id}/approve/t1",
        json={"approved": True, "plan_hash": canonical_hash},
        cookies=cookies,
    )
    assert res_valid.status_code == 200

    # 2. If user_id is empty/whitespace -> access denied (fails closed with 400 or 404)
    from api.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: {"id": "", "email": "empty@ardo.ai"}
    try:
        res_bad_user = client.post(
            f"/orchestrate/{session_id}/approve/t1",
            json={"approved": True, "plan_hash": canonical_hash},
            cookies=cookies,
        )
        assert res_bad_user.status_code in (400, 403, 404)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_runtime_engine_quarantines_when_identity_is_missing(setup_auth_sqlite):
    """RuntimeEngine dispatch quarantines safety-gated tasks if session_id or user_id is missing."""
    db = setup_auth_sqlite
    user, _ = _create_user(db, "operator@ardo.ai")

    nodes = [{"id": "t1", "type": "default", "data": {"label": "Gated Action"}}]
    graph = DAGBuilder.build(nodes, [], goal_text="Goal")
    node = graph.get_node("t1")
    node.approval_needed = True

    # 1. Missing user_id in RuntimeEngine
    engine_no_user = RuntimeEngine(
        graph=graph,
        session_id="ardo_sess_valid_id",
        mode="safe",
        goal_id="Goal",
        user_id=None,  # Missing user identity!
    )
    engine_no_user.approve_task("t1")

    events = []
    async for evt in engine_no_user.run():
        events.append(evt)

    assert node.status == TaskState.FAILED
    assert any("missing mandatory session or user identity" in log for log in node.execution_logs)


def test_generic_plan_hashing_preserves_structural_utility():
    """Generic compute_plan_hash() without session/user remains valid for structural comparisons."""
    nodes = [{"id": "t1", "type": "default", "data": {"label": "Step 1"}}]
    edges = []

    # Structural hashing works deterministically without session/user
    hash_a = compute_plan_hash(nodes, edges, intent="Generic Goal")
    hash_b = compute_plan_hash(nodes, edges, intent="Generic Goal")
    assert isinstance(hash_a, str)
    assert len(hash_a) == 64
    assert hash_a == hash_b

    # Binding session_id produces distinct hash
    hash_with_session = compute_plan_hash(nodes, edges, intent="Generic Goal", session_id="sess_123")
    assert hash_with_session != hash_a

    # Binding user_id produces distinct hash
    hash_with_user = compute_plan_hash(nodes, edges, intent="Generic Goal", session_id="sess_123", user_id="user_abc")
    assert hash_with_user != hash_with_session


# ── Area 4: Live PostgreSQL Tenant Isolation Integration Test ────────────────

@pytest.mark.live_db
def test_live_postgresql_tenant_isolation():
    """
    Direct live integration test verifying tenant isolation in live PostgreSQL:
    - User A writes an execution memory to live PostgreSQL.
    - User B cannot retrieve User A's memory.
    - Query without user_id cannot retrieve User A's memory.
    - User A retrieves User A's memory.
    """
    import psycopg2
    from core.config import settings
    from knowledge.semantic_memory import SemanticMemoryStore, ExecutionMemoryItem
    from knowledge.embeddings import DeterministicLocalEmbeddingProvider

    pg_uri = settings.POSTGRES_URI
    assert pg_uri, "POSTGRES_URI must be configured"

    # Verify live connection
    try:
        conn = psycopg2.connect(pg_uri, connect_timeout=3)
        conn.close()
    except Exception as e:
        pytest.skip(f"Live PostgreSQL unavailable: {e}")

    from knowledge.embeddings import HybridEmbeddingService

    embedder = HybridEmbeddingService(force_local=True)
    store = SemanticMemoryStore(embedding_provider=embedder)
    if not store._is_pg_connected:
        pytest.skip("Live PostgreSQL is not connected")

    tenant_a = f"tenant_a_{int(time.time())}"
    tenant_b = f"tenant_b_{int(time.time())}"
    unique_tag = f"live_tenant_iso_{int(time.time())}"

    # 1. Tenant A stores memory
    item_a = store.record_memory(
        goal_text=f"Deploy staging environment for {unique_tag}",
        task_summary="Build, migrate, smoke test",
        user_id=tenant_a,
        metadata={"tag": unique_tag},
    )
    assert item_a is not None

    try:
        # 2. Tenant B searches -> must NOT return Tenant A's memory
        results_b = store.retrieve_memories(
            goal_query=f"Deploy staging environment {unique_tag}",
            user_id=tenant_b,
            top_k=5,
            min_score=0.1,
        )
        assert not any(getattr(m, "metadata", {}).get("tag") == unique_tag for m in results_b)

        # 3. Unauthenticated / missing identity search -> must NOT return Tenant A's memory
        results_none = store.retrieve_memories(
            goal_query=f"Deploy staging environment {unique_tag}",
            user_id=None,
            top_k=5,
            min_score=0.1,
        )
        assert not any(getattr(m, "metadata", {}).get("tag") == unique_tag for m in results_none)

        # 4. Tenant A searches -> MUST return Tenant A's memory
        results_a = store.retrieve_memories(
            goal_query=f"Deploy staging environment {unique_tag}",
            user_id=tenant_a,
            top_k=5,
            min_score=0.1,
        )
        matched = [m for m in results_a if getattr(m, "metadata", {}).get("tag") == unique_tag]
        assert len(matched) >= 1
        assert matched[0].metadata["user_id"] == tenant_a
    finally:
        # Cleanup
        try:
            cleanup_conn = psycopg2.connect(pg_uri)
            with cleanup_conn.cursor() as cur:
                cur.execute("DELETE FROM execution_memories WHERE metadata->>'tag' = %s", (unique_tag,))
            cleanup_conn.commit()
            cleanup_conn.close()
        except Exception:
            pass
