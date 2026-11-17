"""
test_t7_1_3_session_enumeration_remediation.py — Task T7.1.3 Verification Suite
================================================================================
Verifies the complete elimination of session enumeration via POST /orchestrate/{session_id}/approve/{task_id}:
1. A caller accessing a nonexistent session receives HTTP 404.
2. A caller accessing another user's session receives the same HTTP 404.
3. Neither response reveals whether the foreign session exists (uniform status & format).
4. The foreign session remains completely unchanged after unauthorized access attempts.
5. The authenticated owner can still access and approve their own session.
6. Existing plan-hash verification and approval integrity remain intact.
7. No public API endpoint invokes get_session_internal().
8. Trusted internal system cleanup retains get_session_internal() for background sweeps.
"""

import ast
import os
import pathlib
import time
from unittest.mock import AsyncMock
import pytest
from fastapi.testclient import TestClient

from main import app
from core.auth_db import AuthDatabase
from core.security import generate_session_token
from orchestration.scheduler import ARDOScheduler, ARDOSession
from orchestration.dag_builder import DAGBuilder
from orchestration.state_machine import TaskState


@pytest.fixture
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
        full_name="Test User",
        email=email,
        password_hash="test_hash",
        is_active=True,
        is_verified=True,
    )
    token = generate_session_token()
    db.create_session(user["id"], token)
    return user, token


def test_approve_task_nonexistent_session_returns_404(setup_auth_sqlite):
    """A caller attempting to approve a task on a nonexistent session receives HTTP 404."""
    db = setup_auth_sqlite
    user, token = _create_user(db, "caller@ardo.ai")

    client = TestClient(app)
    cookies = {"ardo_session": token}

    res = client.post(
        "/orchestrate/ardo_nonexistent_999/approve/t1",
        json={"approved": True, "plan_hash": "a" * 64},
        cookies=cookies,
    )
    assert res.status_code == 404
    assert res.json() == {"detail": "Session ardo_nonexistent_999 not found."}


def test_approve_task_foreign_session_returns_same_404(setup_auth_sqlite):
    """A caller attempting to approve a task on another user's session receives identical HTTP 404."""
    db = setup_auth_sqlite
    owner, _ = _create_user(db, "owner@ardo.ai")
    attacker, attacker_token = _create_user(db, "attacker@ardo.ai")

    session_id = "ardo_foreign_session_101"
    nodes = [{"id": "t1", "type": "default", "data": {"label": "Deploy Production"}}]
    graph = DAGBuilder.build(nodes, [], goal_text="Deploy", session_id=session_id, user_id=owner["id"])
    session = ARDOSession(session_id=session_id, graph=graph, mode="safe", user_id=owner["id"], goal_id="Deploy")
    ARDOScheduler._sessions[session_id] = session

    client = TestClient(app)
    cookies = {"ardo_session": attacker_token}

    res = client.post(
        f"/orchestrate/{session_id}/approve/t1",
        json={"approved": True, "plan_hash": "b" * 64},
        cookies=cookies,
    )
    assert res.status_code == 404
    assert res.json() == {"detail": f"Session {session_id} not found."}


def test_approve_task_unowned_session_returns_same_404(setup_auth_sqlite):
    """A caller accessing an unowned session (user_id=None) receives identical HTTP 404."""
    db = setup_auth_sqlite
    caller, token = _create_user(db, "probe@ardo.ai")

    session_id = "ardo_unowned_session_202"
    nodes = [{"id": "t1", "type": "default", "data": {"label": "Unowned Action"}}]
    graph = DAGBuilder.build(nodes, [], goal_text="Action", session_id=session_id, user_id=None)
    session = ARDOSession(session_id=session_id, graph=graph, mode="safe", user_id=None, goal_id="Action")
    ARDOScheduler._sessions[session_id] = session

    client = TestClient(app)
    cookies = {"ardo_session": token}

    res = client.post(
        f"/orchestrate/{session_id}/approve/t1",
        json={"approved": True, "plan_hash": "c" * 64},
        cookies=cookies,
    )
    assert res.status_code == 404
    assert res.json() == {"detail": f"Session {session_id} not found."}


def test_responses_do_not_distinguish_session_existence(setup_auth_sqlite):
    """
    Uniform response check: Proves that the API does not leak whether a session
    exists when probed by an unauthorized caller.
    """
    db = setup_auth_sqlite
    owner, _ = _create_user(db, "victim@ardo.ai")
    prober, prober_token = _create_user(db, "prober@ardo.ai")

    real_session_id = "ardo_real_private_sess"
    fake_session_id = "ardo_fake_missing_sess"

    nodes = [{"id": "t1", "type": "default", "data": {"label": "Private Task"}}]
    graph = DAGBuilder.build(nodes, [], goal_text="Private", session_id=real_session_id, user_id=owner["id"])
    session = ARDOSession(session_id=real_session_id, graph=graph, mode="safe", user_id=owner["id"], goal_id="Private")
    ARDOScheduler._sessions[real_session_id] = session

    client = TestClient(app)
    cookies = {"ardo_session": prober_token}

    res_real = client.post(
        f"/orchestrate/{real_session_id}/approve/t1",
        json={"approved": True, "plan_hash": "d" * 64},
        cookies=cookies,
    )
    res_fake = client.post(
        f"/orchestrate/{fake_session_id}/approve/t1",
        json={"approved": True, "plan_hash": "d" * 64},
        cookies=cookies,
    )

    # Both must return 404
    assert res_real.status_code == 404
    assert res_fake.status_code == 404

    # Both details must follow the exact same format
    assert res_real.json()["detail"] == f"Session {real_session_id} not found."
    assert res_fake.json()["detail"] == f"Session {fake_session_id} not found."


