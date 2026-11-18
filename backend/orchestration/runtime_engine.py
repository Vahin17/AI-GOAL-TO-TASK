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
from datetime import datetime
import logging
import os
import random
import re
import time
import uuid
from typing import AsyncGenerator, Dict, List, Optional, TYPE_CHECKING

from orchestration.state_machine import TaskState, TaskStateMachine
from core.config import settings
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
        tool_registry: Optional[Any] = None,
        browser_agent: Optional[Any] = None,
        user_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        max_approval_wait_ticks: Optional[int] = None,
    ):
        self.graph = graph
        self.session_id = session_id
        self.mode = mode
        self.goal_id = goal_id or session_id
        self.user_id = user_id
        self.db = db
        self.worker_id = worker_id or f"worker_{uuid.uuid4().hex[:12]}"
        self.browser_adapter = browser_adapter
        self._browser_agent = browser_agent
        self.max_approval_wait_ticks = max_approval_wait_ticks
        self._is_cleaned_up: bool = False

        # Phase 2: Worker Lease & Heartbeat State
        self._lease_valid: bool = True
        self._lease_task: Optional[asyncio.Task] = None
        self._lease_stop_event: asyncio.Event = asyncio.Event()

        if tool_registry is not None:
            self.tool_registry = tool_registry
        else:
            from execution.registry.tool_registry import ToolRegistry
            self.tool_registry = ToolRegistry.get_instance().create_session_registry()
            if self.browser_adapter is not None:
                try:
                    from execution.browser.browser_tool import BrowserTool
                    self.tool_registry.register(
                        BrowserTool(
                            executor=getattr(self.browser_adapter, "executor", None),
                            adapter=self.browser_adapter,
                        )
                    )
                except Exception as e:
                    logger.warning(f"[RuntimeEngine] Failed to register session browser tool: {e}")

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

        # Pending events from async callbacks (e.g. BrowserAgent)
        self._pending_events: List[OrchestrationEvent] = []

        # Track tasks that have been dispatched in this cycle
        self._dispatched: set = set()

        # Idempotency / lifecycle guard
        self._is_running = False
        self._is_completed = False
        self._is_paused = False
        self._pause_reason: Optional[str] = None
        self._pause_cooldown: float = 0.0
        self._pause_time: float = 0.0

    async def _lease_heartbeat_loop(self, interval_seconds: float = 5.0, lease_duration: float = 30.0) -> None:
        """
        Periodically renews the worker lease during active execution.
        If renewal fails (lease expired or acquired by another worker),
        sets _lease_valid = False and suspends task dispatch immediately to prevent split-brain.
        """
        from orchestration.checkpoint_manager import ExecutionCheckpointManager
        while not self._lease_stop_event.is_set():
            try:
                await asyncio.sleep(interval_seconds)
                if self._lease_stop_event.is_set():
                    break
                renewed = await ExecutionCheckpointManager.renew_worker_lease(
                    session_id=self.session_id,
                    worker_id=self.worker_id,
                    lease_duration_seconds=lease_duration,
                    db=self.db,
                )
                if not renewed:
                    logger.critical(
                        f"[RuntimeEngine] Worker lease renewal FAILED for worker={self.worker_id} "
                        f"on session={self.session_id}. Suspending task dispatch immediately to prevent split-brain."
                    )
                    self._lease_valid = False
                    break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[RuntimeEngine] Exception during lease heartbeat: {e}")

    def _handle_browser_agent_event(self, event_type: str, task: Any, details: Dict[str, Any]) -> None:
        """Handle granular observability events from BrowserAgent and record via ExecutionMonitor."""
        task_id = getattr(task, "task_id", str(task))
        task_name = getattr(task, "objective", None) or getattr(task, "task_name", task_id)
        
        if event_type == "observation_received":
            evt = self.monitor.on_observation_received(task_id, task_name, details)
        elif event_type == "decision_made":
            evt = self.monitor.on_decision_made(task_id, task_name, details)
        elif event_type == "action_started":
            evt = self.monitor.on_action_started(task_id, task_name, details)
        elif event_type == "action_completed":
            evt = self.monitor.on_action_completed(task_id, task_name, details)
        elif event_type == "action_failed":
            evt = self.monitor.on_action_failed(task_id, task_name, details, details.get("error", "Action failed"))
        elif event_type == "verification_started":
            evt = self.monitor.on_verification_started(task_id, task_name, details)
        elif event_type == "verification_completed":
            evt = self.monitor.on_verification_completed(task_id, task_name, details.get("status", "VERIFIED"), details)
        elif event_type == "recovery_started":
            evt = self.monitor.on_recovery_started(
                task_id, task_name, details.get("reason", "Recovery triggered"),
                attempt=details.get("attempt", 1), max_budget=details.get("max_budget", 3)
            )
        elif event_type == "recovery_exhausted":
            evt = self.monitor.on_recovery_exhausted(task_id, task_name, details.get("reason", "Recovery budget exhausted"))
        else:
            from orchestration.execution_monitor import OrchestrationEvent
            evt = self.monitor.emit(OrchestrationEvent(
                event_type=event_type,
                session_id=self.session_id,
                task_id=task_id,
                task_name=task_name,
                status=details.get("status", "active"),
                details=details,
            ))
        if hasattr(self, "_pending_events"):
            self._pending_events.append(evt)

    def _get_browser_agent(self) -> Any:
        """Get or lazily initialize the single session-scoped BrowserAgent."""
        if self._browser_agent is not None:
            return self._browser_agent
        from execution.browser.browser_agent import BrowserAgent
        from ai.gemini_provider import GeminiProvider
        adapter = self.browser_adapter
        if adapter is None and hasattr(self, "tool_registry") and self.tool_registry:
            browser_tool = self.tool_registry.get_tool("browser")
            adapter = getattr(browser_tool, "adapter", None)
        self._browser_agent = BrowserAgent(
            llm_provider=GeminiProvider(),
            adapter=adapter,
            event_callback=self._handle_browser_agent_event,
        )
        return self._browser_agent

    def _is_browser_task(self, node: ARDOTaskNode) -> bool:
        """
        Determines whether a task represents a browser automation task.
        Browser tasks are always routed to BrowserAgent and NEVER fall through
        to generic simulated execution.
        """
        if getattr(node, "browser_action", None) is not None:
            return True
        if getattr(node, "tool_call", None) is not None and getattr(node.tool_call, "tool_name", None) == "browser":
            return True
        if getattr(node, "tool_name", None) == "browser":
            return True
        meta = getattr(node, "metadata", {}) or {}
        if meta.get("tool") == "browser" or meta.get("is_browser_task") is True:
            return True
        if "is_browser_task" in meta and meta.get("is_browser_task") is False:
            return False

        label_lower = node.task_name.lower()
        if "http://" in label_lower or "https://" in label_lower or "file://" in label_lower:
            return True
        if re.search(
            r'\b(?:navigate|visit|browse|click|fill|extract|screenshot|scroll|page|tab|search|open|wikipedia|youtube|google|website|update|edit|play|song|video|audio|music|playback)\b',
            label_lower,
        ):
            return True
        if re.search(r'\b(?:open|load|close|switch to)\s+(?:a\s+)?(?:new\s+)?(?:tab|page|browser|website|url|site)\b', label_lower):
            return True
        if re.search(r'\b(?:open|visit|load|navigate to)\s+[a-zA-Z0-9-]+\.(?:com|org|net|io|dev|gov|edu)\b', label_lower):
            return True

        return False

    async def cleanup(self) -> None:
        """
        Idempotently clean up session-scoped browser resources.
        Guarantees that stopping this session will not affect any other session.
        """
        if getattr(self, "_is_cleaned_up", False):
            return
        self._is_cleaned_up = True
        executor = getattr(self.browser_adapter, "executor", None)
        if executor is None and hasattr(self, "tool_registry") and self.tool_registry:
            browser_tool = self.tool_registry.get_tool("browser")
            executor = getattr(browser_tool, "executor", None)
        if executor is not None and hasattr(executor, "stop"):
            try:
                # Issue E: BROWSER_INSPECTION_MODE keeps Chromium open on failure in development for inspection
                if getattr(settings, "BROWSER_INSPECTION_MODE", False) and not settings.is_production():
                    has_failure = any(n.status == TaskState.FAILED for n in self.graph.nodes.values())
                    if has_failure:
                        timeout_sec = getattr(settings, "BROWSER_INSPECTION_TIMEOUT_SECONDS", 120)
                        logger.info(
                            f"[RuntimeEngine] BROWSER_INSPECTION_MODE is enabled: preserving browser state "
                            f"for {timeout_sec}s for developer inspection."
                        )
                        await asyncio.sleep(timeout_sec)

                if hasattr(executor, "stop"):
                    await executor.stop()
            except Exception as e:
                logger.warning(f"[RuntimeEngine] Session {self.session_id} browser stop error: {e}")

    async def run(self) -> AsyncGenerator[OrchestrationEvent, None]:
        """
        Main orchestration async generator.
        Yields OrchestrationEvent objects throughout the session lifecycle.
        """
        if self._is_running:
            logger.warning(
                f"[RUN-GUARD] runtime.run ALREADY RUNNING for session {self.session_id}. Aborting duplicate run."
            )
            return
        if self._is_completed:
            logger.warning(
                f"[RUN-GUARD] runtime.run ALREADY COMPLETED for session {self.session_id}. Cannot restart."
            )
            return

        self._is_running = True
        logger.info(f"[RUN-GUARD] runtime.run started session={self.session_id}")

        try:
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

            # Phase 2 Task 5: Acquire authoritative worker lease and start heartbeat
            from orchestration.checkpoint_manager import ExecutionCheckpointManager
            acquired = await ExecutionCheckpointManager.acquire_worker_lease(
                session_id=self.session_id,
                worker_id=self.worker_id,
                lease_duration_seconds=30.0,
                db=self.db,
            )
            if not acquired:
                msg = f"Cannot acquire worker lease for session {self.session_id}: held by another active worker."
                logger.critical(f"[RuntimeEngine] {msg}")
                self._lease_valid = False
                raise RuntimeError(msg)

            self._lease_valid = True
            self._lease_stop_event.clear()
            self._lease_task = asyncio.create_task(
                self._lease_heartbeat_loop(interval_seconds=5.0, lease_duration=30.0)
            )
            
            if self.monitor.db is not None:
                await self.monitor.persist_replay_session(self.graph, self.mode)

            # ── Main orchestration loop ────────────────────────────────────────
            iteration = 0
            max_iterations = max(200, len(self.graph.nodes) * 50)  # Safety cap for orchestration cycles

            while iteration < max_iterations:
                iteration += 1

                # Check for explicit execution pause (e.g. provider recovery or operator pause)
                if self._is_paused:
                    logger.info(f"[RuntimeEngine] Session {self.session_id} is paused (reason: {self._pause_reason}).")
                    yield OrchestrationEvent(
                        event_type="session_paused",
                        session_id=self.session_id,
                        status="paused",
                        details={
                            "reason": self._pause_reason,
                            "cooldown_seconds": self._pause_cooldown,
                            "checkpoint_saved": True,
                        },
                    )
                    return

                # Split-brain check: if lease was revoked or expired, suspend task dispatch immediately
                if not self._lease_valid:
                    logger.critical(
                        f"[RuntimeEngine] Worker lease lost or expired for worker {self.worker_id} "
                        f"on session {self.session_id}. Suspending task dispatch immediately."
                    )
                    yield OrchestrationEvent(
                        event_type="worker_lease_lost",
                        session_id=self.session_id,
                        status="suspended",
                        details={
                            "worker_id": self.worker_id,
                            "reason": "Worker lease expired or acquired by another worker.",
                        },
                    )
                    return

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
                    if self.monitor.db is not None:
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
                    db = self.monitor.db
                    if db is not None:
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
                    # Drain any pending events before waiting
                    while hasattr(self, "_pending_events") and self._pending_events:
                        yield self._pending_events.pop(0)

                    # Check for global deadlock (tasks exist, nothing ready, nothing running/approving)
                    if not approval_needed and not self._dispatched:
                        active_tasks = [t for t, n in self.graph.nodes.items() if n.status == TaskState.ACTIVE]
                        if not active_tasks:
                            logger.error("[RuntimeEngine] Global Deadlock detected.")
                            yield self.monitor.on_session_complete()
                            self._persist_semantic_memory()
                            return

                    # If only waiting for external human approval with nothing dispatched or running, limit wait ticks
                    if approval_needed and not self._dispatched:
                        active_tasks = [t for t, n in self.graph.nodes.items() if n.status == TaskState.ACTIVE]
                        if not active_tasks:
                            self._approval_wait_ticks = getattr(self, "_approval_wait_ticks", 0) + 1
                            max_ticks = getattr(self, "max_approval_wait_ticks", None)
                            if max_ticks is None:
                                timeout_secs = float(getattr(settings, "APPROVAL_WAIT_MAX_SECONDS", 60.0))
                                max_ticks = max(1, int(timeout_secs / 0.5))
                            if self._approval_wait_ticks >= max_ticks:
                                logger.info("[RuntimeEngine] Awaiting approval timeout reached without input. Ending run loop.")
                                return

                    # Waiting for approvals or in-flight tasks
                    await asyncio.sleep(0.5)
                    while hasattr(self, "_pending_events") and self._pending_events:
                        yield self._pending_events.pop(0)
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
        finally:
            self._lease_stop_event.set()
            if self._lease_task and not self._lease_task.done():
                self._lease_task.cancel()
                try:
                    await self._lease_task
                except asyncio.CancelledError:
                    pass
            if self._lease_valid:
                try:
                    from orchestration.checkpoint_manager import ExecutionCheckpointManager
                    await ExecutionCheckpointManager.release_worker_lease(
                        session_id=self.session_id,
                        worker_id=self.worker_id,
                        db=self.db,
                    )
                except Exception as e:
                    logger.debug(f"[RuntimeEngine] Error releasing worker lease: {e}")
            self._is_running = False
            self._is_completed = True
            logger.info(f"[RUN-GUARD] runtime.run finished session={self.session_id}")

    # ── Task Execution ─────────────────────────────────────────────────────

    def _verify_plan_approval_for_task(
        self, task_id: str, node: ARDOTaskNode
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Cryptographic verification of plan approval binding for a gated task.
        Enforced identically across sequential, safe, and parallel execution modes.
        Returns (is_approved, failure_reason, current_plan_hash).
        """
        clean_session_id = str(self.session_id or "").strip()
        clean_user_id = str(self.user_id or getattr(self.graph, "user_id", None) or "").strip()

        if not clean_session_id or not clean_user_id:
            reason = "Cryptographic approval verification failed: missing mandatory session or user identity."
            logger.error(f"[RuntimeEngine] {reason} (session={self.session_id}, task={task_id})")
            return False, reason, None

        current_plan_hash = None
        if hasattr(self.graph, "recompute_plan_hash"):
            try:
                current_plan_hash = self.graph.recompute_plan_hash(
                    goal_text=self.goal_id,
                    session_id=clean_session_id,
                    user_id=clean_user_id,
                )
            except Exception as e:
                logger.error(f"[RuntimeEngine] Authoritative plan hash recomputation failed: {e}")
                current_plan_hash = None

        if not current_plan_hash or not str(current_plan_hash).strip():
            reason = "Cryptographic approval verification failed: authoritative plan hash recomputation failed."
            logger.error(f"[RuntimeEngine] {reason} (session={self.session_id}, task={task_id})")
            return False, reason, None

        from core.auth_db import AuthDatabase
        try:
            auth_db = AuthDatabase.get_instance()
            is_approved = auth_db.is_plan_approved(
                session_id=clean_session_id,
                plan_hash=current_plan_hash,
                task_id=task_id,
            )
        except Exception as e:
            logger.warning(f"[RuntimeEngine] Error checking plan approval DB: {e}")
            is_approved = False

        if not is_approved:
            reason = "Cryptographic approval verification failed: plan modified, unapproved, or approval expired."
            logger.error(f"[RuntimeEngine] {reason} (session={self.session_id}, task={task_id})")
            return False, reason, current_plan_hash

        return True, None, current_plan_hash

    async def _execute_task(
        self, task_id: str
    ) -> AsyncGenerator[OrchestrationEvent, None]:
        """Execute a single task through its full lifecycle."""
        if not self._lease_valid:
            logger.critical(f"[RuntimeEngine] Task {task_id} dispatch refused: worker lease is invalid.")
            return

        node = self.graph.get_node(task_id)
        if not node:
            return

        # Skip if already terminal (completed/failed/blocked) or currently dispatched
        if node.status in (TaskState.COMPLETED, TaskState.FAILED, TaskState.BLOCKED):
            logger.info(f"[RuntimeEngine] Skipping task {task_id} already in terminal state {node.status.value}")
            return

        if task_id in self._dispatched:
            return
        self._dispatched.add(task_id)

        # ── Phase 2: Cryptographic Plan Approval Binding Verification ──
        was_gated = getattr(node, "was_approval_gated", False) or "Operator approved." in "".join(node.execution_logs)
        if was_gated:
            is_valid, reason, current_plan_hash = self._verify_plan_approval_for_task(task_id, node)
            if not is_valid:
                self._quarantine_branch(task_id, reason or "Cryptographic approval verification failed.")
                yield OrchestrationEvent(
                    event_type="task_approval_invalidated",
                    session_id=self.session_id,
                    task_id=task_id,
                    task_name=node.task_name,
                    status="failed",
                    details={"reason": reason, "plan_hash": current_plan_hash},
                )
                return

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
            self._dispatched.discard(task_id)
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

        # ── Execution delay check ───────────────────────────────────────
        # Simulated artificial delay eliminated under fail-closed invariant.

        # ── Determine outcome with bounded execution timeout (Phase 4) ──
        timeout_seconds = getattr(settings, "TASK_EXECUTION_TIMEOUT_SECONDS", 120.0)
        timed_out = False
        try:
            success = await asyncio.wait_for(self._attempt_execution(node), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            logger.error(f"[RuntimeEngine] Task {task_id} timed out after {timeout_seconds}s.")
            node.execution_logs.append(f"Task execution timed out after {timeout_seconds}s.")
            timed_out = True
            success = False

        # Phase 4.1: Handle timeout cancellation on potentially mutating / non-idempotent actions
        if timed_out:
            from orchestration.checkpoint_manager import is_task_idempotent
            is_idemp = is_task_idempotent(node)
            resolved = False

            # 1. Attempt post-timeout observational reconciliation if browser adapter is active
            if not is_idemp and self.browser_adapter is not None:
                try:
                    observation = None
                    if hasattr(self.browser_adapter, "get_observation"):
                        if asyncio.iscoroutinefunction(self.browser_adapter.get_observation):
                            observation = await self.browser_adapter.get_observation()
                        else:
                            observation = self.browser_adapter.get_observation()
                    elif hasattr(self.browser_adapter, "executor") and hasattr(self.browser_adapter.executor, "get_observation"):
                        obs_fn = getattr(self.browser_adapter.executor, "get_observation")
                        if asyncio.iscoroutinefunction(obs_fn):
                            observation = await obs_fn()
                        else:
                            observation = obs_fn()

                    if observation is not None:
                        from execution.browser.agent_contracts import BrowserTask, BrowserVerificationStatus
                        from execution.browser.semantic_verifier import SemanticVerifier

                        expected_url = None
                        success_criteria = None
                        if isinstance(node.metadata, dict):
                            expected_url = node.metadata.get("expected_url")
                            sc = node.metadata.get("success_criteria")
                            if isinstance(sc, list):
                                success_criteria = "; ".join(str(x) for x in sc)
                            elif isinstance(sc, str):
                                success_criteria = sc

                        meta = {}
                        if expected_url:
                            meta["expected_url"] = expected_url

                        btask = BrowserTask(
                            task_id=node.task_id,
                            session_id=self.session_id,
                            objective=node.task_name,
                            expected_url=expected_url,
                            success_criteria=success_criteria,
                            metadata=meta,
                        )
                        verif = SemanticVerifier.verify(btask, observation)
                        if verif.status == BrowserVerificationStatus.VERIFIED:
                            node.status = TaskStateMachine.transition(
                                node.status if node.status in (TaskState.ACTIVE, TaskState.RETRYING) else TaskState.ACTIVE,
                                TaskState.COMPLETED,
                                node.task_id,
                            )
                            node.completed_at = time.time()
                            node.execution_logs.append(
                                f"Post-timeout semantic verification confirmed task completed despite timeout ({verif.verified_by})."
                            )
                            success = True
                            resolved = True
                except Exception as e:
                    logger.warning(f"[RuntimeEngine] Post-timeout verification error for {task_id}: {e}")

            if not resolved:
                if not is_idemp:
                    # Non-idempotent or mutating task timed out: OUTCOME IS UNCERTAIN
                    # DO NOT mark as ordinary retryable failure. Transition to OUTCOME_UNKNOWN and require approval.
                    node.status = TaskStateMachine.transition(
                        node.status if node.status in (TaskState.ACTIVE, TaskState.RETRYING) else TaskState.ACTIVE,
                        TaskState.OUTCOME_UNKNOWN,
                        node.task_id,
                    )
                    node.approval_needed = True
                    node.execution_logs.append(
                        f"Task timed out after {timeout_seconds}s and external outcome is uncertain (non-idempotent). "
                        f"Transitioned to OUTCOME_UNKNOWN. Operator approval required to reconcile or proceed."
                    )
                    # Record uncertain evidence in EvidenceVault
                    try:
                        from execution.evidence.evidence_vault import EvidenceVault
                        ev_owner = str(self.user_id or getattr(self.graph, "user_id", None) or "").strip()
                        EvidenceVault.record_evidence(
                            user_id=ev_owner if ev_owner else None,
                            session_id=self.session_id,
                            plan_id=self.goal_id or self.session_id,
                            plan_hash=getattr(self.graph, "plan_hash", "") or "unhashed",
                            task_id=node.task_id,
                            task_name=node.task_name,
                            evidence_type="timeout_uncertain",
                            raw_content=f"Task timed out after {timeout_seconds}s. External mutating outcome indeterminate.",
                            verification_status="outcome_unknown",
                            verification_reason="Execution timed out during mutating action; external state indeterminate",
                            confidence_score=0.0,
                            metadata={"timeout_seconds": timeout_seconds, "is_idempotent": False},
                            db=self.db,
                        )
                    except Exception as e:
                        logger.warning(f"[RuntimeEngine] EvidenceVault record note: {e}")

                    # Yield event informing monitor/operator
                    yield OrchestrationEvent(
                        event_type="task_outcome_unknown",
                        session_id=self.session_id,
                        task_id=task_id,
                        task_name=node.task_name,
                        status="outcome_unknown",
                        details={
                            "reason": f"Execution timed out after {timeout_seconds}s for non-idempotent action",
                            "requires_approval": True,
                        },
                    )

                    self._execution_queue = self.rescheduler.on_task_failed(
                        task_id, self.graph, can_retry=False
                    )
                    self._dispatched.discard(task_id)

                    # Cascade blocks to downstream dependents
                    newly_blocked = DependencyManager.get_blocked_by_failure(self.graph)
                    for blocked_id in newly_blocked:
                        blocked_node = self.graph.get_node(blocked_id)
                        if blocked_node and blocked_node.status != TaskState.BLOCKED:
                            blocked_node.status = TaskState.BLOCKED
                            yield self.monitor.on_task_blocked(
                                blocked_id, blocked_node.task_name,
                                f"Dependency {task_id} timed out with OUTCOME_UNKNOWN"
                            )

                    self._record_outcome(node, "timeout_outcome_unknown")

                    if self.monitor.db is not None:
                        await self.monitor.persist_runtime_state(self.graph)
                    return
                else:
                    # Idempotent task timed out: safe to mark as failed
                    if node.status in (TaskState.ACTIVE, TaskState.RETRYING):
                        node.status = TaskStateMachine.transition(node.status, TaskState.FAILED, node.task_id)

        try:
            from core.metrics import metrics_collector
            duration = (node.actual_execution_time or (time.time() - (node.started_at or time.time())))
            metrics_collector.record_browser_task(max(0.0, duration), "success" if success else "failed")
        except Exception:
            pass

        # Drain any granular events (e.g. BrowserAgent events) emitted during execution
        while self._pending_events:
            yield self._pending_events.pop(0)

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

            is_ver = bool(
                (node.metadata.get("verification_status") if isinstance(node.metadata, dict) else None) == "verified"
                or node.status == TaskState.VERIFIED
            )
            v_stat = (node.metadata.get("verification_status") if isinstance(node.metadata, dict) else None) or ("verified" if is_ver else "unverified")
            art_h = node.metadata.get("artifact_hash") if isinstance(node.metadata, dict) else None
            yield self.monitor.on_task_completed(
                task_id,
                node.task_name,
                is_verified=is_ver,
                verification_status=v_stat,
                artifact_hash=art_h,
            )

            # Record outcome for weight optimizer
            self._record_outcome(node, "success")

        else:
            await self._handle_failure(node, "Task execution failed")
            yield self.monitor.on_task_failed(task_id, node.task_name, "Task execution failed")
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
                    if blocked_node.status != TaskState.BLOCKED:
                        blocked_node.status = TaskState.BLOCKED
                    yield self.monitor.on_task_blocked(
                        blocked_id, blocked_node.task_name,
                        f"Dependency {task_id} failed"
                    )

            self._record_outcome(node, "failure")

        # ── Persist state ────────────────────────────────────────────────
        if self.monitor.db is not None:
            await self.monitor.persist_runtime_state(self.graph)

    async def _attempt_execution(self, node: ARDOTaskNode) -> bool:
        """
        Attempt task execution.
        For browser tasks: delegates entirely to BrowserAgent (using session-scoped browser resources).
        For non-browser tool tasks: dispatches via session tool_registry.
        For generic tasks: executes simulator with standard retry management.
        Returns True on success, False when execution or retries fail.
        """
        while True:
            # 1. Browser Task: Delegate entirely to BrowserAgent
            if self._is_browser_task(node):
                from execution.browser.agent_contracts import BrowserTask, BrowserAgentOutcome

                try:
                    agent = self._get_browser_agent()
                    node_meta = dict(getattr(node, "metadata", {}) or {})
                    b_act = getattr(node, "browser_action", None)
                    if b_act is not None:
                        act_url = b_act.get("url") if isinstance(b_act, dict) else getattr(b_act, "url", None)
                        act_type = b_act.get("action_type") if isinstance(b_act, dict) else getattr(b_act, "action_type", None)
                        if hasattr(act_type, "value"):
                            act_type = act_type.value
                        if act_url and "url" not in node_meta:
                            node_meta["url"] = act_url
                        if act_url and "expected_url" not in node_meta:
                            node_meta["expected_url"] = act_url
                        if act_type and "action_type" not in node_meta:
                            node_meta["action_type"] = str(act_type)

                    # Inspect predecessor tasks in the DAG for verified postcondition state
                    precondition_url = None
                    precondition_state = {}
                    dep_ids = getattr(node, "dependency_ids", None) or getattr(node, "dependencies", None)
                    if dep_ids:
                        for dep_id in dep_ids:
                            dep_node = self.graph.get_node(dep_id)
                            if dep_node:
                                dep_meta = getattr(dep_node, "metadata", {}) or {}
                                dep_post = dep_meta.get("postcondition") or {}
                                dep_res = getattr(dep_node, "browser_result", None)

                                dep_url = None
                                if dep_res and getattr(dep_res, "verification", None):
                                    dep_url = getattr(dep_res.verification, "observed_url", None)
                                if not dep_url and dep_res:
                                    dep_url = getattr(dep_res, "final_url", None)
                                if not dep_url and isinstance(dep_post, dict):
                                    dep_url = dep_post.get("url")
                                if not dep_url:
                                    dep_url = dep_meta.get("destination_url") or dep_meta.get("expected_url")

                                if dep_url and not precondition_url:
                                    precondition_url = dep_url
                                precondition_state[dep_id] = {
                                    "task_name": dep_node.task_name,
                                    "url": dep_url,
                                    "status": str(dep_node.status),
                                    "postcondition": dep_post,
                                }

                    btask = BrowserTask(
                        task_id=node.task_id,
                        objective=node.task_name,
                        session_id=self.session_id,
                        goal_text=self.goal_id or node.task_name,
                        expected_url=node_meta.get("expected_url") or node_meta.get("destination_url") or node_meta.get("url"),
                        success_criteria=node_meta.get("success_criteria"),
                        constraints=node_meta.get("constraints", []),
                        metadata=node_meta,
                        precondition_url=precondition_url,
                        precondition_state=precondition_state or None,
                    )
                    logger.info(f"[RuntimeEngine] Dispatching task {node.task_id} ('{node.task_name}') to BrowserAgent (precondition_url='{precondition_url}')")
                    agent_res = await agent.run(btask)
                    node.browser_result = agent_res
                except Exception as exc:
                    logger.error(f"[RuntimeEngine] Browser execution failure for {node.task_id}: {exc}")
                    node.execution_logs.append(f"[BrowserAgent] Execution failed: {exc}")
                    node.status = TaskStateMachine.transition(
                        node.status if node.status in (TaskState.ACTIVE, TaskState.RETRYING) else TaskState.ACTIVE,
                        TaskState.FAILED,
                        node.task_id,
                    )
                    return False

                if agent_res.outcome == BrowserAgentOutcome.COMPLETED:
                    from execution.browser.agent_contracts import BrowserVerificationStatus
                    is_verified = bool(
                        agent_res.verification
                        and agent_res.verification.status == BrowserVerificationStatus.VERIFIED
                    )
                    if is_verified:
                        v_info = (agent_res.verification.verified_by if agent_res.verification else None) or "Verified"
                        node.execution_logs.append(f"[BrowserAgent] COMPLETED and VERIFIED: {v_info}")
                        failed = False
                    else:
                        fail_reason = (agent_res.verification.failure_reason if agent_res.verification else None) or agent_res.error or "Verification failed"
                        logger.error(f"[FALSE-SUCCESS-PREVENTED] BrowserAgent finished but verification failed for {node.task_id}: {fail_reason}")
                        node.execution_logs.append(f"[BrowserAgent] COMPLETED but UNVERIFIED: {fail_reason}")
                        failed = True
                elif agent_res.outcome == BrowserAgentOutcome.BLOCKED:
                    node.execution_logs.append(f"[BrowserAgent] BLOCKED by safety policy: {agent_res.error or 'Blocked'}")
                    node.status = TaskStateMachine.transition(
                        node.status if node.status in (TaskState.ACTIVE, TaskState.RETRYING) else TaskState.ACTIVE,
                        TaskState.FAILED,
                        node.task_id,
                    )
                    return False
                else:
                    node.execution_logs.append(f"[BrowserAgent] FAILED: {agent_res.error or 'Failed'}")
                    failed = True

                # BrowserAgent already handles its own internal observe-reason-act-recover loop.
                # RuntimeEngine does not loop retries on a failed BrowserAgent result.
                if failed:
                    node.status = TaskStateMachine.transition(
                        TaskState.ACTIVE, TaskState.FAILED, node.task_id
                    )
                    return False
                else:
                    self.retry_manager.record_success(node)
                    node.status = TaskStateMachine.transition(
                        node.status if node.status in (TaskState.ACTIVE, TaskState.RETRYING) else TaskState.ACTIVE,
                        TaskState.COMPLETED,
                        node.task_id,
                    )
                    node.completed_at = time.time()
                    if node.started_at:
                        node.actual_execution_time = node.completed_at - node.started_at

                    # Record verified postcondition state for dependent tasks
                    post_url = None
                    post_title = None
                    is_verified = False
                    verif = getattr(agent_res, "verification", None)
                    if verif is not None:
                        post_url = getattr(verif, "observed_url", None)
                        post_title = getattr(verif, "observed_title", None)
                        from execution.browser.agent_contracts import BrowserVerificationStatus
                        if getattr(verif, "status", None) == BrowserVerificationStatus.VERIFIED:
                            is_verified = True
                    if not post_url:
                        post_url = getattr(agent_res, "final_url", None)
                    if not post_title:
                        post_title = getattr(agent_res, "final_title", None)

                    node.metadata["postcondition"] = {
                        "url": post_url,
                        "title": post_title,
                        "verified": is_verified,
                        "verified_at": time.time(),
                    }
                    return True

            # 2. Non-browser Tool Execution via session-scoped ToolRegistry
            elif getattr(node, "tool_call", None) is not None:
                from orchestration.safety_schemas import ExecutionDecision
                if isinstance(node.tool_call, dict):
                    from execution.registry.tool_models import ToolCall
                    tc_kwargs = dict(node.tool_call)
                    tc_kwargs.setdefault("call_id", f"call_{node.task_id}")
                    tc_kwargs.setdefault("action_name", tc_kwargs.get("action_type", "execute"))
                    node.tool_call = ToolCall(**tc_kwargs)
                registry = self.tool_registry if hasattr(self, "tool_registry") else None
                if registry is None:
                    from execution.registry.tool_registry import ToolRegistry
                    registry = ToolRegistry.get_instance()
                tool = registry.get_tool(node.tool_call.tool_name)

                # Pre-execution safety verification
                if tool and hasattr(tool, "infer_safety_context"):
                    if self.browser_adapter is not None and hasattr(self.browser_adapter, "safety_policy_engine"):
                        policy_engine = self.browser_adapter.safety_policy_engine
                    else:
                        from orchestration.safety_policy import SafetyPolicyEngine
                        policy_engine = SafetyPolicyEngine()

                    s_ctx = tool.infer_safety_context(node.task_id, node.tool_call)
                    decision = policy_engine.evaluate(
                        s_ctx, node.tool_call.description or node.task_name
                    )
                    if decision.is_blocked:
                        node.execution_logs.append(f"[SafetyEngine] BLOCKED by policy: {decision.explanation}")
                        node.status = TaskStateMachine.transition(
                            node.status if node.status in (TaskState.ACTIVE, TaskState.RETRYING) else TaskState.ACTIVE,
                            TaskState.FAILED,
                            node.task_id,
                        )
                        return False
                    if decision.execution_decision == ExecutionDecision.REQUIRE_APPROVAL:
                        approved = False
                        if hasattr(self.browser_adapter, "safety_engine") and self.browser_adapter.safety_engine:
                            approved = self.browser_adapter.safety_engine.is_approved(self.session_id, node.task_id, node.task_name)
                        if not approved:
                            node.execution_logs.append("[SafetyEngine] APPROVAL_REQUIRED before tool dispatch.")
                            node.status = TaskStateMachine.transition(
                                node.status if node.status in (TaskState.ACTIVE, TaskState.RETRYING) else TaskState.ACTIVE,
                                TaskState.FAILED,
                                node.task_id,
                            )
                            return False

                tool_result = await registry.dispatch(node.tool_call)
                node.tool_result = tool_result
                node.execution_logs.append(
                    f"Tool '{node.tool_call.tool_name}.{node.tool_call.action_name}': success={tool_result.success}, duration={tool_result.duration_ms:.1f}ms"
                )
                failed = not tool_result.success

            # 3. Unbound Tasks Without Suitable Registered Executor — FAIL CLOSED
            else:
                logger.error(
                    f"[RuntimeEngine] Task {node.task_id} ('{node.task_name}') has no executable "
                    f"tool binding or browser action. Simulated completion forbidden. Failing closed."
                )
                node.execution_logs.append(
                    "No executable binding or tool executor. Simulated completion forbidden."
                )
                node.status = TaskStateMachine.transition(
                    node.status if node.status in (TaskState.ACTIVE, TaskState.RETRYING)
                    else TaskState.ACTIVE,
                    TaskState.FAILED,
                    node.task_id,
                )
                return False

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

            # Failed — check retry eligibility for non-browser tasks
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
                node.status = TaskStateMachine.transition(
                    TaskState.RETRYING, TaskState.ACTIVE, node.task_id
                )
            else:
                node.status = TaskStateMachine.transition(
                    TaskState.ACTIVE, TaskState.FAILED, node.task_id
                )
                return False

    async def _handle_success(self, node: ARDOTaskNode) -> None:
        """Post-success bookkeeping & cryptographic evidence recording."""
        completed_at = node.completed_at or time.time()
        actual_time = node.actual_execution_time if node.actual_execution_time is not None else max(0.0, completed_at - (node.started_at or completed_at))
        node.execution_logs.append(
            f"Completed at {completed_at:.2f}. "
            f"Actual time: {actual_time:.2f}s"
        )
        try:
            from execution.evidence.evidence_vault import EvidenceVault
            plan_hash = getattr(self.graph, "plan_hash", "") or "unhashed"
            browser_res = getattr(node, "browser_result", None)
            v_status = "unverified"
            v_reason = None
            conf = 0.0
            ev_type = "task_execution"
            raw_payload = None

            if browser_res and hasattr(browser_res, "verification") and browser_res.verification:
                verif = browser_res.verification
                v_status = verif.status.value if hasattr(verif.status, "value") else str(verif.status)
                v_reason = verif.failure_reason
                v_by = getattr(verif, "verified_by", None)
                conf = getattr(verif, "confidence_score", 0.0)
                ev_type = "browser_verification"
                raw_payload = (
                    f"Task: {node.task_id} ({node.task_name}) | "
                    f"URL: {verif.observed_url} | Title: {verif.observed_title} | "
                    f"VerifiedBy: {v_by} | Reason: {v_reason}"
                )
            elif getattr(node, "tool_result", None) is not None:
                tr = node.tool_result
                ev_type = "tool_execution"
                out_data = getattr(tr, "output_data", None)
                if out_data is None and hasattr(tr, "data"):
                    out_data = getattr(tr, "data", None)
                has_valid_output = bool(
                    (isinstance(out_data, dict) and len(out_data) > 0)
                    or (out_data is not None and not isinstance(out_data, dict) and str(out_data).strip() != "")
                    or bool(getattr(tr, "extracted_text", None))
                    or bool(getattr(tr, "artifacts", None))
                )
                is_tool_verified = bool(getattr(tr, "success", False) is True and has_valid_output)
                v_status = "verified" if is_tool_verified else ("unverified" if getattr(tr, "success", False) else "failed")
                conf = 1.0 if is_tool_verified else 0.0
                if isinstance(out_data, (dict, list)) and len(out_data) > 0:
                    import json
                    raw_payload = json.dumps(out_data, sort_keys=True)
                elif out_data is not None and not isinstance(out_data, (dict, list)) and str(out_data).strip() != "":
                    raw_payload = str(out_data)
                elif getattr(tr, "extracted_text", None):
                    raw_payload = str(tr.extracted_text)
                else:
                    raw_payload = None
            else:
                v_status = "unverified"
                conf = 0.0
                raw_payload = f"Task {node.task_id} ({node.task_name}) completed without empirical verification artifact"

            ev_owner = str(self.user_id or getattr(self.graph, "user_id", None) or "").strip()
            rec = EvidenceVault.record_evidence(
                user_id=ev_owner if ev_owner else None,
                session_id=self.session_id,
                plan_id=self.goal_id or self.session_id,
                plan_hash=plan_hash,
                task_id=node.task_id,
                task_name=node.task_name,
                evidence_type=ev_type,
                raw_content=raw_payload,
                verification_status=v_status,
                verification_reason=v_reason,
                confidence_score=conf,
                metadata={"execution_logs": node.execution_logs[-3:] if node.execution_logs else []},
                db=self.db,
            )
            if node.metadata is None:
                node.metadata = {}
            node.metadata["evidence_id"] = rec.id
            node.metadata["artifact_hash"] = rec.artifact_hash
            node.metadata["verification_status"] = rec.verification_status
            node.metadata["confidence_score"] = rec.confidence_score
        except Exception as e:
            logger.warning(f"[RuntimeEngine] Evidence recording on success note: {e}")

    async def _handle_failure(self, node: ARDOTaskNode, reason: str = "Task execution failed") -> None:
        """Post-failure bookkeeping & cryptographic evidence recording."""
        node.execution_logs.append(f"Failed: {reason}")
        if node.metadata is None:
            node.metadata = {}
        node.metadata["failure_reason"] = reason
        try:
            from execution.evidence.evidence_vault import EvidenceVault
            plan_hash = getattr(self.graph, "plan_hash", "") or "unhashed"
            browser_res = getattr(node, "browser_result", None)
            v_reason = reason
            conf = 0.0
            ev_type = "task_execution"
            raw_payload = f"Task {node.task_id} failed: {reason}"

            if browser_res and hasattr(browser_res, "verification") and browser_res.verification:
                verif = browser_res.verification
                v_reason = verif.failure_reason or browser_res.error or reason
                conf = getattr(verif, "confidence_score", 0.0)
                ev_type = "browser_verification"
                raw_payload = f"URL: {verif.observed_url} | Title: {verif.observed_title} | Error: {v_reason}"

            ev_owner = str(self.user_id or getattr(self.graph, "user_id", None) or "").strip()
            rec = EvidenceVault.record_evidence(
                user_id=ev_owner if ev_owner else None,
                session_id=self.session_id,
                plan_id=self.goal_id or self.session_id,
                plan_hash=plan_hash,
                task_id=node.task_id,
                task_name=node.task_name,
                evidence_type=ev_type,
                raw_content=raw_payload,
                verification_status="failed",
                verification_reason=v_reason,
                confidence_score=conf,
                metadata={"execution_logs": node.execution_logs[-3:] if node.execution_logs else []},
                db=self.db,
            )
            if node.metadata is None:
                node.metadata = {}
            node.metadata["evidence_id"] = rec.id
            node.metadata["artifact_hash"] = rec.artifact_hash
            node.metadata["verification_status"] = "failed"
            node.metadata["confidence_score"] = conf
        except Exception as e:
            logger.warning(f"[RuntimeEngine] Evidence recording on failure note: {e}")

    async def _dispatch_parallel(self, task_ids: List[str]) -> None:
        """Dispatch multiple tasks concurrently (parallel mode)."""
        tasks = [self._run_task_quietly(tid) for tid in task_ids if tid not in self._dispatched]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_task_quietly(self, task_id: str) -> None:
        """Execute a task without yielding (for parallel dispatch)."""
        if not self._lease_valid:
            logger.critical(f"[RuntimeEngine] Task {task_id} parallel dispatch refused: worker lease is invalid.")
            return

        node = self.graph.get_node(task_id)
        if not node:
            return
        if node.status in (TaskState.COMPLETED, TaskState.FAILED, TaskState.BLOCKED, TaskState.APPROVAL_NEEDED):
            return
        if task_id in self._dispatched:
            return
        self._dispatched.add(task_id)

        # ── Cryptographic Plan Approval Binding Verification (Parallel Mode) ──
        was_gated = getattr(node, "was_approval_gated", False) or "Operator approved." in "".join(node.execution_logs)
        if was_gated:
            is_valid, reason, current_plan_hash = self._verify_plan_approval_for_task(task_id, node)
            if not is_valid:
                node.metadata["quarantine_reason"] = reason
                self._quarantine_branch(task_id, reason or "Cryptographic approval verification failed.")
                if not hasattr(self, "_pending_events") or self._pending_events is None:
                    self._pending_events = []
                self._pending_events.append(
                    OrchestrationEvent(
                        event_type="task_approval_invalidated",
                        session_id=self.session_id,
                        task_id=task_id,
                        task_name=node.task_name,
                        status="failed",
                        details={"reason": reason, "plan_hash": current_plan_hash},
                    )
                )
                self._dispatched.discard(task_id)
                return

        try:
            node.status = TaskStateMachine.transition(node.status, TaskState.ACTIVE, task_id)
            node.started_at = time.time()
            success = await self._attempt_execution(node)
            if success:
                node.status = TaskStateMachine.transition(
                    node.status,
                    TaskState.COMPLETED,
                    node.task_id,
                )
                node.completed_at = time.time()
                await self._handle_success(node)
                self._execution_queue = self.rescheduler.on_task_completed(task_id, self.graph)
                self._record_outcome(node, "success")
            else:
                await self._handle_failure(node, "Parallel task failed")
                self._execution_queue = self.rescheduler.on_task_failed(task_id, self.graph, can_retry=False)
                newly_blocked = DependencyManager.get_blocked_by_failure(self.graph)
                for blocked_id in newly_blocked:
                    blocked_node = self.graph.get_node(blocked_id)
                    if blocked_node and blocked_node.status != TaskState.BLOCKED:
                        blocked_node.status = TaskState.BLOCKED
                self._record_outcome(node, "failure")
        except Exception as ex:
            logger.error(f"[RuntimeEngine] Error in parallel task {task_id}: {ex}")
            node.status = TaskState.FAILED
            await self._handle_failure(node, str(ex))
            self._record_outcome(node, "failure")
        finally:
            self._dispatched.discard(task_id)

    # ── Approval Gate ──────────────────────────────────────────────────────

    def approve_task(self, task_id: str) -> None:
        """Called externally when an operator approves a safety-gated task."""
        gate = self._approval_gates.get(task_id)
        if gate:
            gate.set()
        self._approval_wait_ticks = 0
        node = self.graph.get_node(task_id)
        if node:
            node.was_approval_gated = True
        task_name = node.task_name if node else task_id
        new_queue = self.rescheduler.on_task_approved(task_id, self.graph)
        self._execution_queue = new_queue
        evt = self.monitor.on_task_approved(task_id, task_name)
        if hasattr(self, "_pending_events"):
            self._pending_events.append(evt)
        logger.info(f"[RuntimeEngine] Task {task_id} approved. New queue={self._execution_queue}")

    def deny_task(self, task_id: str) -> None:
        """Called externally when an operator denies a safety-gated task."""
        gate = self._approval_gates.get(task_id)
        if gate:
            gate.set()
        self._approval_wait_ticks = 0
        node = self.graph.get_node(task_id)
        task_name = node.task_name if node else task_id
        if node:
            node.approval_needed = False
            node.status = TaskState.BLOCKED
            node.execution_logs.append("Operator denied task. Marked as BLOCKED.")
        self._execution_queue = self.rescheduler.on_task_failed(task_id, self.graph, can_retry=False)
        self._dispatched.discard(task_id)
        newly_blocked = DependencyManager.get_blocked_by_failure(self.graph)
        for blocked_id in newly_blocked:
            blocked_node = self.graph.get_node(blocked_id)
            if blocked_node and blocked_node.status != TaskState.BLOCKED:
                blocked_node.status = TaskState.BLOCKED
        evt = self.monitor.on_task_blocked(task_id, task_name, "Operator denied task")
        if hasattr(self, "_pending_events"):
            self._pending_events.append(evt)
        logger.info(f"[RuntimeEngine] Task {task_id} denied. Status=BLOCKED, New queue={self._execution_queue}")

    # ── Session State Checks ───────────────────────────────────────────────

    def _is_session_complete(self) -> bool:
        """Returns True ONLY when all planned tasks have successfully COMPLETED."""
        nodes = list(self.graph.nodes.values())
        if not nodes:
            return False
        return all(node.status == TaskState.COMPLETED for node in nodes)

    def _all_terminal(self) -> bool:
        """Returns True when all tasks have reached a terminal state (COMPLETED, FAILED, or BLOCKED)."""
        nodes = list(self.graph.nodes.values())
        if not nodes:
            return False
        return all(node.status in (TaskState.COMPLETED, TaskState.BLOCKED, TaskState.FAILED) for node in nodes)
        
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

        # Phase 2: Save database-backed execution checkpoint
        try:
            from orchestration.checkpoint_manager import ExecutionCheckpointManager
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(
                    ExecutionCheckpointManager.save_checkpoint(
                        self.session_id,
                        self.graph,
                        trigger=f"task_{outcome}",
                        db=getattr(self.monitor, "db", None),
                        user_id=self.user_id,
                    )
                )
        except Exception as e:
            logger.debug(f"[RuntimeEngine] Checkpoint save note: {e}")

    def _is_fallback_graph(self) -> bool:
        """Determines whether the active orchestration graph originated from heuristic fallback planning."""
        if not self.graph:
            return False
        if hasattr(self.graph, "is_fallback_plan") and callable(self.graph.is_fallback_plan):
            if self.graph.is_fallback_plan():
                return True
        if getattr(self.graph, "is_fallback", False):
            return True
        g_meta = getattr(self.graph, "metadata", None)
        if isinstance(g_meta, dict) and g_meta.get("is_fallback"):
            return True
        if getattr(g_meta, "is_fallback", False):
            return True
        return any(
            isinstance(getattr(n, "metadata", None), dict) and bool(n.metadata.get("is_fallback"))
            for n in getattr(self.graph, "nodes", {}).values()
        )

    def _persist_semantic_memory(self) -> None:
        """Phase 8 & Phase 3: Evaluates goal outcome verification, stores semantic memory, and generates candidate playbooks."""
        is_fallback_graph = self._is_fallback_graph()
        try:
            from execution.evidence.evidence_vault import EvidenceVault
            from execution.browser.semantic_verifier import SemanticVerifier
            from knowledge.playbook_service import PlaybookService

            clean_uid = str(self.user_id or getattr(self.graph, "user_id", None) or "").strip()
            evidence_items = (
                EvidenceVault.get_session_evidence(session_id=self.session_id, user_id=clean_uid)
                if clean_uid
                else []
            )
            goal_res = SemanticVerifier.verify_goal_outcome(
                goal_text=self.goal_id or self.session_id,
                graph=self.graph,
                session_id=self.session_id,
                evidence_items=evidence_items,
                user_id=clean_uid if clean_uid else None,
            )
            self._goal_verification = goal_res

            # Phase 3: Invariant - Only VERIFIED non-fallback goals with empirical evidence produce candidate playbooks
            # AUD-S3-03: Candidate playbook synthesis requires explicit authenticated user_id; no "system" fallback
            if clean_uid and goal_res.overall_status == "verified" and not is_fallback_graph and evidence_items and len(evidence_items) > 0:
                try:
                    candidate_pb = PlaybookService.generate_candidate_playbook(
                        user_id=clean_uid,
                        session_id=self.session_id,
                        goal_text=self.goal_id or self.session_id,
                        graph=self.graph,
                        goal_verification=goal_res,
                        evidence_records=evidence_items,
                    )
                    if candidate_pb:
                        logger.info(
                            f"[RuntimeEngine] Synthesized candidate playbook {candidate_pb.get('id')} "
                            f"for verified goal '{self.goal_id or self.session_id}'."
                        )
                except Exception as pb_err:
                    logger.debug(f"[RuntimeEngine] Playbook synthesis note: {pb_err}")

            # Update MongoDB goal record with verified terminal status
            db = getattr(self.monitor, "db", None)
            if db is None:
                db = getattr(self, "db", None)
            if db is not None and self.goal_id:
                tasks = list(self.graph.nodes.values())
                completed_count = sum(1 for n in tasks if n.status == TaskState.COMPLETED)
                pct = (completed_count / len(tasks) * 100.0) if tasks else 0.0

                if goal_res.overall_status == "verified":
                    terminal_status = "completed"
                    progress_pct = 100.0
                elif goal_res.overall_status == "needs_human_review":
                    terminal_status = "needs_human_review"
                    progress_pct = pct
                elif goal_res.overall_status == "outcome_unknown":
                    terminal_status = "outcome_unknown"
                    progress_pct = pct
                else:
                    terminal_status = "failed"
                    progress_pct = pct

                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        loop.create_task(
                            db.goals.update_one(
                                {"public_id": self.goal_id},
                                {"$set": {
                                    "status": terminal_status,
                                    "progress_percentage": progress_pct,
                                    "current_step": "completed" if terminal_status == "completed" else "terminated",
                                    "goal_verification": goal_res.to_dict() if hasattr(goal_res, "to_dict") else None,
                                    "updated_at": datetime.utcnow(),
                                }}
                            )
                        )
                except Exception as db_err:
                    logger.debug(f"[RuntimeEngine] Goal status update task dispatch note: {db_err}")
        except Exception as pe:
            logger.warning(f"[RuntimeEngine] Phase 3 verification and playbook note: {pe}")

        try:
            from knowledge.rag_service import RAGService
            rag = RAGService.get_instance()
            if not rag or not getattr(rag, "memory_store", None):
                return

            tasks = list(self.graph.nodes.values())
            completed_count = sum(1 for n in tasks if n.status == TaskState.COMPLETED)
            failed_count = sum(1 for n in tasks if n.status == TaskState.FAILED)
            blocked_count = sum(1 for n in tasks if n.status == TaskState.BLOCKED)
            outcome_unknown_count = sum(1 for n in tasks if n.status == TaskState.OUTCOME_UNKNOWN)

            # Strict Semantic Memory Invariant: A plan can ONLY be recorded as "success" if
            # ALL tasks completed, zero failures, zero blocks, and goal outcome was verified.
            # Incomplete runs or provider-failure aborts must NEVER record successful memories.
            goal_verif = getattr(self, "_goal_verification", None)
            goal_verif_status = getattr(goal_verif, "overall_status", "") if goal_verif else ""

            if getattr(self, "_is_paused", False):
                outcome_status = "paused"
            elif failed_count > 0 or goal_verif_status in ("failed", "rejected"):
                outcome_status = "failed"
            elif outcome_unknown_count > 0:
                outcome_status = "outcome_unknown"
            elif blocked_count > 0 and completed_count == 0:
                outcome_status = "blocked"
            elif completed_count < len(tasks):
                outcome_status = "incomplete"
            elif not goal_verif or goal_verif_status != "verified":
                outcome_status = "unverified"
            elif is_fallback_graph:
                # Heuristic fallback plans are not considered proven successful plans for memory promotion
                outcome_status = "unverified"
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

            # AUD-S3-02: Forward strictly verified supporting evidence hashes to record_plan_outcome
            supporting_hashes: List[str] = []
            if goal_verif and getattr(goal_verif, "overall_status", "") == "verified":
                for h in (getattr(goal_verif, "supporting_evidence_hashes", []) or []):
                    clean_h = str(h).strip()
                    if len(clean_h) == 64 and all(c in "0123456789abcdefABCDEF" for c in clean_h):
                        if clean_h not in supporting_hashes:
                            supporting_hashes.append(clean_h)
            meta_payload = {
                "evidence_hashes": supporting_hashes,
                "verification_status": goal_verif_status,
                "is_fallback": is_fallback_graph,
            }

            rag.record_plan_outcome(
                goal_text=self.goal_id or self.session_id,
                plan_id=self.goal_id or self.session_id,
                session_id=self.session_id,
                outcome_status=outcome_status,
                task_summary=summary_str,
                execution_metrics=metrics,
                safety_summary={"blocked_count": blocked_count},
                metadata=meta_payload,
                user_id=clean_uid if clean_uid else None,
            )
            logger.info(f"[RuntimeEngine] Recorded session {self.session_id} outcome ({outcome_status}) to Semantic Memory.")
        except Exception as e:
            logger.warning(f"[RuntimeEngine] Semantic memory persistence note: {e}")

    # ── Public State & Lifecycle Access ────────────────────────────────────

    @property
    def is_paused(self) -> bool:
        """Returns True if the session execution loop is paused."""
        return self._is_paused

    async def pause(self, reason: str = "manual", cooldown_seconds: float = 0.0) -> None:
        """
        Safely pauses execution, persists an authoritative checkpoint, and signals loop stop.
        """
        self._is_paused = True
        self._pause_reason = reason
        self._pause_cooldown = cooldown_seconds
        self._pause_time = time.time()
        logger.info(
            f"[RuntimeEngine] Pausing session {self.session_id} (reason: {reason}, "
            f"cooldown: {cooldown_seconds:.1f}s)..."
        )
        evt = self.monitor.on_task_paused(reason=reason)
        if hasattr(self, "_pending_events"):
            self._pending_events.append(evt)
        try:
            from orchestration.checkpoint_manager import ExecutionCheckpointManager
            await ExecutionCheckpointManager.save_checkpoint(
                session_id=self.session_id,
                graph=self.graph,
                trigger=f"pause_{reason}",
                metadata={"cooldown_seconds": cooldown_seconds, "pause_reason": reason},
                db=self.db,
                user_id=self.user_id,
            )
        except Exception as e:
            logger.warning(f"[RuntimeEngine] Checkpoint save during pause note: {e}")

    async def cancel(self, reason: str = "manual") -> None:
        """
        Safely cancels execution, transitions active/pending nodes to CANCELLED,
        and marks loop stopped.
        """
        self._is_cancelled = True
        self._cancel_reason = reason
        logger.info(f"[RuntimeEngine] Cancelling session {self.session_id} (reason: {reason})...")
        for node in self.graph.nodes.values():
            if node.status not in (TaskState.COMPLETED, TaskState.VERIFIED, TaskState.FAILED, TaskState.BLOCKED, TaskState.CANCELLED):
                try:
                    node.status = TaskStateMachine.transition(node.status, TaskState.CANCELLED, node.task_id)
                except Exception:
                    node.status = TaskState.CANCELLED
                if node.metadata is None:
                    node.metadata = {}
                node.metadata["failure_reason"] = f"Cancelled: {reason}"
                evt = self.monitor.on_task_cancelled(node.task_id, node.task_name, reason=reason)
                if hasattr(self, "_pending_events"):
                    self._pending_events.append(evt)
        self.monitor.update_counts_from_graph(self.graph)

    @property
    def is_cancelled(self) -> bool:
        return getattr(self, "_is_cancelled", False)

    def resume(self) -> bool:
        """
        Unpauses the session if cooldown period has elapsed.
        Returns True if unpaused, False if still within cooldown.
        """
        now = time.time()
        if self._pause_cooldown > 0.0:
            elapsed = now - getattr(self, "_pause_time", 0.0)
            if elapsed < self._pause_cooldown:
                logger.info(
                    f"[RuntimeEngine] Resume deferred: cooldown remaining {self._pause_cooldown - elapsed:.1f}s"
                )
                return False
        self._is_paused = False
        self._pause_reason = None
        self._pause_cooldown = 0.0
        logger.info(f"[RuntimeEngine] Session {self.session_id} resumed from pause.")
        return True

    def get_goal_verification(self) -> Optional[Any]:
        """Returns or computes the session goal outcome verification."""
        if not hasattr(self, "_goal_verification") or self._goal_verification is None:
            try:
                from execution.evidence.evidence_vault import EvidenceVault
                from execution.browser.semantic_verifier import SemanticVerifier
                clean_uid = str(self.user_id or getattr(self.graph, "user_id", None) or "").strip()
                evidence_items = (
                    EvidenceVault.get_session_evidence(session_id=self.session_id, user_id=clean_uid)
                    if clean_uid
                    else []
                )
                self._goal_verification = SemanticVerifier.verify_goal_outcome(
                    goal_text=self.goal_id or self.session_id,
                    graph=self.graph,
                    session_id=self.session_id,
                    evidence_items=evidence_items,
                    user_id=clean_uid if clean_uid else None,
                )
            except Exception as e:
                logger.warning(f"[RuntimeEngine] Computing goal verification note: {e}")
                return None
        return self._goal_verification

    def get_current_state(self) -> dict:
        """Serialise current engine state for GET /orchestrate/{id}/state."""
        self.monitor.update_counts_from_graph(self.graph)
        goal_verif = self.get_goal_verification()
        g_meta = getattr(self.graph, "metadata", {}) if isinstance(getattr(self.graph, "metadata", None), dict) else {}
        is_fb = self._is_fallback_graph()
        fb_reason = getattr(self.graph, "fallback_reason", None) or g_meta.get("fallback_reason")
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
            "goal_verification": goal_verif.to_dict() if goal_verif and hasattr(goal_verif, "to_dict") else None,
            "plan_hash": getattr(self.graph, "plan_hash", None),
            "is_fallback": is_fb,
            "fallback_reason": fb_reason,
            "planning_mode": g_meta.get("planning_mode", "deterministic_fallback" if is_fb else "primary_ai"),
            "provider_used": g_meta.get("provider_used"),
            "primary_failure_code": g_meta.get("primary_failure_code"),
            "validation_passed": bool(g_meta.get("validation_passed", True)),
            "limitations": list(g_meta.get("limitations", [])),
        }
