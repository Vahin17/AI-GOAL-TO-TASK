"""
rate_limiter.py — In-Memory Sliding Window Rate Limiting (Phase 4)
=================================================================
Protects critical, expensive, and sensitive endpoints (authentication,
planning LLM calls, and browser orchestration sessions) against brute-force
and resource exhaustion attacks.
"""

from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List, Optional, Tuple

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from core.config import settings


class SlidingWindowRateLimiter:
    """
    Thread-safe, sliding-window rate limiter utilizing in-memory timestamp logs.
    """

    def __init__(self):
        self._lock = Lock()
        self._history: Dict[str, List[float]] = defaultdict(list)

    def check(self, key: str, max_requests: int, window_seconds: float = 60.0) -> Tuple[bool, int]:
        """
        Check if request under `key` is allowed.
        Returns (is_allowed, retry_after_seconds).
        """
        now = time.time()
        window_start = now - window_seconds

        with self._lock:
            timestamps = self._history[key]
            # Prune timestamps older than window
            valid_timestamps = [t for t in timestamps if t > window_start]
            self._history[key] = valid_timestamps

            if len(valid_timestamps) >= max_requests:
                earliest = valid_timestamps[0]
                retry_after = max(1, int(earliest + window_seconds - now))
                return False, retry_after

            # Allow request and record timestamp
            valid_timestamps.append(now)
            return True, 0

    def reset(self) -> None:
        """Clear all rate limit history (useful for test isolation)."""
        with self._lock:
            self._history.clear()


# Global rate limiter instance
rate_limiter = SlidingWindowRateLimiter()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    FastAPI middleware applying sliding-window rate limits based on client IP
    and request route categories.
    """

    async def dispatch(self, request: Request, call_next):
        if not getattr(settings, "ENABLE_RATE_LIMITING", True):
            return await call_next(request)

        path = request.url.path
        client_ip = request.client.host if request.client else "127.0.0.1"

        # 1. Plan Generation Endpoint (Expensive LLM call protection)
        if path.endswith("/plan") and request.method == "POST":
            key = f"plan:{client_ip}"
            max_req = getattr(settings, "RATE_LIMIT_PLAN_PER_MINUTE", 15)
            allowed, retry_after = rate_limiter.check(key, max_requests=max_req, window_seconds=60.0)
            if not allowed:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded for planning generation. Please wait before submitting more goals."},
                    headers={"Retry-After": str(retry_after)},
                )

        # 2. Start Orchestration Session Endpoint (Resource-heavy browser automation)
        elif path.endswith("/orchestrate") and request.method == "POST":
            key = f"orch:{client_ip}"
            max_req = getattr(settings, "RATE_LIMIT_ORCHESTRATE_PER_MINUTE", 10)
            allowed, retry_after = rate_limiter.check(key, max_requests=max_req, window_seconds=60.0)
            if not allowed:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded for starting orchestration sessions. Please wait before launching another session."},
                    headers={"Retry-After": str(retry_after)},
                )

        return await call_next(request)