def test_foreign_session_remains_unchanged(setup_auth_sqlite):
    """
    Verifies that after an attacker's cross-user approval attempt:
    - Task approval status is not modified.
    - was_approval_gated is not altered.
    - No approval record is inserted into AuthDB for the session.
    """
    db = setup_auth_sqlite
    owner, _ = _create_user(db, "legit_owner@ardo.ai")
    attacker, attacker_token = _create_user(db, "intruder@ardo.ai")

    session_id = "ardo_victim_session_303"
    nodes = [{"id": "t_critical", "type": "default", "data": {"label": "Critical Op"}}]
    graph = DAGBuilder.build(nodes, [], goal_text="Critical Goal", session_id=session_id, user_id=owner["id"])
    node = graph.get_node("t_critical")
    node.status = TaskState.APPROVAL_NEEDED

    session = ARDOSession(session_id=session_id, graph=graph, mode="safe", user_id=owner["id"], goal_id="Critical Goal")
    ARDOScheduler._sessions[session_id] = session

    client = TestClient(app)
    cookies = {"ardo_session": attacker_token}

    # Attacker tries to approve
    client.post(
        f"/orchestrate/{session_id}/approve/t_critical",
        json={"approved": True, "plan_hash": "e" * 64},
        cookies=cookies,
    )

    # Node remains unchanged in APPROVAL_NEEDED state
    assert node.status == TaskState.APPROVAL_NEEDED
    assert getattr(node, "was_approval_gated", False) is False

    # AuthDB has no approval record for this session
    approval = db.get_plan_approval(session_id)
    assert approval is None


def test_authenticated_owner_can_still_approve_own_session(setup_auth_sqlite):
    """The legitimate session owner can still approve their safety-gated task."""
    db = setup_auth_sqlite
    owner, owner_token = _create_user(db, "true_owner@ardo.ai")

    session_id = "ardo_owner_approved_session"
    nodes = [{"id": "t1", "type": "default", "data": {"label": "Transfer Funds"}}]
    graph = DAGBuilder.build(nodes, [], goal_text="Transfer", session_id=session_id, user_id=owner["id"])
    session = ARDOSession(session_id=session_id, graph=graph, mode="safe", user_id=owner["id"], goal_id="Transfer")
    ARDOScheduler._sessions[session_id] = session

    client = TestClient(app)
    cookies = {"ardo_session": owner_token}

    canonical_hash = session.graph.recompute_plan_hash("Transfer", session_id=session_id, user_id=owner["id"])

    res = client.post(
        f"/orchestrate/{session_id}/approve/t1",
        json={"approved": True, "plan_hash": canonical_hash},
        cookies=cookies,
    )
    assert res.status_code == 200
    assert res.json()["status"] == "approved"
    assert res.json()["session_id"] == session_id
    assert res.json()["plan_hash"] == canonical_hash

    # Verified in AuthDB
    assert db.is_plan_approved(session_id, canonical_hash, task_id="t1") is True


def test_plan_hash_verification_and_integrity_intact(setup_auth_sqlite):
    """Existing plan-hash verification remains strictly enforced for the legitimate owner."""
    db = setup_auth_sqlite
    owner, owner_token = _create_user(db, "strict_owner@ardo.ai")

    session_id = "ardo_strict_hash_session"
    nodes = [{"id": "t1", "type": "default", "data": {"label": "Modify Records"}}]
    graph = DAGBuilder.build(nodes, [], goal_text="Modify", session_id=session_id, user_id=owner["id"])
    session = ARDOSession(session_id=session_id, graph=graph, mode="safe", user_id=owner["id"], goal_id="Modify")
    ARDOScheduler._sessions[session_id] = session

    client = TestClient(app)
    cookies = {"ardo_session": owner_token}

    # Stale/mismatched hash fails with 400
    res = client.post(
        f"/orchestrate/{session_id}/approve/t1",
        json={"approved": True, "plan_hash": "f" * 64},
        cookies=cookies,
    )
    assert res.status_code == 400
    assert "Plan hash mismatch" in res.json()["detail"]


def test_no_public_api_endpoint_invokes_get_session_internal():
    """
    AST inspection across the entire api module:
    Verifies that get_session_internal is never referenced in backend/api.
    """
    api_dir = pathlib.Path(__file__).resolve().parent.parent / "api"
    api_files = list(api_dir.glob("*.py"))

    assert len(api_files) > 0, "api directory must contain Python files"

    for api_file in api_files:
        with open(api_file, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=str(api_file))

        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "get_session_internal":
                pytest.fail(
                    f"Forbidden invocation of get_session_internal() found in API module "
                    f"{api_file.name}:{node.lineno}"
                )


@pytest.mark.asyncio
async def test_internal_system_cleanup_retains_get_session_internal():
    """Trusted internal system background sweep still functions and uses get_session_internal()."""
    session_id = "ardo_abandoned_sweep_test"
    nodes = [{"id": "t1", "type": "default", "data": {"label": "Abandoned Task"}}]
    graph = DAGBuilder.build(nodes, [], goal_text="Old Goal", session_id=session_id, user_id="system_user")
    session = ARDOSession(session_id=session_id, graph=graph, mode="safe", user_id="system_user", goal_id="Old Goal")
    session.engine._is_completed = True
    session.engine._start_time = time.time() - 7200  # 2 hours ago

    ARDOScheduler._sessions[session_id] = session
    assert ARDOScheduler.get_session_internal(session_id) is session

    # Sweep abandoned sessions
    evicted_count = await ARDOScheduler.cleanup_abandoned_sessions(max_idle_minutes=60)
    assert evicted_count == 1
    assert ARDOScheduler.get_session_internal(session_id) is None
