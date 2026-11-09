"""
logging_config.py — Production Structured JSON Logging & Correlation IDs (Phase 4)
==================================================================================
Provides JSON-compatible structured logging, request correlation ID tracking,
and automated credential/secret masking for high-observability production operations.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Request-scoped correlation ID context variable
correlation_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="")


def get_correlation_id() -> str:
    """Retrieve the active request correlation ID or empty string."""
    return correlation_id_ctx.get()


class JSONLogFormatter(logging.Formatter):
    """
    Formats log records as single-line JSON objects with ISO timestamps,
    correlation IDs, and automated secret sanitization.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_data: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Attach request correlation ID if available
        cid = get_correlation_id()
        if cid:
            log_data["correlation_id"] = cid

        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        # Sanitize sensitive patterns from log message if privacy redactor is available
        try:
            from execution.evidence.privacy_redactor import PrivacyRedactor
            if isinstance(log_data["message"], str):
                log_data["message"] = PrivacyRedactor.redact_text(log_data["message"])
        except Exception:
            pass

        return json.dumps(log_data)


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """
    Extracts or generates an X-Correlation-ID for every inbound HTTP request
    and attaches it to the response and execution context.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        cid = (
            request.headers.get("x-correlation-id")
            or request.headers.get("x-request-id")
            or f"corr_{uuid.uuid4().hex[:12]}"
        )
        token = correlation_id_ctx.set(cid)
        try:
            response: Response = await call_next(request)
            response.headers["X-Correlation-ID"] = cid
            return response
        finally:
            correlation_id_ctx.reset(token)


def setup_structured_logging(log_level: str = "INFO", structured: bool = True) -> None:
    """
    Configures the root logging subsystem with JSONLogFormatter when structured is True.
    """
    root_logger = logging.getLogger()
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    root_logger.setLevel(numeric_level)

    # Avoid duplicate handlers
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    console_handler = logging.StreamHandler(sys.stdout)
    if structured:
        console_handler.setFormatter(JSONLogFormatter())
    else:
        console_handler.setFormatter(
            logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s")
        )
    root_logger.addHandler(console_handler)

