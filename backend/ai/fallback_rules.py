import uuid
from schemas.models import TaskNode, TaskEdge, PlanGraphMetadata, PlanGraphResponse

async def generate_fallback_plan(goal: str) -> PlanGraphResponse:
    """
    Constructs a rigid, rule-based safe graph layout when the generative provider fails.
    This guarantees the frontend React Flow canvas always has actionable nodes.
    """
    
    # Generic Baseline Flow: Initialize -> Execute -> Verify
    node_init = TaskNode(
        id=str(uuid.uuid4()),
        type="input",  # React Flow standard
        position={"x": 250.0, "y": 50.0},
        data={"label": "Initialization Phase"}
    )
    
    node_exec = TaskNode(
        id=str(uuid.uuid4()),
        type="default",
        position={"x": 250.0, "y": 200.0},
        data={"label": "Execution Phase"}
    )
    
    node_verify = TaskNode(
        id=str(uuid.uuid4()),
        type="output",
        position={"x": 250.0, "y": 350.0},
        data={"label": "Verification Phase"}
    )
    
    edge_init_exec = TaskEdge(
        id=f"e_{node_init.id}_{node_exec.id}",
        source=node_init.id,
        target=node_exec.id
    )
    
    edge_exec_verify = TaskEdge(
        id=f"e_{node_exec.id}_{node_verify.id}",
        source=node_exec.id,
        target=node_verify.id
    )
    
    metadata = PlanGraphMetadata(
        intent=f"Heuristic Fallback Plan for: {goal}",
        confidence=1.0,  # Absolute heuristic certainty
        reasoning_summary="Primary LLM schema pipeline failure. Triggered heuristic safe graph structure."
    )
    
    return PlanGraphResponse(
        nodes=[node_init, node_exec, node_verify],
        edges=[edge_init_exec, edge_exec_verify],
        metadata=metadata
    )
