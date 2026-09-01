from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import List, Union
from pathlib import Path

# Project Root (3 levels up from backend/core/config.py)
BASE_DIR = Path(__file__).resolve().parent.parent.parent

class Settings(BaseSettings):
    # API & App settings
    PROJECT_NAME: str = "AI Goal-To-Task Planning Engine"
    API_V1_STR: str = "/api/v1"
    
    # LLM Provider Configuration
    LLM_PROVIDER: str = "gemini"
    MODEL_NAME: str = "gemini-1.5-pro"
    
    GEMINI_API_KEY: str | None = None
    OPENAI_API_KEY: str | None = None
    ANTHROPIC_API_KEY: str | None = None

    # Database
    MONGODB_URI: str = "mongodb://localhost:27017"
    DATABASE_NAME: str = "ai_goal_planner"

    # Server settings
    BACKEND_HOST: str = "127.0.0.1"
    BACKEND_PORT: int = 8001
    FRONTEND_URL: str = "http://localhost:3000"

    # Environment
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    
    # Origins handled as string first, then split if needed or just use str
    ALLOWED_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"

    # Logging
    LOG_LEVEL: str = "INFO"

    # Feature Flags
    ENABLE_FALLBACK_RULES: bool = True
    ENABLE_EXECUTION_ENGINE: bool = True
    ENABLE_MEMORY_LAYER: bool = True
    ENABLE_RAG: bool = True

    # Phase 7: PostgreSQL + pgvector & Semantic Memory Settings
    POSTGRES_URI: str = "postgresql://postgres:postgres@localhost:5432/ai_goal_planner"
    PGVECTOR_TABLE: str = "knowledge_chunks"
    PGVECTOR_INDEX_TYPE: str = "hnsw"
    RAG_TOP_K: int = 4
    RAG_MIN_SCORE: float = 0.35
    RAG_MAX_CONTEXT_CHARS: int = 2500

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"), 
        env_file_encoding='utf-8', 
        extra='ignore'
    )

settings = Settings()
