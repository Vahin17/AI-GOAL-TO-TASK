"""
ARDOS Stage 4 Test Suite — Correct Dashboard Execution Reporting.

Validates that:
1. Completed but unverified tasks are reported with is_verified=False and verification_status="unverified".
2. Verified tasks are reported with is_verified=True, verification_status="verified", and canonical artifact_hash.
3. Completed and Verified are NOT conflated in task counts or status display.
4. Failed tasks expose structured failure_reason.
5. Blocked tasks report blocked status without false completion.
6. Cancelled tasks and session cancellation transition correctly to CANCELLED state.
7. Paused tasks report paused status.
8. Waiting for approval tasks report approval_needed.
9. Mixed DAG states report accurate distinct counters.
10. Zero-task DAGs report safe zero progress and default success rate without division by zero.
11. Partial execution progress reports accurate percentage (never 100% while unfinished).
12. All tasks completed with some unverified reports 100% execution progress but partial verification progress.
13. Failed tasks reduce success_rate below 1.0 even with earlier successful tasks.
14. Fallback-generated plans report transparency metadata (planning_mode, fallback_reason, is_fallback).
15. Monotonic terminal state idempotency prevents late out-of-order events from overwriting terminal states.
16. Multi-user tenant isolation is strictly enforced across history, active sessions, and cancellation endpoints.
"""

import pytest
import asyncio
from unittest.mock import MagicMock

from backend.orchestration.state_machine import TaskState, TaskStateMachine, InvalidStateTransitionError
from backend.orchestration.dag_builder import ARDOTaskNode, DAGBuilder, ARDOGraph
from backend.orchestration.execution_monitor import ExecutionMonitor, SessionMetrics
from backend.orchestration.runtime_engine import RuntimeEngine
from backend.orchestration.scheduler import ARDOScheduler, ARDOSession


# ─────────────────────────────────────────────────────────────────────────────
# Test 1 & 2 & 3: Distinction Between Completed and Verified Tasks
# ─────────────────────────────────────────────────────────────────────────────

def test_unverified_completed_task_reporting():
    """Unverified completed task must have is_verified=False and verification_status='unverified'."""
    node = ARDOTaskNode(
        task_id="task-unver",
        task_name="Scrape Webpage Without Evidence",
        status=TaskState.COMPLETED,
        metadata={"verification_status": "unverified", "verified": False}
    )
    data = node.to_dict()
    assert data["status"] == "completed"
    assert data["is_verified"] is False
    assert data["verification_status"] == "unverified"
    assert data["artifact_hash"] is None


def test_verified_task_reporting():
    """Verified task must have is_verified=True, verification_status='verified', and SHA-256 hash."""
    sha256_hash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    node = ARDOTaskNode(
        task_id="task-ver",
        task_name="Verified Payment Confirmation",
        status=TaskState.COMPLETED,
        metadata={
            "verification_status": "verified",
            "verified": True,
            "evidence_hash": sha256_hash
        }
    )
    data = node.to_dict()
    assert data["status"] == "completed"
    assert data["is_verified"] is True
    assert data["verification_status"] == "verified"
    assert data["artifact_hash"] == sha256_hash


def test_completed_and_verified_not_conflated_in_metrics():
    """ExecutionMonitor must count completed unverified and verified tasks separately."""
    monitor = ExecutionMonitor("sess-1")
    graph = MagicMock()

    n1 = ARDOTaskNode(task_id="n1", task_name="N1", status=TaskState.COMPLETED, metadata={"verification_status": "unverified"})
    n2 = ARDOTaskNode(task_id="n2", task_name="N2", status=TaskState.COMPLETED, metadata={"verification_status": "verified", "evidence_hash": "a"*64})
    n3 = ARDOTaskNode(task_id="n3", task_name="N3", status=TaskState.ACTIVE)
    n4 = ARDOTaskNode(task_id="n4", task_name="N4", status=TaskState.PENDING)

    graph.nodes = {"n1": n1, "n2": n2, "n3": n3, "n4": n4}

    metrics = monitor.update_counts_from_graph(graph)

    assert metrics.tasks_total == 4
    assert metrics.tasks_completed == 1      # Only unverified completed
    assert metrics.tasks_verified == 1       # Explicitly verified
    assert metrics.tasks_running == 1
    assert metrics.tasks_pending == 1
    assert metrics.tasks_failed == 0
    assert metrics.execution_progress_pct == 50.0   # 2 finished / 4 total
    assert metrics.verification_progress_pct == 25.0 # 1 verified / 4 total
    # Unfinished tasks prevent 100% success rate
    assert metrics.success_rate < 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Failed Task Reporting with Structured failure_reason
