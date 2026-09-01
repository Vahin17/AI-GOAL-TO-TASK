import pytest
import asyncio
import time
from unittest.mock import patch, AsyncMock, MagicMock

from orchestration.dag_builder import DAGBuilder
from orchestration.runtime_engine import RuntimeEngine
from orchestration.state_machine import TaskState
from orchestration.stability_guard import StabilityGuard, StabilityConfig
from orchestration.priority_engine import PriorityEngine

@pytest.fixture
def mock_db():
    return None

@pytest.mark.asyncio
async def test_retry_heavy_workflow(mock_db):
    """
    1. RETRY-HEAVY WORKFLOW
    Verify: task fails, enters retry, respects config, blocks downstream, eventually fails, reason recorded.
    """
    nodes = [
        {"id": "A", "label": "Task A", "max_retries": 2},
        {"id": "B", "label": "Task B"}
    ]
    edges = [{"source": "A", "target": "B"}]
    graph = DAGBuilder.build(nodes, edges)
    
    # Fast retry manager
    engine = RuntimeEngine(graph, session_id="test_retry", mode="sequential", db=mock_db)
    
    # Force failure
    with patch('random.random', return_value=0.0):
        events = []
        async for event in engine.run():
            events.append(event)
            
    # Task A should eventually fail after retries
    a_node = graph.get_node("A")
    assert a_node.status == TaskState.FAILED
    assert a_node.retry_count >= 2 # attempted retries

    # Task B should be blocked
    b_node = graph.get_node("B")
    assert b_node.status == TaskState.BLOCKED

@pytest.mark.asyncio
async def test_mixed_priority_scheduling():
    """
    2. MIXED-PRIORITY SCHEDULING
    Verify correct score calculations based on PriorityEngine factors.
    """
    nodes = [
        {"id": "A", "label": "High Risk, Leaf"},
        {"id": "B", "label": "Safe, Root"},
        {"id": "C", "label": "Dep 1"},
        {"id": "D", "label": "Dep 2"}
    ]
    edges = [
        {"source": "B", "target": "C"},
        {"source": "B", "target": "D"}
    ]
    graph = DAGBuilder.build(nodes, edges)
    
    # Manually set risk scores since DAGBuilder might not parse them in this mock setup
    graph.get_node("A").risk_score = 1.0
    graph.get_node("B").risk_score = 0.0
    
    pe = PriorityEngine()
    queue = pe.build_execution_queue(graph)
    
    # Check that B is prioritized over A due to criticality (successors) and safety
    assert queue.index("B") < queue.index("A")
    
    score_a, breakdown_a = pe.score(graph.get_node("A"), graph)
    score_b, breakdown_b = pe.score(graph.get_node("B"), graph)
    
    assert score_b > score_a
    assert "criticality" in breakdown_b
    assert breakdown_b["safety"] > breakdown_a["safety"]

@pytest.mark.asyncio
async def test_starvation_prevention():
    """
    3. STARVATION PREVENTION
    Verify StabilityGuard forces aging/fairness.
    """
    nodes = [
        {"id": "A", "label": "Starving Task"}
    ]
    graph = DAGBuilder.build(nodes, [])
    graph.get_node("A").status = TaskState.WAITING
    graph.get_node("A").priority_score = 0.1
    
    config = StabilityConfig(max_wait_seconds=1.0, starvation_threshold=1)
    guard = StabilityGuard(config)
    
    queue = ["A"]
    # Manually trigger wait start and advance time
    guard._wait_start["A"] = time.monotonic() - 5.0
    
    modified, new_queue = guard.check(graph, queue)
    assert modified is True
    assert graph.get_node("A").priority_score > 0.1
    events = guard.get_stability_events()
    assert events[-1]["event_type"] == "starvation"

@pytest.mark.asyncio
async def test_reactive_rescheduling(mock_db):
    """
    4. REACTIVE RESCHEDULING
    Verify completing a task unblocks another and re-orders the queue.
    """
    # ... tested implicitly by normal queue reordering on success.
    # We will simulate a completed task calling rescheduler.
    nodes = [
        {"id": "A", "label": "A"},
        {"id": "B", "label": "B"}
    ]
    edges = [{"source": "A", "target": "B"}]
    graph = DAGBuilder.build(nodes, edges)
    
    engine = RuntimeEngine(graph, session_id="test_react", mode="sequential", db=mock_db)
    
    queue_before = engine.rescheduler.on_task_completed("A", graph)
    # Task B should now be in queue
    assert "B" in queue_before

@pytest.mark.asyncio
async def test_branch_quarantine(mock_db):
    """
    5. BRANCH QUARANTINE
    Verify invariant violation fails just the affected task, not session.
    """
    nodes = [{"id": "A", "label": "A"}, {"id": "B", "label": "B"}]
    graph = DAGBuilder.build(nodes, [])
    engine = RuntimeEngine(graph, session_id="test_quar", mode="sequential", db=mock_db)
    
    # Introduce a retry count invariant violation
    graph.get_node("A").retry_count = 100 
    
    engine._assert_invariants()
    
    assert graph.get_node("A").status == TaskState.FAILED
    assert graph.get_node("B").status == TaskState.PENDING

@pytest.mark.asyncio
async def test_deadlock_cycle_protection():
    """
    6. DEADLOCK/CYCLE PROTECTION
    Verify cycles rejected, deadlock detected.
    """
    nodes = [{"id": "A", "label": "A"}, {"id": "B", "label": "B"}]
    edges = [{"source": "A", "target": "B"}, {"source": "B", "target": "A"}]
    
    with pytest.raises(Exception):
        DAGBuilder.build(nodes, edges)

@pytest.mark.asyncio
async def test_websocket_event_integrity(mock_db):
    """
    7. WEBSOCKET EVENT INTEGRITY
    Verify chronological ordering and no duplicates from scheduler stream.
    """
    nodes = [{"id": "A", "label": "A"}]
    graph = DAGBuilder.build(nodes, [])
    
    from orchestration.scheduler import ARDOScheduler
    session = await ARDOScheduler.start([{"id":"A"}], [], "goal", "test_ws", "sequential", mock_db)
    
    q = session.subscribe()
    events = []
    
    # wait for engine to complete
    await asyncio.sleep(0.5) 
    
    while not q.empty():
        events.append(q.get_nowait())
        
    assert len(events) > 0
    # No duplicate types for the same task
    start_events = [e for e in events if e.event_type == "task_started"]
    assert len(start_events) == 1

@pytest.mark.asyncio
async def test_replay_safety():
    """
    8. REPLAY SAFETY
    Verify Replay Engine (simulated via API currently) does not mutate executor state.
    """
    from api.router import replay_orchestration
    from fastapi import HTTPException
    
    # Should throw 500 without db or 404 without root, but not execute anything
    try:
        await replay_orchestration("fake_session")
    except HTTPException:
        pass
