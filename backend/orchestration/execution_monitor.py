"""
execution_monitor.py — ARDO Execution Telemetry & Persistence
=============================================================
Records all execution events, computes live metrics, and persists
orchestration state to MongoDB for analysis and audit.

Collections used:
    orchestration_logs    — per-event log entries
    execution_metrics     — session-level throughput / latency statistics
    task_runtime_state    — point-in-time graph state snapshots
    scheduler_snapshots   — reasoning snapshots at transitions
    replay_sessions       — metadata for deterministic replays
    replay_events         — strict stream of events for replay
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from orchestration.dag_builder import ARDOGraph

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Event Model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OrchestrationEvent:
    """
    A single runtime event emitted during ARDO execution.
    These events are pushed to the frontend via WebSocket.
    """
    event_type: str          # task_started | task_completed | task_failed |
                             # task_retrying | task_blocked | task_approved |
                             # queue_reordered | session_complete | session_failed
    session_id: str
    task_id: Optional[str]
    task_name: Optional[str]
    status: Optional[str]
    details: Dict[str, Any] = field(default_factory=dict)
    # High-resolution UTC timestamp for deterministic ordering
    timestamp: float = field(default_factory=lambda: time.time_ns() / 1e9)

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "task_name": self.task_name,
            "status": self.status,
            "details": self.details,
            "timestamp": self.timestamp,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Session Metrics
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SessionMetrics:
    """Running metrics for an ARDO execution session."""
    session_id: str
    started_at: float = field(default_factory=lambda: time.time_ns() / 1e9)
    completed_at: Optional[float] = None

    tasks_total: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0
    tasks_blocked: int = 0
    tasks_retried: int = 0

    total_retry_count: int = 0
    reschedule_count: int = 0
    approval_count: int = 0

    # Latency tracking (seconds)
    task_latencies: List[float] = field(default_factory=list)

    # Timeline markers for frontend visualization
    timeline_markers: List[dict] = field(default_factory=list)

    # Advanced Research Metrics
    scheduler_efficiency_score: float = 1.0
    parallelization_utilization_ratio: float = 0.0
    average_dependency_wait_duration: float = 0.0
    starvation_prevention_success_rate: float = 1.0
    orchestration_stability_score: float = 1.0

    @property
    def elapsed_seconds(self) -> float:
        end = self.completed_at or time.time()
        return end - self.started_at

    @property
    def throughput_per_minute(self) -> float:
        elapsed = self.elapsed_seconds
        if elapsed <= 0:
            return 0.0
        return round((self.tasks_completed / elapsed) * 60, 2)

    @property
    def avg_latency_seconds(self) -> float:
        if not self.task_latencies:
            return 0.0
        return round(sum(self.task_latencies) / len(self.task_latencies), 2)

    @property
    def success_rate(self) -> float:
        total = self.tasks_total if self.tasks_total > 0 else (self.tasks_completed + self.tasks_failed)
        if total == 0:
            return 0.0
        return round(self.tasks_completed / total, 3)

    def add_timeline_marker(
        self, marker_type: str, task_id: str, task_name: str, **extra
    ) -> None:
        """Add a timeline marker for the execution timeline visualization."""
        self.timeline_markers.append({
            "type": marker_type,     # start | complete | fail | retry | reschedule | approve
            "task_id": task_id,
            "task_name": task_name,
            "timestamp": time.time_ns() / 1e9,
            **extra,
        })

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_seconds": self.elapsed_seconds,
            "tasks_total": self.tasks_total,
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "tasks_blocked": self.tasks_blocked,
            "tasks_retried": self.tasks_retried,
            "total_retry_count": self.total_retry_count,
            "reschedule_count": self.reschedule_count,
            "approval_count": self.approval_count,
            "throughput_per_minute": self.throughput_per_minute,
            "avg_latency_seconds": self.avg_latency_seconds,
            "success_rate": self.success_rate,
            "scheduler_efficiency_score": self.scheduler_efficiency_score,
            "parallelization_utilization_ratio": self.parallelization_utilization_ratio,
            "average_dependency_wait_duration": self.average_dependency_wait_duration,
            "starvation_prevention_success_rate": self.starvation_prevention_success_rate,
            "orchestration_stability_score": self.orchestration_stability_score,
            "timeline_markers": self.timeline_markers[-50:],  # Cap for API response
        }


# ─────────────────────────────────────────────────────────────────────────────
def redact_sensitive_details(details: Dict[str, Any]) -> Dict[str, Any]:
    """Redacts passwords, tokens, API keys, secrets, and auth credentials from event details."""
    if not isinstance(details, dict):
        return details
    redacted = {}
    SENSITIVE_KEYS = {"password", "passwd", "secret", "token", "key", "credential", "auth", "authorization", "cookie"}
    for k, v in details.items():
        k_str = str(k).lower()
        if any(s in k_str for s in SENSITIVE_KEYS) and isinstance(v, str):
            redacted[k] = "[REDACTED]"
        elif isinstance(v, dict):
            redacted[k] = redact_sensitive_details(v)
        elif isinstance(v, list):
            redacted[k] = [redact_sensitive_details(item) if isinstance(item, dict) else item for item in v]
        else:
            redacted[k] = v
    return redacted


# ─────────────────────────────────────────────────────────────────────────────
# Execution Monitor
# ─────────────────────────────────────────────────────────────────────────────

class ExecutionMonitor:
    """
    Central telemetry hub for an ARDO session.

    Responsibilities:
        - Emit and store OrchestrationEvents
        - Maintain live SessionMetrics
        - Persist to MongoDB (orchestration_logs, execution_metrics, task_runtime_state)
        - Expose data for the /metrics API endpoint and WebSocket push
    """

    def __init__(self, session_id: str, db=None):
        self.session_id = session_id
        self.db = db
        self.metrics = SessionMetrics(session_id=session_id)
        self._event_log: List[OrchestrationEvent] = []
        self._task_start_times: Dict[str, float] = {}
        
        # Track waiting start times for wait duration metric
        self._task_wait_start_times: Dict[str, float] = {}

    # ── Event recording ────────────────────────────────────────────────────

    def emit(self, event: OrchestrationEvent) -> OrchestrationEvent:
        """Record and optionally persist an event. Returns the event."""
        if event.details:
            event.details = redact_sensitive_details(event.details)
        self._event_log.append(event)
        self._update_metrics_from_event(event)
        logger.info(
            f"[Monitor] [{event.event_type.upper()}] "
            f"task={event.task_id} session={self.session_id}"
        )
        return event

    def on_task_started(self, task_id: str, task_name: str) -> OrchestrationEvent:
        now = time.time_ns() / 1e9
        self._task_start_times[task_id] = now
        
        # Update dependency wait duration metric
        if task_id in self._task_wait_start_times:
            wait_duration = now - self._task_wait_start_times.pop(task_id)
            # Rolling average
            current_avg = self.metrics.average_dependency_wait_duration
            completed = self.metrics.tasks_completed
            if completed == 0:
                self.metrics.average_dependency_wait_duration = wait_duration
            else:
                self.metrics.average_dependency_wait_duration = (
                    (current_avg * completed) + wait_duration
                ) / (completed + 1)
                
        self.metrics.add_timeline_marker("start", task_id, task_name)
        return self.emit(OrchestrationEvent(
            event_type="task_started",
            session_id=self.session_id,
            task_id=task_id,
            task_name=task_name,
            status="active",
        ))

    def on_task_completed(self, task_id: str, task_name: str) -> OrchestrationEvent:
        now = time.time_ns() / 1e9
        latency = now - self._task_start_times.pop(task_id, now)
        self.metrics.task_latencies.append(latency)
        self.metrics.tasks_completed += 1
        self.metrics.add_timeline_marker("complete", task_id, task_name, latency=latency)
        return self.emit(OrchestrationEvent(
            event_type="task_completed",
            session_id=self.session_id,
            task_id=task_id,
            task_name=task_name,
            status="completed",
            details={"latency_seconds": latency},
        ))

    def on_task_failed(self, task_id: str, task_name: str, reason: str) -> OrchestrationEvent:
        self.metrics.tasks_failed += 1
        self.metrics.add_timeline_marker("fail", task_id, task_name, reason=reason)
        return self.emit(OrchestrationEvent(
            event_type="task_failed",
            session_id=self.session_id,
            task_id=task_id,
            task_name=task_name,
            status="failed",
            details={"reason": reason},
        ))

    def on_task_retrying(
        self, task_id: str, task_name: str, retry_count: int, delay: float
    ) -> OrchestrationEvent:
        self.metrics.tasks_retried += 1
        self.metrics.total_retry_count += 1
        self.metrics.add_timeline_marker(
            "retry", task_id, task_name, attempt=retry_count, delay=delay
        )
        return self.emit(OrchestrationEvent(
            event_type="task_retrying",
            session_id=self.session_id,
            task_id=task_id,
            task_name=task_name,
            status="retrying",
            details={"retry_count": retry_count, "delay_seconds": delay},
        ))

    def on_task_blocked(self, task_id: str, task_name: str, cause: str) -> OrchestrationEvent:
        self.metrics.tasks_blocked += 1
        return self.emit(OrchestrationEvent(
            event_type="task_blocked",
            session_id=self.session_id,
            task_id=task_id,
            task_name=task_name,
            status="blocked",
            details={"cause": cause},
        ))

    def on_task_approved(self, task_id: str, task_name: str) -> OrchestrationEvent:
        self.metrics.approval_count += 1
        self.metrics.add_timeline_marker("approve", task_id, task_name)
        return self.emit(OrchestrationEvent(
            event_type="task_approved",
            session_id=self.session_id,
            task_id=task_id,
            task_name=task_name,
            status="approved",
        ))

    def on_queue_reordered(
        self, trigger_task_id: str, reason: str, new_queue: List[str]
    ) -> OrchestrationEvent:
        self.metrics.reschedule_count += 1
        self.metrics.add_timeline_marker(
            "reschedule", trigger_task_id, "", reason=reason
        )
        return self.emit(OrchestrationEvent(
            event_type="queue_reordered",
            session_id=self.session_id,
            task_id=trigger_task_id,
            task_name=None,
            status=None,
            details={"reason": reason, "new_queue_preview": new_queue[:5]},
        ))

    def on_observation_received(
        self, task_id: str, task_name: str, observation_summary: Dict[str, Any]
    ) -> OrchestrationEvent:
        return self.emit(OrchestrationEvent(
            event_type="observation_received",
            session_id=self.session_id,
            task_id=task_id,
            task_name=task_name,
            status="observing",
            details=observation_summary,
        ))

    def on_decision_made(
        self, task_id: str, task_name: str, decision_data: Dict[str, Any]
    ) -> OrchestrationEvent:
        return self.emit(OrchestrationEvent(
            event_type="decision_made",
            session_id=self.session_id,
            task_id=task_id,
            task_name=task_name,
            status="decided",
            details=decision_data,
        ))

    def on_action_started(
        self, task_id: str, task_name: str, action_data: Dict[str, Any]
    ) -> OrchestrationEvent:
        return self.emit(OrchestrationEvent(
            event_type="action_started",
            session_id=self.session_id,
            task_id=task_id,
            task_name=task_name,
            status="acting",
            details=action_data,
        ))

    def on_action_completed(
        self, task_id: str, task_name: str, action_data: Dict[str, Any]
    ) -> OrchestrationEvent:
        return self.emit(OrchestrationEvent(
            event_type="action_completed",
            session_id=self.session_id,
            task_id=task_id,
            task_name=task_name,
            status="action_completed",
            details=action_data,
        ))

    def on_action_failed(
        self, task_id: str, task_name: str, action_data: Dict[str, Any], reason: str
    ) -> OrchestrationEvent:
        details = dict(action_data)
        details["error"] = reason
        return self.emit(OrchestrationEvent(
            event_type="action_failed",
            session_id=self.session_id,
            task_id=task_id,
            task_name=task_name,
            status="action_failed",
            details=details,
        ))

    def on_verification_started(
        self, task_id: str, task_name: str, details: Optional[Dict[str, Any]] = None
    ) -> OrchestrationEvent:
        return self.emit(OrchestrationEvent(
            event_type="verification_started",
            session_id=self.session_id,
            task_id=task_id,
            task_name=task_name,
            status="verifying",
            details=details or {},
        ))

    def on_verification_completed(
        self, task_id: str, task_name: str, status: str, details: Dict[str, Any]
    ) -> OrchestrationEvent:
        details_with_status = dict(details)
        details_with_status["status"] = status
        return self.emit(OrchestrationEvent(
            event_type="verification_completed",
            session_id=self.session_id,
            task_id=task_id,
            task_name=task_name,
            status=status.lower(),
            details=details_with_status,
        ))

    def on_recovery_started(
        self, task_id: str, task_name: str, reason: str, attempt: int = 1, max_budget: int = 3
    ) -> OrchestrationEvent:
        return self.emit(OrchestrationEvent(
            event_type="recovery_started",
            session_id=self.session_id,
            task_id=task_id,
            task_name=task_name,
            status="recovering",
            details={"reason": reason, "attempt": attempt, "max_budget": max_budget},
        ))

    def on_recovery_exhausted(
        self, task_id: str, task_name: str, reason: str
    ) -> OrchestrationEvent:
        return self.emit(OrchestrationEvent(
            event_type="recovery_exhausted",
            session_id=self.session_id,
            task_id=task_id,
            task_name=task_name,
            status="recovery_exhausted",
            details={"reason": reason},
        ))

    def on_session_complete(self) -> OrchestrationEvent:
        self.metrics.completed_at = time.time_ns() / 1e9
        is_all_success = (
            self.metrics.tasks_total > 0
            and self.metrics.tasks_completed == self.metrics.tasks_total
            and self.metrics.tasks_failed == 0
        )
        status_str = "completed" if is_all_success else "failed"
        return self.emit(OrchestrationEvent(
            event_type="session_complete",
            session_id=self.session_id,
            task_id=None,
            task_name=None,
            status=status_str,
            details=self.metrics.to_dict(),
        ))

    # ── Persistence ────────────────────────────────────────────────────────

    async def persist_event(self, event: OrchestrationEvent) -> None:
        """Persist a single event to orchestration_logs collection."""
        if self.db is None:
            return
        try:
            await self.db.orchestration_logs.insert_one(event.to_dict())
            await self.db.replay_events.insert_one(event.to_dict()) # Mirror for replay stream
        except Exception as e:
            logger.error(f"[Monitor] Failed to persist event: {e}")

    async def persist_metrics_snapshot(self) -> None:
        """Upsert current session metrics to execution_metrics collection."""
        if self.db is None:
            return
        try:
            await self.db.execution_metrics.update_one(
                {"session_id": self.session_id},
                {"$set": self.metrics.to_dict()},
                upsert=True,
            )
        except Exception as e:
            logger.error(f"[Monitor] Failed to persist metrics: {e}")

    async def persist_runtime_state(self, graph: "ARDOGraph") -> None:
        """Snapshot current graph state to task_runtime_state collection."""
        if self.db is None:
            return
        try:
            await self.db.task_runtime_state.update_one(
                {"session_id": self.session_id},
                {"$set": {
                    "session_id": self.session_id,
                    "nodes": graph.snapshot(),
                    "parallel_groups": graph.parallel_groups,
                }},
                upsert=True,
            )
        except Exception as e:
            logger.error(f"[Monitor] Failed to persist runtime state: {e}")

    async def persist_scheduler_snapshot(self, trigger: str, snapshot_data: dict) -> None:
        """Persist scheduler reasoning snapshots at major orchestration transitions."""
        if self.db is None:
            return
        try:
            doc = {
                "session_id": self.session_id,
                "trigger": trigger,
                "timestamp": time.time_ns() / 1e9,
                "snapshot": snapshot_data
            }
            await self.db.scheduler_snapshots.insert_one(doc)
        except Exception as e:
            logger.error(f"[Monitor] Failed to persist scheduler snapshot: {e}")
            
    async def persist_replay_session(self, graph: "ARDOGraph", mode: str) -> None:
        """Initialize a replay session root record."""
        if self.db is None:
            return
        try:
            doc = {
                "session_id": self.session_id,
                "mode": mode,
                "initial_graph": graph.snapshot(),
                "created_at": time.time_ns() / 1e9,
            }
            await self.db.replay_sessions.update_one(
                {"session_id": self.session_id},
                {"$set": doc},
                upsert=True
            )
        except Exception as e:
            logger.error(f"[Monitor] Failed to persist replay session: {e}")

    # ── Accessors ──────────────────────────────────────────────────────────

    def get_recent_events(self, limit: int = 30) -> List[dict]:
        return [e.to_dict() for e in self._event_log[-limit:]]

    def get_metrics(self) -> dict:
        return self.metrics.to_dict()

    # ── Private helpers ────────────────────────────────────────────────────

    def _update_metrics_from_event(self, event: OrchestrationEvent) -> None:
        """Keep metrics counters in sync with events (beyond what handlers set)."""
        pass  # Handlers already update counters directly
