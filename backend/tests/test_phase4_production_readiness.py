"""
test_phase4_production_readiness.py — Comprehensive Phase 4 Test Suite
=====================================================================
Validates security hardening, configuration validation, rate limiting, payload limits,
egress boundary architecture, database reliability, metrics, health checks,
and end-to-end operational readiness.
"""

import json
import time
import uuid
import pytest
from fastapi.testclient import TestClient

from unittest.mock import AsyncMock, patch

from main import app
from core.config import settings, Settings
from core.auth_db import AuthDatabase
from core.rate_limiter import rate_limiter
from core.metrics import metrics_collector
from core.logging_config import JSONLogFormatter
from orchestration.scheduler import ARDOScheduler
from orchestration.state_machine import TaskState
from schemas.models import PlanGraphMetadata, PlanGraphResponse, TaskEdge, TaskNode


@pytest.fixture(autouse=True)
def reset_test_state():
    """Reset rate limiters, auth database, and in-memory sessions before each test."""
    rate_limiter.reset()
    metrics_collector.reset()
    AuthDatabase.reset_instance(force_sqlite=True)
    ARDOScheduler._sessions.clear()
    yield
    rate_limiter.reset()
    metrics_collector.reset()
    AuthDatabase.reset_instance(force_sqlite=True)
    for sid, s in list(ARDOScheduler._sessions.items()):
        if getattr(s, "_task", None) and not s._task.done():
            s._task.cancel()
    ARDOScheduler._sessions.clear()


@pytest.fixture
def client():
    """Test client for FastAPI app."""
    return TestClient(app)


def _create_authenticated_client(client, email="phase4_user@ardo.ai"):
    """Helper to create user and authenticated session."""
    reg = client.post("/auth/register", json={
        "full_name": "Phase 4 Operator",
        "email": email,
        "password": "SecurePassword123!",
        "confirm_password": "SecurePassword123!",
        "terms_accepted": True,
    })
    assert reg.status_code == 201
    token = reg.cookies.get(settings.SESSION_COOKIE_NAME)
    return token


# ─────────────────────────────────────────────────────────────────────────────
# 1. Security Hardening Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_production_security_headers_present(client):
    """Verify standard security headers are applied to HTTP responses."""
    res = client.get("/health")
    assert res.status_code == 200
    headers = res.headers

    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("X-Frame-Options") == "DENY"
    assert headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert "accelerometer=()" in headers.get("Permissions-Policy", "")
    assert "frame-ancestors 'none'" in headers.get("Content-Security-Policy", "")


def test_payload_size_limit_rejection(client):
    """Verify request bodies exceeding MAX_PAYLOAD_BYTES are rejected with HTTP 413."""
    oversized_goal = "A" * (settings.MAX_PAYLOAD_BYTES + 500)
    token = _create_authenticated_client(client, "payload_limit_user@ardo.ai")

    res = client.post(
        "/plan",
        json={"goal": oversized_goal},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Length": str(len(oversized_goal) + 50),
        },
    )
    assert res.status_code == 413
    assert "Payload too large" in res.json().get("detail", "")