# ─────────────────────────────────────────────────────────────────────────────

def test_failed_task_reporting_with_failure_reason():
    """Failed task node must expose structured failure_reason."""
    node = ARDOTaskNode(
        task_id="task-fail",
        task_name="Checkout Step",
        status=TaskState.FAILED,
        metadata={"failure_reason": "Payment gateway timeout after 3 retries", "error_code": "ERR_GATEWAY_TIMEOUT"}
    )
    data = node.to_dict()
    assert data["status"] == "failed"
    assert data["failure_reason"] == "Payment gateway timeout after 3 retries"
    assert data["is_verified"] is False


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Blocked Task Reporting
# ─────────────────────────────────────────────────────────────────────────────

def test_blocked_task_reporting():
    """Blocked task node must expose blocked status and failure reason of upstream failure."""
    node = ARDOTaskNode(
        task_id="task-blocked",
        task_name="Receipt Generation",
        status=TaskState.BLOCKED,
        metadata={"failure_reason": "Dependency 'task-fail' failed with error"}
    )
    data = node.to_dict()
    assert data["status"] == "blocked"
    assert "Dependency 'task-fail' failed" in data["failure_reason"]
    assert data["is_verified"] is False


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: Cancelled Task & Session Cancellation Reporting
# ─────────────────────────────────────────────────────────────────────────────

def test_cancelled_task_reporting():
    """Cancelled task node must expose cancelled status."""
    node = ARDOTaskNode(
        task_id="task-canc",
        task_name="Long Running Scrape",
        status=TaskState.CANCELLED,
        metadata={"cancellation_reason": "User aborted execution"}
    )
    data = node.to_dict()
    assert data["status"] == "cancelled"
    assert data["failure_reason"] == "User aborted execution"


@pytest.mark.asyncio
async def test_runtime_engine_cancel():
    """Calling RuntimeEngine.cancel() marks unfinished tasks cancelled and updates metrics."""
    n1 = ARDOTaskNode(task_id="t1", task_name="Task 1", dependency_ids=[])
    n2 = ARDOTaskNode(task_id="t2", task_name="Task 2", dependency_ids=["t1"])
    graph = ARDOGraph(
        nodes={"t1": n1, "t2": n2},
        adjacency={"t1": ["t2"], "t2": []},
        reverse_adj={"t1": [], "t2": ["t1"]},
        session_id="sess-cancel-test",
    )

    engine = RuntimeEngine(graph=graph, session_id="sess-cancel-test")
    assert not engine.is_cancelled

    await engine.cancel("User cancelled session")

    assert engine.is_cancelled
    assert engine.graph.nodes["t1"].status == TaskState.CANCELLED
    assert engine.graph.nodes["t2"].status == TaskState.CANCELLED

    metrics = engine.get_current_state()["metrics"]
    assert metrics["tasks_cancelled"] == 2
    assert metrics["execution_progress_pct"] == 100.0


# ─────────────────────────────────────────────────────────────────────────────
# Test 7 & 8: Paused and Waiting for Approval Task Reporting
# ─────────────────────────────────────────────────────────────────────────────

def test_paused_and_approval_needed_reporting():
    """Paused and waiting-for-approval tasks must be accurately tracked in metrics and nodes."""
    node_paused = ARDOTaskNode(task_id="p1", task_name="P1", status=TaskState.PAUSED)
    node_appr = ARDOTaskNode(task_id="a1", task_name="A1", status=TaskState.APPROVAL_NEEDED)

    assert node_paused.to_dict()["status"] == "paused"
    assert node_appr.to_dict()["status"] == "approval_needed"

    monitor = ExecutionMonitor("sess-pause-appr")
    graph = MagicMock()
    graph.nodes = {"p1": node_paused, "a1": node_appr}
    metrics = monitor.update_counts_from_graph(graph)

    assert metrics.tasks_paused == 1
    assert metrics.tasks_approval_required == 1
    assert metrics.execution_progress_pct == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Test 9: Zero-Task Execution Handling
