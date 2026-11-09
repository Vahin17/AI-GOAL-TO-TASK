"""
test_plan_approval.py — Cryptographically Bound Plan Approval Tests (Phase 2 Task 1)
=====================================================================================
Validates:
1. Canonical SHA-256 plan hash computation and recomputation.
2. Invalidation of plan hash upon any graph tampering (label, tool, parameters, dependencies).
3. AuthDatabase persistence of plan approvals in plan_approvals table.
4. Successful approval with matching plan_hash.
5. Strict rejection (HTTP 400) when approving with a mismatched / tampered plan_hash.
6. Multi-user isolation: Cross-user approval rejection (HTTP 403).
7. Expiration handling: Expired approval records fail is_plan_approved check.
8. RuntimeEngine enforcement: Tasks requiring approval verify cryptographic approval before execution.
9. Duplicate execution prevention: RuntimeEngine and ARDOSession prevent duplicate runs.
"""

import asyncio
import os
import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from core.auth_db import AuthDatabase
from orchestration.dag_builder import DAGBuilder, ARDOGraph, ARDOTaskNode
from orchestration.plan_hasher import compute_plan_hash
from orchestration.scheduler import ARDOScheduler, ARDOSession
from orchestration.runtime_engine import RuntimeEngine
from orchestration.state_machine import TaskState
from main import app


@pytest.fixture(autouse=True)
def setup_auth_sqlite(monkeypatch):
    """Ensure tests run against an isolated SQLite database and mock out Mongo/Gemini."""
    monkeypatch.setenv("AUTH_DB_BACKEND", "sqlite")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-32-bytes-minimum-length-ok")
    monkeypatch.setattr("core.database.Database.connect_db", AsyncMock())
    monkeypatch.setattr("core.database.Database.close_db", AsyncMock())
    db = AuthDatabase.reset_instance(force_sqlite=True)
    yield db


def _create_test_user_and_token(db: AuthDatabase, email: str = "operator@ardo.ai"):
    user = db.create_user(
        full_name="Operator User",
        email=email,
        password_hash="test_hash",
        is_active=True,
        is_verified=True,
    )
    from core.security import generate_session_token
    token = generate_session_token()
    db.create_session(user["id"], token)
    return user, token


# ── Test 1: Plan Hash Recomputation & Tamper Sensitivity ─────────────────────

def test_plan_hash_recomputation_and_tamper_detection():
    nodes = [
        {"id": "t1", "type": "default", "data": {"label": "Search docs", "tool_call": {"query": "python"}}},
        {"id": "t2", "type": "default", "data": {"label": "Analyze results", "tool_call": {"action": "parse"}}},
    ]
    edges = [{"source": "t1", "target": "t2"}]
    goal = "Perform python documentation analysis"

    graph = DAGBuilder.build(nodes, edges, goal_text=goal)
    initial_hash = graph.plan_hash
    assert initial_hash is not None
    assert len(initial_hash) == 64

    # Recomputing from unmodified graph yields identical hash
    recomputed = graph.recompute_plan_hash()
    assert recomputed == initial_hash

    # Tampering: modifying a task label strictly changes hash
    graph.nodes["t1"].task_name = "Malicious altered task"
    tampered_hash = graph.recompute_plan_hash()
    assert tampered_hash != initial_hash

    # Resetting label restores hash
    graph.nodes["t1"].task_name = "Search docs"
    assert graph.recompute_plan_hash() == initial_hash

    # Tampering: modifying a tool call strictly changes hash
    graph.nodes["t1"].tool_call = {"query": "rm -rf /"}
    tampered_tool_hash = graph.recompute_plan_hash()
    assert tampered_tool_hash != initial_hash

    # Resetting tool restores hash
    graph.nodes["t1"].tool_call = {"query": "python"}
    assert graph.recompute_plan_hash() == initial_hash

    # Tampering: adding a dependency edge strictly changes hash
    graph.adjacency["t2"] = ["t1"]  # altered adjacency
    tampered_edge_hash = graph.recompute_plan_hash()
    assert tampered_edge_hash != initial_hash


