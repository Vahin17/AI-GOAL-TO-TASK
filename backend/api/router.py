from fastapi import APIRouter
from pydantic import BaseModel, Field
from backend.schemas.models import PlanGraphResponse
from backend.ai.orchestrator import generate_task_graph

router = APIRouter()

class GoalRequest(BaseModel):
    goal: str = Field(..., description="The user's natural language goal description.")

@router.get("/health")
def health_check():
    """Simple API liveness validation standard."""
    return {"status": "ok", "service": "planning-engine"}

@router.post("/plan", response_model=PlanGraphResponse)
def create_plan(request: GoalRequest):
    """
    Endpoint that accepts a user natural language goal and rigorously structures
    the execution intent into a strictly typed React Flow graph logic model.
    """
    # Orchestrator securely triggers generation or rule-based layout
    return generate_task_graph(request.goal)
