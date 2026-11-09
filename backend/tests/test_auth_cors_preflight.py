"""
test_auth_cors_preflight.py — Rigorous Acceptance Tests for Auth CORS Preflights & Connectivity
==============================================================================================
Validates:
1. OPTIONS /auth/me preflight returns 200 OK with no redirect and valid CORS headers
2. OPTIONS /auth/oauth-status preflight returns 200 OK with no redirect and valid CORS headers
3. OPTIONS /auth/register preflight returns 200 OK with no redirect and valid CORS headers
4. OPTIONS /api/v1/auth/* preflight routes also return 200 OK with no redirect
5. Allowed origins (http://localhost:3000, http://127.0.0.1:3000) are permitted with credentials
6. Disallowed origins (e.g. http://evil.com) are rejected / do not receive CORS allow headers
7. Outermost CORSMiddleware ensures error responses (403 CSRF, 422 validation, etc.) retain CORS headers
8. Trailing slash normalization does not issue preflight redirects
9. Complete authentication lifecycle (Register -> /auth/me -> Logout -> /auth/me 401 -> Login -> /auth/me)
"""

from __future__ import annotations

from unittest.mock import AsyncMock
import pytest
from fastapi.testclient import TestClient

from core.auth_db import AuthDatabase
from core.config import settings
from core.security import DevMailbox, login_rate_limiter, register_rate_limiter, reset_rate_limiter
from core.sync_worker import ProfileSyncWorker
from main import app
from orchestration.scheduler import ARDOScheduler


@pytest.fixture(autouse=True)
def setup_cors_test_env(monkeypatch):
    """Ensure in-memory auth DB, clean rate limiters, and mock DB connect."""
    AuthDatabase.reset_instance(force_sqlite=True)
    ProfileSyncWorker.reset_instance()
    DevMailbox.clear()
    login_rate_limiter.reset_all()
    register_rate_limiter.reset_all()
    reset_rate_limiter.reset_all()
    ARDOScheduler._sessions.clear()
    ARDOScheduler._session_history.clear()
    monkeypatch.setattr("core.database.Database.connect_db", AsyncMock())
    yield


@pytest.fixture
def client():
    return TestClient(app)


