import pytest
from ai.fallback_rules import generate_fallback_plan
from schemas.models import PlanGraphResponse

@pytest.mark.asyncio
async def test_fallback_generates_valid_schema():
    """
    Simulates a total systemic LLM failure ensuring our React Flow canvas remains highly resilient and safe.
    """
    goal = "Build a rocket"
    fallback_response = await generate_fallback_plan(goal)
    
    # Instance Assertions
    assert isinstance(fallback_response, PlanGraphResponse)
    
    # Data length assertions indicating heuristic generic shape
    assert len(fallback_response.nodes) == 3
    assert len(fallback_response.edges) == 2
    
    # Safety metadata assertions
    assert "Heuristic Fallback Plan" in fallback_response.metadata.intent
    assert fallback_response.metadata.confidence == 1.0
    
    # Assert nodes format mapping properly into React flow spec
    json_rep = fallback_response.model_dump()
    assert "nodes" in json_rep
    
    node_labels = [n["data"]["label"] for n in json_rep["nodes"]]
    assert "Initialization Phase" in node_labels
    assert "Verification Phase" in node_labels
