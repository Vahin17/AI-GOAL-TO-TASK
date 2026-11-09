"""
test_oauth_and_sync.py — Automated Tests for Google/Facebook OAuth & MongoDB Profile Synchronization
=====================================================================================================
Covers:
1. Google OAuth (PKCE, state validation, token exchange, identity validation, audience check, unverified email)
2. Facebook Login (authorization code exchange, Graph API profile, missing email handling)
3. Safe Account Linking & Unlinking (multiple providers, rejection of unlinking last usable method, duplicate provider rejection)
4. Transactional Outbox & MongoDB Profile Synchronization (idempotent upserts, zero password/token leaks, resilience during MongoDB outage)
5. Multi-User Isolation on /users/me and /users/me/auth-methods
"""

from __future__ import annotations

import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.auth_db import AuthDatabase
from core.config import settings
from core.security import (
    generate_pkce_pair,
    hash_password,
    login_rate_limiter,
    oauth_state_manager,
    register_rate_limiter,
    reset_rate_limiter,
)
from core.sync_worker import ProfileSyncWorker
from main import app
from orchestration.scheduler import ARDOScheduler


@pytest.fixture(autouse=True)
def setup_test_auth_env(monkeypatch):
    """Ensure clean isolated SQLite database and reset rate limiters and schedulers."""
    AuthDatabase.reset_instance(force_sqlite=True)
    ProfileSyncWorker.reset_instance()
    login_rate_limiter.reset_all()
    register_rate_limiter.reset_all()
    reset_rate_limiter.reset_all()
    ARDOScheduler._sessions.clear()
    ARDOScheduler._session_history.clear()
    monkeypatch.setattr("core.database.Database.connect_db", AsyncMock())
    yield
    login_rate_limiter.reset_all()
    register_rate_limiter.reset_all()
    reset_rate_limiter.reset_all()
    ARDOScheduler._sessions.clear()
    ARDOScheduler._session_history.clear()
    ProfileSyncWorker.reset_instance()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def make_mock_mongo():
    """Mock MongoDB database with in-memory collection dictionary."""
    mongo = MagicMock()
    docs = {}

    async def mock_find_one(filter_query):
        doc_id = filter_query.get("_id") or filter_query.get("user_id")
        return dict(docs.get(doc_id)) if doc_id in docs else None

    async def mock_update_one(filter_query, update_query, upsert=False):
        doc_id = filter_query.get("_id")
        set_fields = update_query.get("$set", {})
        if doc_id not in docs and not upsert:
            return MagicMock(matched_count=0, modified_count=0)
        existing = docs.get(doc_id, {})
        existing.update(set_fields)
        docs[doc_id] = existing
        return MagicMock(matched_count=1, modified_count=1, upserted_id=doc_id)

    mongo.user_profiles.find_one = AsyncMock(side_effect=mock_find_one)
    mongo.user_profiles.update_one = AsyncMock(side_effect=mock_update_one)
    mongo._docs = docs
    return mongo


# ─────────────────────────────────────────────────────────────────────────────
# 1. Google OAuth Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_google_login_unconfigured_returns_503(client, monkeypatch):
    """Google OAuth returns 503 Service Unavailable when credentials are missing."""
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", None)
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", None)
    res = client.get("/auth/google")
    assert res.status_code == 503
    assert "not configured" in res.json()["detail"]


def test_google_login_initiation_generates_valid_url(client, monkeypatch):
    """Google OAuth generates state, PKCE challenge, and valid auth URL."""
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "test-google-client-id")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "test-google-secret")

    res = client.get("/auth/google?next=/app")
    assert res.status_code == 200
    data = res.json()
    assert "accounts.google.com" in data["authorization_url"]
    assert "code_challenge=" in data["authorization_url"]
    assert "state=" in data["authorization_url"]
    assert data["provider"] == "google"


def test_google_callback_invalid_state_rejected(client, monkeypatch):
    """Invalid or forged OAuth state parameter is rejected."""
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "test-google-client-id")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "test-google-secret")

    res = client.get("/auth/google/callback?code=mock_code&state=forged_state", follow_redirects=False)
    assert res.status_code == 302
    assert "error=invalid_or_expired_state" in res.headers["location"]


