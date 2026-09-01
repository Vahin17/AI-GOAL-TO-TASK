from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional, Any, Dict
from datetime import datetime
import uuid

class MongoBaseModel(BaseModel):
    """
    Base model that handles MongoDB ObjectId and common fields.
    """
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
        json_encoders={datetime: lambda v: v.isoformat()}
    )

class Goal(MongoBaseModel):
    public_id: str = Field(..., description="Public human-readable ID, e.g., 'goal_xxx'")
    goal: str = Field(..., description="User's natural language goal.")
    status: str = Field(default="pending", description="Current status of the goal processing.")
    current_step: Optional[str] = None
    progress_percentage: float = 0.0

class Plan(MongoBaseModel):
    public_id: str = Field(..., description="Public human-readable ID, e.g., 'plan_xxx'")
    goal_id: str = Field(..., description="Reference to the Goal public_id or ObjectId.")
    nodes: List[Dict[str, Any]] = Field(..., description="React Flow compatible nodes.")
    edges: List[Dict[str, Any]] = Field(..., description="React Flow compatible edges.")
    metadata: Dict[str, Any] = Field(..., description="Model metadata and confidence.")
    reasoning_summary: str = Field(..., description="AI reasoning for this plan.")
    confidence: float = Field(..., description="Model confidence score.")
    status: str = Field(default="generated")
    current_step: Optional[int] = 0
    progress_percentage: float = 0.0

class Task(MongoBaseModel):
    public_id: str = Field(..., description="Public human-readable ID, e.g., 'task_xxx'")
    task_id: str = Field(..., description="The ID from the React Flow graph.")
    plan_id: str = Field(..., description="Reference to the Plan public_id.")
    step_number: int = Field(..., description="Execution sequence number.")
    title: str = Field(..., description="Human-readable task title.")
    reasoning: str = Field(..., description="Why this task is necessary.")
    eta: str = Field(..., description="Estimated time for task completion.")
    status: str = Field(default="queued")
    depends_on: List[str] = Field(default_factory=list, description="IDs of tasks that must complete first.")
    current_step: Optional[str] = None
    progress_percentage: float = 0.0

class Execution(MongoBaseModel):
    public_id: str = Field(..., description="Public human-readable ID, e.g., 'exec_xxx'")
    task_id: str = Field(..., description="Reference to the Task public_id.")
    status: str = Field(default="initialized")
    logs: List[str] = Field(default_factory=list)
    current_step: Optional[str] = None
    progress_percentage: float = 0.0

class Feedback(MongoBaseModel):
    public_id: str = Field(..., description="Public human-readable ID, e.g., 'feed_xxx'")
    target_id: str = Field(..., description="ID of the goal/plan/task this feedback is for.")
    score: int = Field(..., ge=1, le=5)
    comment: Optional[str] = None

class SafetyFlag(MongoBaseModel):
    public_id: str = Field(..., description="Public human-readable ID, e.g., 'safe_xxx'")
    goal_id: str = Field(..., description="Goal being checked.")
    is_safe: bool
    risk_level: str
    reasons: List[str]

class Memory(MongoBaseModel):
    public_id: str = Field(..., description="Public human-readable ID, e.g., 'mem_xxx'")
    content: str
    embedding: Optional[List[float]] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

class User(MongoBaseModel):
    """Placeholder collection for users."""
    public_id: str = Field(..., description="Public human-readable ID, e.g., 'user_xxx'")
    email: str
    full_name: Optional[str] = None

class Session(MongoBaseModel):
    """Placeholder collection for sessions."""
    public_id: str = Field(..., description="Public human-readable ID, e.g., 'sess_xxx'")
    user_id: str
    expires_at: datetime