# ─────────────────────────────────────────────────────────────────────────────

def test_zero_task_graph_handling():
    """Zero-task graph must report 0.0 progress without division by zero errors."""
    monitor = ExecutionMonitor("sess-zero")
    graph = MagicMock()
    graph.nodes = {}

    metrics = monitor.update_counts_from_graph(graph)
    assert metrics.tasks_total == 0
    assert metrics.execution_progress_pct == 0.0
    assert metrics.verification_progress_pct == 0.0
    assert metrics.success_rate == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Test 10: Partial Execution Progress Calculation
# ─────────────────────────────────────────────────────────────────────────────

def test_partial_execution_progress_calculation():
    """5 total tasks, 2 finished (1 completed unverified, 1 verified) -> exactly 40% execution, 20% verified."""
    monitor = ExecutionMonitor("sess-partial")
    graph = MagicMock()
    graph.nodes = {
        "t1": ARDOTaskNode(task_id="t1", task_name="T1", status=TaskState.COMPLETED, metadata={"verification_status": "unverified"}),
        "t2": ARDOTaskNode(task_id="t2", task_name="T2", status=TaskState.COMPLETED, metadata={"verification_status": "verified", "evidence_hash": "b"*64}),
        "t3": ARDOTaskNode(task_id="t3", task_name="T3", status=TaskState.ACTIVE),
        "t4": ARDOTaskNode(task_id="t4", task_name="T4", status=TaskState.READY),
        "t5": ARDOTaskNode(task_id="t5", task_name="T5", status=TaskState.PENDING),
    }

    metrics = monitor.update_counts_from_graph(graph)
    assert metrics.tasks_total == 5
    assert metrics.tasks_completed == 1
    assert metrics.tasks_verified == 1
    assert metrics.tasks_running == 1
    assert metrics.tasks_ready == 1
    assert metrics.tasks_pending == 1
    assert metrics.execution_progress_pct == 40.0
    assert metrics.verification_progress_pct == 20.0
    assert metrics.success_rate < 1.0  # Remaining tasks keep success rate capped


# ─────────────────────────────────────────────────────────────────────────────
# Test 11: All Tasks Finished but Some Unverified
# ─────────────────────────────────────────────────────────────────────────────

def test_all_tasks_finished_with_unverified_tasks():
    """4 tasks: 2 verified, 2 completed unverified -> 100% execution, 50% verification progress."""
    monitor = ExecutionMonitor("sess-done-unver")
    graph = MagicMock()
    graph.nodes = {
        "t1": ARDOTaskNode(task_id="t1", task_name="T1", status=TaskState.COMPLETED, metadata={"verification_status": "verified", "evidence_hash": "c"*64}),
        "t2": ARDOTaskNode(task_id="t2", task_name="T2", status=TaskState.COMPLETED, metadata={"verification_status": "verified", "evidence_hash": "d"*64}),
        "t3": ARDOTaskNode(task_id="t3", task_name="T3", status=TaskState.COMPLETED, metadata={"verification_status": "unverified"}),
        "t4": ARDOTaskNode(task_id="t4", task_name="T4", status=TaskState.COMPLETED, metadata={"verification_status": "unverified"}),
    }

    metrics = monitor.update_counts_from_graph(graph)
    assert metrics.execution_progress_pct == 100.0
    assert metrics.verification_progress_pct == 50.0
    assert metrics.tasks_completed == 2
    assert metrics.tasks_verified == 2


# ─────────────────────────────────────────────────────────────────────────────
# Test 12: Failed Task After Earlier Success Reduces Success Rate
# ─────────────────────────────────────────────────────────────────────────────

