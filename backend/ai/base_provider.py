from abc import ABC, abstractmethod
from schemas.models import PlanGraphResponse

class LLMProvider(ABC):
    """
    Abstract base class for all LLM inference providers.
    Ensures that regardless of the backend (OpenAI, LangChain, Anthropic),
    the module consistently returns a strictly typed PlanGraphResponse.
    """
    
    @abstractmethod
    async def generate_plan(self, goal: str, rag_context: str = "") -> PlanGraphResponse:
        """
        Takes a natural language goal and optional RAG context and converts it to a structured React Flow layout.
        
        Args:
            goal (str): The raw objective provided by the user.
            rag_context (str): Optional retrieved knowledge context to ground planning.

        Returns:
            PlanGraphResponse: Verified Pydantic schema structuring the resulting task graph.
        """
        pass
