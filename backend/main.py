from contextlib import asynccontextmanager
import logging
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from core.config import settings
from api.router import router
from api.auth import auth_router
from api.users import users_router
from core.database import Database
from core.auth_db import AuthDatabase
from core.sync_worker import ProfileSyncWorker
from core.security_headers import SecurityHeadersMiddleware
from core.payload_limit import PayloadLimitMiddleware
from core.rate_limiter import RateLimitMiddleware
from core.logging_config import CorrelationIdMiddleware, get_correlation_id, setup_structured_logging

logger = logging.getLogger("main")


# Database & Worker lifecycle handler
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup phase
    setup_structured_logging(
        log_level=settings.LOG_LEVEL,
        structured=settings.STRUCTURED_LOGGING or settings.is_production(),
    )

    await Database.connect_db()
    AuthDatabase.get_instance()
    ProfileSyncWorker.get_instance().start()

    logger.info("=" * 70)
    logger.info("AI GOAL-TO-TASK PLANNING ENGINE — STARTUP CONFIGURATION")
    logger.info("=" * 70)
    logger.info(f"[LLM-CONFIG] provider={settings.LLM_PROVIDER} model={settings.MODEL_NAME} api_key_configured={bool(settings.GEMINI_API_KEY)}")
    logger.info(f"[EMBEDDING-CONFIG] provider={'openai' if bool(settings.OPENAI_API_KEY) else 'local'}")
    logger.info(f"[BROWSER-CONFIG] headless={settings.HEADLESS_BROWSER}")
    logger.info(f"[SERVER-CONFIG] host={settings.BACKEND_HOST} port={settings.BACKEND_PORT}")
    logger.info(f"[OAUTH-CONFIG] google_configured={bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)} facebook_configured={bool(settings.FACEBOOK_APP_ID and settings.FACEBOOK_APP_SECRET)}")
    logger.info(f"[SECURITY-CONFIG] env={settings.ENVIRONMENT} secure_cookies={settings.COOKIE_SECURE} rate_limiting={settings.ENABLE_RATE_LIMITING}")
    logger.info("=" * 70)

    try:
        from execution.registry.tool_registry import ToolRegistry
        from execution.browser.browser_tool import BrowserTool
        registry = ToolRegistry.get_instance()
        if not registry.get_tool("browser"):
            registry.register(BrowserTool())
    except Exception as e:
        logger.warning(f"Browser tool startup registration note: {e}")

    yield

    # Shutdown phase: bounded graceful teardown
    try:
        from orchestration.scheduler import ARDOScheduler
        await ARDOScheduler.cleanup_abandoned_sessions(max_idle_minutes=0)
    except Exception as e:
        logger.warning(f"[ARDOScheduler] Teardown warning: {e}")

    try:
        await ProfileSyncWorker.get_instance().stop(timeout=2.0)
    except Exception as e:
        logger.warning(f"[ProfileSyncWorker] Graceful stop warning: {e}")

    try:
        await Database.close_db()
    except Exception as e:
        logger.warning(f"[Database] Close connection warning: {e}")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version="1.0.0",
    description="React Flow compatible goal-to-task AI decomposition engine backend.",
    docs_url=None if settings.is_production() else f"{settings.API_V1_STR}/docs",
    redoc_url=None if settings.is_production() else f"{settings.API_V1_STR}/redoc",
    lifespan=lifespan,
    redirect_slashes=False,
)


# ─────────────────────────────────────────────────────────────────────────────
# Global Safe Exception Handlers
# ─────────────────────────────────────────────────────────────────────────────

@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Clean HTTP exception handler ensuring structured JSON without internal details."""
    cid = get_correlation_id()
    headers = getattr(exc, "headers", None) or {}
    if cid:
        headers["X-Correlation-ID"] = cid
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Clean request validation error formatting."""
    cid = get_correlation_id()
    headers = {"X-Correlation-ID": cid} if cid else {}
    return JSONResponse(
        status_code=422,
        content={"detail": "Request validation failed", "errors": exc.errors()},
        headers=headers,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """
    Catches unhandled server exceptions, logs full trace internally with correlation ID,
    and returns a safe generic 500 error to clients (never leaking stack traces or secrets).
    """
    cid = get_correlation_id()
    logger.critical(
        f"[CRITICAL-500] Unhandled server error (correlation_id={cid}): {exc}",
        exc_info=True,
    )
    headers = {"X-Correlation-ID": cid} if cid else {}
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error. Please contact system administrator."},
        headers=headers,
    )


