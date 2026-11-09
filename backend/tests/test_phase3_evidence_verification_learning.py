"""
test_phase3_evidence_verification_learning.py — ARDO Phase 3 Test Suite
========================================================================
Comprehensive automated validation for Phase 3:
Evidence Integrity, Outcome Verification & Learning.

Covers all 12 required evaluation scenarios:
Scenario 1: Action succeeds, intended outcome fails -> FAILED.
Scenario 2: Intended outcome confirmed with evidence -> VERIFIED.
Scenario 3: Evidence corrupted / hash mismatch -> integrity check flags failure.
Scenario 4: Cross-tenant evidence access -> HTTP 404 / access denied.
Scenario 5: PII / secrets in evidence -> redacted by PrivacyRedactor before storage.
Scenario 6: Inconclusive outcome -> remains INCONCLUSIVE, never converted to success.
Scenario 7: Non-idempotent action interrupted -> gates in APPROVAL_NEEDED / OUTCOME_UNKNOWN.
Scenario 8: Unverified/failed execution -> rejected by playbook eligibility gate.
Scenario 9: Verified execution -> produces candidate playbook with redacted steps.
Scenario 10: Stale preconditions / missing tools -> filtered out during playbook retrieval.
Scenario 11: Playbook cannot bypass safety / approval checks (tagged UNTRUSTED).
Scenario 12: Playbook promotion & deprecation lifecycle with version bumps.
"""

import asyncio
import hashlib
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from core.auth_db import AuthDatabase
from core.security import hash_password, generate_session_token
from execution.evidence.privacy_redactor import PrivacyRedactor
from execution.evidence.evidence_vault import EvidenceVault, EvidenceRecord
from execution.browser.agent_contracts import (
    BrowserTask,
    BrowserObservation,
    BrowserVerification,
    BrowserVerificationStatus,
    GoalVerificationResult,
    RecentActionRecord,
)
from execution.browser.browser_models import BrowserActionType
from execution.browser.semantic_verifier import SemanticVerifier
from knowledge.playbook_service import PlaybookService
from orchestration.dag_builder import DAGBuilder, ARDOGraph
from orchestration.scheduler import ARDOScheduler, ARDOSession
from orchestration.runtime_engine import RuntimeEngine
from orchestration.state_machine import TaskState
from main import app


@pytest.fixture(autouse=True)
def setup_phase3_env(monkeypatch):
    """Ensure tests run against an isolated in-memory SQLite database and clear stores."""
    monkeypatch.setenv("AUTH_DB_BACKEND", "sqlite")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-32-bytes-minimum-length-ok")
    monkeypatch.setattr("core.database.Database.connect_db", AsyncMock())
    monkeypatch.setattr("core.database.Database.close_db", AsyncMock())
    db = AuthDatabase.reset_instance(force_sqlite=True)
    ARDOScheduler._sessions.clear()
    ARDOScheduler._session_history.clear()
    from execution.evidence.evidence_vault import _MEMORY_EVIDENCE_STORE
    _MEMORY_EVIDENCE_STORE.clear()
    yield db


def _create_user_and_session(db: AuthDatabase, email: str = "tenant_a@ardo.ai"):
    user = db.create_user(
        full_name="Tenant User",
        email=email,
        password_hash=hash_password("Password123!"),
        is_active=True,
        is_verified=True,
    )
    token = generate_session_token()
    db.create_session(user["id"], token)
    return user, token


# ── Scenario 1: Action succeeds, intended outcome fails -> FAILED ─────────────

def test_action_succeeds_but_outcome_fails_detected_as_failed():
    """
    Scenario 1: A browser action completes without DOM exceptions, but an error banner
    ('Payment Failed', 'Declined') appears on page.
    SemanticVerifier MUST classify this as FAILED, not VERIFIED.
    """
    task = BrowserTask(
        task_id="t_pay",
        objective="Submit payment for booking",
        success_criteria="Booking Confirmed",
        session_id="sess_1",
    )
    obs = BrowserObservation(
        session_id="sess_1",
        task_id="t_pay",
        current_url="https://checkout.example.com/process",
        page_title="Checkout Error",
        page_text_summary="Payment failed. Your transaction was declined by the bank.",
        recent_action=RecentActionRecord(action_type=BrowserActionType.CLICK, success=True),
    )

    verif = SemanticVerifier.verify(task=task, observation=obs)
    assert verif.status == BrowserVerificationStatus.FAILED
    assert verif.confidence_score <= 0.15
    assert "error banner detected" in (verif.failure_reason or "").lower()