def test_failed_task_reduces_success_rate():
    """A failure after successful tasks must reduce success_rate below 1.0."""
    monitor = ExecutionMonitor("sess-fail-rate")
    graph = MagicMock()
    graph.nodes = {
        "t1": ARDOTaskNode(task_id="t1", task_name="T1", status=TaskState.COMPLETED, metadata={"verification_status": "verified", "evidence_hash": "e"*64}),
        "t2": ARDOTaskNode(task_id="t2", task_name="T2", status=TaskState.FAILED, metadata={"failure_reason": "Syntax error"}),
        "t3": ARDOTaskNode(task_id="t3", task_name="T3", status=TaskState.BLOCKED),
    }

    metrics = monitor.update_counts_from_graph(graph)
    assert metrics.tasks_verified == 1
    assert metrics.tasks_failed == 1
    assert metrics.tasks_blocked == 1
    assert metrics.execution_progress_pct == 100.0
    assert metrics.success_rate < 0.5  # 1 successful out of 3 total finished


# ─────────────────────────────────────────────────────────────────────────────
# Test 13: Fallback-Generated Plan Reporting
# ─────────────────────────────────────────────────────────────────────────────

def test_fallback_generated_plan_reporting():
    """State dictionaries returned by runtime engine must carry fallback transparency fields."""
    n1 = ARDOTaskNode(task_id="t1", task_name="Task 1", dependency_ids=[])
    graph = ARDOGraph(
        nodes={"t1": n1},
        adjacency={"t1": []},
        reverse_adj={"t1": []},
        is_fallback=True,
        fallback_reason="Primary AI quota exceeded (HTTP 429)",
        metadata={
            "is_fallback": True,
            "fallback_reason": "Primary AI quota exceeded (HTTP 429)",
            "planning_mode": "deterministic_fallback",
            "primary_failure_code": "LLM_RATE_LIMIT",
            "limitations": ["No dynamic branch generation", "Pre-set sequential template"]
        }
    )

    engine = RuntimeEngine(graph=graph, session_id="sess-fb-1")
    state = engine.get_current_state()

    assert state["is_fallback"] is True
    assert state["fallback_reason"] == "Primary AI quota exceeded (HTTP 429)"
    assert state["planning_mode"] == "deterministic_fallback"
    assert state["primary_failure_code"] == "LLM_RATE_LIMIT"
    assert "No dynamic branch generation" in state["limitations"]


# ─────────────────────────────────────────────────────────────────────────────
# Test 14: Monotonic Terminal State Protection
# ─────────────────────────────────────────────────────────────────────────────

def test_terminal_state_cannot_be_overwritten_by_late_events():
    """Terminal states (COMPLETED, VERIFIED, FAILED, BLOCKED, CANCELLED) are immutable."""
    assert TaskStateMachine.is_terminal(TaskState.COMPLETED)
    assert TaskStateMachine.is_terminal(TaskState.VERIFIED)
    assert TaskStateMachine.is_terminal(TaskState.FAILED)
    assert TaskStateMachine.is_terminal(TaskState.BLOCKED)
    assert TaskStateMachine.is_terminal(TaskState.CANCELLED)

    # Valid transitions from terminal states must raise InvalidStateTransitionError
    with pytest.raises(InvalidStateTransitionError):
        TaskStateMachine.validate(TaskState.COMPLETED, TaskState.ACTIVE)
    with pytest.raises(InvalidStateTransitionError):
        TaskStateMachine.validate(TaskState.FAILED, TaskState.ACTIVE)
    with pytest.raises(InvalidStateTransitionError):
        TaskStateMachine.validate(TaskState.BLOCKED, TaskState.ACTIVE)
    with pytest.raises(InvalidStateTransitionError):
        TaskStateMachine.validate(TaskState.CANCELLED, TaskState.ACTIVE)


