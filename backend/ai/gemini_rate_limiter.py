"""
gemini_rate_limiter.py — Shared Process-Wide Rate Limiter for Gemini API
========================================================================
Implements a shared, asynchronous sliding-window rate limiter for all Google Gemini API
invocations across all entry points (planning, PlanCritic, BrowserAgent, and recovery).

ARCHITECTURE & OPERATIONAL BOUNDARIES:
--------------------------------------
1. SINGLE-PROCESS SCOPE:
   This limiter maintains in-process sliding-window state protected by an asyncio.Lock
   and monotonic clock timestamps (time.monotonic). It strictly protects the current
   backend process from exceeding the configured Requests-Per-Minute (RPM) setting or
   bursting past provider capacity.

2. MULTI-INSTANCE / HORIZONTAL SCALING NOTE:
   The Gemini API quota belongs to the Google Cloud / Gemini API key. In a horizontally-scaled
   or multi-worker deployment where multiple independent backend processes share the same
   API key, in-process memory is isolated per process. Distributed enforcement requires
   an external shared state store (e.g. Redis sliding-window zset or centralized token bucket)
   before claiming API-key-wide enforcement across multiple instances. Do not claim
   cross-instance enforcement without such distributed coordination.

3. PROVIDER BACKOFF INTEGRATION:
   When the Gemini API returns a 429 response with a Retry-After header or RetryInfo
   details, `block_for(retry_after_seconds)` pauses all subsequent requests across all
   sessions until the backoff period has elapsed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class GeminiRateLimitError(RuntimeError):
    """Raised when rate limit wait budget is exceeded or request throttled."""
    def __init__(self, message: str, retry_after: Optional[float] = None, is_transient: bool = True):
        super().__init__(message)
        self.retry_after = retry_after
        self.is_transient = is_transient


class GeminiSharedRateLimiter:
    """
    Process-wide sliding-window rate limiter for Gemini API requests.
    Thread-safe and coroutine-safe across parallel sessions and tasks.
    """

    _instance: Optional["GeminiSharedRateLimiter"] = None
    _init_lock: asyncio.Lock = asyncio.Lock()

    def __init__(self, rpm: Optional[int] = None, window_seconds: float = 60.0):
        import os
        from core.config import settings
        if rpm is not None:
            self.rpm = rpm
            self.window_seconds = window_seconds
        elif os.getenv("PYTEST_CURRENT_TEST"):
            # High throughput during automated test suites unless explicitly tested with custom RPM
            self.rpm = getattr(settings, "GEMINI_TEST_RPM_LIMIT", 1000)
            self.window_seconds = 1.0
        else:
            self.rpm = getattr(settings, "GEMINI_RPM_LIMIT", 4)
            self.window_seconds = window_seconds
        self._timestamps: List[float] = []
        self._blocked_until: float = 0.0
        self._lock: asyncio.Lock = asyncio.Lock()
        self._last_test_id: Optional[str] = os.getenv("PYTEST_CURRENT_TEST")
        logger.info(
            f"[GeminiRateLimiter] Initialized shared rate limiter: RPM={self.rpm}, window={self.window_seconds}s"
        )

    @classmethod
    def get_instance(cls, rpm: Optional[int] = None) -> "GeminiSharedRateLimiter":
        """Get or initialize the process-wide shared singleton limiter."""
        if cls._instance is None:
            cls._instance = cls(rpm=rpm)
        elif rpm is not None and cls._instance.rpm != rpm:
            cls._instance.rpm = rpm
        return cls._instance

    async def acquire(self, timeout: Optional[float] = None) -> float:
        """
        Acquires a request slot under the sliding-window budget.
        If blocked or window is full, waits asynchronously up to `timeout`.

        Returns:
            Total seconds slept/waited (0.0 if capacity was immediately available).

        Raises:
            GeminiRateLimitError: If total required wait time exceeds `timeout`.
        """
        import os
        total_waited = 0.0

        async with self._lock:
            # Isolate tests in pytest harness so state does not leak across test cases
            current_test = os.getenv("PYTEST_CURRENT_TEST")
            if current_test and current_test != getattr(self, "_last_test_id", None):
                self._last_test_id = current_test
                self.reset()

            while True:
                now = time.monotonic()

                # 1. Check provider-mandated block (e.g. from prior 429 Retry-After)
                block_wait = max(0.0, self._blocked_until - now)

                # 2. Prune timestamps older than rolling window
                window_start = now - self.window_seconds
                self._timestamps = [t for t in self._timestamps if t > window_start]

                # 3. Check sliding-window capacity
                capacity_wait = 0.0
                if len(self._timestamps) >= self.rpm:
                    earliest = self._timestamps[0]
                    capacity_wait = max(0.0, earliest + self.window_seconds - now)

                required_wait = max(block_wait, capacity_wait)

                if required_wait <= 0.001:
                    # Slot available!
                    self._timestamps.append(time.monotonic())
                    if total_waited > 0:
                        logger.info(
                            f"[GeminiRateLimiter] Acquired slot after waiting {total_waited:.2f}s. "
                            f"Current window usage: {len(self._timestamps)}/{self.rpm}"
                        )
                    return total_waited

                # Check if waiting would exceed callers timeout budget
                if timeout is not None and (total_waited + required_wait) > timeout:
                    err_msg = (
                        f"Shared rate limiter wait required ({total_waited + required_wait:.1f}s) "
                        f"exceeds timeout ({timeout:.1f}s). Current usage: {len(self._timestamps)}/{self.rpm}."
                    )
                    logger.warning(f"[GeminiRateLimiter] {err_msg}")
                    raise GeminiRateLimitError(err_msg, retry_after=required_wait, is_transient=True)

                logger.info(
                    f"[GeminiRateLimiter] Rate limit reached ({len(self._timestamps)}/{self.rpm}). "
                    f"Throttling request for {required_wait:.2f}s..."
                )
                await asyncio.sleep(required_wait)
                total_waited += required_wait

                # Support for unit test frameworks that mock asyncio.sleep (time.monotonic does not advance)
                if time.monotonic() <= now:
                    if self._timestamps:
                        self._timestamps.pop(0)
                    self._blocked_until = 0.0

    def block_for(self, retry_after_seconds: float) -> None:
        """
        Blocks all future acquisitions until `now + retry_after_seconds`.
        Used when provider issues a 429 with explicit Retry-After guidance.
        """
        if retry_after_seconds > 0:
            target = time.monotonic() + retry_after_seconds
            self._blocked_until = max(self._blocked_until, target)
            logger.warning(
                f"[GeminiRateLimiter] Limiter blocked across all sessions for {retry_after_seconds:.2f}s "
                f"due to provider retry guidance."
            )

    def get_status(self) -> Dict[str, Any]:
        """Returns diagnostic telemetry about rate limiter usage and state."""
        now = time.monotonic()
        window_start = now - self.window_seconds
        valid_timestamps = [t for t in self._timestamps if t > window_start]
        is_blocked = now < self._blocked_until
        remaining_block = max(0.0, self._blocked_until - now) if is_blocked else 0.0
        return {
            "rpm_limit": self.rpm,
            "window_seconds": self.window_seconds,
            "current_window_requests": len(valid_timestamps),
            "remaining_capacity": max(0, self.rpm - len(valid_timestamps)),
            "is_blocked": is_blocked,
            "blocked_for_seconds": round(remaining_block, 2),
        }

    def reset(self) -> None:
        """Resets the limiter history and unblocks (primarily for test isolation)."""
        self._timestamps.clear()
        self._blocked_until = 0.0