# ─────────────────────────────────────────────────────────────────────────────
# CSRF & Origin Validation Middleware
# ─────────────────────────────────────────────────────────────────────────────
class CSRFOriginValidationMiddleware(BaseHTTPMiddleware):
    """
    Validates Origin / Referer on state-changing requests (POST, PUT, DELETE, PATCH)
    when authenticated via cookies, protecting against Cross-Site Request Forgery.
    Bearer-token authenticated calls and public auth endpoints are exempt.
    Safe HTTP methods (GET, HEAD, OPTIONS) are strictly exempt.
    """
    async def dispatch(self, request, call_next):
        # Strictly exempt safe HTTP methods and preflight requests
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)

        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            # Exclude public entry points that do not rely on session cookies
            path = request.url.path
            is_exempt = any(path.endswith(p) for p in ("/auth/register", "/auth/login", "/auth/forgot-password", "/auth/reset-password"))
            if is_exempt:
                return await call_next(request)

            # Check if request is carrying an authentication cookie
            has_cookie = settings.SESSION_COOKIE_NAME in request.cookies
            auth_header = request.headers.get("authorization", "")
            has_bearer = auth_header.startswith("Bearer ")

            # Enforce origin check when request relies on cookie authorization
            if has_cookie and not has_bearer:
                origin = request.headers.get("origin")
                referer = request.headers.get("referer")
                sec_fetch_site = request.headers.get("sec-fetch-site")

                if sec_fetch_site == "cross-site":
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "CSRF origin validation failed: cross-site request rejected."},
                    )

                trusted_origins = {
                    o.strip()
                    for o in (settings.ALLOWED_ORIGINS + "," + getattr(settings, "CSRF_TRUSTED_ORIGINS", "")).split(",")
                    if o.strip() and o.strip() != "*"
                }

                origin_to_check = origin
                if not origin_to_check and referer:
                    try:
                        parsed = urlparse(referer)
                        origin_to_check = f"{parsed.scheme}://{parsed.netloc}"
                    except Exception:
                        origin_to_check = None

                if origin_to_check and origin_to_check not in trusted_origins:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "CSRF origin validation failed: untrusted Origin header."},
                    )

        return await call_next(request)


# Security & CORS Configuration
# Filter out wildcard origins when credentialed CORS is active
allowed_origins_list = [
    origin.strip()
    for origin in settings.ALLOWED_ORIGINS.split(",")
    if origin.strip() and origin.strip() != "*"
]
# Ensure standard local development origins are always permitted with credentials
for default_origin in ("http://localhost:3000", "http://127.0.0.1:3000"):
    if default_origin not in allowed_origins_list:
        allowed_origins_list.append(default_origin)

# Middleware stack registration:
# In Starlette, user middlewares wrap inside-out (the LAST added middleware is the OUTERMOST wrapper).
# CORSMiddleware MUST be added LAST so that:
# 1. Preflight OPTIONS requests are handled immediately at the perimeter without passing through inner middleware.
# 2. Every outbound response (including 4xx/5xx errors, rate limits, and CSRF rejections) is stamped with CORS headers.
app.add_middleware(CSRFOriginValidationMiddleware)
app.add_middleware(PayloadLimitMiddleware, max_bytes=getattr(settings, "MAX_PAYLOAD_BYTES", 2 * 1024 * 1024))
app.add_middleware(RateLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# Register endpoints mapped directly to '/api/v1' config and root
app.include_router(auth_router, prefix=settings.API_V1_STR)
app.include_router(auth_router)
app.include_router(users_router, prefix=settings.API_V1_STR)
app.include_router(users_router)
app.include_router(router, prefix=settings.API_V1_STR)
app.include_router(router)
