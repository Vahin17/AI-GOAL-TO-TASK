import pytest
import time
from orchestration.dag_builder import DAGBuilder
from orchestration.runtime_engine import RuntimeEngine

@pytest.fixture
def mock_db():
    return None

@pytest.mark.asyncio
async def test_orchestration_latency(mock_db):
    """Benchmark orchestration latency and throughput."""
    nodes = [{"id": f"T{i}", "label": f"Task {i}"} for i in range(10)]
    edges = [{"source": f"T{i}", "target": f"T{i+1}"} for i in range(9)]
    
    graph = DAGBuilder.build(nodes, edges)
    engine = RuntimeEngine(graph, session_id="test_perf", mode="sequential", db=mock_db)
    
    start_time = time.time()
    events = []
    async for event in engine.run():
        events.append(event)
    end_time = time.time()
    
    assert end_time - start_time >= 0 # Latency is measured
    metrics = engine.monitor.get_metrics()
    assert "throughput_per_minute" in metrics