# ─────────────────────────────────────────────────────────────────────────────
# Test 15: Reconnection State Synchronization
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reconnection_state_synchronization():
    """get_current_state() synchronizes counts directly from authoritative graph nodes."""
    n1 = ARDOTaskNode(task_id="t1", task_name="Task 1", dependency_ids=[])
    n2 = ARDOTaskNode(task_id="t2", task_name="Task 2", dependency_ids=["t1"])
    graph = ARDOGraph(
        nodes={"t1": n1, "t2": n2},
        adjacency={"t1": ["t2"], "t2": []},
        reverse_adj={"t1": [], "t2": ["t1"]},
        session_id="sess-sync-test",
    )

    engine = RuntimeEngine(graph=graph, session_id="sess-sync-test")

    # Manually transition node in graph
    graph.nodes["t1"].status = TaskState.COMPLETED
    graph.nodes["t1"].metadata["verification_status"] = "verified"
    graph.nodes["t1"].metadata["evidence_hash"] = "f"*64

    # Calling get_current_state() must immediately reconcile metrics
    state = engine.get_current_state()
    metrics = state["metrics"]
    assert metrics["tasks_verified"] == 1
    assert metrics["tasks_pending"] == 1
    assert metrics["execution_progress_pct"] == 50.0
    assert metrics["verification_progress_pct"] == 50.0


# ─────────────────────────────────────────────────────────────────────────────
# Test 16: Multi-User Tenant Isolation in Dashboard APIs
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tenant_isolation_in_dashboard_endpoints():
    """Scheduler must not leak sessions or permit cancellations across distinct user tenants."""
    # Reset scheduler test state
    ARDOScheduler._sessions.clear()
    ARDOScheduler._session_history.clear()

    n_alpha = ARDOTaskNode(task_id="t_a", task_name="Alpha Task", dependency_ids=[])
    graph_alpha = ARDOGraph(nodes={"t_a": n_alpha}, session_id="sess-alpha-001", user_id="user_alpha")
    session_alpha = ARDOSession(
        session_id="sess-alpha-001",
        graph=graph_alpha,
        mode="autonomous",
        user_id="user_alpha"
    )
    ARDOScheduler._sessions["sess-alpha-001"] = session_alpha

    n_beta = ARDOTaskNode(task_id="t_b", task_name="Beta Task", dependency_ids=[])
    graph_beta = ARDOGraph(nodes={"t_b": n_beta}, session_id="sess-beta-002", user_id="user_beta")
    session_beta = ARDOSession(
        session_id="sess-beta-002",
        graph=graph_beta,
        mode="autonomous",
        user_id="user_beta"
    )
    ARDOScheduler._sessions["sess-beta-002"] = session_beta

    # 1. list_active_sessions tenant check
    alpha_active = ARDOScheduler.list_active_sessions(user_id="user_alpha")
    assert len(alpha_active) == 1
    assert alpha_active[0] == "sess-alpha-001"

    beta_active = ARDOScheduler.list_active_sessions(user_id="user_beta")
    assert len(beta_active) == 1
    assert beta_active[0] == "sess-beta-002"

    # Unauthorized empty user_id must be rejected
    assert ARDOScheduler.list_active_sessions(user_id="") == []

    # 2. cancel_session tenant isolation
    # user_beta attempts to cancel user_alpha's session -> must be rejected
    cancelled = await ARDOScheduler.cancel_session("sess-alpha-001", user_id="user_beta")
    assert cancelled is False
    assert not session_alpha.is_cancelled

    # user_alpha cancels their own session -> must succeed
    cancelled_own = await ARDOScheduler.cancel_session("sess-alpha-001", user_id="user_alpha")
    assert cancelled_own is True
    assert session_alpha.is_cancelled

    # 3. get_history tenant isolation
    # Archive session_beta item
    ARDOScheduler._session_history.append({
        "session_id": "sess-beta-002",
        "user_id": "user_beta",
        "goal": "Beta Goal",
        "status": "active",
        "created_at": 1000.0,
        "task_count": 1,
    })

    # user_alpha must see 0 records in history
    alpha_history = ARDOScheduler.get_history(user_id="user_alpha")
    assert len(alpha_history) == 0

    # user_beta sees only their own record
    beta_history = ARDOScheduler.get_history(user_id="user_beta")
    assert len(beta_history) == 1
    assert beta_history[0]["session_id"] == "sess-beta-002"

    # Clean up
    ARDOScheduler._sessions.clear()
    ARDOScheduler._session_history.clear()
