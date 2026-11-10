import pytest
from orchestration.execution_monitor import ExecutionMonitor, SessionMetrics


def test_execution_monitor_tracks_recovery_count():
    monitor = ExecutionMonitor(session_id="test_sess")
    assert monitor.metrics.recovery_count == 0

    # Start recoveries
    monitor.on_recovery_started(task_id="t1", task_name="Search", reason="Selector not found", attempt=1)
    assert monitor.metrics.recovery_count == 1

    monitor.on_recovery_started(task_id="t1", task_name="Search", reason="Verification failed", attempt=2)
    assert monitor.metrics.recovery_count == 2

    # Verify to_dict includes recovery_count
    d = monitor.metrics.to_dict()
    assert d["recovery_count"] == 2
    assert "recovery_count" in d


def test_success_rate_ratio_and_percentage():
    metrics = SessionMetrics(session_id="s1")
    metrics.tasks_total = 3
    metrics.tasks_completed = 3
    # Ratio is 1.0 (representing 100%)
    assert metrics.success_rate == 1.0

    metrics.tasks_total = 4
    metrics.tasks_completed = 3
    metrics.tasks_failed = 1
    assert metrics.success_rate == 0.75

