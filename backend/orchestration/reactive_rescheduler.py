"""
reactive_rescheduler.py — ARDO Runtime Reactive Rescheduler
============================================================
The most critical module in ARDO. Responds to runtime events (failures,
completions, approvals, dependency breaks) by dynamically re-computing
the execution order.

This is what separates ARDO from a static task executor:
→ The schedule is never fixed. It adapts at every state transition.

Event Handlers:
    on_task_completed  — unlock successors, rebuild priority queue
    on_task_failed     — attempt retry, then branch isolation
    on_dependency_break— cascade failure to downstream blocked tasks
    on_task_approved   — move approval_needed → active
    on_task_retrying   — update state, schedule backoff

Design Principle:
    All rescheduling events return an updated execution_queue (ordered
    list of task_ids) reflecting the new scheduling decision.
    The RuntimeEngine applies this queue without re-building the entire DAG.
"""

from __future__ import annotations

import logging
from typing import List, TYPE_CHECKING

from orchestration.state_machine import TaskState, TaskStateMachine
from orchestration.dependency_manager import DependencyManager

if TYPE_CHECKING:
    from orchestration.dag_builder import ARDOGraph
    from orchestration.priority_engine import PriorityEngine

logger = logging.getLogger(__name__)


class ReactiveRescheduler:
    """
    Event-driven task rescheduling engine.

    Every public method accepts the current graph state, applies
    the appropriate state transitions, and returns a refreshed
    execution queue.
    """

    def __init__(self, priority_engine: "PriorityEngine"):
        self.priority_engine = priority_engine

    # ── Event: Task Completed ──────────────────────────────────────────────

    def on_task_completed(
        self, task_id: str, graph: "ARDOGraph"
    ) -> List[str]:
        """
        Called when a task reaches COMPLETED state.

        Actions:
            1. Transition successors PENDING → WAITING if all their other
               deps are now also complete.
            2. Rebuild priority queue.
        """
        # Mark immediate successors as WAITING
        transitioned = DependencyManager.mark_dependents_waiting(task_id, graph)
        if transitioned:
            logger.info(
                f"[Rescheduler] task={task_id} COMPLETED → "
                f"unlocked: {transitioned}"
            )

        return self._rebuild_queue(graph)

    # ── Event: Task Failed ─────────────────────────────────────────────────

    def on_task_failed(
        self,
        task_id: str,
        graph: "ARDOGraph",
        can_retry: bool,
    ) -> List[str]:
        """
        Called when a task fails (and is not being retried immediately).

        Args:
            can_retry: If True, node has already been set to RETRYING.
                       If False, node is FAILED — isolate the branch.

        Actions (no-retry path):
            1. Transition task → FAILED.
            2. Isolate the failed dependency branch.
            3. Continue scheduling independent ready tasks.
        """
        node = graph.get_node(task_id)
        if not node:
            return self._rebuild_queue(graph)

        if not can_retry:
            # Branch isolation: cascade failure to all downstream dependents
            isolated = self._isolate_branch(task_id, graph)
            logger.warning(
                f"[Rescheduler] task={task_id} FAILED (no retry). "
                f"Isolated branch: {isolated}"
            )

        return self._rebuild_queue(graph)

    # ── Event: Task Retrying ───────────────────────────────────────────────

    def on_task_retrying(
        self, task_id: str, graph: "ARDOGraph"
    ) -> List[str]:
        """
        Called when a task transitions to RETRYING state.
        The task stays in the queue at its current priority (slightly
        reduced by the confidence decay that already happened in
        RetryManager).
        """
        logger.info(f"[Rescheduler] task={task_id} queued for retry.")
        return self._rebuild_queue(graph)

    # ── Event: Dependency Break ────────────────────────────────────────────

    def on_dependency_break(
        self, failed_task_id: str, graph: "ARDOGraph"
    ) -> List[str]:
        """
        Called when a dependency task permanently fails.
        All tasks that depend on it (directly or transitively) must be
        evaluated for blocking.
        """
        blocked = DependencyManager.get_blocked_by_failure(graph)
        for task_id in blocked:
            node = graph.get_node(task_id)
            if node and node.status not in (TaskState.BLOCKED, TaskState.COMPLETED):
                try:
                    node.status = TaskStateMachine.transition(
                        node.status, TaskState.BLOCKED, task_id
                    )
                    node.execution_logs.append(
                        f"Blocked: dependency {failed_task_id} failed permanently."
                    )
                except Exception as e:
                    logger.error(f"[Rescheduler] Cannot block {task_id}: {e}")

        logger.warning(
            f"[Rescheduler] Dependency break from {failed_task_id}. "
            f"Blocked: {blocked}"
        )
        return self._rebuild_queue(graph)

    # ── Event: Task Approved ───────────────────────────────────────────────

    def on_task_approved(
        self, task_id: str, graph: "ARDOGraph"
    ) -> List[str]:
        """
        Called when an operator approves a safety-gated task.
        Transitions APPROVAL_NEEDED → ACTIVE and unblocks the queue.
        """
        node = graph.get_node(task_id)
        if node and node.status == TaskState.APPROVAL_NEEDED:
            node.approval_needed = False
            node.status = TaskStateMachine.transition(
                node.status, TaskState.ACTIVE, task_id
            )
            node.execution_logs.append("Operator approved. Transitioning to ACTIVE.")
            logger.info(f"[Rescheduler] task={task_id} approved → ACTIVE")

        return self._rebuild_queue(graph)

    # ── Private helpers ────────────────────────────────────────────────────

    def _rebuild_queue(self, graph: "ARDOGraph") -> List[str]:
        """Re-score all non-terminal nodes and return priority-ordered queue."""
        return self.priority_engine.build_execution_queue(graph)

    def _isolate_branch(self, root_task_id: str, graph: "ARDOGraph") -> List[str]:
        """
        BFS from root_task_id through the downstream adjacency to find
        all tasks that will never be able to execute. Mark them BLOCKED.
        Returns list of blocked task_ids.
        """
        from collections import deque

        blocked_ids: List[str] = []
        queue = deque(graph.successors(root_task_id))

        while queue:
            task_id = queue.popleft()
            node = graph.get_node(task_id)
            if not node:
                continue
            if node.status in (TaskState.BLOCKED, TaskState.COMPLETED):
                continue

            try:
                # Attempt to transition to BLOCKED
                # If currently in a non-standard state, force it
                if node.status == TaskState.FAILED:
                    node.status = TaskStateMachine.transition(
                        TaskState.FAILED, TaskState.BLOCKED, task_id
                    )
                elif node.status == TaskState.PENDING:
                    node.status = TaskState.BLOCKED
                elif node.status == TaskState.WAITING:
                    node.status = TaskState.BLOCKED
                else:
                    node.status = TaskState.BLOCKED

                node.execution_logs.append(
                    f"Branch isolated: upstream task {root_task_id} failed."
                )
                blocked_ids.append(task_id)
                queue.extend(graph.successors(task_id))
            except Exception as e:
                logger.error(f"[Rescheduler] Branch isolation error for {task_id}: {e}")

        return blocked_ids