class TestAuthCORSPreflight:
    """Targeted preflight test suite for authentication endpoints."""

    @pytest.mark.parametrize("path,method", [
        ("/auth/me", "GET"),
        ("/auth/oauth-status", "GET"),
        ("/auth/register", "POST"),
        ("/auth/login", "POST"),
        ("/auth/logout", "POST"),
        ("/auth/forgot-password", "POST"),
        ("/auth/reset-password", "POST"),
        ("/api/v1/auth/me", "GET"),
        ("/api/v1/auth/oauth-status", "GET"),
        ("/api/v1/auth/register", "POST"),
    ])
    def test_preflight_options_returns_200_without_redirect(self, client: TestClient, path: str, method: str):
        """Preflight OPTIONS requests must return 200 OK immediately with no 3xx redirect."""
        res = client.options(
            path,
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": method,
                "Access-Control-Request-Headers": "content-type, authorization",
            },
        )
        assert res.status_code == 200
        assert "location" not in res.headers
        assert res.headers.get("access-control-allow-origin") == "http://localhost:3000"
        assert res.headers.get("access-control-allow-credentials") == "true"
        allow_methods = res.headers.get("access-control-allow-methods", "")
        assert method in allow_methods or "*" in allow_methods

    def test_preflight_from_127_0_0_1_allowed(self, client: TestClient):
        """127.0.0.1:3000 origin is permitted alongside localhost:3000."""
        res = client.options(
            "/auth/register",
            headers={
                "Origin": "http://127.0.0.1:3000",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert res.status_code == 200
        assert res.headers.get("access-control-allow-origin") == "http://127.0.0.1:3000"
        assert res.headers.get("access-control-allow-credentials") == "true"

    def test_preflight_from_untrusted_origin_rejected(self, client: TestClient):
        """Untrusted origins must not receive an Access-Control-Allow-Origin header echoing them."""
        res = client.options(
            "/auth/register",
            headers={
                "Origin": "http://evil.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        # CORSMiddleware rejects untrusted preflights with 400 or omits allow header
        assert res.headers.get("access-control-allow-origin") != "http://evil.com"

    def test_trailing_slash_options_does_not_redirect(self, client: TestClient):
        """Trailing slash OPTIONS requests must never return 301/307/308 redirects."""
        res = client.options(
            "/auth/register/",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert res.status_code == 200
        assert "location" not in res.headers
        assert res.headers.get("access-control-allow-origin") == "http://localhost:3000"

    def test_cors_headers_present_on_error_responses(self, client: TestClient):
        """
        Outermost CORSMiddleware must ensure that 4xx errors (such as 422 validation errors)
        preserve Access-Control-Allow-Origin headers so browsers do not mask real errors as CORS failures.
        """
        # Invalid body triggers 422 Unprocessable Entity
        res = client.post(
            "/auth/register",
            json={"invalid": "payload"},
            headers={"Origin": "http://localhost:3000"},
        )
        assert res.status_code == 422
        assert res.headers.get("access-control-allow-origin") == "http://localhost:3000"
        assert res.headers.get("access-control-allow-credentials") == "true"

    def test_csrf_middleware_exempts_options(self, client: TestClient):
        """CSRF Origin Validation Middleware must strictly exempt OPTIONS requests."""
        res = client.options(
            "/auth/me",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert res.status_code == 200


class TestAuthLifecycleEndToEnd:
    """Verifies end-to-end registration, login, session inspection, and logout."""

    def test_registration_and_session_flow(self, client: TestClient):
        # 1. Preflight register
        preflight_res = client.options(
            "/auth/register",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert preflight_res.status_code == 200
        assert "location" not in preflight_res.headers

        # 2. Actual register request
        register_payload = {
            "full_name": "Integration User",
            "email": "integration@ardo.local",
            "password": "SecurePassword123!",
            "confirm_password": "SecurePassword123!",
        }
        res = client.post(
            "/auth/register",
            json=register_payload,
            headers={"Origin": "http://localhost:3000"},
        )
        assert res.status_code == 201
        assert res.headers.get("access-control-allow-origin") == "http://localhost:3000"
        user_body = res.json()
        assert user_body["email"] == "integration@ardo.local"
        assert "password" not in user_body
        assert "password_hash" not in user_body

        # Session cookie set by registration
        assert settings.SESSION_COOKIE_NAME in client.cookies

        # 3. GET /auth/me with session cookie
        me_res = client.get("/auth/me", headers={"Origin": "http://localhost:3000"})
        assert me_res.status_code == 200
        assert me_res.headers.get("access-control-allow-origin") == "http://localhost:3000"
        assert me_res.json()["email"] == "integration@ardo.local"

        # 4. POST /auth/logout
        logout_res = client.post("/auth/logout", headers={"Origin": "http://localhost:3000"})
        assert logout_res.status_code == 200
        assert logout_res.headers.get("access-control-allow-origin") == "http://localhost:3000"

        # 5. GET /auth/me after logout -> 401
        unauthed_res = client.get("/auth/me", headers={"Origin": "http://localhost:3000"})
        assert unauthed_res.status_code == 401
        assert unauthed_res.headers.get("access-control-allow-origin") == "http://localhost:3000"

        # 6. POST /auth/login
        login_res = client.post(
            "/auth/login",
            json={"email": "integration@ardo.local", "password": "SecurePassword123!"},
            headers={"Origin": "http://localhost:3000"},
        )
        assert login_res.status_code == 200
        assert login_res.headers.get("access-control-allow-origin") == "http://localhost:3000"
        assert settings.SESSION_COOKIE_NAME in client.cookies

        # 7. GET /auth/me after re-login
        me_res_again = client.get("/auth/me", headers={"Origin": "http://localhost:3000"})
        assert me_res_again.status_code == 200
        assert me_res_again.json()["email"] == "integration@ardo.local"

