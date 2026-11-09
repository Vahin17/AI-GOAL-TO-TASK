"""
ardo_models.py — ARDO Pydantic Schemas
=======================================
API-facing schemas for ARDO orchestration endpoints and WebSocket events.
"""

from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional


class ARDOStartRequest(BaseModel):
    goal: str = Field(..., description="The user's natural language goal.")
    mode: str = Field(
        default="safe",
        description="Execution mode: safe | sequential | parallel | aggressive"
    )


class ARDONodeStateResponse(BaseModel):
    """API-facing representation of a single task node in the ARDO graph."""
    task_id: str
    task_name: str
    dependency_ids: List[str]
    status: str
    priority_score: float
    risk_score: float
    retry_count: int
    max_retries: int
    retry_confidence: float
    estimated_execution_time: float
    actual_execution_time: Optional[float] = None
    execution_type: str
    execution_mode: str
    fallback_available: bool
    parallel_group: Optional[int] = None
    approval_needed: bool
    execution_logs: List[str]
    starvation_counter: int


class OrchestrationEventResponse(BaseModel):
    """Single event emitted by the ARDO runtime."""
    event_type: str
    session_id: str
    task_id: Optional[str] = None
    task_name: Optional[str] = None
    status: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)
    timestamp: float


class SessionMetricsResponse(BaseModel):
    session_id: str
    elapsed_seconds: float
    tasks_total: int
    tasks_completed: int
    tasks_failed: int
    tasks_blocked: int
    tasks_retried: int
    total_retry_count: int
    reschedule_count: int
    approval_count: int
    throughput_per_minute: float
    avg_latency_seconds: float
    success_rate: float
    timeline_markers: List[Dict[str, Any]]


class SchedulingExplanationResponse(BaseModel):
    task_id: str
    task_name: str
    final_score: float
    factor_breakdown: Dict[str, float]
    raw_factors: Dict[str, float]
    decision_reason: str
    selected_factors: List[str]
    rescheduling_cause: Optional[str] = None
    why_now: Optional[str] = None
    timestamp: float


class ARDOSessionResponse(BaseModel):
    """Response when a new ARDO session is started."""
    session_id: str
    mode: str
    task_count: int
    parallel_groups: List[List[str]]
    initial_queue: List[str]
    initial_graph: List[ARDONodeStateResponse]
    plan_hash: Optional[str] = Field(default=None, description="Canonical SHA-256 hash of the plan DAG.")


class ARDOStateResponse(BaseModel):
    """Response for GET /orchestrate/{id}/state."""
    session_id: str
    mode: str
    execution_queue: List[str]
    graph_state: List[Dict[str, Any]]
    parallel_groups: List[List[str]]
    metrics: Dict[str, Any]
    why_now: Optional[Dict[str, Any]] = None
    reschedule_log: List[Dict[str, Any]]
    stability_events: List[Dict[str, Any]]
    weight_suggestion: Optional[Dict[str, Any]] = None
    plan_hash: Optional[str] = Field(default=None, description="Canonical SHA-256 hash of the current plan DAG.")


class ApproveTaskRequest(BaseModel):
    approved: bool = Field(default=True)
    plan_hash: Optional[str] = Field(
        default=None,
        description="Canonical SHA-256 plan hash to cryptographically verify before approval."
    )
    notes: Optional[str] = Field(
        default=None,
        description="Optional operator notes or confirmation rationale."
    )
