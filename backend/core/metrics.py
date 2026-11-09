"""
metrics.py — Production Operational Metrics Collector (Phase 4)
===============================================================
Thread-safe, lightweight in-memory metrics aggregator tracking HTTP latency,
LLM planning duration, browser task outcomes, and infrastructure health.
"""

from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock
from typing import Any, Dict, List, Optional


class OperationalMetricsCollector:
    """
    In-memory metrics collector tracking core operational indicators
    for production monitoring and performance observability.
    """

    _instance: Optional["OperationalMetricsCollector"] = None

    def __init__(self):
        self._lock = Lock()
        self._start_time = time.time()
        self._http_requests: Dict[str, int] = defaultdict(int)
        self._http_latencies: List[float] = []
        self._planning_durations: List[float] = []
        self._planning_errors: int = 0
        self._browser_task_durations: List[float] = []
        self._task_outcomes: Dict[str, int] = defaultdict(int)
        self._goal_outcomes: Dict[str, int] = defaultdict(int)
        self._db_errors: Dict[str, int] = defaultdict(int)
        self._worker_lease_losses: int = 0
        self._outbox_retries: int = 0
        self._active_sessions: int = 0

    @classmethod
    def get_instance(cls) -> "OperationalMetricsCollector":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def record_request(self, method: str, path: str, status_code: int, duration: float) -> None:
        """Record an inbound HTTP request completion."""
        endpoint_key = f"{method.upper()} {path} -> {status_code}"
        with self._lock:
            self._http_requests[endpoint_key] += 1
            if len(self._http_latencies) < 2000:
                self._http_latencies.append(duration)
            else:
                self._http_latencies.pop(0)
                self._http_latencies.append(duration)

    def record_planning(self, duration: float, success: bool) -> None:
        """Record LLM goal decomposition latency and errors."""
        with self._lock:
            if success:
                self._planning_durations.append(duration)
                if len(self._planning_durations) > 500:
                    self._planning_durations.pop(0)
            else:
                self._planning_errors += 1

    def record_browser_task(self, duration: float, outcome: str) -> None:
        """Record browser automation duration and semantic outcome."""
        with self._lock:
            self._browser_task_durations.append(duration)
            if len(self._browser_task_durations) > 500:
                self._browser_task_durations.pop(0)
            self._task_outcomes[outcome.lower()] += 1

    def record_goal_outcome(self, status: str) -> None:
        """Record end-to-end goal verification outcome."""
        with self._lock:
            self._goal_outcomes[status.lower()] += 1

    def record_db_error(self, db_type: str = "postgres") -> None:
        """Record a transient or critical database error."""
        with self._lock:
            self._db_errors[db_type] += 1

    def record_worker_lease_loss(self) -> None:
        """Record worker lease revocation or timeout."""
        with self._lock:
            self._worker_lease_losses += 1

    def record_outbox_retry(self, count: int = 1) -> None:
        """Record outbox event synchronization retry."""
        with self._lock:
            self._outbox_retries += count

    def set_active_sessions(self, count: int) -> None:
        """Update current count of in-memory active orchestration sessions."""
        with self._lock:
            self._active_sessions = max(0, count)

    def _calculate_percentiles(self, values: List[float]) -> Dict[str, float]:
        """Compute p50, p90, p99 latencies in milliseconds."""
        if not values:
            return {"count": 0, "p50_ms": 0.0, "p90_ms": 0.0, "p99_ms": 0.0, "avg_ms": 0.0}
        s = sorted(values)
        n = len(s)
        avg = sum(s) / n * 1000.0
        p50 = s[int(n * 0.50)] * 1000.0
        p90 = s[min(n - 1, int(n * 0.90))] * 1000.0
        p99 = s[min(n - 1, int(n * 0.99))] * 1000.0
        return {
            "count": n,
            "avg_ms": round(avg, 2),
            "p50_ms": round(p50, 2),
            "p90_ms": round(p90, 2),
            "p99_ms": round(p99, 2),
        }

    def get_snapshot(self) -> Dict[str, Any]:
        """Return operational metrics summary snapshot."""
        now = time.time()
        with self._lock:
            http_stats = self._calculate_percentiles(self._http_latencies)
            planning_stats = self._calculate_percentiles(self._planning_durations)
            browser_stats = self._calculate_percentiles(self._browser_task_durations)

            return {
                "uptime_seconds": round(now - self._start_time, 2),
                "active_sessions": self._active_sessions,
                "http": {
                    "total_requests": sum(self._http_requests.values()),
                    "by_endpoint": dict(self._http_requests),
                    "latency": http_stats,
                },
                "planning": {
                    "completed_plans": planning_stats["count"],
                    "planning_errors": self._planning_errors,
                    "latency": planning_stats,
                },
                "browser_execution": {
                    "completed_tasks": browser_stats["count"],
                    "task_outcomes": dict(self._task_outcomes),
                    "latency": browser_stats,
                },
                "goal_verification": {
                    "outcomes": dict(self._goal_outcomes),
                },
                "resilience": {
                    "db_errors": dict(self._db_errors),
                    "worker_lease_losses": self._worker_lease_losses,
                    "outbox_retries": self._outbox_retries,
                },
            }

    def reset(self) -> None:
        """Reset metrics (useful between test runs)."""
        with self._lock:
            self._start_time = time.time()
            self._http_requests.clear()
            self._http_latencies.clear()
            self._planning_durations.clear()
            self._planning_errors = 0
            self._browser_task_durations.clear()
            self._task_outcomes.clear()
            self._goal_outcomes.clear()
            self._db_errors.clear()
            self._worker_lease_losses = 0
            self._outbox_retries = 0
            self._active_sessions = 0


# Global singleton instance
metrics_collector = OperationalMetricsCollector.get_instance()

