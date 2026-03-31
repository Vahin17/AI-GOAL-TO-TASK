import os
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # API & App settings
    PROJECT_NAME: str = "AI Goal-To-Task Planning Engine"
    API_V1_STR: str = "/api/v1"
    
    # LLM Provider Configuration
    LLM_PROVIDER: str = "langchain"  # Options: langchain, openai, anthropic, gemini etc.
    OPENAI_API_KEY: str | None = None
    ANTHROPIC_API_KEY: str | None = None
    GEMINI_API_KEY: str | None = None

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding='utf-8', extra='ignore')

settings = Settings()