@pytest.mark.asyncio
async def test_google_callback_successful_authentication(client, monkeypatch):
    """Genuine Google identity creates user, links oauth_accounts, sets cookie, redirects to /app."""
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "test-google-client-id")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "test-google-secret")

    code_verifier, _ = generate_pkce_pair()
    state = oauth_state_manager.create_state(
        provider="google",
        code_verifier=code_verifier,
        next_path="/app",
    )

    mock_token_resp = MagicMock()
    mock_token_resp.status_code = 200
    mock_token_resp.json.return_value = {
        "access_token": "mock_google_access_token",
        "id_token": "mock_id_token",
    }

    mock_userinfo_resp = MagicMock()
    mock_userinfo_resp.status_code = 200
    mock_userinfo_resp.json.return_value = {
        "sub": "google_sub_123456789",
        "email": "alice.google@example.com",
        "email_verified": True,
        "name": "Alice Google",
        "picture": "https://example.com/alice.jpg",
    }

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_token_resp
    mock_client.get.return_value = mock_userinfo_resp

    with patch("httpx.AsyncClient", return_value=mock_client):
        mock_client.__aenter__.return_value = mock_client
        res = client.get(f"/auth/google/callback?code=mock_code&state={state}", follow_redirects=False)

    assert res.status_code == 302
    assert "/app" in res.headers["location"]
    assert settings.SESSION_COOKIE_NAME in res.cookies

    # Verify PostgreSQL records
    db = AuthDatabase.get_instance()
    user = db.get_user_by_email("alice.google@example.com")
    assert user is not None
    assert user["full_name"] == "Alice Google"
    assert user["is_verified"] is True

    # Verify oauth_accounts table
    oac = db.get_oauth_account("google", "google_sub_123456789")
    assert oac is not None
    assert oac["user_id"] == user["id"]
    assert oac["provider_email"] == "alice.google@example.com"


@pytest.mark.asyncio
async def test_google_callback_invalid_id_token_audience_rejected(client, monkeypatch):
    """Google ID token with untrusted/forged audience is rejected."""
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "configured-legit-client-id")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "test-google-secret")

    state = oauth_state_manager.create_state(provider="google", next_path="/app")

    # Construct mock JWT with mismatched audience
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').decode().rstrip("=")
    payload_data = {
        "iss": "https://accounts.google.com",
        "aud": "attacker-client-id.apps.googleusercontent.com",
        "sub": "sub_attacker",
        "exp": time.time() + 3600,
        "email": "attacker@example.com",
        "email_verified": True,
    }
    payload = base64.urlsafe_b64encode(json.dumps(payload_data).encode()).decode().rstrip("=")
    signature = "signature_bytes"
    forged_id_token = f"{header}.{payload}.{signature}"

    mock_token_resp = MagicMock(status_code=200, json=lambda: {
        "access_token": "valid_access_token",
        "id_token": forged_id_token,
    })

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_token_resp

    with patch("httpx.AsyncClient", return_value=mock_client):
        mock_client.__aenter__.return_value = mock_client
        res = client.get(f"/auth/google/callback?code=mock_code&state={state}", follow_redirects=False)

    assert res.status_code == 302
    assert "error=invalid_id_token_audience" in res.headers["location"]


@pytest.mark.asyncio
async def test_google_callback_unverified_email_rejected(client, monkeypatch):
    """Google accounts without verified email addresses are rejected."""
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "test-google-client-id")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "test-google-secret")

    state = oauth_state_manager.create_state(provider="google", next_path="/app")

    mock_client = AsyncMock()
    mock_client.post.return_value = MagicMock(status_code=200, json=lambda: {"access_token": "tok"})
    mock_client.get.return_value = MagicMock(
        status_code=200,
        json=lambda: {
            "sub": "unverified_sub_001",
            "email": "unverified@example.com",
            "email_verified": False,
            "name": "Unverified User",
        },
    )

    with patch("httpx.AsyncClient", return_value=mock_client):
        mock_client.__aenter__.return_value = mock_client
        res = client.get(f"/auth/google/callback?code=mock_code&state={state}", follow_redirects=False)

    assert res.status_code == 302
    assert "error=unverified_google_email" in res.headers["location"]


# ─────────────────────────────────────────────────────────────────────────────
# 2. Facebook (Meta) Login Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_facebook_login_unconfigured_returns_503(client, monkeypatch):
    """Facebook login returns 503 when credentials are not present."""
    monkeypatch.setattr(settings, "FACEBOOK_APP_ID", None)
    monkeypatch.setattr(settings, "FACEBOOK_APP_SECRET", None)
    res = client.get("/auth/facebook")
    assert res.status_code == 503
    assert "not configured" in res.json()["detail"]


