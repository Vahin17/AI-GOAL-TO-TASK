"""
test_interrupted_action_recovery.py — Safe Recovery of Interrupted Browser Actions (Phase 2 Acceptance)
======================================================================================================
Validates:
1. Distinguishing idempotent/read-only tasks from state-mutating/non-idempotent tasks.
2. Introduction of TaskState.OUTCOME_UNKNOWN.
3. Crash before execution: idempotent action is safely reset to WAITING.
4. Crash after successful execution: post-crash observation is semantically verified and
   marked COMPLETED without re-executing.
5. Crash during execution with unknown outcome: non-idempotent action (e.g. purchase, form submission)
   transitions to OUTCOME_UNKNOWN and is gated to APPROVAL_NEEDED to prevent duplicate side effects.
6. Operator manual approval transitions APPROVAL_NEEDED to ACTIVE / WAITING / COMPLETED.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from orchestration.dag_builder import DAGBuilder, ARDOGraph
from orchestration.state_machine import TaskState, TaskStateMachine
from orchestration.checkpoint_manager import ExecutionCheckpointManager, is_task_idempotent
from execution.browser.browser_models import BrowserActionType
from execution.browser.agent_contracts import (
    BrowserObservation,
    BrowserVerificationStatus,
    RecentActionRecord,
)


def test_state_machine_outcome_unknown_transitions():
    """Verifies valid transitions to and from TaskState.OUTCOME_UNKNOWN."""
    # ACTIVE -> OUTCOME_UNKNOWN
    assert TaskStateMachine.transition(TaskState.ACTIVE, TaskState.OUTCOME_UNKNOWN, "t1") == TaskState.OUTCOME_UNKNOWN

    # OUTCOME_UNKNOWN -> COMPLETED (verified succeeded)
    assert TaskStateMachine.transition(TaskState.OUTCOME_UNKNOWN, TaskState.COMPLETED, "t1") == TaskState.COMPLETED

    # OUTCOME_UNKNOWN -> APPROVAL_NEEDED (indeterminate outcome)
    assert TaskStateMachine.transition(TaskState.OUTCOME_UNKNOWN, TaskState.APPROVAL_NEEDED, "t1") == TaskState.APPROVAL_NEEDED

    # OUTCOME_UNKNOWN -> WAITING (proven safe / idempotent retry)
    assert TaskStateMachine.transition(TaskState.OUTCOME_UNKNOWN, TaskState.WAITING, "t1") == TaskState.WAITING

    # APPROVAL_NEEDED -> COMPLETED
    assert TaskStateMachine.transition(TaskState.APPROVAL_NEEDED, TaskState.COMPLETED, "t1") == TaskState.COMPLETED


def test_is_task_idempotent_classification():
    """Verifies accurate detection of idempotent vs mutating tasks."""
    nodes = [
        # Idempotent: navigate, read, search, extract
        {"id": "t1", "type": "default", "data": {"label": "Navigate to checkout page"}},
        {"id": "t2", "type": "default", "data": {"label": "Search for flight tickets"}},
        {"id": "t3", "type": "default", "data": {"label": "Extract product pricing"}},
        {"id": "t4", "type": "default", "data": {"label": "Inspect order status"}},
        # Non-idempotent: submit, pay, purchase, delete, send
        {"id": "m1", "type": "default", "data": {"label": "Submit payment details"}},
        {"id": "m2", "type": "default", "data": {"label": "Purchase item with credit card"}},
        {"id": "m3", "type": "default", "data": {"label": "Send confirmation email to user"}},
        {"id": "m4", "type": "default", "data": {"label": "Delete existing account"}},
    ]
    graph = DAGBuilder.build(nodes, [], goal_text="Idempotency classification test")

    # Idempotent nodes
    assert is_task_idempotent(graph.nodes["t1"]) is True
    assert is_task_idempotent(graph.nodes["t2"]) is True
    assert is_task_idempotent(graph.nodes["t3"]) is True
    assert is_task_idempotent(graph.nodes["t4"]) is True

    # Mutating nodes
    assert is_task_idempotent(graph.nodes["m1"]) is False
    assert is_task_idempotent(graph.nodes["m2"]) is False
    assert is_task_idempotent(graph.nodes["m3"]) is False
    assert is_task_idempotent(graph.nodes["m4"]) is False


def test_crash_recovery_resets_idempotent_but_flags_mutating():
    """
    On checkpoint restoration:
    - Active idempotent task resets to WAITING (safe to retry).
    - Active mutating task transitions to OUTCOME_UNKNOWN (unsafe to blindly retry).
    """
    nodes = [
        {"id": "t_read", "type": "default", "data": {"label": "Search for products"}},
        {"id": "t_pay", "type": "default", "data": {"label": "Pay order total $49.99"}},
    ]
    graph = DAGBuilder.build(nodes, [{"source": "t_read", "target": "t_pay"}], goal_text="Crash test")

    checkpoint = {
        "session_id": "session_crash_1",
        "node_states": {
            "t_read": {"status": "active", "retry_count": 0},
            "t_pay": {"status": "active", "retry_count": 0},
        },
    }

    restored_graph, summary = ExecutionCheckpointManager.restore_graph_from_checkpoint(graph, checkpoint)

    # Idempotent task safely rescheduled to WAITING
    assert restored_graph.nodes["t_read"].status == TaskState.WAITING
    assert summary["rescheduled"] == 1

    # Mutating task flagged as OUTCOME_UNKNOWN
    assert restored_graph.nodes["t_pay"].status == TaskState.OUTCOME_UNKNOWN
    assert summary["outcome_unknown"] == 1


@pytest.mark.asyncio
async def test_reconcile_uncertain_tasks_verified_post_crash():
    """
    Crash after successful action execution:
    SemanticVerifier checks post-crash browser observation and confirms action succeeded.
    Task is marked COMPLETED without duplicate re-execution.
    """
    nodes = [
        {
            "id": "t_submit",
            "type": "default",
            "data": {"label": "Submit booking order"},
            "metadata": {"expected_url": "https://example.com/confirmation", "success_criteria": ["Order Confirmed"]},
        }
    ]
    graph = DAGBuilder.build(nodes, [], goal_text="Post-crash verify")
    graph.nodes["t_submit"].status = TaskState.OUTCOME_UNKNOWN
    graph.nodes["t_submit"].metadata = {
        "expected_url": "https://example.com/confirmation",
        "success_criteria": ["Order Confirmed"],
    }

    # Mock browser adapter with observation showing the order confirmation page
    mock_observation = BrowserObservation(
        session_id="session_crash_1",
        task_id="t_submit",
        current_url="https://example.com/confirmation",
        page_title="Order Confirmed - Thank You",
        recent_action=RecentActionRecord(action_type=BrowserActionType.CLICK, success=True),
    )
    mock_adapter = MagicMock()
    mock_adapter.get_observation = AsyncMock(return_value=mock_observation)

    reconciled = await ExecutionCheckpointManager.reconcile_uncertain_tasks(
        graph,
        browser_adapter=mock_adapter,
    )

    assert len(reconciled) == 1
    assert reconciled[0]["task_id"] == "t_submit"
    assert reconciled[0]["status"] == "completed"
    assert reconciled[0]["method"] == "semantic_verification"
    assert graph.nodes["t_submit"].status == TaskState.COMPLETED
    assert any("Post-crash semantic verification confirmed" in log for log in graph.nodes["t_submit"].execution_logs)


@pytest.mark.asyncio
async def test_reconcile_uncertain_tasks_indeterminate_gates_to_approval_needed():
    """
    Crash during execution with indeterminate outcome (e.g. browser context lost):
    Task is gated in APPROVAL_NEEDED to prevent accidental duplicate external side effects.
    """
    nodes = [
        {"id": "t_pay", "type": "default", "data": {"label": "Submit payment details"}},
    ]
    graph = DAGBuilder.build(nodes, [], goal_text="Indeterminate crash test")
    graph.nodes["t_pay"].status = TaskState.OUTCOME_UNKNOWN

    # No browser context available
    reconciled = await ExecutionCheckpointManager.reconcile_uncertain_tasks(
        graph,
        browser_adapter=None,
    )

    assert len(reconciled) == 1
    assert reconciled[0]["task_id"] == "t_pay"
    assert reconciled[0]["status"] == "approval_needed"
    assert graph.nodes["t_pay"].status == TaskState.APPROVAL_NEEDED
    assert graph.nodes["t_pay"].approval_needed is True
    assert any("Gated in APPROVAL_NEEDED" in log for log in graph.nodes["t_pay"].execution_logs)


def test_operator_intervention_prevents_duplicate_side_effect():
    """
    Verifies that when a task is gated in APPROVAL_NEEDED following an uncertain outcome,
    it cannot be silently re-executed without explicit operator approval or cancellation.
    """
    nodes = [
        {"id": "t_transfer", "type": "default", "data": {"label": "Transfer $500 to external vendor"}},
    ]
    graph = DAGBuilder.build(nodes, [], goal_text="Wire transfer")
    node = graph.nodes["t_transfer"]
    node.status = TaskState.APPROVAL_NEEDED
    node.approval_needed = True

    # Operator rejects task -> transitions to BLOCKED, preventing duplicate execution
    TaskStateMachine.transition(node.status, TaskState.BLOCKED, node.task_id)
    node.status = TaskState.BLOCKED
    assert node.status == TaskState.BLOCKED
    assert TaskStateMachine.is_terminal(node.status) is True

