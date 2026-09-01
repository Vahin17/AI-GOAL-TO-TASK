import logging
import uuid
from datetime import datetime
from schemas.models import PlanGraphResponse
from core.config import settings
from ai.fallback_rules import generate_fallback_plan
from core.database import get_database
from orchestration.safety_engine import SafetyEngine
from knowledge.rag_service import RAGService

logger = logging.getLogger(__name__)

# Lazy-load the provider based on configuration settings
def get_llm_provider():
    if settings.LLM_PROVIDER.lower() == "langchain":
        from ai.langchain_provider import LangChainProvider
        return LangChainProvider()
    else:
        from ai.langchain_provider import LangChainProvider
        return LangChainProvider()

async def generate_task_graph(goal_text: str) -> PlanGraphResponse:
    """
    Main orchestration controller:
    1. Retrieves relevant domain knowledge via RAG (Phase 6).
    2. Decomposes goal using LLM with injected RAG context.
    3. Evaluates batch safety via SafetyEngine (Phase 5).
    4. Persists goal -> plan -> tasks.
    """
    db = get_database()
    
    # 1. Initialize Goal Record
    goal_public_id = f"goal_{uuid.uuid4().hex[:8]}"
    goal_record = {
        "public_id": goal_public_id,
        "goal": goal_text,
        "status": "processing",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "current_step": "planning",
        "progress_percentage": 0.1
    }
    
    # 2. Phase 6: Knowledge Retrieval (RAG)
    rag_context = ""
    provenance = None
    if getattr(settings, "ENABLE_RAG", True):
        try:
            rag_service = RAGService.get_instance()
            rag_context, provenance_obj = rag_service.get_relevant_context_for_goal(goal_text)
            provenance = provenance_obj.model_dump()
            logger.info(f"[Orchestrator] Retrieved RAG context ({provenance_obj.chunk_count} chunks, {provenance_obj.context_char_count} chars, latency: {provenance_obj.retrieval_latency_ms}ms)")
        except Exception as e:
            logger.warning(f"[Orchestrator] RAG context retrieval failed (non-fatal): {e}")
            provenance = {"status": "error", "error_message": str(e)}

    try:
        if db is not None:
            logger.info(f"Inserting goal into 'goals' collection: {goal_public_id}")
            await db.goals.insert_one(goal_record)
            
        # 3. Generate Plan logic with RAG context
        provider = get_llm_provider()
        plan_response: PlanGraphResponse = await provider.generate_plan(goal_text, rag_context=rag_context)
    except Exception as e:
        logger.error(f"Error during generative AI planning: {e}. Executing fallback sequence.")
        plan_response = await generate_fallback_plan(goal_text)

    # Attach RAG provenance to response
    if provenance:
        plan_response.provenance = provenance
        if "sources_used" in provenance:
            plan_response.metadata.knowledge_sources = provenance.get("sources_used", [])
            plan_response.metadata.knowledge_chunk_count = provenance.get("chunk_count", 0)

    # 4. Phase 5: Batch Safety Evaluation of generated plan
    plan_public_id = f"plan_{uuid.uuid4().hex[:8]}"
    plan_response.public_id = plan_public_id

    tasks_for_safety = [
        {"task_id": node.id, "task_name": node.data.get("label", "Unnamed Task")}
        for node in plan_response.nodes
    ]
    goal_id_for_safety = goal_record.get("public_id", "unknown")
    session_id_for_safety = "plan_eval"

    safety_engine = SafetyEngine(db=db)
    try:
        safety_decisions = await safety_engine.evaluate_plan(
            goal=goal_text,
            plan_nodes=tasks_for_safety,
            session_id=session_id_for_safety,
            goal_id=goal_id_for_safety,
            plan_id=plan_public_id,
        )
        plan_response.safety_decisions = safety_decisions
        logger.info(f"[Orchestrator] Safety evaluation complete: {len(safety_decisions)} tasks evaluated.")
    except Exception as e:
        logger.warning(f"[Orchestrator] Safety evaluation failed (non-fatal): {e}")
        plan_response.safety_decisions = {}
    
    plan_record = {
        "public_id": plan_public_id,
        "goal_id": goal_public_id,
        "nodes": [node.dict() for node in plan_response.nodes],
        "edges": [edge.dict() for edge in plan_response.edges],
        "metadata": plan_response.metadata.dict(),
        "reasoning_summary": plan_response.metadata.reasoning_summary,
        "confidence": plan_response.metadata.confidence,
        "provenance": provenance,
        "status": "generated",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "progress_percentage": 100.0
    }
    
    # 5. Decompose into Tasks & Executions
    task_records = []
    for i, node in enumerate(plan_response.nodes):
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        t_record = {
            "public_id": task_id,
            "task_id": node.id,
            "plan_id": plan_public_id,
            "step_number": i + 1,
            "title": node.data.get("label", "Untitled Task"),
            "reasoning": plan_response.metadata.reasoning_summary,
            "eta": "15m",
            "status": "queued",
            "depends_on": [],
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }
        task_records.append(t_record)
        
        # Initialize Execution record if database is connected
        if db is not None:
            exec_id = f"exec_{uuid.uuid4().hex[:8]}"
            logger.info(f"Initializing execution in 'executions' collection for task_id: {task_id}")
            await db.executions.insert_one({
                "public_id": exec_id,
                "task_id": task_id,
                "status": "initialized",
                "logs": [],
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow()
            })

    if db is not None:
        logger.info(f"Inserting plan into 'plans' collection: {plan_public_id}")
        await db.plans.insert_one(plan_record)
        
        if task_records:
            logger.info(f"Inserting {len(task_records)} tasks into 'tasks' collection")
            await db.tasks.insert_many(task_records)
        
        logger.info(f"Updating goal status to 'completed' in 'goals' collection: {goal_public_id}")
        await db.goals.update_one(
            {"public_id": goal_public_id},
            {"$set": {"status": "completed", "progress_percentage": 100.0, "updated_at": datetime.utcnow()}}
        )

    return plan_response
