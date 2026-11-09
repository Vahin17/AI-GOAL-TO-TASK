from pydantic import BaseModel, Field
from typing import List, Any, Dict, Optional

class IntentOutput(BaseModel):
    intent: str = Field(..., description="The categorized pure intent behind the goal requested by the user.")
    category: str = Field(..., description="Broad categorization of the intent (e.g., 'system_architecture', 'ui_development', 'research')")

class TaskNode(BaseModel):
    """
    Representation of a node that works perfectly with React Flow.
    """
    id: str = Field(..., description="Unique string ID for the node.")
    type: str = Field(..., description="Type of standard React Flow node (e.g., 'default', 'input', 'output').")
    position: Dict[str, float] = Field(..., description="Dictionary containing exactly 'x' and 'y' float coordinates. e.g. {'x': 0.0, 'y': 0.0}")
    data: Dict[str, Any] = Field(..., description="Data object containing 'label' and other necessary props to be displayed inside React Flow.")

class TaskEdge(BaseModel):
    """
    Representation of an edge connection array for React Flow.
    """
    id: str = Field(..., description="Unique string ID for the edge connecting two nodes.")
    source: str = Field(..., description="The ID of the source TaskNode.")
    target: str = Field(..., description="The ID of the target TaskNode.")

class PlanGraphMetadata(BaseModel):
    """
    Metadata summarizing the overall response logic and plan intent.
    """
    intent: str = Field(..., description="The intent behind the generated operation.")
    confidence: float = Field(..., description="Model's confidence score ranging from 0.0 to 1.0.")
    reasoning_summary: str = Field(..., description="Brief summary of the reasoning used to logically construct these task nodes.")
    # Phase 6: Knowledge provenance details
    knowledge_sources: Optional[List[str]] = Field(default_factory=list, description="Knowledge base sources used to ground the plan.")
    knowledge_chunk_count: int = Field(default=0, description="Number of retrieved knowledge chunks injected into prompt.")

class PlanGraphResponse(BaseModel):
    """
    Top level structured extraction model that nests the proper React Flow compatible nodes and edge graph response.
    """
    public_id: Optional[str] = Field(None, description="Public human-readable ID for the plan.")
    goal_id: Optional[str] = Field(None, description="Public human-readable ID for the associated goal.")
    nodes: List[TaskNode] = Field(..., description="List of React Flow compatible task nodes.")
    edges: List[TaskEdge] = Field(..., description="List of React Flow compatible edges between the task nodes.")
    metadata: PlanGraphMetadata = Field(..., description="Metadata summarizing the AI response intent and reasoning.")
    # Phase 5: Safety decisions attached after batch evaluation, not serialized in API response
    safety_decisions: Optional[Dict[str, Any]] = Field(None, exclude=True, description="Internal: SafetyDecision map from SafetyEngine.")
    # Phase 6: Detailed RAG retrieval provenance
    provenance: Optional[Dict[str, Any]] = Field(default=None, description="Detailed RAG retrieval provenance metadata.")
    # Phase 1: Canonical plan hash & critic verification result
    plan_hash: Optional[str] = Field(default=None, description="Deterministic canonical SHA-256 hash of the complete plan.")
    critic_result: Optional[Dict[str, Any]] = Field(default=None, description="Plan critic independent verification result.")
