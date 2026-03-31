from backend.ai.base_provider import LLMProvider
from backend.schemas.models import PlanGraphResponse
from backend.core.config import settings

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langchain_core.output_parsers import PydanticOutputParser

class LangChainProvider(LLMProvider):
    def __init__(self):
        # Initializing core LLM connection
        api_key = settings.OPENAI_API_KEY
        # If the API key is strictly None, we create it dynamically.
        # But for Langchain instantiation it's better to bypass directly or raise error explicitly via Orchestrator
        self.llm = ChatOpenAI(temperature=0.0, model="gpt-4o-mini", api_key=api_key) if api_key else None
        
        # Rigorous schema enforcement parsing logic
        self.parser = PydanticOutputParser(pydantic_object=PlanGraphResponse)
        
        # The Master Prompt
        self.prompt = ChatPromptTemplate.from_messages([
            ("system", 
             """You are an elite operational copilot agent. Your job is to strictly decompose a user's natural language goal into execution steps and return them structured precisely as a directed React Flow graph.
             
             Design Constraints:
             - Y-coordinates represent sequence (e.g. +150 per sequential node mapping downwards).
             - X-coordinates dictate parallelism or distinct branches. Central nodes are generally at X=250.
             - Keep step counts lean and strictly aligned with fulfilling the User's goal safely.
             
             {format_instructions}
             """),
            ("user", "User Goal: {goal}")
        ])
        
    def generate_plan(self, goal: str) -> PlanGraphResponse:
        """
        Executes the LangChain chain, resolving validation rules automatically via OutputParser.
        """
        if not self.llm:
            raise ValueError("LLM Provider requires an external Provider API key (e.g. OPENAI_API_KEY) in the .env or configuration.")
            
        formatted_prompt = self.prompt.format_prompt(
            goal=goal, 
            format_instructions=self.parser.get_format_instructions()
        )
        
        # Invoke completion and parse raw string -> Pydantic schemas mapped structure
        response = self.llm.invoke(formatted_prompt.to_messages())
        return self.parser.parse(response.content)
