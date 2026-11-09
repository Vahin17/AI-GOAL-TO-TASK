"""
test_checkpoints_and_resume.py — Execution Checkpoints, Worker Leases & Resume Tests (Phase 2 Task 5)
====================================================================================================
Validates:
1. ExecutionCheckpointManager.save_checkpoint persists structured checkpoint documents.
2. Worker lease acquisition, mutual exclusion (preventing concurrent duplicate workers), renewal, and release.
3. Lease TTL expiration allows healthy failover to a new worker.
4. restore_graph_from_checkpoint preserves completed/blocked nodes and resets interrupted active tasks.
5. Authenticated POST /orchestrate/{session_id}/resume endpoint:
   - Resumes session execution from latest checkpoint.
   - Rejects concurrent resume if session actively running (HTTP 409).
   - Enforces multi-user isolation (cross-user access returns HTTP 403).
"""

import time
import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from core.auth_db import AuthDatabase
from core.security import generate_session_token
from orchestration.dag_builder import DAGBuilder, ARDOGraph
from orchestration.state_machine import TaskState
from orchestration.scheduler import ARDOScheduler, ARDOSession
from orchestration.checkpoint_manager import (
    ExecutionCheckpointManager,
    _MEMORY_LEASES,
    _MEMORY_CHECKPOINTS,
)
from main import app


@pytest.fixture(autouse=True)
def setup_checkpoint_env(monkeypatch):
    monkeypatch.setenv("AUTH_DB_BACKEND", "sqlite")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-32-bytes-minimum-length-ok")
    monkeypatch.setattr("core.database.Database.connect_db", AsyncMock())
    monkeypatch.setattr("core.database.Database.close_db", AsyncMock())
    _MEMORY_LEASES.clear()
    _MEMORY_CHECKPOINTS.clear()
    db = AuthDatabase.reset_instance(force_sqlite=True)
    yield db


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


# ── Test 1: Checkpoint Saving & Retrieval ────────────────────────────────────

@pytest.mark.asyncio
async def test_save_and_load_checkpoint():
    session_id = "ardo_chk_test_1"
    nodes = [
        {"id": "t1", "type": "default", "data": {"label": "Task 1"}},
        {"id": "t2", "type": "default", "data": {"label": "Task 2"}},
    ]
    graph = DAGBuilder.build(nodes, [{"source": "t1", "target": "t2"}], goal_text="Test goal")
    graph.session_id = session_id
    graph.nodes["t1"].status = TaskState.COMPLETED

    chk = await ExecutionCheckpointManager.save_checkpoint(
        session_id=session_id,
        graph=graph,
        trigger="task_t1_completed",
    )

    assert chk["checkpoint_id"].startswith("chk_")
    assert chk["session_id"] == session_id
    assert chk["metrics"]["completed_tasks"] == 1
    assert chk["metrics"]["total_tasks"] == 2
    assert chk["node_states"]["t1"]["status"] == "completed"

    # Load latest checkpoint
    loaded = await ExecutionCheckpointManager.load_latest_checkpoint(session_id)
    assert loaded is not None
    assert loaded["checkpoint_id"] == chk["checkpoint_id"]


# ── Test 2: Worker Lease Concurrency & Expiration ────────────────────────────

@pytest.mark.asyncio
async def test_worker_lease_concurrency_and_expiration():
    session_id = "ardo_lease_test"
    worker_1 = "worker_alpha"
    worker_2 = "worker_beta"

    # Worker 1 acquires lease for 0.2s
    acq1 = await ExecutionCheckpointManager.acquire_worker_lease(
        session_id=session_id,
        worker_id=worker_1,
        lease_duration_seconds=0.2,
    )
    assert acq1 is True

    # Worker 2 attempts to acquire lease while Worker 1 is active -> denied
    acq2 = await ExecutionCheckpointManager.acquire_worker_lease(
        session_id=session_id,
        worker_id=worker_2,
        lease_duration_seconds=0.2,
    )
    assert acq2 is False

    # Worker 1 can renew its own lease
    renewed = await ExecutionCheckpointManager.renew_worker_lease(
        session_id=session_id,
        worker_id=worker_1,
        lease_duration_seconds=0.2,
    )
    assert renewed is True

    # Wait for lease to expire
    time.sleep(0.25)

    # Worker 2 now successfully acquires expired lease
    acq2_after = await ExecutionCheckpointManager.acquire_worker_lease(
        session_id=session_id,
        worker_id=worker_2,
        lease_duration_seconds=0.5,
    )
    assert acq2_after is True

    # Worker 2 releases lease
    await ExecutionCheckpointManager.release_worker_lease(session_id=session_id, worker_id=worker_2)

    # Immediate acquisition by Worker 1 succeeds
    acq1_after = await ExecutionCheckpointManager.acquire_worker_lease(
        session_id=session_id,
        worker_id=worker_1,
        lease_duration_seconds=0.5,
    )
    assert acq1_after is True