@pytest.mark.asyncio
async def test_facebook_callback_successful_authentication(client, monkeypatch):
    """Facebook login authenticates user, associates account, and sets session cookie."""
    monkeypatch.setattr(settings, "FACEBOOK_APP_ID", "test-fb-app-id")
    monkeypatch.setattr(settings, "FACEBOOK_APP_SECRET", "test-fb-secret")

    state = oauth_state_manager.create_state(provider="facebook", next_path="/app")

    mock_client = AsyncMock()
    mock_client.get.side_effect = [
        MagicMock(status_code=200, json=lambda: {"access_token": "mock_fb_access_token"}),
        MagicMock(
            status_code=200,
            json=lambda: {
                "id": "fb_user_id_987654",
                "name": "Bob Facebook",
                "email": "bob.fb@example.com",
                "picture": {"data": {"url": "https://example.com/bob.jpg"}},
            },
        ),
    ]

    with patch("httpx.AsyncClient", return_value=mock_client):
        mock_client.__aenter__.return_value = mock_client
        res = client.get(f"/auth/facebook/callback?code=mock_fb_code&state={state}", follow_redirects=False)

    assert res.status_code == 302
    assert "/app" in res.headers["location"]
    assert settings.SESSION_COOKIE_NAME in res.cookies

    db = AuthDatabase.get_instance()
    user = db.get_user_by_email("bob.fb@example.com")
    assert user is not None
    oac = db.get_oauth_account("facebook", "fb_user_id_987654")
    assert oac is not None
    assert oac["user_id"] == user["id"]


@pytest.mark.asyncio
async def test_facebook_callback_missing_email_handled_safely(client, monkeypatch):
    """Users without email on Facebook are given a provisional account without crashes."""
    monkeypatch.setattr(settings, "FACEBOOK_APP_ID", "test-fb-app-id")
    monkeypatch.setattr(settings, "FACEBOOK_APP_SECRET", "test-fb-secret")

    state = oauth_state_manager.create_state(provider="facebook", next_path="/app")

    mock_client = AsyncMock()
    mock_client.get.side_effect = [
        MagicMock(status_code=200, json=lambda: {"access_token": "mock_fb_access_token"}),
        MagicMock(
            status_code=200,
            json=lambda: {
                "id": "fb_phone_only_user_555",
                "name": "Phone Facebook User",
                # email field intentionally absent
            },
        ),
    ]

    with patch("httpx.AsyncClient", return_value=mock_client):
        mock_client.__aenter__.return_value = mock_client
        res = client.get(f"/auth/facebook/callback?code=mock_fb_code&state={state}", follow_redirects=False)

    assert res.status_code == 302
    assert "/app" in res.headers["location"]

    db = AuthDatabase.get_instance()
    oac = db.get_oauth_account("facebook", "fb_phone_only_user_555")
    assert oac is not None
    user = db.get_user_by_id(oac["user_id"])
    assert user is not None
    assert "facebook.ardo.user" in user["email"]


# ─────────────────────────────────────────────────────────────────────────────
# 3. Account Linking & Unlinking Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_password_account_links_google_and_facebook(client):
    """An email/password account can safely link Google and Facebook providers."""
    db = AuthDatabase.get_instance()
    user = db.create_user("Multi User", "multi@example.com", hash_password("Password123!"))

    # Link Google
    oac_g = db.create_oauth_account(user["id"], "google", "sub_google_1", "multi@example.com", True)
    assert oac_g["user_id"] == user["id"]

    # Link Facebook
    oac_f = db.create_oauth_account(user["id"], "facebook", "sub_fb_1", "multi@example.com", True)
    assert oac_f["user_id"] == user["id"]

    # Check methods summary
    methods = db.count_user_auth_methods(user["id"])
    assert methods["has_password"] is True
    assert methods["total_methods"] == 3
    assert "password" in methods["providers"]
    assert "google" in methods["providers"]
    assert "facebook" in methods["providers"]


def test_duplicate_provider_identity_rejected(client):
    """An external identity cannot be linked to more than one ARDO user."""
    db = AuthDatabase.get_instance()
    user_a = db.create_user("User A", "usera@example.com", hash_password("Password123!"))
    user_b = db.create_user("User B", "userb@example.com", hash_password("Password123!"))

    # User A links Google sub_unique_999
    db.create_oauth_account(user_a["id"], "google", "sub_unique_999", "usera@example.com", True)

    # User B tries to link the same Google sub_unique_999
    with pytest.raises(ValueError) as exc:
        db.create_oauth_account(user_b["id"], "google", "sub_unique_999", "userb@example.com", True)
    assert "already linked" in str(exc.value)