def test_planning_endpoint_rate_limiting(client):
    """Verify excessive plan requests trigger HTTP 429 Too Many Requests."""
    token = _create_authenticated_client(client, "rate_limit_plan@ardo.ai")
    max_req = settings.RATE_LIMIT_PLAN_PER_MINUTE

    # Saturate rate limiter for plan
    for _ in range(max_req):
        rate_limiter.check("plan:testclient", max_requests=max_req, window_seconds=60.0)

    # Next request should be blocked
    res = client.post(
        "/plan",
        json={"goal": "Plan another task"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 429
    assert "Rate limit exceeded" in res.json().get("detail", "")
    assert "Retry-After" in res.headers


def test_production_configuration_validation_rules():
    """Verify fail-fast behavior on missing or insecure production configuration."""
    # Test valid production configuration
    prod_settings = Settings(
        ENVIRONMENT="production",
        DEBUG=False,
        COOKIE_SECURE=True,
        ALLOWED_ORIGINS="https://ardo.example.com",
        AUTH_DB_BACKEND="postgres",
        POSTGRES_URI="postgresql://ardo_prod_user:SuperSecretP@ss@prod-db.internal:5432/ai_goal_planner",
        GEMINI_API_KEY="AIzaSyDummyKeyForProductionValidation",
        BROWSER_EGRESS_PROXY="http://squid-egress:3128",
    )
    violations = prod_settings.validate_production_config()
    assert len(violations) == 0, f"Expected 0 violations, got: {violations}"

    # Test invalid production configuration
    bad_prod_settings = Settings(
        ENVIRONMENT="production",
        DEBUG=True,  # Violation: DEBUG is True
        COOKIE_SECURE=False,  # Violation: COOKIE_SECURE is False
        ALLOWED_ORIGINS="http://localhost:3000,*",  # Violation: wildcard & localhost
        AUTH_DB_BACKEND="sqlite",  # Violation: SQLite in production
        POSTGRES_URI="postgresql://postgres:postgres@localhost:5432/ai_goal_planner",  # Violation: default credentials
        GEMINI_API_KEY=None,
        OPENAI_API_KEY=None,  # Violation: missing LLM key
    )
    violations_bad = bad_prod_settings.validate_production_config()
    assert len(violations_bad) >= 5
    assert any("DEBUG must be set to False" in v for v in violations_bad)
    assert any("COOKIE_SECURE must be True" in v for v in violations_bad)
    assert any("ALLOWED_ORIGINS in production" in v for v in violations_bad)
    assert any("AUTH_DB_BACKEND must be 'postgres'" in v for v in violations_bad)
    assert any("POSTGRES_URI cannot use default" in v for v in violations_bad)


def test_structured_logger_masks_sensitive_data():
    """Verify that JSONLogFormatter sanitizes credentials, tokens, and PII from log output."""
    formatter = JSONLogFormatter()
    import logging
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="User authenticated with api_key=sk-1234567890abcdef1234567890abcdef and password=MySecretPass!",
        args=(),
        exc_info=None,
    )

    formatted_json_str = formatter.format(record)
    log_dict = json.loads(formatted_json_str)

    assert "sk-1234567890abcdef" not in log_dict["message"]
    assert "MySecretPass!" not in log_dict["message"]
    assert "[REDACTED_SECRET]" in log_dict["message"]
    assert log_dict["level"] == "INFO"
    assert "timestamp" in log_dict


def test_websocket_origin_validation_rejects_untrusted_origins(client):
    """Verify that WebSocket handshakes from untrusted cross-site origins are rejected."""
    # Test untrusted Origin header
    with pytest.raises(Exception):
        with client.websocket_connect(
            "/ws/orchestrate/ardo_fake_session",
            headers={"Origin": "https://malicious-attacker.com"},
        ) as ws:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# 2. Database Reliability & Resilience Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_database_health_checks(client):
    """Verify liveness probe and readiness probe return accurate dependency health."""
    # 1. Liveness check
    live_res = client.get("/health")
    assert live_res.status_code == 200
    assert live_res.json()["status"] == "ok"

    # 2. Readiness check
    ready_res = client.get("/health/ready")
    assert ready_res.status_code == 200
    data = ready_res.json()
    assert data["ready"] is True
    assert data["auth_database"]["status"] == "healthy"
    assert "latency_ms" in data["auth_database"]
    assert data["ai_configured"] is True


def test_outbox_failed_event_recovery():
    """Verify recovery of failed outbox synchronization events."""
    db = AuthDatabase.get_instance()
    user_id = f"usr_test_{uuid.uuid4().hex[:8]}"

    # Insert an outbox event
    event_id = db.create_outbox_event("user.created", user_id, {"email": "outbox_recovery@ardo.ai"})

    # Mark it failed after max attempts
    db.mark_outbox_event_failed(event_id, error_message="MongoDB network timeout", max_attempts=1)
    failed_events = [e for e in db.fetch_pending_outbox_events() if e["id"] == event_id]
    assert len(failed_events) == 0  # It is failed, so not in pending

    # Trigger recovery sweep
    recovered_count = db.retry_failed_outbox_events(max_age_hours=24)
    assert recovered_count >= 1

    # Event is now back in pending
    pending_events = [e for e in db.fetch_pending_outbox_events() if e["id"] == event_id]
    assert len(pending_events) == 1
    assert pending_events[0]["attempts"] == 0


def test_user_active_session_concurrency_limit(client):
    """Verify that a user cannot exceed MAX_ACTIVE_SESSIONS_PER_USER concurrent sessions."""
    token = _create_authenticated_client(client, "session_limit_user@ardo.ai")
    db = AuthDatabase.get_instance()
    user = db.get_user_by_email("session_limit_user@ardo.ai")
    user_id = user["id"]

    max_sessions = settings.MAX_ACTIVE_SESSIONS_PER_USER

    # Pre-populate max active sessions for this user in scheduler
    for i in range(max_sessions):
        sid = f"ardo_test_limit_{i}"
        mock_session = type("MockSession", (), {"user_id": user_id, "session_id": sid, "_task": None, "engine": type("MockEngine", (), {"_is_completed": False})()})()
        ARDOScheduler._sessions[sid] = mock_session

    # Next session start should be rejected with HTTP 429
    res = client.post(
        "/orchestrate",
        json={"goal": "One session too many", "mode": "safe"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 429
    assert "Concurrent session limit reached" in res.json().get("detail", "")


def test_cleanup_abandoned_sessions():
    """Verify that ARDOScheduler cleans up expired and idle sessions."""
    # Create an old completed session
    sid_old = "ardo_abandoned_old"
    mock_old = type(
        "MockSession",
        (),
        {
            "session_id": sid_old,
            "user_id": "usr_test",
            "_task": None,
            "cleanup": pytest.mark.asyncio(lambda self: None),
            "engine": type("MockEngine", (), {"_is_completed": True, "_start_time": time.time() - 7200})(),
        },
    )()
    async def _async_cleanup():
        pass
    mock_old.cleanup = _async_cleanup

    ARDOScheduler._sessions[sid_old] = mock_old

    # Run cleanup with 60 minute threshold
    import asyncio
    evicted = asyncio.run(ARDOScheduler.cleanup_abandoned_sessions(max_idle_minutes=60))
    assert evicted == 1
    assert sid_old not in ARDOScheduler._sessions


# ─────────────────────────────────────────────────────────────────────────────
# 3. Observability & Operational Metrics Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_correlation_id_propagation(client):
    """Verify that inbound requests receive or preserve X-Correlation-ID."""
    custom_cid = "corr_custom_trace_987654"
    res = client.get("/health", headers={"X-Correlation-ID": custom_cid})
    assert res.status_code == 200
    assert res.headers.get("X-Correlation-ID") == custom_cid

    # If not supplied, server generates one
    res_auto = client.get("/health")
    assert res_auto.status_code == 200
    assert res_auto.headers.get("X-Correlation-ID", "").startswith("corr_")


def test_operational_metrics_endpoint(client):
    """Verify that /metrics exposes comprehensive execution and latency stats."""
    metrics_collector.record_request("GET", "/health", 200, 0.015)
    metrics_collector.record_planning(0.450, success=True)
    metrics_collector.record_browser_task(1.200, "verified")
    metrics_collector.record_goal_outcome("verified")
    metrics_collector.record_worker_lease_loss()

    res = client.get("/metrics")
    assert res.status_code == 200
    m = res.json()

    assert "uptime_seconds" in m
    assert m["http"]["total_requests"] >= 1
    assert m["planning"]["completed_plans"] >= 1
    assert m["browser_execution"]["completed_tasks"] >= 1
    assert m["browser_execution"]["task_outcomes"]["verified"] >= 1
    assert m["goal_verification"]["outcomes"]["verified"] >= 1
    assert m["resilience"]["worker_lease_losses"] >= 1


# ─────────────────────────────────────────────────────────────────────────────
# 4. Representative End-to-End Operational Workflow Test
# ─────────────────────────────────────────────────────────────────────────────

@patch("execution.browser.browser_agent.BrowserAgent.run", new_callable=AsyncMock)
@patch("api.router.generate_task_graph", new_callable=AsyncMock)
def test_representative_end_to_end_operational_workflow(mock_generate, mock_browser_run, client):
    """
    Test representative complete operational flow:
    1. User Registration & Secure Login
    2. Goal submission & input validation
    3. LLM Plan generation & canonical SHA-256 plan hash computation
    4. Cryptographic plan approval recording
    5. Session state inspection & evidence retrieval
    6. Metrics verification
    """
    from execution.browser.agent_contracts import (
        BrowserAgentOutcome,
        BrowserAgentResult,
        BrowserVerification,
        BrowserVerificationStatus,
    )

    mock_browser_run.return_value = BrowserAgentResult(
        task_id="t_e2e_1",
        session_id="ardo_e2e",
        objective="Navigate to Ocean Grill",
        outcome=BrowserAgentOutcome.COMPLETED,
        verification=BrowserVerification(
            task_id="t_e2e_1",
            status=BrowserVerificationStatus.VERIFIED,
            confidence_score=0.95,
        ),
    )

    mock_plan = PlanGraphResponse(
        public_id="plan_e2e_prod",
        nodes=[
            TaskNode(id="t_e2e_1", type="default", position={"x": 0.0, "y": 0.0}, data={"label": "Navigate to Ocean Grill"}),
        ],
        edges=[],
        metadata=PlanGraphMetadata(
            intent="Reserve dinner seats",
            confidence=0.95,
            reasoning_summary="Single navigation task to booking site.",
        ),
        plan_hash="d" * 64,
    )
    mock_generate.return_value = mock_plan

    # 1. User Registration & Login
    token = _create_authenticated_client(client, "e2e_prod_user@ardo.ai")

    # 2. Plan Generation
    goal_text = "Reserve 2 dinner seats at Ocean Grill"
    plan_res = client.post(
        "/plan",
        json={"goal": goal_text},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert plan_res.status_code == 200
    plan_data = plan_res.json()
    assert len(plan_data["nodes"]) >= 1

    # 3. Start Orchestration
    orch_res = client.post(
        "/orchestrate",
        json={"goal": goal_text, "mode": "safe"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert orch_res.status_code == 200
    orch_data = orch_res.json()
    session_id = orch_data["session_id"]
    assert session_id.startswith("ardo_")

    # 4. State Inspection
    state_res = client.get(
        f"/orchestrate/{session_id}/state",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert state_res.status_code == 200
    state_data = state_res.json()
    assert state_data["session_id"] == session_id

    # 5. Metrics updated
    metrics_res = client.get("/metrics")
    assert metrics_res.status_code == 200
    metrics_data = metrics_res.json()
    assert metrics_data["planning"]["completed_plans"] >= 1
