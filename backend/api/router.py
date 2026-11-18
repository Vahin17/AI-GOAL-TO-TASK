"""
router.py — FastAPI Route Definitions
======================================
Exposes all planning and ARDO orchestration endpoints.

Routes:
    GET  /health                                    — liveness check
    POST /plan                                      — generate task graph (LLM planner)
    POST /orchestrate                               — start ARDO session
    GET  /orchestrate/{session_id}/state            — current orchestration state
    POST /orchestrate/{session_id}/approve/{tid}    — safety gate approval
    GET  /orchestrate/{session_id}/metrics          — session execution metrics
    GET  /orchestrate/{session_id}/explain/{tid}    — scheduling explanation for a task
    GET  /orchestrate/{session_id}/why-now          — active "Why This Task Now?" context
    WS   /ws/orchestrate/{session_id}               — real-time event stream
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Depends
from pydantic import BaseModel, Field

from api.auth import get_current_user, get_optional_current_user

from schemas.models import PlanGraphResponse
from schemas.ardo_models import (
    ARDOStartRequest,
    ARDOSessionResponse,
    ARDOStateResponse,
    ApproveTaskRequest,
    ARDONodeStateResponse,
    SessionMetricsResponse,
    SchedulingExplanationResponse,
)
from ai.orchestrator import generate_task_graph
from orchestration.scheduler import ARDOScheduler
from core.database import Database, get_database

logger = logging.getLogger(__name__)
router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# Request Models
# ─────────────────────────────────────────────────────────────────────────────

class GoalRequest(BaseModel):
    goal: str = Field(..., description="The user's natural language goal description.")


# ─────────────────────────────────────────────────────────────────────────────
# Health & Observability (Liveness, Readiness, Metrics)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/health")
def health_check():
    """Liveness probe: returns 200 if the process is responsive."""
    return {
        "status": "ok",
        "service": "planning-engine",
        "ardo": "active",
        "timestamp": datetime.now(timezone.utc).isoformat() if hasattr(datetime, "now") else str(datetime.utcnow()),
    }


@router.get("/health/ready")
async def readiness_check():
    """
    Readiness probe: validates actual database connections, AI config, and tool availability.
    Returns 200 if dependencies are operational; 503 if critical dependencies are down.
    """
    from core.auth_db import AuthDatabase
    from core.database import Database
    from core.config import settings
    from execution.registry.tool_registry import ToolRegistry

    auth_db_status = AuthDatabase.get_instance().check_health()
    mongo_status = await Database.check_health()

    ai_configured = bool(settings.GEMINI_API_KEY or settings.OPENAI_API_KEY)
    tool_registered = ToolRegistry.get_instance().get_tool("browser") is not None

    is_ready = auth_db_status.get("status") == "healthy" and ai_configured

    payload = {
        "ready": is_ready,
        "auth_database": auth_db_status,
        "mongodb": mongo_status,
        "ai_configured": ai_configured,
        "browser_tool_registered": tool_registered,
        "environment": settings.ENVIRONMENT,
    }

    if not is_ready:
        raise HTTPException(status_code=503, detail=payload)
    return payload


@router.get("/metrics")
def get_metrics_snapshot():
    """Exposes internal operational metrics for observability and monitoring."""
    from core.metrics import metrics_collector
    metrics_collector.set_active_sessions(len(ARDOScheduler._sessions))
    return metrics_collector.get_snapshot()


@router.get("/tools")
def get_registered_tools():
    """Diagnostic tool listing endpoint."""
    from execution.registry.tool_registry import ToolRegistry
    from execution.browser.browser_tool import BrowserTool
    registry = ToolRegistry.get_instance()
    if not registry.get_tool("browser"):
        try:
            registry.register(BrowserTool())
        except Exception:
            pass
    tools = registry.list_tools()
    return {
        "status": "ok",
        "total_tools": len(tools),
        "tools": tools,
    }


@router.get("/orchestrate/{session_id}/inspect")
async def inspect_session_page(
    session_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Diagnostic endpoint returning live DOM PageSnapshot for an active browser session with strict tenant isolation."""
    from execution.browser.environment_perception import EnvironmentPerceptionEngine

    active_session = ARDOScheduler._sessions.get(session_id)
    if not active_session or not active_session.user_id or active_session.user_id != current_user["id"]:
        raise HTTPException(
            status_code=403,
            detail=f"Forbidden: Session {session_id} not found or access denied."
        )

    page = None
    executor_obj = None
    if hasattr(active_session, "browser_adapter") and active_session.browser_adapter:
        executor_obj = getattr(active_session.browser_adapter, "executor", None)
        page = getattr(executor_obj, "_page", None)
    if not page and hasattr(active_session, "engine") and getattr(active_session.engine, "browser_adapter", None):
        executor_obj = getattr(active_session.engine.browser_adapter, "executor", None)
        page = getattr(executor_obj, "_page", None)

    if not page:
        raise HTTPException(status_code=404, detail=f"No active browser page for session {session_id}")

    from execution.browser.playwright_executor import PlaywrightBrowserExecutor
    if isinstance(executor_obj, PlaywrightBrowserExecutor):
        snapshot = await executor_obj.inspect_page()
    else:
        engine = EnvironmentPerceptionEngine()
        snapshot = await engine.inspect(page)
    snapshot_data = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot.model_dump()

    # Defense-in-depth: Ensure any sensitive input values in serialized snapshot elements are strictly redacted
    import re
    sensitive_pattern = re.compile(
        r"password|secret|token|apikey|api_key|auth|credential|cvv|cvc|csc|cardnumber|card_number|card_no|cardno|creditcard|credit_card|debit_card|pan|ssn|social_security|socialsecurity|pin|passcode|access_token|refresh_token|bearer|private_key|privkey",
        re.IGNORECASE,
    )
    sensitive_auto = re.compile(
        r"current-password|new-password|one-time-code|cc-number|cc-csc|cc-exp|cc-exp-month|cc-exp-year|cc-type|transaction-amount",
        re.IGNORECASE,
    )
    if "elements" in snapshot_data and isinstance(snapshot_data["elements"], list):
        for el in snapshot_data["elements"]:
            if isinstance(el, dict):
                inp_type = str(el.get("input_type") or "").lower()
                name = str(el.get("name") or "")
                eid = str(el.get("element_id") or "")
                sel = str(el.get("css_selector") or "")
                auto = str(el.get("autocomplete") or "")
                aria = str(el.get("aria_label") or "")
                place = str(el.get("placeholder") or "")
                is_sensitive = (
                    inp_type == "password"
                    or sensitive_pattern.search(name)
                    or sensitive_pattern.search(eid)
                    or sensitive_pattern.search(sel)
                    or sensitive_pattern.search(auto)
                    or sensitive_auto.search(auto)
                    or sensitive_pattern.search(aria)
                    or sensitive_pattern.search(place)
                )
                if is_sensitive:
                    if el.get("value"):
                        el["value"] = "[REDACTED]"

    return {
        "status": "active",
        "session_id": session_id,
        "snapshot": snapshot_data,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Plan Generation (existing, unchanged behaviour with metrics)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/plan", response_model=PlanGraphResponse)
async def create_plan(
    request: GoalRequest,
    current_user: Optional[Dict[str, Any]] = Depends(get_optional_current_user),
):
    """
    Generate a task graph from a natural language goal.
    Persists goal/plan/tasks to MongoDB and optionally starts ARDO.
    Enforces tenant-isolated RAG retrieval when authenticated.
    """
    import time
    from core.metrics import metrics_collector

    if Database.db is not None:
        try:
            logger.info(f"DEBUG: Active Database Name: {Database.db.name}")
            collections = await Database.db.list_collection_names()
            logger.info(f"DEBUG: Active Collections: {collections}")
            await Database.db.goals.insert_one({
                "public_id": f"manual_debug_test_{datetime.utcnow().timestamp()}",
                "goal": request.goal,
                "status": "received",
                "created_at": datetime.utcnow()
            })
            logger.info("DEBUG INSERT SUCCESS → goals")
        except Exception as e:
            logger.error(f"DEBUG INSERT FAILED: {e}")

    user_id = current_user["id"] if current_user else None
    start_t = time.time()
    from ai.input_guard import InputReviewRequiredError, InputDeniedError
    try:
        plan = await generate_task_graph(request.goal, user_id=user_id)
        metrics_collector.record_planning(time.time() - start_t, success=True)
        return plan
    except InputReviewRequiredError as ire:
        metrics_collector.record_planning(time.time() - start_t, success=False)
        gr = ire.guard_result
        raise HTTPException(
            status_code=422,
            detail={
                "status": "review_required",
                "message": gr.reason if gr else str(ire),
                "risk_score": gr.risk_score if gr else 0.5,
                "flagged_patterns": gr.flagged_patterns if gr else [],
                "requires_clarification": True,
            },
        )
    except (InputDeniedError, ValueError) as ve:
        metrics_collector.record_planning(time.time() - start_t, success=False)
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        metrics_collector.record_planning(time.time() - start_t, success=False)
        raise e


# ─────────────────────────────────────────────────────────────────────────────
# ARDO Orchestration
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/orchestrate", response_model=ARDOSessionResponse)
async def start_orchestration(
    request: ARDOStartRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Start a new ARDO orchestration session.

    1. Enforce per-user active session limits and engine capacity.
    2. Generate plan via LLM planner.
    3. Build ARDO DAG from the plan.
    4. Launch RuntimeEngine in background.
    5. Return session_id + initial graph state.
    """
    from core.config import settings
    from ai.input_guard import InputReviewRequiredError, InputDeniedError

    user_id = current_user["id"]

    # 1. Per-user active session capacity check
    user_active_sessions = ARDOScheduler.list_active_sessions(user_id=user_id)
    max_user_sessions = getattr(settings, "MAX_ACTIVE_SESSIONS_PER_USER", 3)
    if len(user_active_sessions) >= max_user_sessions:
        raise HTTPException(
            status_code=429,
            detail=f"Concurrent session limit reached ({len(user_active_sessions)}/{max_user_sessions}). Please wait for an active session to conclude before launching another.",
        )

    # 2. Global concurrency capacity check
    global_active = len(ARDOScheduler._sessions)
    max_global = getattr(settings, "MAX_GLOBAL_CONCURRENT_TASKS", 10)
    if global_active >= max_global:
        raise HTTPException(
            status_code=503,
            detail=f"Engine capacity reached ({global_active}/{max_global} sessions). Please retry shortly.",
        )

    # Step 1: Generate plan
    try:
        plan = await generate_task_graph(request.goal, user_id=user_id)
    except InputReviewRequiredError as ire:
        gr = ire.guard_result
        raise HTTPException(
            status_code=422,
            detail={
                "status": "review_required",
                "message": gr.reason if gr else str(ire),
                "risk_score": gr.risk_score if gr else 0.5,
                "flagged_patterns": gr.flagged_patterns if gr else [],
                "requires_clarification": True,
            },
        )
    except (InputDeniedError, ValueError) as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    # Step 2: Build session
    session_id = f"ardo_{uuid.uuid4().hex[:10]}"
    db = get_database()

    nodes_data = [n.model_dump() if hasattr(n, "model_dump") else n.dict() for n in plan.nodes]
    edges_data = [e.model_dump() if hasattr(e, "model_dump") else e.dict() for e in plan.edges]

    goal_public_id = getattr(plan, "goal_id", None) or plan.public_id or "unknown"
    if db is not None and goal_public_id != "unknown":
        try:
            await db.goals.update_one(
                {"public_id": goal_public_id},
                {"$set": {"status": "executing", "current_step": "executing", "progress_percentage": 25.0, "updated_at": datetime.utcnow()}}
            )
        except Exception as e:
            logger.warning(f"[Router] Could not update goal state to executing: {e}")

    is_fallback = bool(getattr(plan.metadata, "is_fallback", False))
    fallback_reason = getattr(plan.metadata, "fallback_reason", None)
    planning_metadata = plan.metadata.model_dump() if hasattr(plan.metadata, "model_dump") else {}

    session = await ARDOScheduler.start(
        plan_nodes=nodes_data,
        plan_edges=edges_data,
        goal_id=goal_public_id,
        session_id=session_id,
        mode=request.mode,
        db=db,
        safety_decisions=getattr(plan, "safety_decisions", None) or {},
        user_id=user_id,
        is_fallback=is_fallback,
        fallback_reason=fallback_reason,
        planning_metadata=planning_metadata,
    )

    graph = session.graph
    initial_graph = [
        ARDONodeStateResponse(**node.to_dict())
        for node in graph.nodes.values()
    ]

    ARDOScheduler.record_history(
        session_id=session_id,
        goal=request.goal,
        mode=request.mode,
        task_count=len(graph.nodes),
        user_id=user_id,
    )

    return ARDOSessionResponse(
        session_id=session_id,
        mode=request.mode,
        task_count=len(graph.nodes),
        parallel_groups=graph.parallel_groups,
        initial_queue=session.engine._execution_queue[:10],
        initial_graph=initial_graph,
        plan_hash=getattr(graph, "plan_hash", None),
        is_fallback=is_fallback,
        fallback_reason=fallback_reason,
        planning_mode=getattr(plan.metadata, "planning_mode", "deterministic_fallback" if is_fallback else "primary_ai"),
        provider_used=getattr(plan.metadata, "provider_used", None),
        primary_failure_code=getattr(plan.metadata, "primary_failure_code", None),
        validation_passed=bool(getattr(plan.metadata, "validation_passed", True)),
        limitations=list(getattr(plan.metadata, "limitations", []) or []),
        requires_clarification=bool(getattr(plan.metadata, "requires_clarification", False)),
    )


@router.get("/orchestrate/{session_id}/state", response_model=ARDOStateResponse)
async def get_orchestration_state(
    session_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Return the current execution state of an ARDO session."""
    user_id = current_user["id"]
    state = ARDOScheduler.get_state(session_id, user_id=user_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found.")
    return ARDOStateResponse(**state)


@router.post("/orchestrate/{session_id}/approve/{task_id}")
async def approve_task(
    session_id: str,
    task_id: str,
    body: ApproveTaskRequest = ApproveTaskRequest(),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Approve (or reject) a safety-gated task in an ARDO session with cryptographic plan hash verification and tenant isolation."""
    session = ARDOScheduler.get_session(session_id, user_id=current_user["id"])
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found.")

    if body.approved:
        # Cryptographic plan hash validation (fail closed)
        submitted_hash = (body.plan_hash or "").strip()
        if not submitted_hash:
            raise HTTPException(
                status_code=400,
                detail="Missing plan_hash. A valid cryptographic plan hash is required for task approval."
            )

        clean_session_id = str(session_id or "").strip()
        clean_user_id = str(current_user.get("id") or "").strip()
        if not clean_session_id or not clean_user_id:
            raise HTTPException(
                status_code=400,
                detail="Missing mandatory session or user identity for approval issuance."
            )

        # Derive authoritative expected hash directly from current live graph via canonical recomputation
        expected_hash = None
        if hasattr(session.graph, "recompute_plan_hash"):
            expected_hash = session.graph.recompute_plan_hash(
                goal_text=session.goal_id or getattr(session.graph, "goal_text", ""),
                session_id=clean_session_id,
                user_id=clean_user_id,
            )

        if not expected_hash or not str(expected_hash).strip():
            raise HTTPException(
                status_code=400,
                detail="Authoritative plan hash could not be determined for verification. Approval rejected."
            )

        if submitted_hash != expected_hash:
            raise HTTPException(
                status_code=400,
                detail=f"Plan hash mismatch. Provided: {submitted_hash}, expected: {expected_hash}. Plan has been modified or hash is stale."
            )

        # AUD-S3-08: Persist approval record before updating in-memory task state.
        # If persistence fails, fail closed: the task remains unapproved.
        plan_hash = expected_hash
        approval_id = None
        expires_at = None
        try:
            from core.auth_db import AuthDatabase
            auth_db = AuthDatabase.get_instance()
            rec = auth_db.record_plan_approval(
                session_id=session_id,
                user_id=current_user["id"],
                plan_hash=plan_hash,
                status="approved",
                task_id=task_id,
                metadata={"notes": body.notes} if body.notes else {},
            )
            if not rec or not isinstance(rec, dict) or not rec.get("id"):
                raise RuntimeError("AuthDatabase failed to return valid persisted approval record.")
            approval_id = rec.get("id")
            expires_at = rec.get("expires_at")
        except Exception as e:
            logger.error(f"Failed to persist plan approval record: {e}")
            raise HTTPException(
                status_code=500,
                detail="Failed to persist plan approval record. Task remains unapproved."
            )

        # In-memory approval state change executed ONLY after guaranteed database persistence
        session.approve_task(task_id)

        res_data = {
            "status": "approved",
            "task_id": task_id,
            "session_id": session_id,
            "plan_hash": plan_hash,
        }
        if approval_id:
            res_data["approval_id"] = approval_id
        if expires_at:
            res_data["expires_at"] = expires_at
        return res_data
    else:
        # Rejection: block the task
        session.deny_task(task_id)
        plan_hash = getattr(session.graph, "plan_hash", "") or (body.plan_hash or "")
        approval_id = None
        try:
            from core.auth_db import AuthDatabase
            auth_db = AuthDatabase.get_instance()
            rec = auth_db.record_plan_approval(
                session_id=session_id,
                user_id=current_user["id"],
                plan_hash=plan_hash,
                status="rejected",
                task_id=task_id,
                metadata={"notes": body.notes} if body.notes else {},
            )
            if rec and isinstance(rec, dict):
                approval_id = rec.get("id")
        except Exception as e:
            logger.warning(f"Failed to persist plan rejection record: {e}")

        res_data = {
            "status": "rejected",
            "task_id": task_id,
            "session_id": session_id,
            "plan_hash": plan_hash,
        }
        if approval_id:
            res_data["approval_id"] = approval_id
        return res_data


@router.post("/orchestrate/{session_id}/resume")
async def resume_orchestration(
    session_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Resume an interrupted ARDO orchestration session from its database checkpoint.
    Enforces multi-user tenant isolation.
    """
    # 1. Tenant ownership validation on active session in memory
    active_session = ARDOScheduler._sessions.get(session_id)
    if active_session and (not active_session.user_id or active_session.user_id != current_user["id"]):
        raise HTTPException(status_code=403, detail="Cross-user resume forbidden.")

    # 2. Check checkpoint ownership in DB if present
    db = get_database()
    from orchestration.checkpoint_manager import ExecutionCheckpointManager
    checkpoint = await ExecutionCheckpointManager.load_latest_checkpoint(session_id, db=db)
    if not checkpoint and not active_session:
        raise HTTPException(status_code=404, detail=f"No checkpoint found for session {session_id}.")

    if checkpoint:
        chk_user = checkpoint.get("user_id")
        if not chk_user or chk_user != current_user["id"]:
            raise HTTPException(status_code=403, detail="Cross-user resume forbidden.")

    worker_id = f"worker_{current_user['id'][:8]}_{uuid.uuid4().hex[:6]}"

    try:
        session = await ARDOScheduler.resume(
            session_id, user_id=current_user["id"], db=db, worker_id=worker_id
        )
    except PermissionError as pe:
        raise HTTPException(status_code=403, detail=str(pe))
    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))
    except RuntimeError as re:
        raise HTTPException(status_code=409, detail=str(re))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to resume session: {e}")

    from orchestration.state_machine import TaskState
    completed_count = sum(1 for n in session.graph.nodes.values() if n.status == TaskState.COMPLETED)
    total_count = len(session.graph.nodes)
    remaining_count = total_count - completed_count

    return {
        "status": "resumed",
        "session_id": session_id,
        "worker_id": worker_id,
        "plan_hash": session.graph.plan_hash,
        "restored_completed_tasks": completed_count,
        "remaining_tasks": remaining_count,
        "total_tasks": total_count,
    }


@router.post("/orchestrate/{session_id}/cancel")
async def cancel_orchestration(
    session_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Safely cancel an ARDO execution session with tenant ownership validation."""
    success = await ARDOScheduler.cancel_session(session_id, user_id=current_user["id"])
    if not success:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found or access denied.")
    return {"session_id": session_id, "status": "cancelled"}


@router.get("/orchestrate/{session_id}/metrics")
async def get_metrics(
    session_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Return execution metrics for a session."""
    session = ARDOScheduler.get_session(session_id, user_id=current_user["id"])
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found.")
    return session.engine.monitor.get_metrics()


@router.get("/orchestrate/{session_id}/explain/{task_id}")
async def get_scheduling_explanation(
    session_id: str,
    task_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Return the full scheduling explanation for a specific task."""
    session = ARDOScheduler.get_session(session_id, user_id=current_user["id"])
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found.")
    explanations = session.get_all_explanations_for_task(task_id)
    if not explanations:
        raise HTTPException(
            status_code=404,
            detail=f"No explanation found for task {task_id} in session {session_id}."
        )
    return {
        "task_id": task_id,
        "session_id": session_id,
        "explanations": explanations,
        "latest": explanations[-1] if explanations else None,
    }


@router.get("/orchestrate/{session_id}/why-now")
async def get_why_now(
    session_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Return the active 'Why This Task Now?' context."""
    session = ARDOScheduler.get_session(session_id, user_id=current_user["id"])
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found.")
    why_now = session.get_why_now()
    if not why_now:
        return {"message": "No active task context available yet."}
    return why_now


@router.get("/orchestrate/sessions")
async def list_sessions(current_user: Dict[str, Any] = Depends(get_current_user)):
    """List all active ARDO session IDs."""
    return {"sessions": ARDOScheduler.list_active_sessions(user_id=current_user["id"])}


@router.get("/orchestrate/history")
async def get_orchestration_history(current_user: Dict[str, Any] = Depends(get_current_user)):
    """Return historical ARDO execution records."""
    return {"history": ARDOScheduler.get_history(user_id=current_user["id"])}


@router.get("/orchestrate/{session_id}/evidence")
async def get_session_evidence(
    session_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Return browser execution artifacts and screenshot evidence with ownership check."""
    evidence = ARDOScheduler.get_evidence(session_id, user_id=current_user["id"])
    if not evidence:
        raise HTTPException(status_code=404, detail=f"No evidence found for session {session_id} or access denied.")
    return evidence


@router.get("/orchestrate/{session_id}/verify")
async def get_session_goal_verification(
    session_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Return goal outcome verification for a session with ownership validation."""
    verif = ARDOScheduler.get_goal_verification(session_id, user_id=current_user["id"])
    if not verif:
        raise HTTPException(status_code=404, detail=f"No verification found for session {session_id} or access denied.")
    return verif


@router.post("/orchestrate/replay/{session_id}")
async def replay_orchestration(
    session_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Trigger a deterministic replay of a historical orchestration session.
    Reconstructs the timeline without executing external actions.
    """
    active_session = ARDOScheduler._sessions.get(session_id)
    if active_session and active_session.user_id is not None and active_session.user_id != current_user["id"]:
        raise HTTPException(status_code=403, detail="Forbidden: Cross-user replay not permitted.")

    for h in ARDOScheduler._session_history:
        if h.get("session_id") == session_id and h.get("user_id") is not None and h.get("user_id") != current_user["id"]:
            raise HTTPException(status_code=403, detail="Forbidden: Cross-user replay not permitted.")

    db = get_database()
    if db is None:
        if active_session:
            return {"status": "replaying", "session_id": session_id}
        raise HTTPException(status_code=404, detail=f"No replay found for {session_id}")

    replay_root = await db.replay_sessions.find_one({"session_id": session_id})
    if not replay_root:
        if active_session:
            return {"status": "replaying", "session_id": session_id}
        raise HTTPException(status_code=404, detail=f"No replay found for {session_id}")

    if replay_root.get("user_id") and replay_root["user_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Cross-user replay forbidden.")

    return {
        "status": "replay_initialized",
        "session_id": session_id,
        "mode": "replay",
        "message": "Connect to WebSocket to stream replay events."
    }


# ─────────────────────────────────────────────────────────────────────────────
# Phase 6: Knowledge Base & RAG Endpoints
# ─────────────────────────────────────────────────────────────────────────────

class IngestTextRequest(BaseModel):
    title: str = Field(..., description="Document title")
    content: str = Field(..., description="Document text content to chunk and index")
    source: str = Field(default="api_input", description="Source identifier")
    category: str = Field(default="playbook", description="Knowledge category")
    metadata: dict = Field(default_factory=dict)


@router.get("/knowledge/search")
def search_knowledge(
    q: str,
    top_k: int = 4,
    min_score: float = 0.35,
    category: str | None = None,
):
    """
    Search the PostgreSQL + pgvector knowledge base for relevant context chunks.
    """
    from knowledge.rag_service import RAGService
    filter_meta = {"category": category} if category else None
    rag = RAGService.get_instance()
    res = rag.retriever.retrieve(query=q, top_k=top_k, min_score=min_score, filter_metadata=filter_meta)
    return res.model_dump()


@router.get("/knowledge/stats")
def knowledge_stats():
    """
    Returns knowledge base and semantic memory statistics including embedding fallback status.
    """
    from knowledge.rag_service import RAGService
    from knowledge.embeddings import HybridEmbeddingService
    rag = RAGService.get_instance()
    is_pg = getattr(rag.vector_store, "_is_pg_connected", False)
    diag = HybridEmbeddingService.get_diagnostics()
    return {
        "vector_backend": "postgresql_pgvector",
        "database_mode": "postgresql" if is_pg else "fallback",
        "postgres_connected": is_pg,
        "pgvector_enabled": is_pg,
        "total_knowledge_chunks": rag.vector_store.count(),
        "total_historical_memories": rag.memory_store.count() if rag.memory_store else 0,
        "embedding_dimension": getattr(rag.embeddings, "active_dimension", 128),
        "is_cloud_embedding": getattr(rag.embeddings, "is_using_cloud", False),
        "embedding_provider": "openai" if getattr(rag.embeddings, "is_using_cloud", False) else "deterministic_local",
        "embedding_fallback_active": diag["embedding_fallback_active"],
        "openai_quota_exhausted": diag["openai_quota_exhausted"],
        "last_embedding_error": diag["last_embedding_error"],
        "last_fallback_reason": diag["last_fallback_reason"],
    }


@router.get("/knowledge/diagnostics")
def knowledge_diagnostics():
    """
    Returns diagnostic information about active vector storage, PostgreSQL, and embedding fallback status.
    Never exposes passwords or sensitive credentials.
    """
    from knowledge.rag_service import RAGService
    from knowledge.embeddings import HybridEmbeddingService
    rag = RAGService.get_instance()
    store = rag.vector_store
    raw_uri = getattr(store, "connection_uri", "") or ""
    host = "localhost"
    port = 5432
    dbname = "ai_goal_planner"
    if "@" in raw_uri:
        try:
            after_at = raw_uri.split("@", 1)[1]
            host_port_db = after_at.split("/", 1)
            host_port = host_port_db[0].split(":")
            host = host_port[0]
            port = int(host_port[1]) if len(host_port) > 1 else 5432
            dbname = host_port_db[1].split("?")[0] if len(host_port_db) > 1 else dbname
        except Exception:
            pass

    is_pg = getattr(store, "_is_pg_connected", False)
    diag = HybridEmbeddingService.get_diagnostics()
    return {
        "database_mode": "postgresql" if is_pg else "fallback",
        "postgres_connected": is_pg,
        "pgvector_enabled": is_pg,
        "database_host": host,
        "database_port": port,
        "database_name": dbname,
        "table_name": getattr(store, "table_name", "knowledge_chunks"),
        "total_knowledge_chunks": store.count(),
        "total_historical_memories": rag.memory_store.count() if rag.memory_store else 0,
        "embedding_dimension": getattr(rag.embeddings, "active_dimension", 128),
        "is_cloud_embedding": getattr(rag.embeddings, "is_using_cloud", False),
        "embedding_provider": "openai" if getattr(rag.embeddings, "is_using_cloud", False) else "deterministic_local",
        "embedding_fallback_active": diag["embedding_fallback_active"],
        "openai_quota_exhausted": diag["openai_quota_exhausted"],
        "last_embedding_error": diag["last_embedding_error"],
        "last_fallback_reason": diag["last_fallback_reason"],
    }


@router.get("/knowledge/memories")
def search_memories(
    q: str,
    top_k: int = 3,
    min_score: float = 0.35,
    memory_type: str | None = None,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Search historical execution memories with tenant isolation.
    """
    from knowledge.rag_service import RAGService
    rag = RAGService.get_instance()
    if not rag.memory_store:
        return []
    memories = rag.memory_store.retrieve_memories(
        goal_query=q,
        top_k=top_k,
        min_score=min_score,
        memory_type=memory_type,
        user_id=current_user["id"],
    )
    return [m.model_dump() for m in memories]


@router.get("/knowledge/memories/{memory_id}")
def get_memory_by_id_endpoint(
    memory_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Retrieve a specific historical execution memory by ID with strict tenant isolation.
    """
    from knowledge.rag_service import RAGService
    rag = RAGService.get_instance()
    if not rag.memory_store:
        raise HTTPException(status_code=404, detail=f"Memory '{memory_id}' not found.")
    mem = rag.memory_store.get_memory_by_id(memory_id=memory_id, user_id=current_user["id"])
    if not mem:
        raise HTTPException(status_code=404, detail=f"Memory '{memory_id}' not found.")
    return mem.model_dump()


@router.delete("/knowledge/memories/{memory_id}")
def delete_memory_by_id_endpoint(
    memory_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Delete or invalidate a tenant-owned memory record by ID.
    """
    from knowledge.rag_service import RAGService
    rag = RAGService.get_instance()
    if not rag.memory_store:
        raise HTTPException(status_code=404, detail=f"Memory '{memory_id}' not found.")
    deleted = rag.memory_store.delete_memory(memory_id=memory_id, user_id=current_user["id"])
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Memory '{memory_id}' not found.")
    return {"status": "deleted", "memory_id": memory_id}


@router.post("/knowledge/ingest")
def ingest_knowledge(
    req: IngestTextRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Ingest a document into the PostgreSQL + pgvector knowledge store with tenant binding.
    """
    from knowledge.rag_service import RAGService
    rag = RAGService.get_instance()
    clean_meta = dict(req.metadata or {})
    clean_meta["user_id"] = current_user["id"]
    report = rag.ingest_raw_text(
        title=req.title,
        content=req.content,
        source=req.source,
        category=req.category,
        metadata=clean_meta,
        user_id=current_user["id"],
    )
    return report.model_dump()


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3: Playbooks & Controlled Learning Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/playbooks")
async def list_playbooks(
    category: Optional[str] = None,
    status: Optional[str] = None,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """List user-owned playbooks with optional category and status filtering."""
    from knowledge.playbook_service import PlaybookService
    playbooks = PlaybookService.get_playbooks_for_user(
        user_id=current_user["id"],
        category=category,
        status=status,
    )
    return {"playbooks": playbooks, "total": len(playbooks)}


@router.get("/playbooks/{playbook_id}")
async def get_playbook_endpoint(
    playbook_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Retrieve a single tenant-owned playbook by ID."""
    from knowledge.playbook_service import PlaybookService
    pb = PlaybookService.get_playbook_by_id(playbook_id=playbook_id, user_id=current_user["id"])
    if not pb:
        raise HTTPException(status_code=404, detail=f"Playbook '{playbook_id}' not found.")
    return pb


@router.post("/playbooks/{playbook_id}/promote")
async def promote_playbook(
    playbook_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Promote a candidate playbook to promoted status for production reuse."""
    from knowledge.playbook_service import PlaybookService
    promoted = PlaybookService.promote_candidate(
        playbook_id=playbook_id,
        user_id=current_user["id"],
    )
    if not promoted:
        raise HTTPException(status_code=400, detail=f"Failed to promote playbook {playbook_id}. Ensure it exists and is owned by you.")
    return {"status": "promoted", "playbook_id": playbook_id}


@router.post("/playbooks/{playbook_id}/deprecate")
async def deprecate_playbook(
    playbook_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Deprecate a playbook to prevent future contextual reuse."""
    from knowledge.playbook_service import PlaybookService
    deprecated = PlaybookService.deprecate_playbook(
        playbook_id=playbook_id,
        user_id=current_user["id"],
    )
    if not deprecated:
        raise HTTPException(status_code=400, detail=f"Failed to deprecate playbook {playbook_id}. Ensure it exists and is owned by you.")
    return {"status": "deprecated", "playbook_id": playbook_id}


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket — Real-Time Event Stream
# ─────────────────────────────────────────────────────────────────────────────

@router.websocket("/ws/orchestrate/{session_id}")
async def orchestration_websocket(websocket: WebSocket, session_id: str):
    """
    WebSocket endpoint for real-time ARDO event streaming.
    Requires ticket or valid session cookie. Rejects unauthenticated connections.
    Enforces Origin header validation against trusted origins to prevent CSWSH attacks.
    """
    from core.security import ws_ticket_manager
    from core.auth_db import AuthDatabase
    from core.config import settings

    # 1. Origin header validation (Protection against Cross-Site WebSocket Hijacking)
    origin = websocket.headers.get("origin")
    if origin:
        trusted_origins = {
            o.strip()
            for o in (settings.ALLOWED_ORIGINS + "," + getattr(settings, "CSRF_TRUSTED_ORIGINS", "")).split(",")
            if o.strip() and o.strip() != "*"
        }
        if origin not in trusted_origins:
            logger.warning(f"[WS] Rejecting WebSocket connection from untrusted origin: '{origin}'")
            await websocket.close(code=1008)
            return

    ticket = websocket.query_params.get("ticket")
    user_id = None
    if ticket:
        user_id = ws_ticket_manager.redeem_ticket(ticket, expected_session_id=session_id)
        if not user_id:
            await websocket.close(code=1008)
            return
    else:
        cookie_name = getattr(settings, "SESSION_COOKIE_NAME", "ardo_session")
        cookie_token = websocket.cookies.get(cookie_name) or websocket.cookies.get("ardo_session")
        if cookie_token:
            auth_db = AuthDatabase.get_instance()
            sess = auth_db.get_session_by_token(cookie_token)
            if sess:
                user_id = sess.get("user_id")

    if not user_id:
        await websocket.close(code=1008)
        return

    active_session = ARDOScheduler.get_session(session_id, user_id=user_id)
    if not active_session:
        logger.warning(f"[WS] Rejecting connection: session {session_id} not active or not owned by user {user_id}")
        await websocket.close(code=1008)
        return

    await websocket.accept()
    logger.info(f"[WS] Client connected to session: {session_id} (user={user_id})")

    try:
        async for event_dict in ARDOScheduler.stream_events(session_id, user_id=user_id):
            await websocket.send_json(event_dict)
            if event_dict.get("event_type") in ("session_complete", "session_failed"):
                break
    except WebSocketDisconnect:
        logger.info(f"[WS] Client disconnected from session: {session_id}")
    except Exception as e:
        logger.error(f"[WS] Error in session {session_id}: {e}")
        try:
            await websocket.send_json({"error": str(e), "session_id": session_id})
        except Exception:
            pass
    finally:
        logger.info(f"[WS] Session {session_id} WebSocket closed.")
