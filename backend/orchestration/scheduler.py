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
    Wraps the RuntimeEngine and manages its async lifecycle.
    """

    def __init__(
        self,
        session_id: str,
        graph: ARDOGraph,
        mode: str,
        db=None,
    ):
        self.session_id = session_id
        self.graph = graph
        browser_adapter = None
        try:
            from execution.browser.browser_task_adapter import BrowserTaskAdapter
            browser_adapter = BrowserTaskAdapter()
        except Exception as e:
            logger.debug(f"[ARDOSession] Browser adapter init: {e}")
        self.engine = RuntimeEngine(graph, session_id, mode=mode, db=db, browser_adapter=browser_adapter)
        self.event_buffer: List[OrchestrationEvent] = []
        self._task: Optional[asyncio.Task] = None
        self._subscribers: List[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        """Create a new event subscriber queue (one per WebSocket connection)."""
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.remove(q)

    async def _broadcast(self, event: OrchestrationEvent) -> None:
        """Push event to all subscribers and the buffer."""
        self.event_buffer.append(event)
        for q in self._subscribers:
            await q.put(event)

    async def start(self) -> None:
        """Launch the RuntimeEngine in a background asyncio task."""
        async def _run():
            async for event in self.engine.run():
                await self._broadcast(event)
        self._task = asyncio.create_task(_run())
        logger.info(f"[ARDOSession] Session {self.session_id} started.")

    def approve_task(self, task_id: str) -> None:
        self.engine.approve_task(task_id)

    def get_state(self) -> dict:
        return self.engine.get_current_state()

    def get_evidence(self) -> dict:
        """Collect browser and execution evidence from graph nodes."""
        evidence_items = []
        for task_id, node in self.graph.nodes.items():
            if getattr(node, "browser_action", None) or getattr(node, "browser_result", None):
                action_data = node.browser_action.model_dump() if node.browser_action else {}
                result_data = node.browser_result.model_dump() if node.browser_result else {}
                if "metadata" in result_data and isinstance(result_data["metadata"], dict):
                    if "screenshot_base64" in result_data["metadata"]:
                        result_data["screenshot_base64"] = result_data["metadata"]["screenshot_base64"]
                # Mask sensitive fields
                if "value" in action_data and any(k in str(action_data.get("selector", "")).lower() for k in ("password", "secret", "token", "key")):
                    action_data["value"] = "********"
                evidence_items.append({
                    "task_id": task_id,
                    "task_name": node.task_name,
                    "status": node.status.value,
                    "action": action_data,
                    "result": result_data,
                })
        return {
            "session_id": self.session_id,
            "evidence_count": len(evidence_items),
            "evidence_items": evidence_items,
        }

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
        # Build and validate the DAG — pass safety_decisions for Phase 5 integration
        graph = DAGBuilder.build(
            nodes_data=plan_nodes,
            edges_data=plan_edges,
            execution_mode=mode,
            safety_decisions=safety_decisions,
        )
        graph.session_id = session_id

        session = ARDOSession(session_id=session_id, graph=graph, mode=mode, db=db)
        cls._sessions[session_id] = session

        await session.start()
        logger.info(
            f"[ARDOScheduler] Session {session_id} launched. "
            f"Nodes={len(graph.nodes)}, Mode={mode}"
        )
        return session

    @classmethod
    def get_session(cls, session_id: str) -> Optional[ARDOSession]:
        return cls._sessions.get(session_id)

    @classmethod
    def get_state(cls, session_id: str) -> Optional[dict]:
        session = cls.get_session(session_id)
        return session.get_state() if session else None

    @classmethod
    def approve_task(cls, session_id: str, task_id: str) -> bool:
        session = cls.get_session(session_id)
        if not session:
            return False
        session.approve_task(task_id)
        return True

    @classmethod
    def list_active_sessions(cls) -> List[str]:
        return list(cls._sessions.keys())

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
        }
        for i, h in enumerate(cls._session_history):
            if h["session_id"] == session_id:
                cls._session_history[i].update(record)
                return
        cls._session_history.insert(0, record)
        if len(cls._session_history) > 100:
            cls._session_history.pop()

    @classmethod
    def get_history(cls) -> List[dict]:
        """Return history of execution sessions with enriched current states."""
        history = []
        for item in cls._session_history:
            session = cls.get_session(item["session_id"])
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
    def get_evidence(cls, session_id: str) -> Optional[dict]:
        """Retrieve browser and execution evidence for a session."""
        session = cls.get_session(session_id)
        if not session:
            return None
        return session.get_evidence()
