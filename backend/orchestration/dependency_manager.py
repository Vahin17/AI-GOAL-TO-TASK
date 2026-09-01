"""
dependency_manager.py — ARDO Dependency Resolution
===================================================
Resolves which tasks are ready to execute at any point in the workflow.
A task is "ready" when ALL of its predecessor nodes are in COMPLETED state.

Also manages parallel group readiness — all tasks within a group that
are ready can be dispatched concurrently in parallel execution mode.
"""

from __future__ import annotations

import logging
from typing import List, Set, TYPE_CHECKING

from orchestration.state_machine import TaskState

if TYPE_CHECKING:
    from orchestration.dag_builder import ARDOGraph, ARDOTaskNode

logger = logging.getLogger(__name__)


class DependencyManager:
    """
    Runtime dependency resolution for the ARDO execution graph.

    Key responsibilities:
        - Determine which tasks are ready (all deps completed)
        - Identify blocked tasks (at least one dep failed/blocked)
        - Group ready tasks into parallel execution batches
        - Update task states when dependencies change
    """

    @staticmethod
    def get_ready_tasks(graph: "ARDOGraph") -> List[str]:
        """
        Return all task IDs that are eligible to start execution.

        A task is ready when:
            1. It is in PENDING or WAITING state.
            2. All predecessor tasks are COMPLETED.
            3. It is NOT approval_needed (unless approval was granted).

        Returns:
            List of task_ids sorted by priority_score (highest first).
        """
        ready: List[str] = []

        for task_id, node in graph.nodes.items():
            if node.status not in (TaskState.PENDING, TaskState.WAITING):
                continue
            if node.approval_needed:
                continue  # Must go through safety gate first

            if DependencyManager._all_deps_completed(task_id, graph):
                ready.append(task_id)

        # Sort by priority_score descending for ordered dispatch
        ready.sort(
            key=lambda tid: graph.nodes[tid].priority_score, reverse=True
        )
        return ready

    @staticmethod
    def get_approval_pending_tasks(graph: "ARDOGraph") -> List[str]:
        """
        Return tasks that need human/operator approval before execution.
        These are ready dependency-wise but gated by risk.
        """
        approval_needed: List[str] = []
        for task_id, node in graph.nodes.items():
            if node.status == TaskState.APPROVAL_NEEDED:
                approval_needed.append(task_id)
            elif (
                node.status in (TaskState.PENDING, TaskState.WAITING)
                and node.approval_needed
                and DependencyManager._all_deps_completed(task_id, graph)
            ):
                approval_needed.append(task_id)
        return approval_needed

    @staticmethod
    def get_blocked_by_failure(graph: "ARDOGraph") -> List[str]:
        """
        Return tasks that are now blocked because at least one predecessor
        has FAILED or BLOCKED status (and no fallback).
        """
        newly_blocked: List[str] = []
        for task_id, node in graph.nodes.items():
            if node.status in (
                TaskState.COMPLETED, TaskState.BLOCKED, TaskState.FAILED
            ):
                continue
            for dep_id in node.dependency_ids:
                dep_node = graph.get_node(dep_id)
                if dep_node and dep_node.status in (
                    TaskState.FAILED, TaskState.BLOCKED
                ) and not dep_node.fallback_available:
                    newly_blocked.append(task_id)
                    break
        return newly_blocked

    @staticmethod
    def get_parallel_ready_groups(graph: "ARDOGraph") -> List[List[str]]:
        """
        From the ready task list, group tasks that belong to the same
        parallel group — these can be dispatched simultaneously.

        Returns:
            List of groups. Each group is a list of task_ids.
            Single-task groups are valid (sequential tasks).
        """
        ready_ids: Set[str] = set(DependencyManager.get_ready_tasks(graph))
        if not ready_ids:
            return []

        groups: dict[int, List[str]] = {}
        ungrouped: List[str] = []

        for task_id in ready_ids:
            node = graph.nodes[task_id]
            if node.parallel_group is not None:
                groups.setdefault(node.parallel_group, []).append(task_id)
            else:
                ungrouped.append(task_id)

        result = list(groups.values())
        for tid in ungrouped:
            result.append([tid])

        return result

    @staticmethod
    def mark_dependents_waiting(task_id: str, graph: "ARDOGraph") -> List[str]:
        """
        When a task transitions to ACTIVE, update its direct successors
        from PENDING to WAITING (they are now in the dependency queue).
        Returns list of task_ids transitioned to WAITING.
        """
        from orchestration.state_machine import TaskStateMachine

        transitioned: List[str] = []
        for succ_id in graph.successors(task_id):
            succ = graph.get_node(succ_id)
            if succ and succ.status == TaskState.PENDING:
                try:
                    succ.status = TaskStateMachine.transition(
                        succ.status, TaskState.WAITING, succ_id
                    )
                    transitioned.append(succ_id)
                except Exception:
                    pass  # Transition may not be valid; skip silently
        return transitioned

    # ── Private helpers ────────────────────────────────────────────────────

    @staticmethod
    def _all_deps_completed(task_id: str, graph: "ARDOGraph") -> bool:
        """Returns True if all predecessor nodes are in COMPLETED state."""
        node = graph.get_node(task_id)
        if not node:
            return False
        for dep_id in node.dependency_ids:
            dep_node = graph.get_node(dep_id)
            if not dep_node or dep_node.status != TaskState.COMPLETED:
                return False
        return True
