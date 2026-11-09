"""
security_headers.py — Production Security Headers Middleware (Phase 4)
=====================================================================
Applies enterprise-grade HTTP security headers to all incoming responses
to protect against Clickjacking, MIME-sniffing, XSS, and unauthorized framing.
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from core.config import settings


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Appends standard hardened HTTP security headers to all outbound responses.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response: Response = await call_next(request)

        # 1. Prevent MIME type sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"

        # 2. Prevent clickjacking via iframes
        response.headers["X-Frame-Options"] = "DENY"

        # 3. Restrict browser referrer information
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # 4. Disable dangerous browser device features by default
        response.headers["Permissions-Policy"] = (
            "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
            "magnetometer=(), microphone=(), payment=(), usb=()"
        )

        # 5. Restrict framing in Content-Security-Policy
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; frame-ancestors 'none'; object-src 'none';"
        )

        # 6. HTTP Strict Transport Security (HSTS) when running on HTTPS / production
        if settings.is_production() or getattr(settings, "COOKIE_SECURE", False):
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"

        return response