# ── Test 2: AuthDatabase Plan Approval Storage & Retrieval ───────────────────

def test_auth_db_plan_approval_persistence(setup_auth_sqlite):
    db = setup_auth_sqlite
    user, _ = _create_test_user_and_token(db)
    session_id = "ardo_session_test_1"
    plan_hash = "a" * 64

    # Record approval
    record = db.record_plan_approval(
        session_id=session_id,
        user_id=user["id"],
        plan_hash=plan_hash,
        task_id="t1",
        status="approved",
        expires_in_seconds=900,
        metadata={"notes": "Approved for production"},
    )
    assert record["id"].startswith("appr_")
    assert record["session_id"] == session_id
    assert record["plan_hash"] == plan_hash
    assert record["status"] == "approved"

    # Verify is_plan_approved returns True
    assert db.is_plan_approved(session_id, plan_hash, task_id="t1") is True
    # Different hash returns False
    assert db.is_plan_approved(session_id, "b" * 64, task_id="t1") is False
    # Different session returns False
    assert db.is_plan_approved("other_sess", plan_hash, task_id="t1") is False

    # Invalidate approvals
    revoked_count = db.invalidate_plan_approvals(session_id, reason="plan_mutated")
    assert revoked_count == 1
    # Now is_plan_approved must return False
    assert db.is_plan_approved(session_id, plan_hash, task_id="t1") is False


# ── Test 3: Plan Approval Expiration ─────────────────────────────────────────

def test_plan_approval_expiration(setup_auth_sqlite):
    db = setup_auth_sqlite
    user, _ = _create_test_user_and_token(db)
    session_id = "ardo_session_exp_test"
    plan_hash = "c" * 64

    # Record approval that expired 1 second ago
    db.record_plan_approval(
        session_id=session_id,
        user_id=user["id"],
        plan_hash=plan_hash,
        task_id="t1",
        status="approved",
        expires_in_seconds=-1,
    )

    # Must be considered NOT approved due to expiration
    assert db.is_plan_approved(session_id, plan_hash, task_id="t1") is False


# ── Test 4: API Approval with Valid Plan Hash ────────────────────────────────