def test_cannot_unlink_last_usable_authentication_method(client):
    """Unlinking the only available login method is strictly blocked with 400."""
    db = AuthDatabase.get_instance()
    # Create Google-only user without password
    user = db.create_user("Google Only", "googleonly@example.com", password_hash=None)
    db.create_oauth_account(user["id"], "google", "sub_google_only", "googleonly@example.com", True)

    token = "session_token_google_only"
    db.create_session(user["id"], token)

    # Attempt to unlink Google
    res = client.delete(
        "/users/me/auth-methods/google",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 400
    assert "retain at least one usable login method" in res.json()["detail"]


def test_can_unlink_when_another_method_exists(client):
    """Can unlink a provider if password or another provider remains."""
    db = AuthDatabase.get_instance()
    # User with password and Google
    user = db.create_user("Two Methods", "twomethods@example.com", hash_password("Password123!"))
    db.create_oauth_account(user["id"], "google", "sub_twomethods", "twomethods@example.com", True)

    token = "session_token_two_methods"
    db.create_session(user["id"], token)

    res = client.delete(
        "/users/me/auth-methods/google",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    assert "unlinked successfully" in res.json()["message"]

    # Verify in DB
    oac = db.get_oauth_account("google", "sub_twomethods")
    assert oac is None


# ─────────────────────────────────────────────────────────────────────────────
# 4. Transactional Outbox & MongoDB Profile Synchronization Tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_user_registration_creates_outbox_event_and_syncs_to_mongodb():
    """Registration persists user in PostgreSQL and transactional outbox event syncs to MongoDB."""
    db = AuthDatabase.get_instance()
    user = db.create_user("Sync Tester", "synctest@example.com", hash_password("Password123!"))

    # Outbox event was atomically recorded
    events = db.fetch_pending_outbox_events()
    assert len(events) >= 1
    assert any(e["aggregate_id"] == user["id"] for e in events)

    # Process outbox events with mock MongoDB
    mock_mongo = make_mock_mongo()
    with patch("core.sync_worker.get_database", return_value=mock_mongo):
        worker = ProfileSyncWorker.get_instance()
        processed = await worker.process_pending_events()
        assert processed >= 1

    # Verify MongoDB received the document
    profile_doc = mock_mongo._docs.get(user["id"])
    assert profile_doc is not None
    assert profile_doc["email"] == "synctest@example.com"
    assert profile_doc["full_name"] == "Sync Tester"
    assert "password" in profile_doc["auth_providers"]

    # Verify zero password hashes or tokens entered MongoDB
    assert "password_hash" not in profile_doc
    assert "password" not in profile_doc
    assert "token" not in profile_doc
    assert "session" not in profile_doc


@pytest.mark.asyncio
async def test_mongodb_temporary_outage_does_not_break_auth_and_retries_on_recovery():
    """Temporary MongoDB outage does not block auth operations; outbox recovers and syncs."""
    db = AuthDatabase.get_instance()
    user = db.create_user("Outage User", "outage@example.com", hash_password("Password123!"))

    # Step 1: MongoDB is offline (get_database returns None)
    with patch("core.sync_worker.get_database", return_value=None):
        worker = ProfileSyncWorker.get_instance()
        processed = await worker.process_pending_events()
        assert processed == 0  # Gracefully zero processed, no crashes

    # Events remain pending
    pending_events = db.fetch_pending_outbox_events()
    assert any(e["aggregate_id"] == user["id"] for e in pending_events)

    # Step 2: MongoDB recovers
    mock_mongo = make_mock_mongo()
    with patch("core.sync_worker.get_database", return_value=mock_mongo):
        worker = ProfileSyncWorker.get_instance()
        processed_after_recovery = await worker.process_pending_events()
        assert processed_after_recovery >= 1

    # MongoDB document created successfully
    assert user["id"] in mock_mongo._docs


@pytest.mark.asyncio
async def test_repeated_sync_events_are_idempotent():
    """Processing identical synchronization events repeatedly does not corrupt or duplicate profile."""
    db = AuthDatabase.get_instance()
    user = db.create_user("Idempotent User", "idempotent@example.com", hash_password("Password123!"))

    mock_mongo = make_mock_mongo()
    with patch("core.sync_worker.get_database", return_value=mock_mongo):
        worker = ProfileSyncWorker.get_instance()
        await worker._sync_user_to_mongodb(user["id"], mock_mongo)
        await worker._sync_user_to_mongodb(user["id"], mock_mongo)
        await worker._sync_user_to_mongodb(user["id"], mock_mongo)

    # Only 1 document exists for user
    assert len(mock_mongo._docs) == 1
    assert user["id"] in mock_mongo._docs


# ─────────────────────────────────────────────────────────────────────────────
# 5. User Profile API & Multi-User Isolation Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_get_user_profile_isolation(client):
    """User A cannot access User B's profile via /users/me."""
    db = AuthDatabase.get_instance()
    user_a = db.create_user("Alice User", "alice.p@example.com", hash_password("Password123!"))
    user_b = db.create_user("Bob User", "bob.p@example.com", hash_password("Password123!"))

    token_a = "token_alice_profile"
    token_b = "token_bob_profile"
    db.create_session(user_a["id"], token_a)
    db.create_session(user_b["id"], token_b)

    # Alice requests /users/me
    res_a = client.get("/users/me", headers={"Authorization": f"Bearer {token_a}"})
    assert res_a.status_code == 200
    assert res_a.json()["email"] == "alice.p@example.com"

    # Bob requests /users/me
    res_b = client.get("/users/me", headers={"Authorization": f"Bearer {token_b}"})
    assert res_b.status_code == 200
    assert res_b.json()["email"] == "bob.p@example.com"
    assert res_b.json()["email"] != res_a.json()["email"]


def test_patch_user_profile_updates_display_name(client):
    """User can update their display name and preferences through /users/me."""
    db = AuthDatabase.get_instance()
    user = db.create_user("Old Name", "update.me@example.com", hash_password("Password123!"))
    token = "token_update_user"
    db.create_session(user["id"], token)

    res = client.patch(
        "/users/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"full_name": "New Display Name"},
    )
    assert res.status_code == 200
    assert res.json()["full_name"] == "New Display Name"

    # Verify in PostgreSQL
    refreshed = db.get_user_by_id(user["id"])
    assert refreshed["full_name"] == "New Display Name"


def test_oauth_login_with_existing_account_does_not_auto_merge(client, monkeypatch):
    """Unauthenticated OAuth login matching an existing account email must not auto-merge."""
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "test-google-client-id")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "test-google-secret")

    # Create existing password user
    db = AuthDatabase.get_instance()
    user = db.create_user("Existing User", "existing@example.com", hash_password("Password123!"))

    # Initiates unauthenticated Google login (no linking_user_id)
    state = oauth_state_manager.create_state(provider="google", next_path="/app")

    mock_client = AsyncMock()
    mock_client.post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"access_token": "mock_google_token"},
    )
    mock_client.get.return_value = MagicMock(
        status_code=200,
        json=lambda: {
            "sub": "google_sub_unauth_attempt_123",
            "email": "existing@example.com",
            "email_verified": True,
            "name": "Attacker Impersonator",
        },
    )

    with patch("httpx.AsyncClient", return_value=mock_client):
        mock_client.__aenter__.return_value = mock_client
        res = client.get(f"/auth/google/callback?code=mock_code&state={state}", follow_redirects=False)

    # Must be redirected to /login with error explaining account already exists
    assert res.status_code == 302
    assert "/login?error=" in res.headers["location"]
    assert "already%20exists" in res.headers["location"]

    # Must NOT have linked the Google account to the existing user
    oac = db.get_oauth_account("google", "google_sub_unauth_attempt_123")
    assert oac is None

    # Existing user's auth methods remain strictly unchanged
    methods = db.count_user_auth_methods(user["id"])
    assert methods["has_password"] is True
    assert methods["total_methods"] == 1
    assert "google" not in methods["providers"]


