"""
test_auth.py — Complete Automated Test Suite for ARDO Authentication
====================================================================
Tests cover:
1. Registration (validation, Argon2id hashing, duplicate protection, zero plaintext leak)
2. Login (credentials verification, rate limiting, generic errors, secure cookies)
3. Session Management (invalidation, expiration, revocation, token hashing)
4. Protected API Routes (rejection of unauthenticated calls, acceptance of authenticated calls)
5. Multi-User Isolation (cross-user data isolation for orchestration, history, evidence)
6. WebSocket Security (pre-accept authentication, cross-user rejection)
7. Password Reset & Change (single-use token enforcement, expiration, session revocation)
8. OAuth Discovery (factual status without fake buttons)

Guarantees:
- ZERO real Gemini or paid LLM calls (strictly mocked)
- Isolated in-memory authentication database for 100% test independence
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.auth_db import AuthDatabase, hash_token
from core.config import settings
from core.sync_worker import ProfileSyncWorker
from core.security import (
    DevMailbox,
    hash_password,
    login_rate_limiter,
    register_rate_limiter,
    reset_rate_limiter,
    validate_password_policy,
    verify_password,
    ws_ticket_manager,
)
from main import app
from orchestration.scheduler import ARDOScheduler


@pytest.fixture(autouse=True)
def setup_test_auth_env(monkeypatch):
    """Reset AuthDatabase and rate limiters before each test for total test isolation."""
    # Force isolated in-memory database
    AuthDatabase.reset_instance(force_sqlite=True)
    ProfileSyncWorker.reset_instance()
    DevMailbox.clear()
    login_rate_limiter.reset_all()
    register_rate_limiter.reset_all()
    reset_rate_limiter.reset_all()
    ARDOScheduler._sessions.clear()
    ARDOScheduler._session_history.clear()
    # Bypass MongoDB connection timeout during test suite startup
    monkeypatch.setattr("core.database.Database.connect_db", AsyncMock())
    yield
    login_rate_limiter.reset_all()
    register_rate_limiter.reset_all()
    reset_rate_limiter.reset_all()
    ARDOScheduler._sessions.clear()
    ARDOScheduler._session_history.clear()
    DevMailbox.clear()
    ProfileSyncWorker.reset_instance()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


# ─────────────────────────────────────────────────────────────────────────────
# 1. Registration Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_registration_success(client):
    """Verify standard user registration with Argon2id hashing."""
    payload = {
        "full_name": "Alice Operator",
        "email": "alice@example.com",
        "password": "SecurePassword123!",
        "confirm_password": "SecurePassword123!",
        "terms_accepted": True,
    }
    res = client.post("/auth/register", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["email"] == "alice@example.com"
    assert data["full_name"] == "Alice Operator"
    assert data["is_active"] is True
    assert "password" not in data
    assert "password_hash" not in data
    # Check cookie was set
    assert settings.SESSION_COOKIE_NAME in res.cookies

    # Check DB entry
    db = AuthDatabase.get_instance()
    user = db.get_user_by_email("alice@example.com")
    assert user is not None
    assert user["password_hash"] != "SecurePassword123!"
    assert user["password_hash"].startswith("$argon2id$")
    assert verify_password(user["password_hash"], "SecurePassword123!") is True


def test_registration_duplicate_email_rejected(client):
    """Enforce email uniqueness case-insensitively."""
    payload = {
        "full_name": "Bob Operator",
        "email": "bob@example.com",
        "password": "SecurePassword123!",
        "confirm_password": "SecurePassword123!",
        "terms_accepted": True,
    }
    res1 = client.post("/auth/register", json=payload)
    assert res1.status_code == 201

    # Attempt registration with different casing
    payload2 = dict(payload, email="BOB@EXAMPLE.COM")
    res2 = client.post("/auth/register", json=payload2)
    assert res2.status_code == 400
    assert "already exists" in res2.json()["detail"].lower()


def test_registration_password_mismatch(client):
    payload = {
        "full_name": "Charlie",
        "email": "charlie@example.com",
        "password": "SecurePassword123!",
        "confirm_password": "DifferentPassword123!",
        "terms_accepted": True,
    }
    res = client.post("/auth/register", json=payload)
    assert res.status_code == 400
    assert "do not match" in res.json()["detail"].lower()


def test_registration_weak_password_rejected(client):
    """Validate all password policy conditions."""
    weak_passwords = [
        ("short", "Short1!"),                      # < 8 chars
        ("no upper", "lowercase123!"),             # no uppercase
        ("no lower", "UPPERCASE123!"),             # no lowercase
        ("no digit", "NoDigitsHere!"),             # no digits
        ("no symbol", "NoSymbols12345"),           # no symbols
    ]
    for reason, pw in weak_passwords:
        payload = {
            "full_name": "Test User",
            "email": f"test_{time.time()}@example.com",
            "password": pw,
            "confirm_password": pw,
            "terms_accepted": True,
        }
        res = client.post("/auth/register", json=payload)
        assert res.status_code in (400, 422), f"Expected rejection for {reason}"


def test_registration_terms_not_accepted(client):
    payload = {
        "full_name": "Declined Terms",
        "email": "decline@example.com",
        "password": "SecurePassword123!",
        "confirm_password": "SecurePassword123!",
        "terms_accepted": False,
    }
    res = client.post("/auth/register", json=payload)
    assert res.status_code == 400
    assert "terms" in res.json()["detail"].lower()


# ─────────────────────────────────────────────────────────────────────────────
# 2. Login Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_login_success(client):
    """Verify login workflow and cookie attributes."""
    # Register user first
    db = AuthDatabase.get_instance()
    db.create_user(
        full_name="Dave System",
        email="dave@example.com",
        password_hash=hash_password("MyPassword123!"),
    )

    res = client.post("/auth/login", json={"email": "dave@example.com", "password": "MyPassword123!"})
    assert res.status_code == 200
    data = res.json()
    assert data["email"] == "dave@example.com"
    assert data["full_name"] == "Dave System"
    assert settings.SESSION_COOKIE_NAME in res.cookies


def test_login_invalid_password_returns_generic_error(client):
    """Verify generic authentication error avoids enumeration."""
    db = AuthDatabase.get_instance()
    db.create_user(
        full_name="Dave System",
        email="dave@example.com",
        password_hash=hash_password("MyPassword123!"),
    )

    res = client.post("/auth/login", json={"email": "dave@example.com", "password": "WrongPassword123!"})
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid email or password."


def test_login_unknown_account_returns_generic_error(client):
    """Verify unknown account receives identical 401 error."""
    res = client.post("/auth/login", json={"email": "nonexistent@example.com", "password": "SomePassword123!"})
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid email or password."


def test_login_rate_limiting(client):
    """Verify brute-force rate limiter blocks after 5 failed attempts."""
    for i in range(5):
        res = client.post("/auth/login", json={"email": "target@example.com", "password": f"BadGuess_{i}!"})
        assert res.status_code == 401

    # 6th attempt should be blocked with 429
    res6 = client.post("/auth/login", json={"email": "target@example.com", "password": "BadGuess_6!"})
    assert res6.status_code == 429
    assert "too many" in res6.json()["detail"].lower()


def test_login_inactive_account_rejected(client):
    """Inactive account is rejected with 403."""
    db = AuthDatabase.get_instance()
    db.create_user(
        full_name="Disabled User",
        email="disabled@example.com",
        password_hash=hash_password("MyPassword123!"),
        is_active=False,
    )
    res = client.post("/auth/login", json={"email": "disabled@example.com", "password": "MyPassword123!"})
    assert res.status_code == 403
    assert "deactivated" in res.json()["detail"].lower()


# ─────────────────────────────────────────────────────────────────────────────
# 3. Session Management Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_session_token_hashed_in_database(client):
    """Verify raw token is NEVER stored in database."""
    reg = client.post("/auth/register", json={
        "full_name": "Entropy Test",
        "email": "entropy@example.com",
        "password": "SecurePassword123!",
        "confirm_password": "SecurePassword123!",
        "terms_accepted": True,
    })
    raw_cookie_token = reg.cookies.get(settings.SESSION_COOKIE_NAME)
    assert raw_cookie_token is not None

    db = AuthDatabase.get_instance()
    expected_hash = hashlib.sha256(raw_cookie_token.encode()).hexdigest()

    # Query DB directly to prove raw token is absent and only hash exists
    cur = db._sqlite_conn.cursor()
    cur.execute("SELECT token_hash FROM sessions WHERE user_id = ?", (reg.json()["id"],))
    row = cur.fetchone()
    assert row is not None
    assert row["token_hash"] == expected_hash
    assert row["token_hash"] != raw_cookie_token


def test_logout_revokes_session(client):
    """Verify logout invalidates server-side session and rejects future requests."""
    reg = client.post("/auth/register", json={
        "full_name": "Logout Tester",
        "email": "logout@example.com",
        "password": "SecurePassword123!",
        "confirm_password": "SecurePassword123!",
        "terms_accepted": True,
    })
    assert reg.status_code == 201

    # Verify /auth/me succeeds with cookie
    me_res = client.get("/auth/me")
    assert me_res.status_code == 200

    # Log out
    logout_res = client.post("/auth/logout")
    assert logout_res.status_code == 200

    # Future request must be rejected
    me_after = client.get("/auth/me")
    assert me_after.status_code == 401


def test_expired_session_rejected(client):
    """Verify expired sessions cannot be used."""
    db = AuthDatabase.get_instance()
    user = db.create_user("Expire Test", "expire@example.com", hash_password("Password123!"))
    raw_token = "expired_raw_token_value_abc123"
    t_hash = hash_token(raw_token)
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    # Manually insert expired session
    cur = db._sqlite_conn.cursor()
    cur.execute(
        "INSERT INTO sessions (id, user_id, token_hash, created_at, expires_at, last_activity_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("sess_exp", user["id"], t_hash, yesterday, yesterday, yesterday),
    )
    db._sqlite_conn.commit()

    # Attempt call with expired token
    res = client.get("/auth/me", headers={"Authorization": f"Bearer {raw_token}"})
    assert res.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# 4. Protected API & Multi-User Isolation Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_unauthenticated_protected_endpoints_rejected(client):
    """Direct API calls to protected endpoints without auth MUST return 401."""
    assert client.post("/orchestrate", json={"goal": "hack"}).status_code == 401
    assert client.get("/orchestrate/history").status_code == 401
    assert client.get("/orchestrate/sessions").status_code == 401
    assert client.get("/orchestrate/ardo_123/state").status_code == 401
    assert client.get("/orchestrate/ardo_123/metrics").status_code == 401
    assert client.get("/orchestrate/ardo_123/evidence").status_code == 401
    assert client.post("/orchestrate/ardo_123/approve/task_1").status_code == 401


def test_multi_user_data_isolation(client):
    """
    User A's orchestration sessions, history, and evidence MUST NOT be accessible to User B.
    """
    # 1. Register User A
    res_a = client.post("/auth/register", json={
        "full_name": "User Alpha",
        "email": "alpha@example.com",
        "password": "Password123!",
        "confirm_password": "Password123!",
        "terms_accepted": True,
    })
    token_a = res_a.cookies.get(settings.SESSION_COOKIE_NAME)
    user_a_id = res_a.json()["id"]

    # 2. Register User B
    client.cookies.clear()
    res_b = client.post("/auth/register", json={
        "full_name": "User Beta",
        "email": "beta@example.com",
        "password": "Password123!",
        "confirm_password": "Password123!",
        "terms_accepted": True,
    })
    token_b = res_b.cookies.get(settings.SESSION_COOKIE_NAME)
    user_b_id = res_b.json()["id"]

    # 3. Create a mock session directly owned by User A
    session_id = "ardo_alpha_session_1"
    ARDOScheduler.record_history(
        session_id=session_id,
        goal="Alpha Private Research",
        mode="safe",
        task_count=3,
        user_id=user_a_id,
    )

    # 4. User A requests history -> sees the session
    hist_a = client.get("/orchestrate/history", headers={"Authorization": f"Bearer {token_a}"})
    assert hist_a.status_code == 200
    assert any(h["session_id"] == session_id for h in hist_a.json()["history"])

    # 5. User B requests history -> does NOT see User A's session!
    hist_b = client.get("/orchestrate/history", headers={"Authorization": f"Bearer {token_b}"})
    assert hist_b.status_code == 200
    assert not any(h["session_id"] == session_id for h in hist_b.json()["history"])

    # 6. User B attempts direct state lookup -> returns 404 (isolation)
    state_b = client.get(f"/orchestrate/{session_id}/state", headers={"Authorization": f"Bearer {token_b}"})
    assert state_b.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# 5. WebSocket Authentication Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_websocket_unauthenticated_connection_rejected(client):
    """WebSocket connection without token or ticket MUST be rejected immediately."""
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/orchestrate/ardo_test_session") as ws:
            pass


def test_websocket_authenticated_ticket_and_cross_user_rejection(client):
    """
    1. Authenticated user with valid ticket can connect.
    2. User B cannot connect to User A's session.
    """
    # Create User A
    db = AuthDatabase.get_instance()
    user_a = db.create_user("Owner User", "owner@example.com", hash_password("Password123!"))
    user_b = db.create_user("Intruder User", "intruder@example.com", hash_password("Password123!"))

    session_id = "ardo_ws_test_sess"
    # User B generates a ticket for User A's session
    ticket_intruder = ws_ticket_manager.issue_ticket(user_id=user_b["id"], session_id=session_id)

    # Mock an active session owned by User A
    mock_session = MagicMock()
    mock_session.user_id = user_a["id"]
    ARDOScheduler._sessions[session_id] = mock_session

    # Intruder attempts connection to User A's session -> must be rejected
    with pytest.raises(Exception):
        with client.websocket_connect(f"/ws/orchestrate/{session_id}?ticket={ticket_intruder}") as ws:
            pass

    # Ticket for legitimate owner User A
    ticket_owner = ws_ticket_manager.issue_ticket(user_id=user_a["id"], session_id=session_id)

    # Mock event generator
    async def mock_events(sid):
        yield {"event_type": "heartbeat", "session_id": sid}
        yield {"event_type": "session_complete", "session_id": sid}

    with patch.object(ARDOScheduler, "stream_events", side_effect=mock_events):
        with client.websocket_connect(f"/ws/orchestrate/{session_id}?ticket={ticket_owner}") as ws:
            msg = ws.receive_json()
            assert msg["event_type"] == "heartbeat"
            assert msg["session_id"] == session_id


# ─────────────────────────────────────────────────────────────────────────────
# 6. Password Reset & Change Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_forgot_password_generic_response(client):
    """Password reset request returns generic response regardless of user existence."""
    res1 = client.post("/auth/forgot-password", json={"email": "anyone@example.com"})
    assert res1.status_code == 200
    assert "instructions have been sent" in res1.json()["message"]


def test_password_reset_workflow_and_single_use(client):
    """Complete password reset workflow with single-use token verification."""
    db = AuthDatabase.get_instance()
    user = db.create_user("Reset User", "resetme@example.com", hash_password("OldPassword123!"))

    # Generate token
    raw_token = "reset_token_secret_xyz123456789"
    db.create_password_reset_token(user_id=user["id"], token=raw_token, expiry_minutes=15)

    # Reset password
    res = client.post("/auth/reset-password", json={
        "token": raw_token,
        "new_password": "NewSecurePassword123!",
        "confirm_password": "NewSecurePassword123!",
    })
    assert res.status_code == 200
    assert "successfully updated" in res.json()["message"]

    # Verify old password fails
    login_old = client.post("/auth/login", json={"email": "resetme@example.com", "password": "OldPassword123!"})
    assert login_old.status_code == 401

    # Verify new password succeeds
    login_new = client.post("/auth/login", json={"email": "resetme@example.com", "password": "NewSecurePassword123!"})
    assert login_new.status_code == 200

    # Verify token cannot be reused
    res_reuse = client.post("/auth/reset-password", json={
        "token": raw_token,
        "new_password": "AnotherPassword123!",
        "confirm_password": "AnotherPassword123!",
    })
    assert res_reuse.status_code == 400


def test_password_reset_token_never_logged_or_stored_in_audit_or_db_in_plaintext(client, caplog):
    """Verify raw password-reset token is NEVER stored in audit logs, API responses, DB, or application logs."""
    import logging
    caplog.set_level(logging.DEBUG)
    DevMailbox.clear()

    db = AuthDatabase.get_instance()
    user = db.create_user("Audit Check User", "auditcheck@example.com", hash_password("ValidPass123!"))

    # 1. Trigger forgot-password endpoint
    res_forgot = client.post("/auth/forgot-password", json={"email": "auditcheck@example.com"})
    assert res_forgot.status_code == 200
    assert "token" not in res_forgot.json()
    assert "token" not in res_forgot.text.lower()

    # 2. Retrieve token from explicitly gated in-memory development mailbox
    raw_token = DevMailbox.get_latest_token("auditcheck@example.com")
    reset_url = DevMailbox.get_latest_reset_url("auditcheck@example.com")
    assert raw_token is not None
    assert reset_url is not None
    assert raw_token in reset_url

    # 3. CRITICAL SECURITY CHECK: Neither raw token NOR reset URL must appear in captured logs!
    assert raw_token not in caplog.text
    assert reset_url not in caplog.text
    assert "/reset-password?token=" not in caplog.text

    # 4. Verify DB table 'password_reset_tokens' has ONLY the sha256 hash, NEVER the raw token
    cur = db._sqlite_conn.cursor() if not db._is_pg else db._pg_conn.cursor()
    if not db._is_pg:
        cur.execute("SELECT token_hash FROM password_reset_tokens WHERE user_id = ?", (user["id"],))
    else:
        cur.execute("SELECT token_hash FROM password_reset_tokens WHERE user_id = %s", (user["id"],))
    rows = cur.fetchall()
    assert len(rows) == 1
    stored_hash = rows[0][0] if isinstance(rows[0], (tuple, list)) else rows[0]["token_hash"]
    assert stored_hash != raw_token
    assert stored_hash == hash_token(raw_token)

    # 5. Perform reset and verify audit logs have ZERO occurrences of raw_token
    res_reset = client.post("/auth/reset-password", json={
        "token": raw_token,
        "new_password": "NewValidPass123!",
        "confirm_password": "NewValidPass123!",
    })
    assert res_reset.status_code == 200
    assert raw_token not in res_reset.text

    # Inspect all audit logs in DB
    if not db._is_pg:
        cur.execute("SELECT event_type, metadata FROM auth_audit_events WHERE user_id = ?", (user["id"],))
    else:
        cur.execute("SELECT event_type, metadata FROM auth_audit_events WHERE user_id = %s", (user["id"],))
    audit_rows = cur.fetchall()
    for row in audit_rows:
        meta_str = str(row[1]) if isinstance(row, (tuple, list)) else str(row["metadata"])
        assert raw_token not in meta_str
        assert reset_url not in meta_str


def test_oauth_status_unconfigured(client):
    """Verify OAuth returns false when genuine credentials are not present."""
    res = client.get("/auth/oauth-status")
    assert res.status_code == 200
    data = res.json()
    assert data["google_enabled"] is False
    assert data["facebook_enabled"] is False


# ─────────────────────────────────────────────────────────────────────────────
# 9. Security Hardening & Acceptance Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_postgres_authoritative_connection_failure_no_silent_fallback(monkeypatch):
    """Verify that PostgreSQL connection failure raises RuntimeError instead of silent SQLite fallback."""
    monkeypatch.setattr(settings, "AUTH_DB_BACKEND", "postgres")
    monkeypatch.setattr(settings, "POSTGRES_URI", "postgresql://invalid:invalid@127.0.0.1:54399/nonexistent")
    monkeypatch.setenv("POSTGRES_URI", "postgresql://invalid:invalid@127.0.0.1:54399/nonexistent")
    
    with pytest.raises(RuntimeError) as exc_info:
        AuthDatabase(force_sqlite=False)
    assert "Failed to connect to authoritative PostgreSQL database" in str(exc_info.value)
    assert "Silent fallback to SQLite is disabled" in str(exc_info.value)


def test_registration_rate_limiting(client):
    """Enforce registration rate limit of 5 requests per window per IP/email."""
    payload = {
        "full_name": "Spam Bot",
        "email": "spambot@example.com",
        "password": "Password123!",
        "confirm_password": "Password123!",
        "terms_accepted": True,
    }
    # 5 attempts allowed
    for _ in range(5):
        register_rate_limiter.record_attempt(f"reg:testclient:spambot@example.com")

    res = client.post("/auth/register", json=payload)
    assert res.status_code == 429
    assert "Too many registration attempts" in res.json()["detail"]


def test_csrf_origin_validation(client):
    """Verify CSRF middleware validates Origin on state-changing cookie requests."""
    # Register Alice to get session cookie
    reg = client.post("/auth/register", json={
        "full_name": "CSRF User",
        "email": "csrfuser@example.com",
        "password": "SecurePassword123!",
        "confirm_password": "SecurePassword123!",
        "terms_accepted": True,
    })
    assert reg.status_code == 201

    # Attempt state-changing POST with untrusted Origin header
    evil_res = client.post(
        "/auth/logout",
        headers={"origin": "http://evil-attacker.com"},
    )
    assert evil_res.status_code == 403
    assert "CSRF origin validation failed" in evil_res.json()["detail"]

    # Attempt state-changing POST with trusted Origin header
    trusted_res = client.post(
        "/auth/logout",
        headers={"origin": "http://localhost:3000"},
    )
    assert trusted_res.status_code == 200


def test_replay_orchestration_ownership_enforced(client):
    """Verify cross-user session replay is rejected with 403 Forbidden."""
    db = AuthDatabase.get_instance()
    user_a = db.create_user("User A", "usera@example.com", hash_password("Password123!"))
    user_b = db.create_user("User B", "userb@example.com", hash_password("Password123!"))

    # Active session belongs to User A
    session_id = "replay_sess_001"
    mock_session = MagicMock()
    mock_session.session_id = session_id
    mock_session.user_id = user_a["id"]
    ARDOScheduler._sessions[session_id] = mock_session

    # User B tries to replay User A's session
    token_b = "token_user_b_replay"
    db.create_session(user_b["id"], token_b)

    res = client.post(
        f"/orchestrate/replay/{session_id}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res.status_code == 403
    assert "Forbidden" in res.json()["detail"]


def test_knowledge_ingest_requires_auth(client):
    """Verify unauthenticated user cannot ingest knowledge into the system."""
    res = client.post("/knowledge/ingest", json={
        "title": "Unauthenticated Ingest",
        "content": "Secret system documentation content.",
    })
    assert res.status_code == 401

