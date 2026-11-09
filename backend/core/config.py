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
    MODEL_NAME: str = "gemini-3.5-flash"
    MAX_SESSION_LLM_CALLS: int = 15
    GEMINI_RPM_LIMIT: int = 4
    
    # Browser Inspection Mode (Debug only)
    BROWSER_INSPECTION_MODE: bool = False
    BROWSER_INSPECTION_TIMEOUT_SECONDS: int = 120
    
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

    # Browser Execution
    HEADLESS_BROWSER: bool = True
    BROWSER_EGRESS_PROXY: str | None = None  # e.g., "http://squid-proxy:3128" for network-level egress boundary

    # Authentication & Session Security
    AUTH_DB_BACKEND: str = "postgres"  # "postgres" (authoritative) or "sqlite" (isolated testing only)
    SESSION_COOKIE_NAME: str = "ardo_session"
    SESSION_DURATION_DAYS: int = 7
    COOKIE_SECURE: bool = False  # Set to True in production (HTTPS)
    COOKIE_SAMESITE: str = "lax"  # "lax", "strict", or "none"
    COOKIE_DOMAIN: str | None = None
    
    # CSRF Protection: Trusted frontend origins for state-changing requests
    CSRF_TRUSTED_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"
    
    # Optional OAuth Provider Credentials (disabled unless genuinely configured)
    GOOGLE_CLIENT_ID: str | None = None
    GOOGLE_CLIENT_SECRET: str | None = None
    GOOGLE_REDIRECT_URI: str = "http://localhost:8001/auth/google/callback"
    FACEBOOK_APP_ID: str | None = None
    FACEBOOK_APP_SECRET: str | None = None
    FACEBOOK_REDIRECT_URI: str = "http://localhost:8001/auth/facebook/callback"

    # Production Hardening & Resource Controls (Phase 4)
    ENABLE_RATE_LIMITING: bool = True
    RATE_LIMIT_AUTH_PER_MINUTE: int = 5
    RATE_LIMIT_PLAN_PER_MINUTE: int = 15
    RATE_LIMIT_ORCHESTRATE_PER_MINUTE: int = 10
    MAX_PAYLOAD_BYTES: int = 2 * 1024 * 1024  # 2MB
    MAX_ACTIVE_SESSIONS_PER_USER: int = 3
    MAX_GLOBAL_CONCURRENT_TASKS: int = 10
    BROWSER_ACTION_TIMEOUT_SECONDS: float = 30.0
    TASK_EXECUTION_TIMEOUT_SECONDS: float = 120.0
    AI_CALL_TIMEOUT_SECONDS: float = 45.0
    SESSION_IDLE_TIMEOUT_MINUTES: int = 60
    STRUCTURED_LOGGING: bool = False
    POSTGRES_POOL_MIN_CONN: int = 2
    POSTGRES_POOL_MAX_CONN: int = 20
    OUTBOX_RETRY_INTERVAL_SECONDS: float = 60.0

    def is_production(self) -> bool:
        """Helper to determine whether running in production mode."""
        return self.ENVIRONMENT.strip().lower() in ("production", "prod")

    def validate_production_config(self) -> List[str]:
        """
        Validates mandatory production configuration constraints.
        Returns a list of violation messages. If running in production and
        critical violations are found, callers can fail-fast.
        """
        violations: List[str] = []
        if not self.is_production():
            return violations

        if self.DEBUG:
            violations.append("DEBUG must be set to False in production.")

        if not self.COOKIE_SECURE:
            violations.append("COOKIE_SECURE must be True in production to enforce HTTPS cookies.")

        if not self.GEMINI_API_KEY and not self.OPENAI_API_KEY:
            violations.append("Production requires a valid LLM API key (GEMINI_API_KEY or OPENAI_API_KEY).")

        origins = [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]
        if not origins or any("*" in o or "localhost" in o or "127.0.0.1" in o for o in origins):
            violations.append("ALLOWED_ORIGINS in production must specify explicit HTTPS domains and cannot contain wildcards or localhost.")

        if self.AUTH_DB_BACKEND.lower() != "postgres":
            violations.append("AUTH_DB_BACKEND must be 'postgres' in production. SQLite is restricted to unit test suites.")

        pg_uri = self.POSTGRES_URI or ""
        if "postgres:postgres" in pg_uri or "localhost" in pg_uri:
            violations.append("POSTGRES_URI cannot use default development credentials (postgres:postgres) or localhost in production.")

        if not getattr(self, "BROWSER_EGRESS_PROXY", None):
            violations.append("BROWSER_EGRESS_PROXY must be configured in production to enforce network-level browser egress boundary.")

        return violations
    FRONTEND_URL: str = "http://localhost:3000"

    model_config = SettingsConfigDict(
        env_file=[
            str(Path(__file__).resolve().parent.parent / ".env"),
            str(BASE_DIR / ".env"),
        ],
        env_file_encoding='utf-8', 
        extra='ignore'
    )

settings = Settings()
# If deprecated gemini model is in .env, normalize to active working model
if settings.MODEL_NAME in ("gemini-1.5-pro", "gemini-1.5-flash"):
    settings.MODEL_NAME = "gemini-flash-latest"

# Diagnostic logging for LLM configuration
_backend_env = Path(__file__).resolve().parent.parent / ".env"
_root_env = BASE_DIR / ".env"
_env_source = "backend/.env" if _backend_env.exists() else (".env" if _root_env.exists() else "process_env")
import logging
_logger = logging.getLogger("core.config")
_logger.info(
    f"[LLM-CONFIG] provider={settings.LLM_PROVIDER} model={settings.MODEL_NAME} "
    f"api_key_configured={bool(settings.GEMINI_API_KEY or settings.OPENAI_API_KEY)} env_source={_env_source}"
)

# Fail-fast validation when explicitly configured for production
if settings.is_production():
    _violations = settings.validate_production_config()
    if _violations:
        _logger.critical(f"[CONFIG] Production configuration validation failed with {len(_violations)} violation(s):")
        for v in _violations:
            _logger.critical(f"  - {v}")
        raise ValueError(f"Production configuration invalid: {'; '.join(_violations)}")

