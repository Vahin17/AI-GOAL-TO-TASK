"""
payload_limit.py — Request Body & Payload Size Enforcement Middleware (Phase 4)
==============================================================================
Protects the FastAPI backend from Memory Exhaustion and Denial of Service (DoS)
caused by oversized payloads submitted to planning, ingestion, or auth endpoints.
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from core.config import settings


class PayloadLimitMiddleware(BaseHTTPMiddleware):
    """
    Rejects HTTP requests whose body exceeds the configured maximum byte limit.
    """

    def __init__(self, app, max_bytes: int = 2 * 1024 * 1024):
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH"):
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    length = int(content_length)
                    if length > self.max_bytes:
                        return JSONResponse(
                            status_code=413,
                            content={
                                "detail": f"Payload too large ({length} bytes). Maximum permitted is {self.max_bytes} bytes."
                            },
                        )
                except ValueError:
                    return JSONResponse(
                        status_code=400,
                        content={"detail": "Invalid Content-Length header."},
                    )

        return await call_next(request)

