"""
test_worker_leases.py — Worker Lease Renewal, Heartbeat & Split-Brain Prevention (Phase 2 Acceptance)
======================================================================================================
Validates:
1. Worker lease heartbeat maintains active lease during execution.
2. Renewal failure immediately suspends task dispatch.
3. Expired leases permit failover to a new worker.
4. Old worker cannot dispatch tasks after its lease is lost or expired (split-brain prevention).
"""

import asyncio
import time
import pytest
from unittest.mock import AsyncMock, patch

from orchestration.dag_builder import DAGBuilder
from orchestration.state_machine import TaskState
from orchestration.runtime_engine import RuntimeEngine
from orchestration.checkpoint_manager import (
    ExecutionCheckpointManager,
    _MEMORY_LEASES,
)


@pytest.fixture(autouse=True)
def clear_leases():
    _MEMORY_LEASES.clear()
    yield
    _MEMORY_LEASES.clear()


@pytest.mark.asyncio
async def test_lease_heartbeat_renews_actively():
    """Verifies that RuntimeEngine lease heartbeat extends the lease TTL."""
    session_id = "lease_heartbeat_sess"
    worker_id = "worker_heartbeat_test"

    nodes = [
        {"id": "t1", "type": "default", "data": {"label": "Search docs"}},
    ]
    graph = DAGBuilder.build(nodes, [], goal_text="Heartbeat test")

    engine = RuntimeEngine(graph, session_id=session_id, mode="safe", worker_id=worker_id)

    # Initial lease acquisition
    acquired = await ExecutionCheckpointManager.acquire_worker_lease(
        session_id=session_id,
        worker_id=worker_id,
        lease_duration_seconds=0.3,
    )
    assert acquired is True
    initial_expiry = _MEMORY_LEASES[session_id]["lease_expires_at"]

    # Start heartbeat with short interval (0.05s)
    engine._lease_stop_event.clear()
    engine._lease_task = asyncio.create_task(
        engine._lease_heartbeat_loop(interval_seconds=0.05, lease_duration=0.3)
    )

    # Wait 0.12s (allowing ~2 heartbeat renewals)
    await asyncio.sleep(0.12)
    engine._lease_stop_event.set()
    engine._lease_task.cancel()
    try:
        await engine._lease_task
    except asyncio.CancelledError:
        pass

    renewed_expiry = _MEMORY_LEASES[session_id]["lease_expires_at"]
    assert renewed_expiry > initial_expiry
    assert engine._lease_valid is True


@pytest.mark.asyncio
async def test_renewal_failure_suspends_dispatch():
    """
    If worker lease cannot be renewed (e.g. lease expired or revoked),
    _lease_valid is set to False and task dispatch is refused.
    """
    session_id = "lease_failure_sess"
    worker_1 = "worker_orig"
    worker_2 = "worker_usurper"

    nodes = [
        {"id": "t1", "type": "default", "data": {"label": "Process batch"}},
    ]
    graph = DAGBuilder.build(nodes, [], goal_text="Failure test")

    engine = RuntimeEngine(graph, session_id=session_id, mode="safe", worker_id=worker_1)

    # Worker 1 acquires lease for 0.05s
    await ExecutionCheckpointManager.acquire_worker_lease(
        session_id=session_id,
        worker_id=worker_1,
        lease_duration_seconds=0.05,
    )

    # Wait for lease to expire
    await asyncio.sleep(0.08)

    # Worker 2 acquires the expired lease
    acquired_2 = await ExecutionCheckpointManager.acquire_worker_lease(
        session_id=session_id,
        worker_id=worker_2,
        lease_duration_seconds=10.0,
    )
    assert acquired_2 is True

    # Worker 1 attempts heartbeat renewal -> MUST FAIL
    renewed_1 = await ExecutionCheckpointManager.renew_worker_lease(
        session_id=session_id,
        worker_id=worker_1,
        lease_duration_seconds=10.0,
    )
    assert renewed_1 is False

    # Simulate heartbeat loop tick in worker 1
    engine._lease_stop_event.clear()
    engine._lease_task = asyncio.create_task(
        engine._lease_heartbeat_loop(interval_seconds=0.02, lease_duration=10.0)
    )
    await asyncio.sleep(0.05)
    engine._lease_stop_event.set()
    engine._lease_task.cancel()
    try:
        await engine._lease_task
    except asyncio.CancelledError:
        pass

    assert engine._lease_valid is False

    # Worker 1 tries to dispatch a task -> MUST BE REFUSED
    events = []
    async for evt in engine._execute_task("t1"):
        events.append(evt)

    assert len(events) == 0
    assert graph.nodes["t1"].status == TaskState.PENDING


@pytest.mark.asyncio
async def test_expired_lease_failover_to_new_worker():
    """When a worker lease expires due to worker death/crash, a new worker can acquire it."""
    session_id = "lease_failover_sess"
    dead_worker = "worker_crashed"
    new_worker = "worker_failover"

    # Dead worker held lease that expired
    _MEMORY_LEASES[session_id] = {
        "worker_id": dead_worker,
        "acquired_at": time.time() - 60.0,
        "lease_expires_at": time.time() - 30.0,  # Expired 30s ago
    }

    # New worker acquires lease
    acquired = await ExecutionCheckpointManager.acquire_worker_lease(
        session_id=session_id,
        worker_id=new_worker,
        lease_duration_seconds=30.0,
    )
    assert acquired is True
    assert _MEMORY_LEASES[session_id]["worker_id"] == new_worker


@pytest.mark.asyncio
async def test_split_brain_prevention_two_engines():
    """
    Two RuntimeEngines cannot run simultaneously for the same session.
    The second engine is rejected with a RuntimeError on startup.
    """
    session_id = "split_brain_sess"
    nodes = [{"id": "t1", "type": "default", "data": {"label": "Task 1"}}]
    graph_1 = DAGBuilder.build(nodes, [], goal_text="Split brain 1")
    graph_2 = DAGBuilder.build(nodes, [], goal_text="Split brain 2")

    engine_1 = RuntimeEngine(graph_1, session_id=session_id, worker_id="worker_active")
    engine_2 = RuntimeEngine(graph_2, session_id=session_id, worker_id="worker_rogue")

    # Engine 1 acquires lease
    await ExecutionCheckpointManager.acquire_worker_lease(
        session_id=session_id,
        worker_id="worker_active",
        lease_duration_seconds=30.0,
    )

    # Engine 2 tries to run() -> must fail lease acquisition and raise RuntimeError
    with pytest.raises(RuntimeError, match="held by another active worker"):
        async for _ in engine_2.run():
            pass

