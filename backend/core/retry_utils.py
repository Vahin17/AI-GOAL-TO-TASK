"""
retry_utils.py — Standardized HTTP Retry-After & Provider Error Classification
=============================================================================
Provides standard-compliant HTTP Retry-After parsing and provider error
classification across LLM providers and embedding services in ARDO:
- RFC 7231 / RFC 2822 integer seconds and HTTP-date formats.
- Bounded retry delays with sensible upper bounds.
- Fine-grained classification of temporary rate limits vs. permanent quota exhaustion.
"""

from __future__ import annotations

import email.utils
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_MAX_RETRY_AFTER_SECONDS: float = 300.0  # 5 minutes bounded maximum


def parse_retry_after(
    header_val: Any,
    max_bounded_seconds: float = DEFAULT_MAX_RETRY_AFTER_SECONDS,
) -> Optional[float]:
    """
    Parses an HTTP Retry-After header value per RFC 7231 Section 7.1.3:
    Supports:
      1. Delay-seconds: Non-negative integer or float (e.g. "120", 120, "30.5", "51.236756038s").
      2. HTTP-date: RFC 7231 / RFC 2822 format (e.g. "Wed, 21 Oct 2026 07:28:00 GMT").

    Returns:
        Seconds to wait as a float >= 0.0, capped at max_bounded_seconds,
        or None if header_val is missing, empty, or unparseable.
    """
    if header_val is None:
        return None

    raw_str = str(header_val).strip()
    if not raw_str:
        return None

    # 1. Strip trailing 's' or 'sec' if present (common in cloud APIs e.g. "51.23s")
    cleaned_str = re.sub(r'(?i)\s*(s|sec|seconds)$', '', raw_str)

    # 2. Try numeric parsing (delay-seconds)
    try:
        val = float(cleaned_str)
        if val < 0.0:
            return 0.0
        return min(val, max_bounded_seconds)
    except (ValueError, TypeError):
        pass

    # 3. Try HTTP-date parsing per RFC 7231 / RFC 2822
    try:
        parsed_tuple = email.utils.parsedate_to_datetime(raw_str)
        if parsed_tuple is not None:
            # Ensure timezone awareness
            if parsed_tuple.tzinfo is None:
                parsed_dt = parsed_tuple.replace(tzinfo=timezone.utc)
            else:
                parsed_dt = parsed_tuple.astimezone(timezone.utc)

            now_utc = datetime.now(timezone.utc)
            diff_seconds = (parsed_dt - now_utc).total_seconds()
            if diff_seconds < 0.0:
                return 0.0
            return min(diff_seconds, max_bounded_seconds)
    except Exception:
        pass

    # 4. Unparseable format
    logger.debug(f"[RetryUtils] Unparseable Retry-After header: {header_val!r}")
    return None


def classify_provider_error(
    status_code: int,
    error_msg: str,
    details: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, bool]:
    """
    Classifies provider errors into distinct operational categories.

    Returns:
        (failure_type, is_permanent)
        where failure_type is one of:
          - "auth_401": Invalid or missing credentials (permanent)
          - "quota_exhausted": Credit / daily quota exhausted (permanent until operator intervention)
          - "rate_limit_429": Temporary concurrency / RPM / TPM limit (transient)
          - "service_unavailable_503": Backend overloaded / temporary maintenance (transient)
          - "network_error": Connection reset, timeout, DNS glitch (transient)
          - "bad_request_400": Malformed request payload (permanent)
          - "not_found_404": Model or endpoint not found (permanent)
          - "server_error_500": Upstream unexpected internal error (transient)
    """
    msg_lower = (error_msg or "").lower()
    details = details or []

    # 1. Authentication / Permission
    if status_code in (401, 403) or any(
        k in msg_lower
        for k in ("unauthorized", "invalid api key", "incorrect api key", "permission_denied", "forbidden")
    ):
        return "auth_401", True

    # 2. Not Found
    if status_code == 404 or "model not found" in msg_lower:
        return "not_found_404", True

    # 3. Bad Request
    if status_code == 400 and not any(k in msg_lower for k in ("quota", "credit")):
        return "bad_request_400", True

    # 4. Quota Exhaustion vs. Temporary Rate Limit
    if status_code == 429 or "rate limit" in msg_lower or "quota" in msg_lower or "resource_exhausted" in msg_lower:
        # Check structured details
        for d in details:
            if isinstance(d, dict):
                reason = str(d.get("reason", "")).upper()
                meta = d.get("metadata", {}) if isinstance(d.get("metadata"), dict) else {}
                metric = str(meta.get("quota_metric", "")).lower()
                limit = str(meta.get("quota_limit", "")).lower()
                if any(k in reason for k in ("DAILY", "PER_DAY", "BILLING")):
                    return "quota_exhausted", True
                if any(k in metric for k in ("per_day", "daily", "billing")) or any(k in limit for k in ("per_day", "daily")):
                    return "quota_exhausted", True

        # Check error message strings for hard billing / quota exhaustion
        hard_quota_indicators = (
            "insufficient_quota",
            "insufficient quota",
            "billing",
            "exceeded your current quota",
            "plan and billing details",
            "check your plan",
            "credit balance",
            "free tier limit",
            "daily limit",
            "per day",
            "quota exceeded",
        )
        if any(k in msg_lower for k in hard_quota_indicators):
            return "quota_exhausted", True

        # Otherwise, 429 represents a temporary rate limit (RPM/TPM/concurrency)
        return "rate_limit_429", False

    # 5. Service Unavailable
    if status_code == 503 or "service unavailable" in msg_lower or "overloaded" in msg_lower:
        return "service_unavailable_503", False

    # 6. Transient Server Errors
    if status_code in (500, 502, 504):
        return "server_error_500", False

    # 7. Default to transient network error
    return "network_error", False