def test_authenticated_user_can_link_oauth_account_via_linking_user_id(client, monkeypatch):
    """Authenticated user linking OAuth account through settings/state succeeds safely."""
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "test-google-client-id")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "test-google-secret")

    db = AuthDatabase.get_instance()
    user = db.create_user("Owner User", "owner@example.com", hash_password("Password123!"))

    # Linking state created by authenticated user
    state = oauth_state_manager.create_state(
        provider="google",
        linking_user_id=user["id"],
        next_path="/app",
    )

    mock_client = AsyncMock()
    mock_client.post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"access_token": "mock_google_token"},
    )
    mock_client.get.return_value = MagicMock(
        status_code=200,
        json=lambda: {
            "sub": "google_sub_owner_authorized_456",
            "email": "owner@example.com",
            "email_verified": True,
            "name": "Owner User",
        },
    )

    with patch("httpx.AsyncClient", return_value=mock_client):
        mock_client.__aenter__.return_value = mock_client
        res = client.get(f"/auth/google/callback?code=mock_code&state={state}", follow_redirects=False)

    assert res.status_code == 302
    assert "/app" in res.headers["location"]

    # Google account is now linked to user
    oac = db.get_oauth_account("google", "google_sub_owner_authorized_456")
    assert oac is not None
    assert oac["user_id"] == user["id"]