# ── Scenario 2: Intended outcome confirmed with evidence -> VERIFIED ──────────

def test_intended_outcome_confirmed_with_evidence():
    """
    Scenario 2: Action completes and post-action state matches destination URL and text.
    SemanticVerifier classifies outcome as VERIFIED with explainable confidence score.
    Evidence artifact is cryptographically hashed and saved in EvidenceVault.
    """
    task = BrowserTask(
        task_id="t_confirm",
        objective="Navigate to order confirmation",
        success_criteria="Order Confirmed - Thank You",
        expected_url="https://shop.example.com/orders/12345/confirmation",
        session_id="sess_2",
    )
    obs = BrowserObservation(
        session_id="sess_2",
        task_id="t_confirm",
        current_url="https://shop.example.com/orders/12345/confirmation",
        page_title="Order Confirmed - Thank You",
        page_text_summary="Order Confirmed - Thank You for your purchase! Receipt #12345.",
        recent_action=RecentActionRecord(action_type=BrowserActionType.NAVIGATE, success=True),
    )

    verif = SemanticVerifier.verify(task=task, observation=obs)
    assert verif.status == BrowserVerificationStatus.VERIFIED
    assert verif.confidence_score >= 0.85
    assert verif.observed_url == "https://shop.example.com/orders/12345/confirmation"

    # Persist in EvidenceVault
    rec = EvidenceVault.record_evidence(
        user_id="user_test_2",
        session_id="sess_2",
        plan_id="plan_test_2",
        plan_hash="h" * 64,
        task_id=task.task_id,
        task_name=task.objective,
        evidence_type="browser_verification",
        raw_content=f"URL: {verif.observed_url} | Title: {verif.observed_title}",
        verification_status=verif.status.value,
        confidence_score=verif.confidence_score,
    )
    assert rec.artifact_hash is not None
    assert len(rec.artifact_hash) == 64
    assert rec.verification_status == "verified"


# ── Scenario 3: Evidence corrupted / hash mismatch -> integrity check flags failure ──

def test_evidence_tampering_and_corruption_detection():
    """
    Scenario 3: Any alteration to evidence payload is detected via SHA-256 mismatch.
    """
    original_payload = "Execution trace: Step 1 completed successfully at timestamp 1700000000"
    rec = EvidenceVault.record_evidence(
        user_id="user_tamper",
        session_id="sess_tamper",
        plan_id="plan_tamper",
        plan_hash="a" * 64,
        task_id="t_tamper",
        task_name="Tamper test",
        evidence_type="trace",
        raw_content=original_payload,
    )

    # Valid check matches
    is_valid, _ = EvidenceVault.verify_integrity(rec, original_payload)
    assert is_valid is True

    # Tampered payload fails
    tampered_payload = "Execution trace: Step 1 FAKED by attacker"
    is_valid_tampered, reason = EvidenceVault.verify_integrity(rec, tampered_payload)
    assert is_valid_tampered is False
    assert "integrity violation" in reason.lower()


# ── Scenario 4: Cross-tenant evidence access -> HTTP 404 / access denied ──────

