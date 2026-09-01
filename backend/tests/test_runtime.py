import pytest
import asyncio
from unittest.mock import patch
from orchestration.dag_builder import DAGBuilder
from orchestration.runtime_engine import RuntimeEngine
from orchestration.execution_monitor import ExecutionMonitor

@pytest.fixture
def mock_db():
    return None

@pytest.mark.asyncio
async def test_sequential_workflow(mock_db):
    nodes = [
        {"id": "A", "label": "Task A"},
        {"id": "B", "label": "Task B"},
        {"id": "C", "label": "Task C"}
    ]
    edges = [
        {"source": "A", "target": "B"},
        {"source": "B", "target": "C"}
    ]
    
    graph = DAGBuilder.build(nodes, edges)
    engine = RuntimeEngine(graph, session_id="test_seq", mode="sequential", db=mock_db)
    
    with patch("random.random", return_value=1.0), patch("random.uniform", return_value=0.001):
        events = []
        async for event in engine.run():
            events.append(event)
            
        start_events = [e for e in events if e.event_type == "task_started"]
        assert len(start_events) == 3
        assert start_events[0].task_id == "A"
        assert start_events[1].task_id == "B"
        assert start_events[2].task_id == "C"

@pytest.mark.asyncio
async def test_parallel_workflow(mock_db):
    nodes = [
        {"id": "A", "label": "Task A"},
        {"id": "B", "label": "Task B"},
        {"id": "C", "label": "Task C"},
        {"id": "D", "label": "Task D"}
    ]
    edges = [
        {"source": "A", "target": "B"},
        {"source": "A", "target": "C"},
        {"source": "B", "target": "D"},
        {"source": "C", "target": "D"}
    ]
    
    graph = DAGBuilder.build(nodes, edges)
    engine = RuntimeEngine(graph, session_id="test_par", mode="parallel", db=mock_db)
    
    with patch("random.random", return_value=1.0), patch("random.uniform", return_value=0.001):
        events = []
        async for event in engine.run():
            events.append(event)
            
        complete_events = [e for e in events if e.event_type == "session_complete"]
        assert len(complete_events) > 0

@pytest.mark.asyncio
async def test_blocked_workflow(mock_db):
    nodes = [
        {"id": "A", "label": "Task A"},
        {"id": "B", "label": "Format C Drive"}
    ]
    edges = [
        {"source": "A", "target": "B"}
    ]
    
    graph = DAGBuilder.build(nodes, edges, risk_threshold=0.5)
    engine = RuntimeEngine(graph, session_id="test_blk", mode="safe", db=mock_db)
    
    events = []
    async for event in engine.run():
        events.append(event)
        
    approval_events = [e for e in events if e.event_type == "task_approval_needed"]
    assert len(approval_events) > 0
    assert approval_events[0].task_id == "B"
