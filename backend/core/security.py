"""
security.py — Argon2id Password Hashing, Token Generation & Rate Limiting
========================================================================
Implements:
- Argon2id password hashing and constant-time verification
- Strict password policy evaluation
- Cryptographically secure random token generation (session & reset)
- In-memory sliding-window rate limiting for login brute-force protection
- Short-lived WebSocket ticket generation for secure cross-origin WS authentication
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

logger = logging.getLogger(__name__)

# Argon2id password hasher with secure, production-grade parameters
# Memory: 64MB (65536 KB), Time iterations: 3, Parallelism: 4 threads
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)


def hash_password(password: str) -> str:
    """Hash password using Argon2id algorithm."""
    return _hasher.hash(password)


def verify_password(password_hash: str, candidate_password: str) -> bool:
    """
    Verify candidate password against Argon2id hash.
    Safe against timing attacks. Returns False on any verification error.
    """
    try:
        return _hasher.verify(password_hash, candidate_password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    except Exception as e:
        logger.warning(f"[Security] Unexpected error during password verification: {e}")
        return False


def validate_password_policy(password: str) -> Tuple[bool, Optional[str]]:
    """
    Validates password strength according to ARDO security policy:
    - Minimum 8 characters
    - At least one uppercase letter (A-Z)
    - At least one lowercase letter (a-z)
    - At least one number (0-9)
    - At least one special symbol
    """
    if len(password) < 8:
        return False, "Password must be at least 8 characters in length."
    if len(password) > 128:
        return False, "Password must not exceed 128 characters."
    if not re.search(r"[A-Z]", password):
        return False, "Password must include at least one uppercase letter."
    if not re.search(r"[a-z]", password):
        return False, "Password must include at least one lowercase letter."
    if not re.search(r"[0-9]", password):
        return False, "Password must include at least one digit."
    if not re.search(r"[!@#$%^&*(),.?\":{}|<>\-_=+[\]\\/`~]", password):
        return False, "Password must include at least one special character."
    return True, None


def validate_email_format(email: str) -> bool:
    """Validate RFC-compliant email format."""
    email_clean = email.strip()
    if not email_clean or len(email_clean) > 254:
        return False
    # Standard robust email regex
    pattern = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
    return bool(re.match(pattern, email_clean))


def generate_session_token() -> str:
    """Generate 256-bit cryptographically secure URL-safe random session token."""
    return secrets.token_urlsafe(32)


def generate_reset_token() -> str:
    """Generate cryptographically secure password reset token."""
    return secrets.token_urlsafe(32)


# ─────────────────────────────────────────────────────────────────────────────
# Rate Limiting (In-Memory Sliding Window)
# ─────────────────────────────────────────────────────────────────────────────

class RateLimiter:
    """
    Sliding-window in-memory rate limiter to protect against:
    - Brute-force password guessing
    - Credential stuffing
    - Rapid automated requests
    """

    def __init__(self, max_attempts: int = 5, window_seconds: int = 60):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        # key -> list of attempt timestamps
        self._attempts: Dict[str, List[float]] = {}

    def is_rate_limited(self, key: str) -> bool:
        """Returns True if the key has exceeded max_attempts within window_seconds."""
        now = time.time()
        timestamps = self._attempts.get(key, [])
        # Prune timestamps older than window
        valid_timestamps = [t for t in timestamps if (now - t) < self.window_seconds]
        self._attempts[key] = valid_timestamps
        return len(valid_timestamps) >= self.max_attempts

    def record_attempt(self, key: str) -> None:
        """Record an attempt timestamp for key."""
        now = time.time()
        timestamps = self._attempts.get(key, [])
        valid_timestamps = [t for t in timestamps if (now - t) < self.window_seconds]
        valid_timestamps.append(now)
        self._attempts[key] = valid_timestamps

    def clear(self, key: str) -> None:
        """Clear attempts for key (e.g. upon successful authentication)."""
        self._attempts.pop(key, None)

    def reset_all(self) -> None:
        """Reset all rate limit tracking (useful in test teardown)."""
        self._attempts.clear()


# Global rate limiter instances:
# 1. Login rate limiter: 5 attempts per 60 seconds per IP/email
login_rate_limiter = RateLimiter(max_attempts=5, window_seconds=60)
# 2. Registration rate limiter: 5 attempts per 60 seconds per IP/email
register_rate_limiter = RateLimiter(max_attempts=5, window_seconds=60)
# 3. Password reset rate limiter: 3 requests per 300 seconds (5 min) per IP/email
reset_rate_limiter = RateLimiter(max_attempts=3, window_seconds=300)


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket One-Time Ticket Manager
# ─────────────────────────────────────────────────────────────────────────────

class WebSocketTicketManager:
    """
    Manages short-lived single-use authentication tickets for WebSocket connections.
    Solves cross-origin cookie restrictions in development while enforcing server validation.
    """

    def __init__(self, ttl_seconds: int = 60):
        self.ttl_seconds = ttl_seconds
        # ticket -> (user_id, session_id, expires_at)
        self._tickets: Dict[str, Tuple[str, str, float]] = {}

    def issue_ticket(self, user_id: str, session_id: str) -> str:
        ticket = f"wst_{secrets.token_urlsafe(24)}"
        expires_at = time.time() + self.ttl_seconds
        self._tickets[ticket] = (user_id, session_id, expires_at)
        return ticket

    def redeem_ticket(self, ticket: str, expected_session_id: str) -> Optional[str]:
        """
        Redeems ticket if valid and unexpired.
        Returns user_id if valid, None otherwise.
        Tickets are strictly single-use and deleted upon check.
        """
        self._cleanup()
        data = self._tickets.pop(ticket, None)
        if not data:
            return None
        user_id, sid, expires_at = data
        if time.time() > expires_at:
            return None
        if sid != expected_session_id:
            return None
        return user_id

    def _cleanup(self) -> None:
        now = time.time()
        expired = [t for t, (_, _, exp) in self._tickets.items() if exp < now]
        for t in expired:
            self._tickets.pop(t, None)


ws_ticket_manager = WebSocketTicketManager(ttl_seconds=60)


# ─────────────────────────────────────────────────────────────────────────────
# OAuth PKCE & State Security Manager
# ─────────────────────────────────────────────────────────────────────────────

def generate_pkce_pair() -> Tuple[str, str]:
    """
    Generate PKCE code_verifier and S256 code_challenge per RFC 7636.
    Ensures authorization code cannot be intercepted or injected.
    """
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return code_verifier, code_challenge


class OAuthStateManager:
    """
    Manages short-lived, single-use state and PKCE challenges for OAuth flows.
    Mitigates CSRF, replay attacks, and state tampering.
    """

    def __init__(self, ttl_seconds: int = 600):
        self.ttl_seconds = ttl_seconds
        # state -> dict with provider, code_verifier, linking_user_id, expires_at, next
        self._states: Dict[str, Dict[str, Any]] = {}

    def create_state(
        self,
        provider: str,
        code_verifier: Optional[str] = None,
        linking_user_id: Optional[str] = None,
        next_path: str = "/app",
    ) -> str:
        state = secrets.token_urlsafe(32)
        expires_at = time.time() + self.ttl_seconds
        self._states[state] = {
            "provider": provider.lower(),
            "code_verifier": code_verifier,
            "linking_user_id": linking_user_id,
            "expires_at": expires_at,
            "next": next_path,
        }
        return state

    def consume_state(self, state: str, expected_provider: str) -> Optional[Dict[str, Any]]:
        """
        Atomically validate and redeem an OAuth state.
        States are single-use and immediately invalidated upon verification.
        """
        self._cleanup()
        data = self._states.pop(state, None)
        if not data:
            return None
        if time.time() > data["expires_at"]:
            return None
        if data["provider"] != expected_provider.lower():
            return None
        return data

    def _cleanup(self) -> None:
        now = time.time()
        expired = [s for s, d in self._states.items() if d["expires_at"] < now]
        for s in expired:
            self._states.pop(s, None)


oauth_state_manager = OAuthStateManager(ttl_seconds=600)


# ─────────────────────────────────────────────────────────────────────────────
# Local Development & Testing Mailbox
# ─────────────────────────────────────────────────────────────────────────────

class DevMailbox:
    """
    In-memory, explicitly gated sink for local development and automated testing.
    Captures outbound messages (such as password reset notices) without writing
    sensitive tokens or URLs to application logs, stdout, or persistent records.
    Only active in 'development' and 'test' environments; inert in production.
    """
    _messages: List[Dict[str, Any]] = []

    @classmethod
    def send(
        cls,
        to_email: str,
        subject: str,
        body_text: str,
        reset_url: Optional[str] = None,
        token: Optional[str] = None,
    ) -> None:
        from core.config import settings
        if settings.ENVIRONMENT not in ("development", "test"):
            return
        cls._messages.append({
            "to": to_email,
            "subject": subject,
            "body": body_text,
            "reset_url": reset_url,
            "token": token,
            "timestamp": time.time(),
        })

    @classmethod
    def get_latest_message(cls, to_email: str) -> Optional[Dict[str, Any]]:
        for msg in reversed(cls._messages):
            if msg["to"].lower() == to_email.lower():
                return msg
        return None

    @classmethod
    def get_latest_reset_url(cls, to_email: str) -> Optional[str]:
        msg = cls.get_latest_message(to_email)
        return msg.get("reset_url") if msg else None

    @classmethod
    def get_latest_token(cls, to_email: str) -> Optional[str]:
        msg = cls.get_latest_message(to_email)
        return msg.get("token") if msg else None

    @classmethod
    def clear(cls) -> None:
        cls._messages.clear()



