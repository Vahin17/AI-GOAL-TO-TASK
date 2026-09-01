from schemas.models import (
    TaskNode,
    TaskEdge,
    PlanGraphMetadata,
    PlanGraphResponse
)

def test_plan_graph_response_serialization_matches_react_flow():
    """
    Verify that our Pydantic schema perfectly shapes a React Flow compatible JSON when serialized.
    """
    node = TaskNode(
        id="1",
        type="input",
        position={"x": 50.0, "y": 100.0},
        data={"label": "Start Setup"}
    )
    
    node2 = TaskNode(
        id="2",
        type="default",
        position={"x": 50.0, "y": 200.0},
        data={"label": "Execution"}
    )
    
    edge = TaskEdge(id="e1-2", source="1", target="2")
    
    metadata = PlanGraphMetadata(
        intent="Initialize setup",
        confidence=0.98,
        reasoning_summary="User requested setup."
    )
    
    response = PlanGraphResponse(
        nodes=[node, node2],
        edges=[edge],
        metadata=metadata
    )
    
    json_output = response.model_dump()
    
    # Top level arrays and objects
    assert "nodes" in json_output
    assert "edges" in json_output
    assert "metadata" in json_output
    
    # Deep node layout matching React Flow node schema requirement
    first_node = json_output["nodes"][0]
    assert first_node["id"] == "1"
    assert first_node["type"] == "input"
    assert "x" in first_node["position"]
    assert "y" in first_node["position"]
    assert first_node["position"]["x"] == 50.0
    assert first_node["data"]["label"] == "Start Setup"
    
    # Edges match standard Edge representation
    first_edge = json_output["edges"][0]
    assert first_edge["source"] == "1"
    assert first_edge["target"] == "2"