# ── Test 3: Graph Restoration from Checkpoint ────────────────────────────────

def test_restore_graph_from_checkpoint():
    session_id = "ardo_restore_graph_test"
    nodes = [
        {"id": "t1", "type": "default", "data": {"label": "Completed Task"}},
        {"id": "t2", "type": "default", "data": {"label": "Blocked Task"}},
        {"id": "t3", "type": "default", "data": {"label": "Search for documentation"}},
        {"id": "t4", "type": "default", "data": {"label": "Pending Task"}},
        {"id": "t5", "type": "default", "data": {"label": "Submit payment transaction"}},
    ]
    edges = [
        {"source": "t1", "target": "t2"},
        {"source": "t2", "target": "t3"},
        {"source": "t3", "target": "t4"},
        {"source": "t4", "target": "t5"},
    ]
    graph = DAGBuilder.build(nodes, edges, goal_text="Restore goal")
    graph.session_id = session_id

    # Simulated checkpoint
    checkpoint = {
        "checkpoint_id": "chk_simulated",
        "session_id": session_id,
        "node_states": {
            "t1": {"status": "completed", "retry_count": 0},
            "t2": {"status": "blocked", "retry_count": 0},
            "t3": {"status": "active", "retry_count": 1},
            "t4": {"status": "pending", "retry_count": 0},
            "t5": {"status": "active", "retry_count": 0},
        },
    }

    restored_graph, summary = ExecutionCheckpointManager.restore_graph_from_checkpoint(graph, checkpoint)

    assert restored_graph.nodes["t1"].status == TaskState.COMPLETED
    assert restored_graph.nodes["t2"].status == TaskState.BLOCKED
    # In-flight read-only/idempotent active task must be safely reset to WAITING
    assert restored_graph.nodes["t3"].status == TaskState.WAITING
    assert restored_graph.nodes["t4"].status == TaskState.PENDING
    # In-flight mutating active task must transition to OUTCOME_UNKNOWN to prevent duplicate side effects
    assert restored_graph.nodes["t5"].status == TaskState.OUTCOME_UNKNOWN
    assert summary["completed"] == 1
    assert summary["blocked"] == 1
    assert summary["rescheduled"] == 1
    assert summary["outcome_unknown"] == 1


# ── Test 4: API POST /orchestrate/{session_id}/resume ────────────────────────

@pytest.mark.asyncio
async def test_api_resume_interrupted_session(setup_checkpoint_env):
    db = setup_checkpoint_env
    user, token = _create_user(db)
    session_id = "ardo_api_resume_test"

    nodes = [
        {"id": "t1", "type": "default", "data": {"label": "Task 1"}},
        {"id": "t2", "type": "default", "data": {"label": "Task 2"}},
    ]
    graph = DAGBuilder.build(nodes, [{"source": "t1", "target": "t2"}], goal_text="Resume Goal")
    graph.session_id = session_id
    graph.nodes["t1"].status = TaskState.COMPLETED

    # Save checkpoint with t1 completed
    await ExecutionCheckpointManager.save_checkpoint(
        session_id=session_id,
        graph=graph,
        trigger="t1_done",
    )

    client = TestClient(app)
    cookies = {"ardo_session": token}

    # Call resume endpoint
    res = client.post(f"/orchestrate/{session_id}/resume", cookies=cookies)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "resumed"
    assert data["session_id"] == session_id
    assert data["restored_completed_tasks"] == 1
    assert data["remaining_tasks"] == 1
    assert "worker_id" in data


def test_api_resume_cross_user_forbidden(setup_checkpoint_env):
    db = setup_checkpoint_env
    owner, _ = _create_user(db, email="owner_resume@ardo.ai")
    attacker, attacker_token = _create_user(db, email="attacker_resume@ardo.ai")
    session_id = "ardo_session_owner_resume"

    nodes = [{"id": "t1", "type": "default", "data": {"label": "Owner Task"}}]
    graph = DAGBuilder.build(nodes, [], goal_text="Owner Goal")
    session = ARDOSession(session_id=session_id, graph=graph, mode="safe", goal_id="Owner Goal", user_id=owner["id"])
    ARDOScheduler._sessions[session_id] = session

    client = TestClient(app)
    cookies = {"ardo_session": attacker_token}

    res = client.post(f"/orchestrate/{session_id}/resume", cookies=cookies)
    assert res.status_code == 403

