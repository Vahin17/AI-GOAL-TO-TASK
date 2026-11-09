"""
scheduler.py — ARDO Public Session Entry Point
===============================================
ARDOScheduler is the single public interface to the ARDO system.
All backend routes and the AI orchestrator call ARDOScheduler methods.

Responsibilities:
    - Build the DAG from a plan response
    - Start and manage async RuntimeEngine sessions
    - Store sessions keyed by session_id
    - Provide state access for REST endpoints
    - Handle WebSocket event streaming
    - Manage safety gate approvals
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncGenerator, Dict, List, Optional, TYPE_CHECKING

from orchestration.dag_builder import DAGBuilder, ARDOGraph
from orchestration.runtime_engine import RuntimeEngine
from orchestration.execution_monitor import OrchestrationEvent

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class ARDOSession:
    """
    Represents a single active ARDO orchestration session.
    Wraps the RuntimeEngine, owns session-scoped browser resources,
    and manages its async lifecycle and event distribution.
    """

    def __init__(
        self,
        session_id: str,
        graph: ARDOGraph,
        mode: str,
        db=None,
        goal_id: Optional[str] = None,
        browser_adapter: Optional[Any] = None,
        tool_registry: Optional[Any] = None,
        user_id: Optional[str] = None,
        worker_id: Optional[str] = None,
    ):
        self.session_id = session_id
        self.graph = graph
        self.goal_id = goal_id
        self.user_id = user_id
        self.worker_id = worker_id
        self._is_cleaned_up: bool = False

        # 1. Create dedicated session-scoped ToolRegistry (isolated from other sessions)
        from execution.registry.tool_registry import ToolRegistry
        global_registry = ToolRegistry.get_instance()
        self.tool_registry = tool_registry or global_registry.create_session_registry()

        # 2. Session owns its dedicated browser adapter and tool
        if browser_adapter is None:
            try:
                from execution.browser.browser_task_adapter import BrowserTaskAdapter
                from execution.browser.playwright_executor import PlaywrightBrowserExecutor
                from execution.browser.browser_tool import BrowserTool

                session_executor = PlaywrightBrowserExecutor()
                self.browser_adapter = BrowserTaskAdapter(executor=session_executor)
                session_browser_tool = BrowserTool(executor=session_executor, adapter=self.browser_adapter)
                self.tool_registry.register(session_browser_tool)
            except Exception as e:
                logger.warning(f"[ARDOSession] Browser adapter init error for session {session_id}: {e}")
                self.browser_adapter = None
        else:
            self.browser_adapter = browser_adapter
            try:
                from execution.browser.browser_tool import BrowserTool
                existing_tool = self.tool_registry.get_tool("browser")
                if not existing_tool or getattr(existing_tool, "adapter", None) != browser_adapter:
                    session_browser_tool = BrowserTool(
                        executor=getattr(browser_adapter, "executor", None),
                        adapter=browser_adapter,
                    )
                    self.tool_registry.register(session_browser_tool)
            except Exception as e:
                logger.warning(f"[ARDOSession] Browser tool registration error for session {session_id}: {e}")

        # 3. Instantiate RuntimeEngine with session-scoped resources
        self.engine = RuntimeEngine(
            graph,
            session_id,
            mode=mode,
            db=db,
            goal_id=goal_id,
            browser_adapter=self.browser_adapter,
            tool_registry=self.tool_registry,
            user_id=user_id,
            worker_id=worker_id,
        )
        self.event_buffer: List[OrchestrationEvent] = []
        self._task: Optional[asyncio.Task] = None
        self._subscribers: List[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        """Create a new event subscriber queue (one per WebSocket connection)."""
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    async def _broadcast(self, event: OrchestrationEvent) -> None:
        """Push event to all subscribers and the buffer."""
        self.event_buffer.append(event)
        for q in list(self._subscribers):
            await q.put(event)

    async def cleanup(self) -> None:
        """
        Idempotent cleanup of session-scoped browser resources and execution tasks.
        Guarantees that closing this session will not affect any other session's browser state.
        """
        if self._is_cleaned_up:
            return
        self._is_cleaned_up = True
        logger.info(f"[ARDOSession] Cleaning up session {self.session_id} resources.")
        if hasattr(self, "engine") and self.engine is not None:
            await self.engine.cleanup()
        elif self.browser_adapter and hasattr(self.browser_adapter, "executor") and self.browser_adapter.executor:
            try:
                if getattr(settings, "BROWSER_INSPECTION_MODE", False) and not settings.is_production():
                    timeout_sec = getattr(settings, "BROWSER_INSPECTION_TIMEOUT_SECONDS", 120)
                    logger.info(f"[ARDOSession] BROWSER_INSPECTION_MODE: preserving browser for {timeout_sec}s.")
                    await asyncio.sleep(timeout_sec)
                if getattr(self.browser_adapter.executor, "is_running", False):
                    await self.browser_adapter.executor.stop()
            except Exception as e:
                logger.warning(f"[ARDOSession] Browser executor cleanup error for session {self.session_id}: {e}")

    async def start(self) -> None:
        """Launch the RuntimeEngine in a background asyncio task with lifecycle cleanup."""
        if self._task is not None and not self._task.done():
            logger.warning(f"[RUN-GUARD] ARDOSession.start ALREADY ACTIVE for session {self.session_id}.")
            return
        if getattr(self.engine, "_is_completed", False):
            logger.warning(f"[RUN-GUARD] ARDOSession.start session {self.session_id} ALREADY COMPLETED.")
            return

        async def _run():
            try:
                async for event in self.engine.run():
                    await self._broadcast(event)
            finally:
                await self.cleanup()

        self._task = asyncio.create_task(_run())
        logger.info(f"[ARDOSession] Session {self.session_id} started.")

    def approve_task(self, task_id: str) -> None:
        self.engine.approve_task(task_id)

    def deny_task(self, task_id: str) -> None:
        self.engine.deny_task(task_id)

    def get_state(self) -> dict:
        return self.engine.get_current_state()

    def get_evidence(self) -> dict:
        """Collect browser and execution evidence from EvidenceVault and graph nodes."""
        from execution.evidence.evidence_vault import EvidenceVault
        vault_records = EvidenceVault.get_session_evidence(self.session_id, self.user_id or "system")
        vault_by_task = {r.get("task_id"): r for r in vault_records}

        evidence_items = []
        for task_id, node in self.graph.nodes.items():
            if getattr(node, "browser_action", None) or getattr(node, "browser_result", None) or task_id in vault_by_task:
                if node.browser_action:
                    if hasattr(node.browser_action, "model_dump"):
                        action_data = node.browser_action.model_dump()
                    elif hasattr(node.browser_action, "to_dict"):
                        action_data = node.browser_action.to_dict()
                    elif isinstance(node.browser_action, dict):
                        action_data = dict(node.browser_action)
                    else:
                        action_data = {}
                else:
                    action_data = {}

                if node.browser_result:
                    if hasattr(node.browser_result, "model_dump"):
                        result_data = node.browser_result.model_dump()
                    elif hasattr(node.browser_result, "to_dict"):
                        result_data = node.browser_result.to_dict()
                    elif isinstance(node.browser_result, dict):
                        result_data = dict(node.browser_result)
                    else:
                        result_data = {}
                else:
                    result_data = {}

                if "metadata" in result_data and isinstance(result_data["metadata"], dict):
                    if "screenshot_base64" in result_data["metadata"]:
                        result_data["screenshot_base64"] = result_data["metadata"]["screenshot_base64"]
                if "artifacts" in result_data and isinstance(result_data["artifacts"], dict):
                    screenshot_meta = result_data["artifacts"].get("screenshot", {})
                    if isinstance(screenshot_meta, dict) and "screenshot_base64" in screenshot_meta:
                        result_data["screenshot_base64"] = screenshot_meta["screenshot_base64"]
                # Mask sensitive fields
                if "value" in action_data and any(k in str(action_data.get("selector", "")).lower() for k in ("password", "secret", "token", "key")):
                    action_data["value"] = "********"

                vr = vault_by_task.get(task_id, {})
                evidence_items.append({
                    "id": vr.get("id"),
                    "task_id": task_id,
                    "task_name": node.task_name,
                    "status": vr.get("verification_status") or node.status.value,
                    "verification_status": vr.get("verification_status") or ("verified" if node.status.value == "completed" else node.status.value),
                    "artifact_hash": vr.get("artifact_hash") or (node.metadata.get("artifact_hash") if node.metadata else None),
                    "confidence_score": vr.get("confidence_score") or (node.metadata.get("confidence_score") if node.metadata else 1.0),
                    "captured_at": vr.get("captured_at"),
                    "action": action_data,
                    "result": result_data,
                    "metadata": vr.get("metadata", {}),
                    "content_payload": vr.get("content_payload"),
                })

        # Also append any vault records that might not have a direct node in memory
        recorded_tasks = {e["task_id"] for e in evidence_items}
        for vr in vault_records:
            tid = vr.get("task_id")
            if tid not in recorded_tasks:
                evidence_items.append({
                    "id": vr.get("id"),
                    "task_id": tid,
                    "task_name": vr.get("task_name", tid),
                    "status": vr.get("verification_status", "verified"),
                    "verification_status": vr.get("verification_status", "verified"),
                    "artifact_hash": vr.get("artifact_hash"),
                    "confidence_score": vr.get("confidence_score", 1.0),
                    "captured_at": vr.get("captured_at"),
                    "action": {},
                    "result": {},
                    "metadata": vr.get("metadata", {}),
                    "content_payload": vr.get("content_payload"),
                })

        return {
            "session_id": self.session_id,
            "evidence_count": len(evidence_items),
            "evidence_items": evidence_items,
        }

    def get_goal_verification(self) -> Optional[dict]:
        verif = self.engine.get_goal_verification()
        return verif.to_dict() if verif and hasattr(verif, "to_dict") else verif

    def get_explanation(self, task_id: str) -> Optional[dict]:
        return self.engine.explainability.get_latest_explanation(task_id)

    def get_all_explanations_for_task(self, task_id: str) -> List[dict]:
        return self.engine.explainability.get_task_explanations(task_id)

    def get_why_now(self) -> Optional[dict]:
        exp = self.engine.explainability.get_active_why_now()
        return exp.to_dict() if exp else None

    def get_reschedule_log(self) -> List[dict]:
        return self.engine.explainability.get_reschedule_log()

    def get_stability_report(self) -> dict:
        return self.engine.stability_guard.get_system_health(self.graph)

    def get_weight_suggestion(self) -> Optional[dict]:
        return self.engine.weight_optimizer.get_latest_suggestion()

    def get_efficiency_stats(self) -> dict:
        return self.engine.weight_optimizer.get_efficiency_stats()


class ARDOScheduler:
    """
    Global session manager for all active ARDO orchestration sessions.
    Thread-safe for concurrent sessions.
    """

    # In-memory session registry: session_id → ARDOSession
    _sessions: Dict[str, ARDOSession] = {}
    _session_history: List[dict] = []

    @classmethod
    async def start(
        cls,
        plan_nodes: List[dict],
        plan_edges: List[dict],
        goal_id: str,
        session_id: str,
        mode: str = "safe",
        db=None,
        safety_decisions: Optional[dict] = None,
        user_id: Optional[str] = None,
    ) -> ARDOSession:
        """
        Build a DAG from plan data and launch an ARDO orchestration session.

        Args:
            plan_nodes:       List of TaskNode dicts from the LLM planner.
            plan_edges:       List of TaskEdge dicts from the LLM planner.
            goal_id:          Associated goal public_id.
            session_id:       Unique session identifier (ardo_{hex}).
            mode:             Execution mode (safe | sequential | parallel | aggressive).
            db:               Motor database instance for persistence.
            safety_decisions: Optional {task_id: SafetyDecision} from SafetyEngine.
                              When provided, replaces keyword-based heuristic risk scoring.

        Returns:
            ARDOSession instance (already started).
        """
        logger.info(f"[RUN-GUARD] scheduler.start called session={session_id}")
        if session_id in cls._sessions:
            logger.warning(f"[RUN-GUARD] Session {session_id} already exists in scheduler. Re-using existing.")
            return cls._sessions[session_id]

        # Build and validate the DAG — pass safety_decisions for Phase 5 integration
        graph = DAGBuilder.build(
            nodes_data=plan_nodes,
            edges_data=plan_edges,
            execution_mode=mode,
            safety_decisions=safety_decisions,
            goal_text=goal_id or "",
        )
        graph.session_id = session_id

        session = ARDOSession(session_id=session_id, graph=graph, mode=mode, db=db, goal_id=goal_id, user_id=user_id)
        cls._sessions[session_id] = session

        await session.start()
        logger.info(
            f"[ARDOScheduler] Session {session_id} launched (user={user_id}). "
            f"Nodes={len(graph.nodes)}, Mode={mode}"
        )
        return session

    @classmethod
    async def resume(
        cls,
        session_id: str,
        user_id: Optional[str] = None,
        db=None,
        worker_id: Optional[str] = None,
    ) -> ARDOSession:
        """
        Resumes an interrupted ARDO session from its authoritative database checkpoint.
        """
        from orchestration.checkpoint_manager import ExecutionCheckpointManager

        # 1. Check if session is already actively running
        existing = cls._sessions.get(session_id)
        if existing and getattr(existing.engine, "_is_running", False):
            raise RuntimeError(f"Session {session_id} is already actively running.")

        # 2. Load latest checkpoint
        checkpoint = await ExecutionCheckpointManager.load_latest_checkpoint(session_id, db=db)
        if not checkpoint:
            raise ValueError(f"No execution checkpoint found for session {session_id}")

        # 3. Reconstruct or retrieve graph
        if existing and hasattr(existing, "graph"):
            graph = existing.graph
        else:
            nodes_snapshot = checkpoint.get("nodes_snapshot") or checkpoint.get("nodes") or []
            plan_nodes = [
                {
                    "id": n["task_id"],
                    "data": {"label": n["task_name"], "tool_call": n.get("tool_call")},
                    "type": n.get("raw_type", "default"),
                    "browser_action": n.get("browser_action"),
                    "metadata": n.get("metadata", {}),
                }
                for n in nodes_snapshot
            ]
            plan_edges = []
            for n in nodes_snapshot:
                for dep in n.get("dependency_ids", []):
                    plan_edges.append({"source": dep, "target": n["task_id"]})

            graph = DAGBuilder.build(
                nodes_data=plan_nodes,
                edges_data=plan_edges,
                goal_text=checkpoint.get("session_id", ""),
            )
            graph.session_id = session_id

        # 4. Apply checkpoint statuses
        graph, summary = ExecutionCheckpointManager.restore_graph_from_checkpoint(graph, checkpoint)

        # 4b. Reconcile any uncertain (OUTCOME_UNKNOWN) tasks via semantic verification or operator gating
        await ExecutionCheckpointManager.reconcile_uncertain_tasks(
            graph,
            browser_adapter=getattr(existing, "browser_adapter", None) if existing else None,
            db=db,
        )

        # 5. Build resumed session
        session = ARDOSession(
            session_id=session_id,
            graph=graph,
            mode=checkpoint.get("mode", "safe"),
            db=db,
            goal_id=session_id,
            user_id=user_id,
            worker_id=worker_id,
        )
        cls._sessions[session_id] = session

        # 6. Start resumed execution
        await session.start()
        logger.info(f"[ARDOScheduler] Resumed session {session_id} from checkpoint: {summary}")
        return session

    @classmethod
    def get_session(cls, session_id: str, user_id: Optional[str] = None) -> Optional[ARDOSession]:
        session = cls._sessions.get(session_id)
        if not session:
            return None
        # Enforce multi-user data isolation
        if user_id is not None and session.user_id is not None and session.user_id != user_id:
            logger.warning(f"[Security] User {user_id} attempted unauthorized access to session {session_id} owned by {session.user_id}")
            return None
        return session

    @classmethod
    def get_state(cls, session_id: str, user_id: Optional[str] = None) -> Optional[dict]:
        session = cls.get_session(session_id, user_id=user_id)
        return session.get_state() if session else None

    @classmethod
    def approve_task(cls, session_id: str, task_id: str, user_id: Optional[str] = None) -> bool:
        session = cls.get_session(session_id, user_id=user_id)
        if not session:
            return False
        session.approve_task(task_id)
        return True

    @classmethod
    def list_active_sessions(cls, user_id: Optional[str] = None) -> List[str]:
        if user_id is not None:
            return [sid for sid, s in cls._sessions.items() if s.user_id == user_id]
        return list(cls._sessions.keys())

    @classmethod
    async def stop_session(cls, session_id: str, user_id: Optional[str] = None) -> bool:
        """
        Stop and clean up a specific session and its browser resources.
        Guarantees other sessions remain unaffected.
        """
        session = cls.get_session(session_id, user_id=user_id)
        if not session:
            return False
        cls._sessions.pop(session_id, None)
        if session._task is not None and not session._task.done():
            session._task.cancel()
        await session.cleanup()
        return True

    @classmethod
    async def cleanup_session(cls, session_id: str) -> bool:
        """Alias for stop_session for explicit lifecycle management."""
        return await cls.stop_session(session_id)

    @classmethod
    async def cleanup_abandoned_sessions(cls, max_idle_minutes: int = 60) -> int:
        """
        Sweeps inactive sessions that have concluded or been idle beyond max_idle_minutes,
        ensuring all browser resources, contexts, and leases are cleanly released.
        """
        now = time.time()
        max_idle_seconds = max_idle_minutes * 60
        abandoned_ids = []

        for sid, sess in list(cls._sessions.items()):
            is_done = sess._task is None or sess._task.done()
            engine_completed = getattr(sess.engine, "_is_completed", False)
            start_time = getattr(sess.engine, "_start_time", 0.0) or now
            if (is_done or engine_completed) and (now - start_time > max_idle_seconds):
                abandoned_ids.append(sid)

        for sid in abandoned_ids:
            logger.info(f"[ARDOScheduler] Evicting abandoned/expired session: {sid}")
            await cls.stop_session(sid)

        return len(abandoned_ids)

    @classmethod
    async def stream_events(
        cls, session_id: str
    ) -> AsyncGenerator[dict, None]:
        """
        WebSocket event generator for a session.
        Yields events as JSON-serialisable dicts.
        """
        session = cls.get_session(session_id)
        if not session:
            yield {"error": f"Session {session_id} not found"}
            return

        q = session.subscribe()
        try:
            # First, replay buffered events for late subscribers
            for buffered_event in session.event_buffer:
                yield buffered_event.to_dict()

            # Then stream live events
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield event.to_dict()
                    if event.event_type in ("session_complete", "session_failed"):
                        break
                except asyncio.TimeoutError:
                    # Heartbeat to keep WebSocket alive
                    yield {"event_type": "heartbeat", "session_id": session_id}
        finally:
            session.unsubscribe(q)

    @classmethod
    def record_history(
        cls,
        session_id: str,
        goal: str,
        mode: str,
        task_count: int,
        status: str = "active",
        duration_seconds: float = 0.0,
        user_id: Optional[str] = None,
    ) -> None:
        """Record or update a session in the execution history registry."""
        record = {
            "session_id": session_id,
            "goal": goal,
            "mode": mode,
            "task_count": task_count,
            "status": status,
            "duration_seconds": duration_seconds,
            "timestamp": time.time(),
            "user_id": user_id,
        }
        for i, h in enumerate(cls._session_history):
            if h["session_id"] == session_id:
                cls._session_history[i].update(record)
                return
        cls._session_history.insert(0, record)
        if len(cls._session_history) > 100:
            cls._session_history.pop()

    @classmethod
    def get_history(cls, user_id: Optional[str] = None) -> List[dict]:
        """Return history of execution sessions with enriched current states, scoped by user_id."""
        history = []
        for item in cls._session_history:
            # Multi-user isolation filter: if user_id is provided, only return user's records
            if user_id is not None and item.get("user_id") is not None and item.get("user_id") != user_id:
                continue
            session = cls.get_session(item["session_id"], user_id=user_id)
            if session:
                metrics = session.engine.monitor.get_metrics()
                item_copy = dict(item)
                item_copy["duration_seconds"] = round(metrics.get("elapsed_seconds", 0.0), 2)
                item_copy["tasks_completed"] = metrics.get("tasks_completed", 0)
                item_copy["tasks_failed"] = metrics.get("tasks_failed", 0)
                item_copy["tasks_blocked"] = metrics.get("tasks_blocked", 0)
                if metrics.get("tasks_completed", 0) == item.get("task_count", 0) and item.get("task_count", 0) > 0:
                    item_copy["status"] = "completed"
                elif metrics.get("tasks_blocked", 0) > 0:
                    item_copy["status"] = "blocked"
                elif metrics.get("tasks_failed", 0) > 0:
                    item_copy["status"] = "failed"
                history.append(item_copy)
            else:
                history.append(item)
        return history

    @classmethod
    def get_evidence(cls, session_id: str, user_id: Optional[str] = None) -> Optional[dict]:
        """Retrieve browser and execution evidence for a session with ownership validation."""
        session = cls.get_session(session_id, user_id=user_id)
        if session:
            return session.get_evidence()

        # Fallback to persistent EvidenceVault if in-memory session is already closed
        if user_id:
            from execution.evidence.evidence_vault import EvidenceVault
            vault_records = EvidenceVault.get_session_evidence(session_id, user_id)
            if vault_records:
                return {
                    "session_id": session_id,
                    "evidence_count": len(vault_records),
                    "evidence_items": vault_records,
                }
        return None

    @classmethod
    def get_goal_verification(cls, session_id: str, user_id: Optional[str] = None) -> Optional[dict]:
        """Retrieve goal outcome verification for a session with ownership validation."""
        session = cls.get_session(session_id, user_id=user_id)
        if not session:
            return None
        return session.get_goal_verification()