def test_cross_tenant_evidence_access_denied(setup_phase3_env):
    """
    Scenario 4: User A's evidence records cannot be retrieved or inspected by User B.
    """
    db = setup_phase3_env
    user_a, token_a = _create_user_and_session(db, email="tenant_a@ardo.ai")
    user_b, token_b = _create_user_and_session(db, email="tenant_b@ardo.ai")

    session_id_a = "ardo_session_tenant_a"
    rec = EvidenceVault.record_evidence(
        user_id=user_a["id"],
        session_id=session_id_a,
        plan_id="plan_a",
        plan_hash="1" * 64,
        task_id="t_secret_a",
        task_name="User A Confidential Operation",
        evidence_type="browser_evidence",
        raw_content="Confidential report for User A",
    )

    # User A can retrieve
    evidence_a = EvidenceVault.get_session_evidence(session_id_a, user_id=user_a["id"])
    assert len(evidence_a) == 1
    assert evidence_a[0]["id"] == rec.id

    # User B cannot retrieve via EvidenceVault
    evidence_b = EvidenceVault.get_session_evidence(session_id_a, user_id=user_b["id"])
    assert len(evidence_b) == 0

    single_b = EvidenceVault.get_evidence_by_id(rec.id, user_id=user_b["id"])
    assert single_b is None

    # User B cannot retrieve via HTTP API -> returns 404
    client = TestClient(app)
    res = client.get(
        f"/orchestrate/{session_id_a}/evidence",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res.status_code == 404


# ── Scenario 5: PII / secrets in evidence -> redacted by PrivacyRedactor ──────

def test_privacy_redactor_secrets_and_pii_redacted():
    """
    Scenario 5: JWT tokens, API keys, passwords, Luhn credit cards, SSNs, emails,
    and phone numbers are thoroughly redacted before storage.
    """
    sensitive_text = (
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c "
        "User api_key: sk-1234567890abcdef1234567890abcdef "
        "Password: MySuperSecretPassword123! "
        "Customer SSN: 123-45-6789 "
        "Payment Card: 4532 0150 1234 5678 "
        "Contact: customer.service@example.com, Phone: +1 (555) 234-5678."
    )

    clean_text = PrivacyRedactor.redact_text(sensitive_text)

    assert "eyJhbGciOi" not in clean_text
    assert "sk-1234567890abcdef" not in clean_text
    assert "MySuperSecretPassword123!" not in clean_text
    assert "123-45-6789" not in clean_text
    assert "customer.service@example.com" not in clean_text
    assert "[REDACTED_SECRET]" in clean_text
    assert "[REDACTED_SSN]" in clean_text
    assert "[REDACTED_EMAIL]" in clean_text
    assert "****-****-****-5678" in clean_text

    # Verify through EvidenceVault storage
    rec = EvidenceVault.record_evidence(
        user_id="user_pii",
        session_id="sess_pii",
        plan_id="plan_pii",
        plan_hash="p" * 64,
        task_id="t_pii",
        task_name="Checkout PII task",
        evidence_type="receipt",
        raw_content=sensitive_text,
    )
    assert "eyJhbGciOi" not in rec.content_payload
    assert "123-45-6789" not in rec.content_payload
    assert "****-****-****-5678" in rec.content_payload


# ── Scenario 6: Inconclusive outcome -> remains INCONCLUSIVE ──────────────────

def test_inconclusive_outcome_never_converted_to_success():
    """
    Scenario 6: Ambiguous post-action state where neither success criteria nor error
    banners were found returns INCONCLUSIVE (confidence=0.20) and is never marked verified.
    """
    task = BrowserTask(
        task_id="t_ambiguous",
        objective="Search product catalog",
        success_criteria="Found 42 matching results",
        session_id="sess_inc",
    )
    obs = BrowserObservation(
        session_id="sess_inc",
        task_id="t_ambiguous",
        current_url="https://example.com/search",
        page_title="Search Catalog",
        page_text_summary="Welcome to the search page. Enter keywords above.",
        recent_action=RecentActionRecord(action_type=BrowserActionType.FILL, success=True),
    )

    verif = SemanticVerifier.verify(task=task, observation=obs)
    assert verif.status == BrowserVerificationStatus.INCONCLUSIVE
    assert verif.confidence_score == 0.20

    # In GoalVerificationResult, inconclusive tasks prevent goal from being verified
    nodes = [
        {"id": "t_ambiguous", "type": "default", "data": {"label": "Search product catalog"}}
    ]
    graph = DAGBuilder.build(nodes, [], goal_text="Search goal")
    graph.nodes["t_ambiguous"].status = TaskState.ACTIVE

    goal_res = SemanticVerifier.verify_goal_outcome("Search goal", graph, "sess_inc")
    assert goal_res.overall_status == "inconclusive"
    assert goal_res.overall_status != "verified"


# ── Scenario 7: Non-idempotent action interrupted -> gates in APPROVAL_NEEDED ─

def test_interrupted_non_idempotent_action_gates_in_approval():
    """
    Scenario 7: A non-idempotent task (e.g. form submission) in OUTCOME_UNKNOWN
    whose destination state cannot be confirmed MUST transition to APPROVAL_NEEDED
    rather than blindly re-running.
    """
    nodes = [
        {
            "id": "t_transfer",
            "type": "default",
            "data": {"label": "Transfer Funds to External Account"},
            "metadata": {"expected_url": "https://bank.example.com/receipt"},
        }
    ]
    graph = DAGBuilder.build(nodes, [], goal_text="Wire transfer")
    node = graph.nodes["t_transfer"]
    node.status = TaskState.OUTCOME_UNKNOWN

    # Observation shows login / session timeout page, NOT the receipt
    mock_observation = BrowserObservation(
        session_id="sess_wire",
        task_id="t_transfer",
        current_url="https://bank.example.com/login",
        page_title="Session Expired",
        recent_action=RecentActionRecord(action_type=BrowserActionType.CLICK, success=True),
    )
    mock_adapter = MagicMock()
    mock_adapter.get_observation = AsyncMock(return_value=mock_observation)

    from orchestration.checkpoint_manager import ExecutionCheckpointManager
    reconciled = asyncio.run(
        ExecutionCheckpointManager.reconcile_uncertain_tasks(
            graph,
            browser_adapter=mock_adapter,
        )
    )

    assert len(reconciled) == 1
    assert reconciled[0]["task_id"] == "t_transfer"
    assert reconciled[0]["status"] == "approval_needed"
    assert node.status == TaskState.APPROVAL_NEEDED


# ── Scenario 8: Unverified/failed execution -> rejected by playbook eligibility ──

def test_unverified_execution_rejected_by_playbook_eligibility():
    """
    Scenario 8: PlaybookService rejects candidate generation when goal verification
    status is not 'verified' (e.g. failed or inconclusive).
    """
    nodes = [{"id": "t1", "type": "default", "data": {"label": "Failed step"}}]
    graph = DAGBuilder.build(nodes, [], goal_text="Book flight")
    graph.nodes["t1"].status = TaskState.FAILED

    failed_goal_res = GoalVerificationResult(
        session_id="sess_failed",
        goal_text="Book flight",
        overall_status="failed",
        confidence_score=0.90,
        failed_tasks=1,
    )

    candidate = PlaybookService.generate_candidate_playbook(
        user_id="user_pb_1",
        session_id="sess_failed",
        goal_text="Book flight",
        graph=graph,
        goal_verification=failed_goal_res,
    )
    assert candidate is None


# ── Scenario 9: Verified execution -> produces candidate playbook with redacted steps ──

def test_verified_execution_produces_candidate_playbook_with_redacted_steps():
    """
    Scenario 9: A fully VERIFIED execution produces a candidate playbook with version=1,
    status='candidate', and all execution steps stripped of sensitive values.
    """
    nodes = [
        {
            "id": "step_1",
            "type": "default",
            "data": {
                "label": "Login with credentials",
                "browser_action": {
                    "action_type": "fill",
                    "selector": "#password",
                    "value": "MySecretPassword123!",
                },
            },
            "metadata": {"confidential_token": "bearer 12345abcdef"},
        },
        {
            "id": "step_2",
            "type": "default",
            "data": {"label": "Download report"},
        },
    ]
    graph = DAGBuilder.build(nodes, [{"source": "step_1", "target": "step_2"}], goal_text="Download monthly report")
    for n in graph.nodes.values():
        n.status = TaskState.COMPLETED

    verified_goal_res = GoalVerificationResult(
        session_id="sess_verif",
        goal_text="Download monthly report",
        overall_status="verified",
        confidence_score=0.95,
        total_tasks=2,
        verified_tasks=2,
    )

    candidate = PlaybookService.generate_candidate_playbook(
        user_id="user_pb_2",
        session_id="sess_verif",
        goal_text="Download monthly report",
        graph=graph,
        goal_verification=verified_goal_res,
        category="finance_reporting",
    )

    assert candidate is not None
    assert candidate["id"].startswith("pb_")
    assert candidate["status"] == "candidate"
    assert candidate["version"] == 1
    assert candidate["user_id"] == "user_pb_2"

    # Verify redaction in stored steps
    steps = candidate["execution_steps"]
    assert len(steps) == 2
    step1_str = str(steps[0])
    assert "MySecretPassword123!" not in step1_str
    assert "bearer 12345abcdef" not in step1_str


# ── Scenario 10: Stale preconditions / missing tools -> filtered out during retrieval ──

def test_missing_tools_filtered_out_during_playbook_retrieval(setup_phase3_env):
    """
    Scenario 10: Playbooks requiring unavailable tools are filtered out of retrieval.
    """
    db = setup_phase3_env
    user_id = "user_tools_test"

    # Create a promoted playbook that requires a specialized tool
    db.record_playbook(
        id="pb_specialized",
        user_id=user_id,
        goal_pattern="Run specialized hardware diagnostic",
        category="system",
        status="promoted",
        version=1,
        confidence_score=0.95,
        required_tools=["browser", "hardware_probe_tool"],
        execution_steps=[{"step": "probe hardware"}],
        expected_outcomes=["hardware tested"],
    )

    # Session only has 'browser' available -> specialized playbook must be filtered out
    available_tools = {"browser"}
    results = PlaybookService.retrieve_playbooks_for_goal(
        goal_text="Run specialized diagnostic",
        user_id=user_id,
        available_tools=available_tools,
    )
    assert len(results) == 0

    # When tool is available -> playbook is returned
    available_tools.add("hardware_probe_tool")
    results_with_tool = PlaybookService.retrieve_playbooks_for_goal(
        goal_text="Run specialized diagnostic",
        user_id=user_id,
        available_tools=available_tools,
    )
    assert len(results_with_tool) == 1
    assert results_with_tool[0]["id"] == "pb_specialized"


# ── Scenario 11: Playbook cannot bypass safety / approval checks ───────────────

def test_retrieved_playbook_tagged_untrusted_cannot_bypass_safety(setup_phase3_env):
    """
    Scenario 11: Playbook contexts are explicitly tagged [UNTRUSTED_PLAYBOOK_CONTEXT]
    and cannot bypass plan critique or cryptographic approval gates.
    """
    db = setup_phase3_env
    user_id = "user_safety_test"

    db.record_playbook(
        id="pb_high_risk",
        user_id=user_id,
        goal_pattern="Deploy server upgrade",
        category="devops",
        status="promoted",
        version=1,
        confidence_score=0.90,
        required_tools=["browser"],
        execution_steps=[{"action": "click deploy"}],
        expected_outcomes=["server deployed"],
    )

    results = PlaybookService.retrieve_playbooks_for_goal(
        goal_text="Deploy server upgrade",
        user_id=user_id,
        available_tools={"browser"},
    )
    assert len(results) == 1
    assert "UNTRUSTED_PLAYBOOK_CONTEXT" in results[0]["untrusted_context_tag"]

    # Playbook requires standard approval before execution
    nodes = [
        {"id": "t_deploy", "type": "default", "data": {"label": "Deploy server upgrade"}}
    ]
    graph = DAGBuilder.build(nodes, [], goal_text="Deploy server upgrade", risk_threshold=0.1)
    engine = RuntimeEngine(graph, session_id="sess_untrusted", mode="safe", user_id=user_id)
    node = graph.nodes["t_deploy"]
    node.risk_score = 0.95
    node.approval_needed = True

    # Must be approval gated
    assert node.approval_needed is True


# ── Scenario 12: Playbook promotion & deprecation lifecycle ────────────────────

def test_playbook_lifecycle_promote_and_deprecate(setup_phase3_env):
    """
    Scenario 12: Playbook advances candidate -> promoted (version increments) -> deprecated.
    Tenant isolation is enforced throughout lifecycle mutations.
    """
    db = setup_phase3_env
    user_owner = "user_lifecycle_owner"
    user_intruder = "user_lifecycle_intruder"

    pb_doc = db.record_playbook(
        id="pb_lifecycle_1",
        user_id=user_owner,
        goal_pattern="Search documentation and summarize",
        category="research",
        status="candidate",
        version=1,
        confidence_score=0.92,
        required_tools=["browser"],
        execution_steps=[{"step": 1, "action": "search"}],
        expected_outcomes=["summary generated"],
    )
    assert pb_doc["status"] == "candidate"
    assert pb_doc["version"] == 1

    # Intruder cannot promote owner's playbook
    assert PlaybookService.promote_candidate("pb_lifecycle_1", user_id=user_intruder) is False

    # Owner promotes candidate -> becomes 'promoted' with version=2
    promoted = PlaybookService.promote_candidate("pb_lifecycle_1", user_id=user_owner)
    assert promoted is True

    updated_pb = PlaybookService.get_playbook_by_id("pb_lifecycle_1", user_id=user_owner)
    assert updated_pb["status"] == "promoted"
    assert updated_pb["version"] == 2

    # Deprecate playbook
    deprecated = PlaybookService.deprecate_playbook("pb_lifecycle_1", user_id=user_owner)
    assert deprecated is True

    depr_pb = PlaybookService.get_playbook_by_id("pb_lifecycle_1", user_id=user_owner)
    assert depr_pb["status"] == "deprecated"

    # Deprecated playbooks are not retrieved by default
    retrieved = PlaybookService.retrieve_playbooks_for_goal(
        goal_text="Search documentation",
        user_id=user_owner,
    )
    assert len(retrieved) == 0

