import logging
import uuid
from datetime import datetime
from schemas.models import PlanGraphResponse
from core.config import settings
from ai.fallback_rules import generate_fallback_plan
from core.database import get_database
from orchestration.safety_engine import SafetyEngine
from knowledge.rag_service import RAGService
from ai.input_guard import InputGuard, InputGuardVerdict
from ai.plan_critic import PlanCritic
from orchestration.plan_hasher import compute_plan_hash

logger = logging.getLogger(__name__)

# Lazy-load the provider based on configuration settings
def get_llm_provider():
    if settings.LLM_PROVIDER.lower() == "gemini" or bool(settings.GEMINI_API_KEY):
        from ai.gemini_provider import GeminiProvider
        return GeminiProvider()
    elif bool(settings.OPENAI_API_KEY):
        from ai.langchain_provider import LangChainProvider
        return LangChainProvider()
    else:
        from ai.gemini_provider import GeminiProvider
        return GeminiProvider()

async def generate_task_graph(goal_text: str) -> PlanGraphResponse:
    """
    Main orchestration controller:
    1. Validates and sanitizes input goal via InputGuard (Phase 1).
    2. Retrieves relevant domain knowledge via Hybrid RAG (Phase 1).
    3. Decomposes goal using LLM with injected RAG context and untrusted tags.
    4. Evaluates plan validity via Independent Plan Critic with bounded retry (Phase 1).
    5. Evaluates batch safety via SafetyEngine (Phase 5).
    6. Generates canonical deterministic plan hash (Phase 1).
    7. Persists goal -> plan -> tasks.
    """
    db = get_database()
    
    # 1. Phase 1: Input Guard Validation
    guard_result = InputGuard.validate_goal(goal_text)
    if guard_result.verdict == InputGuardVerdict.DENY:
        logger.warning(f"[InputGuard] Blocked goal '{goal_text[:50]}': {guard_result.reason}")
        raise ValueError(f"Input rejected by safety guard: {guard_result.reason}")
    if guard_result.verdict == InputGuardVerdict.REVIEW:
        logger.warning(f"[InputGuard] Review required for goal '{goal_text[:50]}': {guard_result.reason}")
        raise ValueError(f"Review required: {guard_result.reason}")

    sanitized_goal = guard_result.sanitized_goal

    # Initialize Goal Record
    goal_public_id = f"goal_{uuid.uuid4().hex[:8]}"
    goal_record = {
        "public_id": goal_public_id,
        "goal": sanitized_goal,
        "raw_goal": goal_text,
        "input_guard": guard_result.model_dump(),
        "status": "planning",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "current_step": "planning",
        "progress_percentage": 10.0
    }
    
    # 2. Phase 1: Hybrid Knowledge Retrieval (RAG)
    rag_context = ""
    provenance = None
    if getattr(settings, "ENABLE_RAG", True):
        try:
            rag_service = RAGService.get_instance()
            rag_context, provenance_obj = rag_service.get_relevant_context_for_goal(sanitized_goal)
            provenance = provenance_obj.model_dump()
            logger.info(f"[Orchestrator] Retrieved Hybrid RAG context ({provenance_obj.chunk_count} chunks, {provenance_obj.context_char_count} chars, latency: {provenance_obj.retrieval_latency_ms}ms)")
        except Exception as e:
            logger.warning(f"[Orchestrator] RAG context retrieval failed (non-fatal): {e}")
            provenance = {"status": "error", "error_message": str(e)}

    # 3. Phase 1: LLM Planning with Bounded Critic Verification
    plan_response: Optional[PlanGraphResponse] = None
    max_critic_attempts = 2
    attempt = 0
    critic_feedback = None

    provider = get_llm_provider()

    while attempt < max_critic_attempts:
        attempt += 1
        try:
            if db is not None and attempt == 1:
                logger.info(f"Inserting goal into 'goals' collection: {goal_public_id}")
                await db.goals.insert_one(goal_record)

            plan_response = await provider.generate_plan(
                sanitized_goal,
                rag_context=rag_context,
                critic_feedback=critic_feedback,
            )
            logger.info(f"[LLM-PLAN] provider={settings.LLM_PROVIDER} model={settings.MODEL_NAME} inference=SUCCESS attempt={attempt} nodes={len(plan_response.nodes)}")
        except Exception as e:
            logger.error(f"Error during generative AI planning (attempt {attempt}): {e}. Executing fallback sequence.")
            logger.info(f"[LLM-PLAN] provider={settings.LLM_PROVIDER} model={settings.MODEL_NAME} inference=FALLBACK_ENGAGED")
            plan_response = await generate_fallback_plan(sanitized_goal)
            break

        # Independent Plan Critic Pass
        critic_result = PlanCritic.evaluate_grounding_and_relevance(
            goal=sanitized_goal,
            nodes=plan_response.nodes,
            edges=plan_response.edges,
            rag_context=rag_context,
        )
        plan_response.critic_result = critic_result.model_dump()

        if not critic_result.requires_regeneration or attempt >= max_critic_attempts:
            if critic_result.requires_regeneration:
                logger.warning(f"[PlanCritic] Critic issues remaining after attempt {attempt}: {critic_result.summary}")
            else:
                logger.info(f"[PlanCritic] Verification passed: {critic_result.summary}")
            break
        else:
            logger.warning(f"[PlanCritic] Plan rejected on attempt {attempt}: {critic_result.summary}. Retrying with feedback.")
            critic_feedback = critic_result.regeneration_prompt_feedback

    if plan_response is None:
        plan_response = await generate_fallback_plan(sanitized_goal)

    # Attach RAG provenance to response
    if provenance:
        plan_response.provenance = provenance
        if "sources_used" in provenance:
            plan_response.metadata.knowledge_sources = provenance.get("sources_used", [])
            plan_response.metadata.knowledge_chunk_count = provenance.get("chunk_count", 0)

    # 4. Phase 5: Batch Safety Evaluation of generated plan
    plan_public_id = f"plan_{uuid.uuid4().hex[:8]}"
    plan_response.public_id = plan_public_id
    plan_response.goal_id = goal_public_id

    tasks_for_safety = [
        {"task_id": node.id, "task_name": node.data.get("label", "Unnamed Task")}
        for node in plan_response.nodes
    ]
    goal_id_for_safety = goal_record.get("public_id", "unknown")
    session_id_for_safety = "plan_eval"

    safety_engine = SafetyEngine(db=db)
    try:
        safety_decisions = await safety_engine.evaluate_plan(
            goal=sanitized_goal,
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

    # 5. Phase 1: Canonical Deterministic Plan Hashing
    plan_hash = compute_plan_hash(
        nodes=plan_response.nodes,
        edges=plan_response.edges,
        intent=getattr(plan_response.metadata, "intent", sanitized_goal),
    )
    plan_response.plan_hash = plan_hash
    logger.info(f"[PlanHasher] Canonical plan_hash={plan_hash[:12]}...")

    plan_record = {
        "public_id": plan_public_id,
        "goal_id": goal_public_id,
        "plan_hash": plan_hash,
        "critic_result": plan_response.critic_result,
        "nodes": [node.model_dump() for node in plan_response.nodes],
        "edges": [edge.model_dump() for edge in plan_response.edges],
        "metadata": plan_response.metadata.model_dump(),
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
        
        logger.info(f"Updating goal status to 'planned' in 'goals' collection: {goal_public_id}")
        await db.goals.update_one(
            {"public_id": goal_public_id},
            {"$set": {"status": "planned", "current_step": "planned", "progress_percentage": 20.0, "updated_at": datetime.utcnow()}}
        )

    return plan_response