def test_api_approve_task_matching_hash(setup_auth_sqlite):
    db = setup_auth_sqlite
    user, token = _create_test_user_and_token(db)

    nodes = [
        {"id": "t1", "type": "default", "data": {"label": "High risk action"}},
    ]
    edges = []
    goal = "Execute high risk operation"
    session_id = "ardo_api_test_approve"

    # Build session owned by user
    graph = DAGBuilder.build(nodes, edges, goal_text=goal)
    session = ARDOSession(
        session_id=session_id,
        graph=graph,
        mode="safe",
        goal_id=goal,
        user_id=user["id"],
    )
    ARDOScheduler._sessions[session_id] = session

    client = TestClient(app)
    cookies = {"ardo_session": token}

    canonical_hash = session.graph.recompute_plan_hash(goal)

    # Approve with exact matching plan_hash
    res = client.post(
        f"/orchestrate/{session_id}/approve/t1",
        json={"approved": True, "plan_hash": canonical_hash, "notes": "Operator sign-off"},
        cookies=cookies,
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "approved"
    assert data["plan_hash"] == canonical_hash
    assert "approval_id" in data
    assert "expires_at" in data

    # Verify database persistence
    assert db.is_plan_approved(session_id, canonical_hash, task_id="t1") is True


# ── Test 5: API Approval Rejection on Hash Mismatch (Tamper Defense) ─────────

def test_api_approve_task_mismatched_hash_rejected(setup_auth_sqlite):
    db = setup_auth_sqlite
    user, token = _create_test_user_and_token(db)

    nodes = [{"id": "t1", "type": "default", "data": {"label": "Normal task"}}]
    edges = []
    session_id = "ardo_api_tamper_test"

    graph = DAGBuilder.build(nodes, edges, goal_text="Normal goal")
    session = ARDOSession(
        session_id=session_id,
        graph=graph,
        mode="safe",
        goal_id="Normal goal",
        user_id=user["id"],
    )
    ARDOScheduler._sessions[session_id] = session

    client = TestClient(app)
    cookies = {"ardo_session": token}

    fake_tampered_hash = "f" * 64

    # Approve with mismatched hash -> must return HTTP 400
    res = client.post(
        f"/orchestrate/{session_id}/approve/t1",
        json={"approved": True, "plan_hash": fake_tampered_hash},
        cookies=cookies,
    )
    assert res.status_code == 400
    assert "Plan hash mismatch" in res.json()["detail"]

    # Verify that database does NOT have approval
    assert db.is_plan_approved(session_id, fake_tampered_hash, task_id="t1") is False


# ── Test 6: Cross-User Approval Rejection (Multi-User Isolation) ──────────────

def test_api_approve_task_cross_user_forbidden(setup_auth_sqlite):
    db = setup_auth_sqlite
    owner, _ = _create_test_user_and_token(db, email="owner@ardo.ai")
    attacker, attacker_token = _create_test_user_and_token(db, email="attacker@ardo.ai")

    nodes = [{"id": "t1", "type": "default", "data": {"label": "Owner task"}}]
    session_id = "ardo_session_owner_1"

    graph = DAGBuilder.build(nodes, [], goal_text="Owner goal")
    session = ARDOSession(
        session_id=session_id,
        graph=graph,
        mode="safe",
        goal_id="Owner goal",
        user_id=owner["id"],
    )
    ARDOScheduler._sessions[session_id] = session

    client = TestClient(app)
    cookies = {"ardo_session": attacker_token}

    # Attacker tries to approve owner's session -> HTTP 404/403 access denied
    res = client.post(
        f"/orchestrate/{session_id}/approve/t1",
        json={"approved": True, "plan_hash": session.graph.plan_hash},
        cookies=cookies,
    )
    assert res.status_code in (403, 404)


# ── Test 7: RuntimeEngine Verification of Cryptographic Approval ─────────────

@pytest.mark.asyncio
async def test_runtime_engine_verifies_cryptographic_approval(setup_auth_sqlite):
    db = setup_auth_sqlite
    user, _ = _create_test_user_and_token(db)
    session_id = "ardo_runtime_approval_test"

    nodes = [
        {"id": "t1", "type": "default", "data": {"label": "Gated Action"}}
    ]
    graph = DAGBuilder.build(nodes, [], goal_text="Gated operation")
    node = graph.get_node("t1")
    node.approval_needed = True

    engine = RuntimeEngine(
        graph=graph,
        session_id=session_id,
        mode="safe",
        goal_id="Gated operation",
        user_id=user["id"],
    )

    # 1. Simulate operator approving without recording in AuthDB
    engine.approve_task("t1")
    assert node.was_approval_gated is True

    # 2. Run engine — should detect missing cryptographic approval in DB and quarantine task
    events = []
    async for evt in engine.run():
        events.append(evt)

    # Task must be quarantined / failed due to missing approval
    assert node.status == TaskState.FAILED
    assert any("Cryptographic approval verification failed" in log for log in node.execution_logs)

    # 3. Now record authoritative approval in AuthDB for a fresh node
    graph2 = DAGBuilder.build(nodes, [], goal_text="Gated operation")
    node2 = graph2.get_node("t1")
    node2.approval_needed = True
    session_id2 = "ardo_runtime_approval_test_2"

    db.record_plan_approval(
        session_id=session_id2,
        user_id=user["id"],
        plan_hash=graph2.plan_hash,
        task_id="t1",
        status="approved",
    )

    engine2 = RuntimeEngine(
        graph=graph2,
        session_id=session_id2,
        mode="safe",
        goal_id="Gated operation",
        user_id=user["id"],
    )
    engine2.approve_task("t1")

    events2 = []
    async for evt in engine2.run():
        events2.append(evt)

    # Task should successfully execute and complete
    assert node2.status == TaskState.COMPLETED
