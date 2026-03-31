import logging
from backend.schemas.models import PlanGraphResponse
from backend.core.config import settings
from backend.ai.fallback_rules import generate_fallback_plan

logger = logging.getLogger(__name__)

# Lazy-load the provider based on configuration settings
def get_llm_provider():
    if settings.LLM_PROVIDER.lower() == "langchain":
        from backend.ai.langchain_provider import LangChainProvider
        return LangChainProvider()
    else:
        # Default fallback or extending other models securely
        from backend.ai.langchain_provider import LangChainProvider
        return LangChainProvider()

def generate_task_graph(goal: str) -> PlanGraphResponse:
    """
    Main orchestration controller. Routes the prompt to the selected LLM provider,
    catches all API/parsing exceptions, and delegates to the fallback heuristic rules if AI fails.
    """
    try:
        # Initialize and route correctly
        provider = get_llm_provider()
        return provider.generate_plan(goal)
    except Exception as e:
        logger.error(f"Error during generative AI planning: {e}. Executing fallback sequence.")
        # If any validation error, timeout, or missing key issue happens, default to explicit fallback model
        return generate_fallback_plan(goal)
