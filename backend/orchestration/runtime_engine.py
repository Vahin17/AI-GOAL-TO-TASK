"""
runtime_engine.py — ARDO Main Async Orchestration Loop
=======================================================
The central execution brain. Drives the ARDO workflow by:

1. Iterating the priority-ordered execution queue
2. Respecting parallel groups for concurrent dispatch
3. Applying state machine transitions at every step
4. Invoking retry, rescheduling, and stability logic reactively
5. Generating and surfacing explainability context
6. Emitting OrchestrationEvents for real-time frontend updates

Execution Modes:
    sequential  — Tasks execute one at a time, strictly ordered
    parallel    — Independent tasks in the same parallel group run concurrently
    safe        — Like sequential, but high-risk tasks require approval first
    aggressive  — Like parallel, with faster retries and less safety gating

The RuntimeEngine is an async generator — it yields OrchestrationEvents
which the scheduler pushes to WebSocket subscribers.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import AsyncGenerator, Dict, List, Optional, TYPE_CHECKING

from orchestration.state_machine import TaskState, TaskStateMachine
from orchestration.dag_builder import ARDOGraph, ARDOTaskNode
from orchestration.priority_engine import PriorityEngine
from orchestration.dependency_manager import DependencyManager
from orchestration.retry_manager import RetryManager
from orchestration.reactive_rescheduler import ReactiveRescheduler
from orchestration.explainability_engine import ExplainabilityEngine
from orchestration.stability_guard import StabilityGuard
from orchestration.weight_optimizer import WeightOptimizer, ExecutionOutcome
from orchestration.execution_monitor import ExecutionMonitor, OrchestrationEvent

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Simulated execution time range per task (seconds)
# In production this would be replaced by actual task executors
SIM_EXEC_TIME_MIN = 1.5
SIM_EXEC_TIME_MAX = 4.0

# Simulated failure rate for demonstration (0.0 = never, 1.0 = always)
SIM_FAILURE_RATE = 0.15


class RuntimeEngine:
    """
    Main ARDO orchestration loop.

    Usage:
        engine = RuntimeEngine(graph, session_id, mode="safe", db=db)
        async for event in engine.run():
            await ws.send_json(event.to_dict())
    """

    def __init__(
        self,
        graph: ARDOGraph,
        session_id: str,
        mode: str = "safe",
        db=None,
        goal_id: Optional[str] = None,
        browser_adapter: Optional[Any] = None,
    ):
        self.graph = graph
        self.session_id = session_id
        self.mode = mode
        self.goal_id = goal_id or session_id
        self.browser_adapter = browser_adapter

        # Subsystems
        self.priority_engine = PriorityEngine()
        self.retry_manager = RetryManager.for_mode(mode)
        self.rescheduler = ReactiveRescheduler(self.priority_engine)
        self.explainability = ExplainabilityEngine()
        self.stability_guard = StabilityGuard()
        self.weight_optimizer = WeightOptimizer()
        self.monitor = ExecutionMonitor(session_id=session_id, db=db)

        # Pending approval queue (task_id → asyncio.Event)
        self._approval_gates: Dict[str, asyncio.Event] = {}

        # Execution queue (ordered list of task_ids)
        self._execution_queue: List[str] = []

        # Track tasks that have been dispatched in this cycle
        self._dispatched: set = set()

    async def run(self) -> AsyncGenerator[OrchestrationEvent, None]:
        """
        Main orchestration async generator.
        Yields OrchestrationEvent objects throughout the session lifecycle.
        """
        # Initialise execution queue
        self._execution_queue = self.priority_engine.build_execution_queue(self.graph)
        self.monitor.metrics.tasks_total = len(self.graph.nodes)

        # Transition all root tasks (no deps) to WAITING immediately
        for task_id, node in self.graph.nodes.items():
            if not node.dependency_ids and node.status == TaskState.PENDING:
                node.status = TaskStateMachine.transition(
                    TaskState.PENDING, TaskState.WAITING, task_id
                )

        logger.info(
            f"[RuntimeEngine] Session {self.session_id} started. "
            f"Mode={self.mode}, Tasks={len(self.graph.nodes)}"
        )
        
        if self.monitor.db:
            await self.monitor.persist_replay_session(self.graph, self.mode)

        # ── Main orchestration loop ────────────────────────────────────────
        iteration = 0
        max_iterations = len(self.graph.nodes) * 10  # Safety cap

        while iteration < max_iterations:
            iteration += 1

            # 0. Invariant Validation (Branch-level quarantine)
            try:
                self._assert_invariants()
            except RuntimeError as e:
                logger.error(f"[RuntimeEngine] Global invariant violation: {e}")
                self.monitor.on_task_failed(None, "Global", f"Global invariant failure: {e}")
                yield self.monitor.on_session_complete()
                return

            # 1. Check stability
            modified, self._execution_queue = self.stability_guard.check(
                self.graph, self._execution_queue
            )
            if modified:
                yield self.monitor.on_queue_reordered(
                    "stability_guard", "Stability rebalancing applied", self._execution_queue
                )
                if self.monitor.db:
                    await self.monitor.persist_scheduler_snapshot("stability_rebalance", {
                        "queue": self._execution_queue,
                        "metrics": self.monitor.get_metrics()
                    })

            # 2. Get ready tasks
            ready = DependencyManager.get_ready_tasks(self.graph)
            approval_needed = DependencyManager.get_approval_pending_tasks(self.graph)

            # 3. Handle approval-needed tasks
            for task_id in approval_needed:
                node = self.graph.get_node(task_id)
                if node and node.status not in (TaskState.APPROVAL_NEEDED, TaskState.ACTIVE):
                    node.status = TaskState.APPROVAL_NEEDED
                    gate = asyncio.Event()
                    self._approval_gates[task_id] = gate
                    yield OrchestrationEvent(
                        event_type="task_approval_needed",
                        session_id=self.session_id,
                        task_id=task_id,
                        task_name=node.task_name,
                        status="approval_needed",
                        details={
                            "risk_score": node.risk_score,
                            "reason": node.safety_metadata.get("explanation", f"Risk score {node.risk_score:.2f} exceeds threshold."),
                            # Phase 5: Full safety metadata for frontend SafetyApprovalPanel
                            "safety_metadata": node.safety_metadata if node.safety_metadata else {
                                "risk_level": "unknown",
                                "execution_decision": "require_approval",
                                "triggered_policies": [],
                                "explanation": f"Risk score {node.risk_score:.2f} exceeds threshold.",
                                "semantic_confidence": 1.0,
                            },
                        },
                    )

            # 4. Check session completion
            if self._is_session_complete():
                yield self.monitor.on_session_complete()
                if db := self.monitor.db:
                    await self.monitor.persist_metrics_snapshot()
                    await self.monitor.persist_runtime_state(self.graph)
                self._persist_semantic_memory()
                return

            # 5. No tasks to run right now
            if not ready:
                if self._all_terminal():
                    yield self.monitor.on_session_complete()
                    self._persist_semantic_memory()
                    return
                # Check for global deadlock (tasks exist, nothing ready, nothing running/approving)
                if not approval_needed and not self._dispatched:
                    active_tasks = [t for t, n in self.graph.nodes.items() if n.status == TaskState.ACTIVE]
                    if not active_tasks:
                        logger.error("[RuntimeEngine] Global Deadlock detected.")
                        yield self.monitor.on_session_complete()
                        self._persist_semantic_memory()
                        return
                
                # Waiting for approvals or in-flight tasks
                await asyncio.sleep(0.5)
                continue

            # 6. Dispatch tasks based on execution mode
            if self.mode == "parallel":
                groups = DependencyManager.get_parallel_ready_groups(self.graph)
                tasks_to_run = [task_id for group in groups for task_id in group]
                await self._dispatch_parallel(tasks_to_run)
            else:
                # Sequential / safe: dispatch one task at a time
                if ready:
                    task_id = ready[0]
                    async for event in self._execute_task(task_id):
                        yield event

            await asyncio.sleep(0.1)

        logger.warning(
            f"[RuntimeEngine] Session {self.session_id} reached iteration cap."
        )
        self._persist_semantic_memory()

    # ── Task Execution ─────────────────────────────────────────────────────

    async def _execute_task(
        self, task_id: str
    ) -> AsyncGenerator[OrchestrationEvent, None]:
        """Execute a single task through its full lifecycle."""
        node = self.graph.get_node(task_id)
        if not node:
            return

        # Skip if already dispatched in this cycle
        if task_id in self._dispatched:
            return
        self._dispatched.add(task_id)

        # ── Explainability: generate "Why Now?" ─────────────────────────
        explanation = self.explainability.explain_scheduling_decision(
            node, self.graph, self.priority_engine
        )
        self.explainability.set_active_why_now(explanation)

        # ── Transition to ACTIVE ─────────────────────────────────────────
        try:
            node.status = TaskStateMachine.transition(
                node.status, TaskState.ACTIVE, task_id
            )
        except Exception as e:
            logger.error(f"[RuntimeEngine] Cannot activate {task_id}: {e}")
            return

        node.started_at = time.time()
        event = self.monitor.on_task_started(task_id, node.task_name)
        yield event

        # Yield dependency updates for immediate successors
        transitioned = DependencyManager.mark_dependents_waiting(task_id, self.graph)
        if transitioned:
            yield OrchestrationEvent(
                event_type="dependents_waiting",
                session_id=self.session_id,
                task_id=task_id,
                task_name=node.task_name,
                status="active",
                details={
                    "newly_waiting": transitioned,
                    "explanation": explanation.to_dict(),
                },
            )

        # ── Simulate task execution ──────────────────────────────────────
        exec_time = random.uniform(SIM_EXEC_TIME_MIN, SIM_EXEC_TIME_MAX)
        node.estimated_execution_time = exec_time
        await asyncio.sleep(exec_time)

        # ── Determine outcome (with retry logic) ────────────────────────
        success = await self._attempt_execution(node)

        if success:
            await self._handle_success(node)
            prev_queue = list(self._execution_queue)
            self._execution_queue = self.rescheduler.on_task_completed(
                task_id, self.graph
            )
            self._dispatched.discard(task_id)

            # Record reschedule if queue changed
            if self._execution_queue != prev_queue:
                reschedule_event = self.explainability.record_reschedule_event(
                    task_id, "completed", prev_queue, self._execution_queue, self.graph
                )
                yield self.monitor.on_queue_reordered(
                    task_id, reschedule_event.reason, self._execution_queue
                )

            yield self.monitor.on_task_completed(task_id, node.task_name)

            # Record outcome for weight optimizer
            self._record_outcome(node, "success")

        else:
            yield self.monitor.on_task_failed(task_id, node.task_name, "Simulated execution failure")
            prev_queue = list(self._execution_queue)
            self._execution_queue = self.rescheduler.on_task_failed(
                task_id, self.graph, can_retry=False
            )
            self._dispatched.discard(task_id)

            # Cascade dependency breaks
            newly_blocked = DependencyManager.get_blocked_by_failure(self.graph)
            for blocked_id in newly_blocked:
                blocked_node = self.graph.get_node(blocked_id)
                if blocked_node:
                    yield self.monitor.on_task_blocked(
                        blocked_id, blocked_node.task_name,
                        f"Dependency {task_id} failed"
                    )

            self._record_outcome(node, "failure")

        # ── Persist state ────────────────────────────────────────────────
        if self.monitor.db:
            await self.monitor.persist_runtime_state(self.graph)

    async def _attempt_execution(self, node: ARDOTaskNode) -> bool:
        """
        Attempt task execution with retry logic.
        Executes real browser actions if present, otherwise executes default simulator.
        Returns True on success, False when all retries exhausted.
        """
        while True:
            # 1. Real Browser Execution if node has browser_action attached
            if getattr(node, "browser_action", None) is not None and self.browser_adapter is not None:
                action = node.browser_action
                result, decision = await self.browser_adapter.execute_safe_browser_task(
                    task_id=node.task_id,
                    session_id=self.session_id,
                    action=action,
                )
                node.browser_result = result
                node.execution_logs.append(
                    f"Browser action '{action.action_type}': success={result.success}, duration={result.duration_ms:.1f}ms"
                )
                if result.error:
                    node.execution_logs.append(f"Browser error: {result.error}")

                if decision.is_blocked:
                    node.status = TaskStateMachine.transition(
                        node.status if node.status in (TaskState.ACTIVE, TaskState.RETRYING) else TaskState.ACTIVE,
                        TaskState.FAILED,
                        node.task_id,
                    )
                    return False

                failed = not result.success
            else:
                # Simulated success/failure
                failed = random.random() < SIM_FAILURE_RATE

            if not failed:
                self.retry_manager.record_success(node)
                node.status = TaskStateMachine.transition(
                    node.status if node.status in (TaskState.ACTIVE, TaskState.RETRYING)
                    else TaskState.ACTIVE,
                    TaskState.COMPLETED,
                    node.task_id,
                )
                node.completed_at = time.time()
                if node.started_at:
                    node.actual_execution_time = node.completed_at - node.started_at
                return True

            # Failed — check retry eligibility
            self.retry_manager.record_failure(node)

            if self.retry_manager.can_retry(node):
                node.status = TaskStateMachine.transition(
                    TaskState.ACTIVE, TaskState.RETRYING, node.task_id
                )
                delay = self.retry_manager.get_delay(node.retry_count)
                self.monitor.on_task_retrying(
                    node.task_id, node.task_name, node.retry_count, delay
                )
                await self.retry_manager.wait_before_retry(node)

                # Transition back to ACTIVE for retry
                node.status = TaskStateMachine.transition(
                    TaskState.RETRYING, TaskState.ACTIVE, node.task_id
                )
                if getattr(node, "browser_action", None) is None:
                    await asyncio.sleep(random.uniform(SIM_EXEC_TIME_MIN, SIM_EXEC_TIME_MAX))
            else:
                # All retries exhausted
                node.status = TaskStateMachine.transition(
                    TaskState.ACTIVE, TaskState.FAILED, node.task_id
                )
                return False

    async def _handle_success(self, node: ARDOTaskNode) -> None:
        """Post-success bookkeeping."""
        node.execution_logs.append(
            f"Completed at {node.completed_at:.2f}. "
            f"Actual time: {node.actual_execution_time:.2f}s"
        )

    async def _dispatch_parallel(self, task_ids: List[str]) -> None:
        """Dispatch multiple tasks concurrently (parallel mode)."""
        tasks = [self._run_task_quietly(tid) for tid in task_ids]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_task_quietly(self, task_id: str) -> None:
        """Execute a task without yielding (for parallel dispatch)."""
        node = self.graph.get_node(task_id)
        if not node or task_id in self._dispatched:
            return
        self._dispatched.add(task_id)
        if node.status in (TaskState.PENDING, TaskState.WAITING):
            node.status = TaskState.ACTIVE
        await self._attempt_execution(node)
        self._dispatched.discard(task_id)

    # ── Approval Gate ──────────────────────────────────────────────────────

    def approve_task(self, task_id: str) -> None:
        """Called externally when an operator approves a safety-gated task."""
        gate = self._approval_gates.get(task_id)
        if gate:
            gate.set()
        new_queue = self.rescheduler.on_task_approved(task_id, self.graph)
        self._execution_queue = new_queue
        self.monitor.on_task_approved(
            task_id,
            self.graph.get_node(task_id).task_name if self.graph.get_node(task_id) else task_id,
        )

    # ── Session State Checks ───────────────────────────────────────────────

    def _is_session_complete(self) -> bool:
        return all(
            node.status in (TaskState.COMPLETED, TaskState.BLOCKED, TaskState.FAILED)
            for node in self.graph.nodes.values()
        )

    def _all_terminal(self) -> bool:
        return self._is_session_complete()
        
    # ── Invariant Validation ───────────────────────────────────────────────
    
    def _assert_invariants(self) -> None:
        """
        Validates continuous runtime invariants.
        Uses branch-level quarantine for isolated failures.
        """
        # 1. No duplicate active tasks
        active_ids = [tid for tid, n in self.graph.nodes.items() if n.status == TaskState.ACTIVE]
        if len(active_ids) != len(set(active_ids)):
            raise RuntimeError("Duplicate active tasks detected in state machine")
            
        # 2. Blocked tasks never execute
        for task_id in self._execution_queue:
            node = self.graph.get_node(task_id)
            if node and node.status == TaskState.BLOCKED and task_id in self._dispatched:
                self._quarantine_branch(task_id, "Invariant violation: Blocked task attempted execution")
                
        # 3. Retry count boundaries
        for task_id, node in self.graph.nodes.items():
            if node.retry_count > (node.max_retries or 3) + 1:
                self._quarantine_branch(task_id, f"Invariant violation: Retry count {node.retry_count} exceeded boundaries")

    def _quarantine_branch(self, task_id: str, reason: str) -> None:
        """Fails a task and triggers isolated cascade failure without stopping whole engine."""
        logger.error(f"[RuntimeEngine] Quarantining {task_id}: {reason}")
        node = self.graph.get_node(task_id)
        if node:
            node.status = TaskState.FAILED
            node.execution_logs.append(f"QUARANTINED: {reason}")
            self._dispatched.discard(task_id)
            if task_id in self._execution_queue:
                self._execution_queue.remove(task_id)
            # Monitor event handled during normal cascade logic or explicitly here
            self.monitor.on_task_failed(task_id, node.task_name, reason)

    # ── Weight Optimizer Integration ───────────────────────────────────────

    def _record_outcome(self, node: ARDOTaskNode, outcome: str) -> None:
        from orchestration.priority_engine import DEFAULT_WEIGHTS
        _, breakdown = self.priority_engine.score(node, self.graph)
        self.weight_optimizer.record_outcome(ExecutionOutcome(
            task_id=node.task_id,
            task_name=node.task_name,
            session_id=self.session_id,
            weights_used={
                "dependency": DEFAULT_WEIGHTS.dependency,
                "safety": DEFAULT_WEIGHTS.safety,
                "criticality": DEFAULT_WEIGHTS.criticality,
                "execution_cost": DEFAULT_WEIGHTS.execution_cost,
                "retry_confidence": DEFAULT_WEIGHTS.retry_confidence,
            },
            priority_score=node.priority_score,
            factor_breakdown=breakdown,
            outcome=outcome,
            retry_count=node.retry_count,
            actual_execution_time=node.actual_execution_time or 0.0,
            estimated_execution_time=node.estimated_execution_time,
            wait_time=0.0,  # Would be computed from queue_wait_start in production
        ))

    def _persist_semantic_memory(self) -> None:
        """Phase 8: Persists the completed session outcome into semantic memory."""
        try:
            from knowledge.rag_service import RAGService
            rag = RAGService.get_instance()
            if not rag or not getattr(rag, "memory_store", None):
                return

            tasks = list(self.graph.nodes.values())
            completed_count = sum(1 for n in tasks if n.status == TaskState.COMPLETED)
            failed_count = sum(1 for n in tasks if n.status == TaskState.FAILED)
            blocked_count = sum(1 for n in tasks if n.status == TaskState.BLOCKED)

            if failed_count > 0:
                outcome_status = "failed"
            elif blocked_count > 0 and completed_count == 0:
                outcome_status = "blocked"
            else:
                outcome_status = "success"

            task_summaries = [f"{n.task_name} [{n.status.value}]" for n in tasks[:8]]
            summary_str = f"Executed {len(tasks)} tasks: " + ", ".join(task_summaries)

            metrics = {
                "tasks_total": len(tasks),
                "tasks_completed": completed_count,
                "tasks_failed": failed_count,
                "tasks_blocked": blocked_count,
                "success_rate": (completed_count / len(tasks) * 100.0) if tasks else 0.0,
            }

            rag.record_plan_outcome(
                goal_text=self.goal_id or self.session_id,
                plan_id=self.goal_id or self.session_id,
                session_id=self.session_id,
                outcome_status=outcome_status,
                task_summary=summary_str,
                execution_metrics=metrics,
                safety_summary={"blocked_count": blocked_count},
            )
            logger.info(f"[RuntimeEngine] Recorded session {self.session_id} outcome ({outcome_status}) to Semantic Memory.")
        except Exception as e:
            logger.warning(f"[RuntimeEngine] Semantic memory persistence note: {e}")

    # ── Public State Access ────────────────────────────────────────────────

    def get_current_state(self) -> dict:
        """Serialise current engine state for GET /orchestrate/{id}/state."""
        return {
            "session_id": self.session_id,
            "mode": self.mode,
            "execution_queue": self._execution_queue[:10],
            "graph_state": self.graph.snapshot(),
            "parallel_groups": self.graph.parallel_groups,
            "metrics": self.monitor.get_metrics(),
            "why_now": (
                self.explainability.get_active_why_now().to_dict()
                if self.explainability.get_active_why_now()
                else None
            ),
            "reschedule_log": self.explainability.get_reschedule_log(limit=10),
            "stability_events": self.stability_guard.get_stability_events(limit=5),
            "weight_suggestion": self.weight_optimizer.get_latest_suggestion(),
        }
