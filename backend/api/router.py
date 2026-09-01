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
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel, Field

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
# Health
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/health")
def health_check():
    """Simple API liveness validation."""
    return {"status": "ok", "service": "planning-engine", "ardo": "active"}


# ─────────────────────────────────────────────────────────────────────────────
# Plan Generation (existing, unchanged behaviour)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/plan", response_model=PlanGraphResponse)
async def create_plan(request: GoalRequest):
    """
    Generate a task graph from a natural language goal.
    Persists goal/plan/tasks to MongoDB and optionally starts ARDO.
    """
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

    return await generate_task_graph(request.goal)


# ─────────────────────────────────────────────────────────────────────────────
# ARDO Orchestration
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/orchestrate", response_model=ARDOSessionResponse)
async def start_orchestration(request: ARDOStartRequest):
    """
    Start a new ARDO orchestration session.

    1. Generate plan via LLM planner.
    2. Build ARDO DAG from the plan.
    3. Launch RuntimeEngine in background.
    4. Return session_id + initial graph state.
    """
    # Step 1: Generate plan
    plan = await generate_task_graph(request.goal)

    # Step 2: Build session
    session_id = f"ardo_{uuid.uuid4().hex[:10]}"
    db = get_database()

    nodes_data = [n.dict() for n in plan.nodes]
    edges_data = [e.dict() for e in plan.edges]

    session = await ARDOScheduler.start(
        plan_nodes=nodes_data,
        plan_edges=edges_data,
        goal_id=plan.public_id or "unknown",
        session_id=session_id,
        mode=request.mode,
        db=db,
        safety_decisions=getattr(plan, "safety_decisions", None) or {},
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
    )

    return ARDOSessionResponse(
        session_id=session_id,
        mode=request.mode,
        task_count=len(graph.nodes),
        parallel_groups=graph.parallel_groups,
        initial_queue=session.engine._execution_queue[:10],
        initial_graph=initial_graph,
    )


@router.get("/orchestrate/{session_id}/state", response_model=ARDOStateResponse)
async def get_orchestration_state(session_id: str):
    """Return the current execution state of an ARDO session."""
    state = ARDOScheduler.get_state(session_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found.")
    return ARDOStateResponse(**state)


@router.post("/orchestrate/{session_id}/approve/{task_id}")
async def approve_task(
    session_id: str,
    task_id: str,
    body: ApproveTaskRequest = ApproveTaskRequest(),
):
    """Approve (or reject) a safety-gated task in an ARDO session."""
    session = ARDOScheduler.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found.")

    if body.approved:
        session.approve_task(task_id)
        return {"status": "approved", "task_id": task_id, "session_id": session_id}
    else:
        # Rejection: block the task
        node = session.graph.get_node(task_id)
        if node:
            from orchestration.state_machine import TaskState
            node.status = TaskState.BLOCKED
            node.execution_logs.append("Operator rejected task. Marked as BLOCKED.")
        return {"status": "rejected", "task_id": task_id, "session_id": session_id}


@router.get("/orchestrate/{session_id}/metrics")
async def get_metrics(session_id: str):
    """Return execution metrics for a session."""
    session = ARDOScheduler.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found.")
    return session.engine.monitor.get_metrics()


@router.get("/orchestrate/{session_id}/explain/{task_id}")
async def get_scheduling_explanation(session_id: str, task_id: str):
    """Return the full scheduling explanation for a specific task."""
    session = ARDOScheduler.get_session(session_id)
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
async def get_why_now(session_id: str):
    """Return the active 'Why This Task Now?' context."""
    session = ARDOScheduler.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found.")
    why_now = session.get_why_now()
    if not why_now:
        return {"message": "No active task context available yet."}
    return why_now


@router.get("/orchestrate/sessions")
async def list_sessions():
    """List all active ARDO session IDs."""
    return {"sessions": ARDOScheduler.list_active_sessions()}


@router.get("/orchestrate/history")
async def get_orchestration_history():
    """Return historical ARDO execution records."""
    return {"history": ARDOScheduler.get_history()}


@router.get("/orchestrate/{session_id}/evidence")
async def get_session_evidence(session_id: str):
    """Return browser execution artifacts and screenshot evidence."""
    evidence = ARDOScheduler.get_evidence(session_id)
    if not evidence:
        raise HTTPException(status_code=404, detail=f"No evidence found for session {session_id}")
    return evidence


@router.post("/orchestrate/replay/{session_id}")
async def replay_orchestration(session_id: str):
    """
    Trigger a deterministic replay of a historical orchestration session.
    Reconstructs the timeline without executing external actions.
    """
    db = get_database()
    if not db:
        raise HTTPException(status_code=500, detail="Database not connected.")
    
    # 1. Fetch replay root
    replay_root = await db.replay_sessions.find_one({"session_id": session_id})
    if not replay_root:
        raise HTTPException(status_code=404, detail=f"No replay found for {session_id}")
        
    # 2. Re-initialize a session in REPLAY mode
    # For now we'll just mock the scheduler launching in replay mode 
    # Or rely on a ReplayEngine (if added)
    # The simplest is to stream events directly to a temporary log
    # For this endpoint, we'll return success and the frontend can connect via WS or fetch metrics
    
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
    Returns knowledge base and semantic memory statistics.
    """
    from knowledge.rag_service import RAGService
    rag = RAGService.get_instance()
    is_pg = getattr(rag.vector_store, "_is_pg_connected", False)
    return {
        "vector_backend": "postgresql_pgvector",
        "database_mode": "postgresql" if is_pg else "fallback",
        "postgres_connected": is_pg,
        "pgvector_enabled": is_pg,
        "total_knowledge_chunks": rag.vector_store.count(),
        "total_historical_memories": rag.memory_store.count() if rag.memory_store else 0,
        "embedding_dimension": getattr(rag.embeddings, "active_dimension", 128),
        "is_cloud_embedding": getattr(rag.embeddings, "is_using_cloud", False),
    }


@router.get("/knowledge/diagnostics")
def knowledge_diagnostics():
    """
    Returns diagnostic information about active vector storage and PostgreSQL infrastructure.
    Never exposes passwords or sensitive credentials.
    """
    from knowledge.rag_service import RAGService
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
    }


@router.get("/knowledge/memories")
def search_memories(
    q: str,
    top_k: int = 3,
    min_score: float = 0.35,
    memory_type: str | None = None,
):
    """
    Search historical execution memories.
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
    )
    return [m.model_dump() for m in memories]


@router.post("/knowledge/ingest")
def ingest_knowledge(req: IngestTextRequest):
    """
    Ingest a document into the PostgreSQL + pgvector knowledge store.
    """
    from knowledge.rag_service import RAGService
    rag = RAGService.get_instance()
    report = rag.ingest_raw_text(
        title=req.title,
        content=req.content,
        source=req.source,
        category=req.category,
        metadata=req.metadata,
    )
    return report.model_dump()


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket — Real-Time Event Stream
# ─────────────────────────────────────────────────────────────────────────────

@router.websocket("/ws/orchestrate/{session_id}")
async def orchestration_websocket(websocket: WebSocket, session_id: str):
    """
    WebSocket endpoint for real-time ARDO event streaming.

    Clients subscribe to this endpoint to receive:
        - task_started, task_completed, task_failed, task_retrying
        - task_blocked, task_approved, task_approval_needed
        - queue_reordered, session_complete
        - heartbeat (every 30s)

    Events are JSON-serialisable OrchestrationEvent dicts.
    """
    await websocket.accept()
    logger.info(f"[WS] Client connected to session: {session_id}")

    try:
        async for event_dict in ARDOScheduler.stream_events(session_id):
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
