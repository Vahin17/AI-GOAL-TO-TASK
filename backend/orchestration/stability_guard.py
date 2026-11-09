"""
stability_guard.py — ARDO Deadlock & Starvation Protection
===========================================================
Monitors the runtime graph for two classes of scheduling pathology:

1. DEADLOCK — Cyclic waiting between tasks where each is waiting for
   another that is also waiting. Detected by checking if the graph has
   no currently runnable tasks and no terminal-state exit is reachable.

2. STARVATION — A task remains perpetually in the queue while higher-
   priority tasks keep getting scheduled. Detected via wait duration
   and a fairness counter. Protected by forced priority rebalancing.

Academic relevance:
    Dynamic schedulers without starvation/deadlock protection can exhibit
    livelock (system appears active but makes no real progress) or
    starvation (certain tasks never execute). Both are well-documented
    problems in operating systems scheduling theory (Coffman conditions,
    aging algorithms). ARDO implements aging-based starvation prevention
    and a conservative deadlock detector.
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, TYPE_CHECKING

from orchestration.state_machine import TaskState

if TYPE_CHECKING:
    from orchestration.dag_builder import ARDOGraph, ARDOTaskNode

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StabilityConfig:
    """
    Tuneable parameters for stability protection.

    Args:
        max_wait_seconds:       Max time a WAITING/PENDING task can stay
                                in queue before starvation intervention.
        starvation_threshold:   Number of queue cycles before forced elevation.
        max_retry_loops:        If a task exceeds this many retry cycles,
                                force-fail it to prevent infinite loops.
        deadlock_check_interval: Seconds between deadlock scans.
        fairness_boost:         Priority bonus added during starvation recovery.
    """
    max_wait_seconds:        float = 120.0
    starvation_threshold:    int   = 10
    max_retry_loops:         int   = 5
    deadlock_check_interval: float = 30.0
    fairness_boost:          float = 0.30


DEFAULT_STABILITY_CONFIG = StabilityConfig()


# ─────────────────────────────────────────────────────────────────────────────
# Stability Events
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StabilityEvent:
    """Represents a detected stability issue and the recovery action taken."""
    event_type: str              # deadlock | starvation | retry_loop
    affected_tasks: List[str]
    action_taken: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "affected_tasks": self.affected_tasks,
            "action_taken": self.action_taken,
            "timestamp": self.timestamp,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Stability Guard
# ─────────────────────────────────────────────────────────────────────────────

class StabilityGuard:
    """
    Monitors and corrects scheduling instability in the ARDO runtime.

    Implements:
        - Aging-based starvation prevention (inspired by multilevel
          feedback queue aging in OS scheduling)
        - Deadlock detection via "no-progress" state analysis
        - Infinite retry loop breaker
        - Emergency fairness rebalancing
    """

    def __init__(self, config: StabilityConfig = DEFAULT_STABILITY_CONFIG):
        self.config = config
        self._events: List[StabilityEvent] = []
        self._last_deadlock_check: float = 0.0
        # task_id → monotonic timestamp when it first became WAITING
        self._wait_start: Dict[str, float] = {}

    # ── Main check: call every scheduling cycle ────────────────────────────

    def check(
        self, graph: "ARDOGraph", execution_queue: List[str]
    ) -> tuple[bool, List[str]]:
        """
        Run all stability checks and apply corrections.

        Args:
            graph:           Current ARDO graph.
            execution_queue: Current ordered queue.

        Returns:
            (modified, updated_queue)
                modified = True if any correction was applied.
        """
        modified = False
        current_time = time.monotonic()

        # 1. Track wait start times for WAITING/PENDING tasks
        for task_id, node in graph.nodes.items():
            if node.status in (TaskState.WAITING, TaskState.PENDING):
                if task_id not in self._wait_start:
                    self._wait_start[task_id] = current_time
            else:
                self._wait_start.pop(task_id, None)

        # 2. Starvation check
        starving = self._detect_starvation(graph, current_time)
        if starving:
            execution_queue = self._apply_fairness_boost(
                starving, graph, execution_queue
            )
            modified = True

        # 3. Retry loop check
        retry_loops = self._detect_retry_loops(graph)
        if retry_loops:
            self._break_retry_loops(retry_loops, graph)
            modified = True

        # 4. Deadlock check (rate-limited)
        if current_time - self._last_deadlock_check > self.config.deadlock_check_interval:
            self._last_deadlock_check = current_time
            deadlocked = self._detect_deadlock(graph, execution_queue)
            if deadlocked:
                execution_queue = self._emergency_recovery(deadlocked, graph, execution_queue)
                modified = True

        return modified, execution_queue

    # ── Starvation detection and correction ───────────────────────────────

    def _detect_starvation(
        self, graph: "ARDOGraph", current_time: float
    ) -> List[str]:
        """
        Find tasks that have been waiting longer than max_wait_seconds
        OR whose starvation_counter exceeds the threshold.
        """
        starving: List[str] = []
        for task_id, node in graph.nodes.items():
            if node.status not in (TaskState.WAITING, TaskState.PENDING):
                continue

            wait_start = self._wait_start.get(task_id)
            wait_duration = (current_time - wait_start) if wait_start else 0.0

            if (
                wait_duration > self.config.max_wait_seconds
                or node.starvation_counter >= self.config.starvation_threshold
            ):
                starving.append(task_id)
                node.starvation_counter += 1
                logger.warning(
                    f"[StabilityGuard] Starvation detected: task={task_id} "
                    f"wait={wait_duration:.1f}s counter={node.starvation_counter}"
                )

        return starving

    def _apply_fairness_boost(
        self,
        starving: List[str],
        graph: "ARDOGraph",
        queue: List[str],
    ) -> List[str]:
        """
        Apply priority boost to starving tasks ("aging" algorithm).
        Moves them forward in the execution queue.
        """
        boost = self.config.fairness_boost
        for task_id in starving:
            node = graph.get_node(task_id)
            if node:
                old_score = node.priority_score
                node.priority_score = min(1.0, node.priority_score + boost)
                logger.info(
                    f"[StabilityGuard] Fairness boost: task={task_id} "
                    f"priority {old_score:.3f} → {node.priority_score:.3f}"
                )

        # Reorder queue based on updated scores
        queue.sort(
            key=lambda tid: graph.nodes[tid].priority_score
            if tid in graph.nodes else 0.0,
            reverse=True,
        )

        event = StabilityEvent(
            event_type="starvation",
            affected_tasks=starving,
            action_taken=f"Priority boost +{boost:.2f} applied. Queue reordered.",
        )
        self._events.append(event)
        return queue

    # ── Retry loop detection ───────────────────────────────────────────────

    def _detect_retry_loops(self, graph: "ARDOGraph") -> List[str]:
        """Find tasks stuck in retry loops beyond the safety limit."""
        return [
            task_id
            for task_id, node in graph.nodes.items()
            if node.retry_count > self.config.max_retry_loops
            and node.status == TaskState.RETRYING
        ]

    def _break_retry_loops(self, loop_tasks: List[str], graph: "ARDOGraph") -> None:
        """Force-fail tasks exceeding the retry loop limit."""
        for task_id in loop_tasks:
            node = graph.get_node(task_id)
            if node:
                node.status = TaskState.FAILED
                node.execution_logs.append(
                    f"[StabilityGuard] Force-failed: exceeded "
                    f"{self.config.max_retry_loops} retry loop threshold."
                )
                logger.error(
                    f"[StabilityGuard] Retry loop broken: task={task_id} "
                    f"retries={node.retry_count}"
                )

        event = StabilityEvent(
            event_type="retry_loop",
            affected_tasks=loop_tasks,
            action_taken="Force-failed to break infinite retry loop.",
        )
        self._events.append(event)

    # ── Deadlock detection ─────────────────────────────────────────────────

    def _detect_deadlock(
        self, graph: "ARDOGraph", execution_queue: List[str]
    ) -> List[str]:
        """
        Detect scheduling deadlock: no tasks are ACTIVE, but WAITING/PENDING
        tasks exist that cannot become ready due to cyclic mutual blocking.

        Simplified: if active=0 and ready_count=0 but non-terminal tasks exist,
        declare deadlock on the waiting set.
        """
        from orchestration.dependency_manager import DependencyManager

        active_count = sum(
            1 for n in graph.nodes.values()
            if n.status in (TaskState.ACTIVE, TaskState.RETRYING)
        )
        if active_count > 0:
            return []  # System is making progress

        ready = DependencyManager.get_ready_tasks(graph)
        if ready:
            return []  # Tasks are available to run

        approval_needed = DependencyManager.get_approval_pending_tasks(graph)
        if approval_needed:
            return []  # Tasks are awaiting operator approval, not deadlocked

        # Check if non-terminal tasks remain (excluding approval-needed tasks)
        stuck = [
            task_id for task_id, node in graph.nodes.items()
            if node.status in (TaskState.WAITING, TaskState.PENDING)
            and not getattr(node, "approval_needed", False)
        ]
        if stuck:
            logger.error(
                f"[StabilityGuard] DEADLOCK DETECTED: "
                f"{len(stuck)} tasks stuck with no progress path."
            )
        return stuck

    def _emergency_recovery(
        self,
        deadlocked: List[str],
        graph: "ARDOGraph",
        queue: List[str],
    ) -> List[str]:
        """
        Emergency recovery: force the highest-priority deadlocked task
        into ACTIVE state to break the deadlock.
        """
        if not deadlocked:
            return queue

        # Select the task with highest priority for emergency activation
        best = max(
            deadlocked,
            key=lambda tid: graph.nodes[tid].priority_score if tid in graph.nodes else 0.0,
        )
        node = graph.get_node(best)
        if node:
            node.status = TaskState.ACTIVE
            node.execution_logs.append(
                "[StabilityGuard] Emergency activation: deadlock recovery."
            )
            logger.warning(
                f"[StabilityGuard] Emergency recovery: force-activating task={best}"
            )

        event = StabilityEvent(
            event_type="deadlock",
            affected_tasks=deadlocked,
            action_taken=f"Emergency activation of task={best} to break deadlock.",
        )
        self._events.append(event)

        # Rebuild queue with recovered task first
        queue = [best] + [t for t in queue if t != best]
        return queue

    # ── Reporting ──────────────────────────────────────────────────────────

    def get_stability_events(self, limit: int = 20) -> List[dict]:
        return [e.to_dict() for e in self._events[-limit:]]

    def get_system_health(self, graph: "ARDOGraph") -> dict:
        """Return a summary of current system stability metrics."""
        states = {}
        for state in TaskState:
            states[state.value] = sum(
                1 for n in graph.nodes.values() if n.status == state
            )
        starving_count = sum(
            1 for n in graph.nodes.values() if n.starvation_counter > 0
        )
        return {
            "task_state_distribution": states,
            "starving_tasks": starving_count,
            "total_stability_events": len(self._events),
            "retry_loop_breaches": sum(
                1 for e in self._events if e.event_type == "retry_loop"
            ),
        }
