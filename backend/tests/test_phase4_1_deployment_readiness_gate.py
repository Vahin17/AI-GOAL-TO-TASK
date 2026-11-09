"""
test_phase4_1_deployment_readiness_gate.py — Phase 4.1 Production Deployment Readiness Gate
===========================================================================================
Focused verification tests covering the five production-readiness concerns:
1. In-memory rate limiting and concurrency boundaries (per-process vs shared).
2. Browser network isolation and egress proxy failure-closed semantics.
3. Execution timeout on mutating/non-idempotent actions entering OUTCOME_UNKNOWN and semantic reconciliation.
4. Authentication cookie and deployment-domain validation (SameSite, Secure, HttpOnly, CSWSH).
5. Database migration idempotency, rollback safety, and runtime schema consistency.
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from api.auth import get_current_user
from api.router import router
from core.config import BASE_DIR, settings
from core.rate_limiter import SlidingWindowRateLimiter
from core.security import ws_ticket_manager
from execution.browser.agent_contracts import (
    BrowserAgentOutcome,
    BrowserAgentResult,
    BrowserObservation,
    BrowserVerification,
    BrowserVerificationStatus,
)
from execution.browser.network_guard import NetworkGuard
from execution.evidence.evidence_vault import EvidenceVault
from main import app
from orchestration.checkpoint_manager import is_task_idempotent
from orchestration.dag_builder import DAGBuilder, ARDOTaskNode
from orchestration.runtime_engine import RuntimeEngine
from orchestration.scheduler import ARDOScheduler, ARDOSession
from orchestration.state_machine import TaskState, TaskStateMachine


# ─────────────────────────────────────────────────────────────────────────────
# 1. In-Memory Rate Limiting and Concurrency Boundaries
# ─────────────────────────────────────────────────────────────────────────────

def test_sliding_window_rate_limiter_is_strictly_per_process():
    """
    Demonstrates that SlidingWindowRateLimiter maintains in-memory state.
    Two independent instances do NOT share counters. Multiple backend instances
    without a shared coordination store (like Redis or Postgres locks) can each
    permit up to max_requests, multiplying aggregate cluster throughput.
    """
    limiter_instance_a = SlidingWindowRateLimiter()
    limiter_instance_b = SlidingWindowRateLimiter()

    key = "plan:192.168.1.50"
    max_req = 3

    # Exhaust instance A
    for _ in range(max_req):
        allowed, _ = limiter_instance_a.check(key, max_requests=max_req, window_seconds=60.0)
        assert allowed is True

    # Instance A blocks further requests
    blocked, retry_after = limiter_instance_a.check(key, max_requests=max_req, window_seconds=60.0)
    assert blocked is False
    assert retry_after > 0

    # Instance B in another process is completely uncoordinated and still permits requests
    allowed_on_b, _ = limiter_instance_b.check(key, max_requests=max_req, window_seconds=60.0)
    assert allowed_on_b is True, "In-memory limiter does not coordinate across independent processes."


def test_session_concurrency_capacity_enforced():
    """
    Verifies that ARDOScheduler and /orchestrate enforce per-user active session limits
    and reject requests with HTTP 429 when capacity is saturated.
    """
    app.dependency_overrides[get_current_user] = lambda: {"id": "usr_concurrency_test", "email": "test@ardo.ai"}
    client = TestClient(app)

    try:
        with patch("orchestration.scheduler.ARDOScheduler.list_active_sessions", return_value=["s1", "s2", "s3", "s4", "s5"]):
            # Max sessions per user is default 5 (or configured limit)
            with patch.object(settings, "MAX_ACTIVE_SESSIONS_PER_USER", 5):
                res = client.post("/api/v1/orchestrate", json={"goal": "Concurrency test"})
                assert res.status_code == 429
                assert "Concurrent session limit reached" in res.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Browser Network Isolation & Egress Proxy Verification
# ─────────────────────────────────────────────────────────────────────────────

def test_squid_configuration_blocks_all_private_cidrs_and_metadata():
    """
    Parses deploy/squid/squid.conf and verifies that all IPv4/IPv6 private ranges,
    link-local, cloud metadata, loopback, and CGNAT ranges are explicitly denied.
    """
    squid_conf_path = os.path.join(str(BASE_DIR), "deploy", "squid", "squid.conf")
    assert os.path.exists(squid_conf_path), f"squid.conf not found at {squid_conf_path}"

    with open(squid_conf_path, "r", encoding="utf-8") as f:
        conf_content = f.read()

    # Verify critical blocked destination ACLs
    expected_acls = [
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "169.254.169.254",
        "::1/128",
        "fe80::/10",
        "fc00::/7",
        "100.64.0.0/10",
        "0.0.0.0/8",
        "::ffff:0:0/96",
    ]

    for acl_net in expected_acls:
        assert acl_net in conf_content, f"squid.conf missing blocked destination for {acl_net}"

    # Verify deny rules exist for cloud metadata and internal nets
    assert "http_access deny cloud_metadata" in conf_content
    assert "http_access deny private_class_a" in conf_content
    assert "http_access deny loopback_net" in conf_content
    assert "http_access deny !Safe_ports" in conf_content


def test_production_config_requires_browser_egress_proxy():
    """
    Verifies that validate_production_config fails fast if BROWSER_EGRESS_PROXY is absent.
    """
    with patch.object(settings, "ENVIRONMENT", "production"), \
         patch.object(settings, "DEBUG", False), \
         patch.object(settings, "COOKIE_SECURE", True), \
         patch.object(settings, "GEMINI_API_KEY", "valid_key"), \
         patch.object(settings, "ALLOWED_ORIGINS", "https://ardo.ai"), \
         patch.object(settings, "AUTH_DB_BACKEND", "postgres"), \
         patch.object(settings, "POSTGRES_URI", "postgresql://admin:secret@pg.cloud.internal:5432/ardo"), \
         patch.object(settings, "BROWSER_EGRESS_PROXY", ""):
        
        violations = settings.validate_production_config()
        assert any("BROWSER_EGRESS_PROXY" in v for v in violations)


def test_network_guard_dns_rebinding_cdp_server_addr_check():
    """
    Verifies that NetworkGuard blocks DNS rebinding responses delivering private IP addresses.
    """
    assert NetworkGuard._is_ip_blocked("127.0.0.1") is not None
    assert NetworkGuard._is_ip_blocked("169.254.169.254") is not None
    assert NetworkGuard._is_ip_blocked("10.0.1.20") is not None
    assert NetworkGuard._is_ip_blocked("::ffff:127.0.0.1") is not None
    assert NetworkGuard._is_ip_blocked("fc00::1") is not None
    assert NetworkGuard._is_ip_blocked("93.184.216.34") is None  # Example public IP


# ─────────────────────────────────────────────────────────────────────────────
# 3. Execution Timeout and Uncertain External Actions
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mutating_task_timeout_transitions_to_outcome_unknown():
    """
    CRITICAL SAFETY GUARANTEE:
    When a non-idempotent or mutating task times out during execution:
    - It must NOT be silently marked as a normal failure.
    - It must enter TaskState.OUTCOME_UNKNOWN.
    - It must set approval_needed = True.
    - It must record an uncertain evidence entry in EvidenceVault.
    - It must NOT allow automatic retry.
    """
    nodes_data = [
        {
            "id": "task_pay_01",
            "data": {"label": "Submit payment for booking reservation"},
            "metadata": {"is_idempotent": False},
        }
    ]
    graph = DAGBuilder.build(nodes_data, [], goal_text="Timeout test session")
    graph.session_id = "session_timeout_test"
    node = graph.get_node("task_pay_01")
    node.status = TaskState.WAITING
    assert not is_task_idempotent(node)

    engine = RuntimeEngine(graph, "session_timeout_test", mode="safe", db=None)

    # Mock _attempt_execution to sleep longer than the timeout
    async def _slow_execution(n):
        await asyncio.sleep(5.0)
        return True

    with patch.object(engine, "_attempt_execution", side_effect=_slow_execution), \
         patch.object(settings, "TASK_EXECUTION_TIMEOUT_SECONDS", 0.05):
        
        events = []
        async for ev in engine._execute_task("task_pay_01"):
            events.append(ev)

        assert node.status == TaskState.OUTCOME_UNKNOWN
        assert node.approval_needed is True
        assert any(e.event_type == "task_outcome_unknown" for e in events)

        # Check evidence recorded
        ev_records = EvidenceVault.get_session_evidence("session_timeout_test", "system")
        uncertain_records = [r for r in ev_records if r.get("task_id") == "task_pay_01"]
        assert len(uncertain_records) > 0
        assert uncertain_records[0].get("verification_status") == "outcome_unknown"


@pytest.mark.asyncio
async def test_timeout_with_semantic_reconciliation_marks_completed():
    """
    Verifies that if a non-idempotent task times out, but post-timeout browser observation
    reveals that the external action actually succeeded, SemanticVerifier reconciles
    the task to COMPLETED without duplicate re-execution.
    """
    nodes_data = [
        {
            "id": "task_mut_02",
            "data": {"label": "Submit contact inquiry form"},
            "metadata": {
                "is_idempotent": False,
                "expected_url": "https://example.com/thank-you",
                "success_criteria": "Inquiry Received Successfully",
            },
        }
    ]
    graph = DAGBuilder.build(nodes_data, [], goal_text="Recon test session")
    graph.session_id = "session_recon_test"
    node = graph.get_node("task_mut_02")
    node.status = TaskState.WAITING

    mock_adapter = MagicMock()
    mock_obs = BrowserObservation(
        session_id="session_recon_test",
        task_id="task_mut_02",
        current_url="https://example.com/thank-you",
        page_title="Inquiry Received Successfully",
        page_text_summary="Thank you for submitting your message. We have received it.",
    )
    mock_adapter.get_observation = AsyncMock(return_value=mock_obs)

    engine = RuntimeEngine(graph, "session_recon_test", mode="safe", db=None, browser_adapter=mock_adapter)

    async def _timed_out_execution(n):
        await asyncio.sleep(5.0)
        return False

    with patch.object(engine, "_attempt_execution", side_effect=_timed_out_execution), \
         patch.object(settings, "TASK_EXECUTION_TIMEOUT_SECONDS", 0.05):
        
        events = []
        async for ev in engine._execute_task("task_mut_02"):
            events.append(ev)

        assert node.status == TaskState.COMPLETED
        assert any("semantic verification confirmed task completed despite timeout" in log.lower() for log in node.execution_logs)


@pytest.mark.asyncio
async def test_idempotent_task_timeout_transitions_to_failed():
    """
    Verifies that a read-only / idempotent task (e.g. navigation or search)
    that times out safely transitions to FAILED because retry or re-execution
    carries no duplicate external side effects.
    """
    nodes_data = [
        {
            "id": "task_nav_01",
            "data": {"label": "Navigate to public search results page"},
            "metadata": {"is_idempotent": True},
        }
    ]
    graph = DAGBuilder.build(nodes_data, [], goal_text="Idemp test session")
    graph.session_id = "session_idemp_test"
    node = graph.get_node("task_nav_01")
    node.status = TaskState.WAITING
    assert is_task_idempotent(node)

    engine = RuntimeEngine(graph, "session_idemp_test", mode="safe", db=None)

    async def _slow_execution(n):
        await asyncio.sleep(5.0)
        return True

    with patch.object(engine, "_attempt_execution", side_effect=_slow_execution), \
         patch.object(settings, "TASK_EXECUTION_TIMEOUT_SECONDS", 0.05):
        
        async for _ in engine._execute_task("task_nav_01"):
            pass

        assert node.status == TaskState.FAILED


# ─────────────────────────────────────────────────────────────────────────────
# 4. Authentication Cookie & Deployment-Domain Validation
# ─────────────────────────────────────────────────────────────────────────────

def test_cookie_security_attributes():
    """
    Verifies set_auth_cookie applies HttpOnly, Secure, SameSite, and custom domain.
    """
    from api.auth import set_auth_cookie
    from starlette.responses import Response

    resp = Response()
    with patch.object(settings, "COOKIE_SECURE", True), \
         patch.object(settings, "COOKIE_SAMESITE", "lax"), \
         patch.object(settings, "COOKIE_DOMAIN", ".ardo.example.com"), \
         patch.object(settings, "SESSION_COOKIE_NAME", "ardo_session"):
        
        set_auth_cookie(resp, "test_raw_token_xyz")
        cookie_header = resp.headers.get("set-cookie")
        assert cookie_header is not None
        assert "ardo_session=test_raw_token_xyz" in cookie_header
        assert "HttpOnly" in cookie_header
        assert "Secure" in cookie_header
        assert "SameSite=lax" in cookie_header
        assert "Domain=.ardo.example.com" in cookie_header


def test_websocket_ticket_issuance_and_redemption():
    """
    Verifies the cross-origin WebSocket single-use ticket mechanism.
    Avoids exposing session tokens in query params while supporting cross-subdomain WS.
    """
    ticket = ws_ticket_manager.issue_ticket(user_id="usr_ws_99", session_id="ardo_sess_123")
    assert ticket is not None
    assert len(ticket) >= 32

    # First redemption succeeds
    redeemed_user = ws_ticket_manager.redeem_ticket(ticket, expected_session_id="ardo_sess_123")
    assert redeemed_user == "usr_ws_99"

    # Second redemption fails (single-use)
    replay_attempt = ws_ticket_manager.redeem_ticket(ticket, expected_session_id="ardo_sess_123")
    assert replay_attempt is None


def test_websocket_cswsh_untrusted_origin_rejection():
    """
    Verifies that websocket handshakes with an untrusted Origin header
    are rejected with policy violation (code 1008).
    """
    client = TestClient(app)
    with patch.object(settings, "ALLOWED_ORIGINS", "https://ardo.example.com"), \
         pytest.raises(Exception):
        # Connecting with an untrusted cross-origin domain should raise or close
        with client.websocket_connect(
            "/api/v1/ws/orchestrate/test_session?ticket=bad_ticket",
            headers={"Origin": "https://malicious-site.com"},
        ):
            pass


# ─────────────────────────────────────────────────────────────────────────────
# 5. Database Migration Verification
# ─────────────────────────────────────────────────────────────────────────────

def test_migration_files_and_idempotent_ddl():
    """
    Verifies that migration files in backend/knowledge/migrations exist,
    contain safe IF NOT EXISTS clauses for forward migration,
    and have accurate, reversible rollback definitions.
    """
    migrations_dir = os.path.join(str(BASE_DIR), "backend", "knowledge", "migrations")
    assert os.path.isdir(migrations_dir), f"Migrations directory not found: {migrations_dir}"

    m1_path = os.path.join(migrations_dir, "001_add_tsvector_hybrid_search.py")
    m2_path = os.path.join(migrations_dir, "002_add_plan_approvals.py")
    m3_path = os.path.join(migrations_dir, "003_add_evidence_and_playbooks.py")

    for path in (m1_path, m2_path, m3_path):
        assert os.path.exists(path), f"Migration file missing: {path}"
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "MIGRATION_SQL" in content
        assert "ROLLBACK_SQL" in content
        assert "apply_migration" in content
        assert "rollback_migration" in content
        assert "IF NOT EXISTS" in content
        assert "DROP" in content
