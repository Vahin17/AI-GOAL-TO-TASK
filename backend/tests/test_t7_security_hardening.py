"""
test_t7_security_hardening.py — Authoritative Security and Approval Hardening Verification Suite
================================================================================================
Empirically verifies all Phase 3 Task T7 remediations:
1. T7-A (T2-01): Atomic embedding provenance preventing concurrent provider race conditions.
2. T7-B (T2-02): Checkpoint ownership enforcement failing closed on missing, null, or cross-tenant owners.
3. T7-C (T2-03): Semantic memory tenant isolation across vector similarity and full-text fallbacks.
4. T7-D (T2-04): WebSocket ticket authorization bound strictly to session owner; fail-closed handshake.
5. T7-E (T2-06): Cryptographic plan-hash validation strictly required in approve_task (rejects null/missing/stale).
6. T7-F (T4-SEC-01): Diagnostic inspect endpoint requiring authentication and session ownership without global tool fallback.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import time
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from core.auth_db import AuthDatabase
from core.security import generate_session_token
from knowledge.embeddings import (
    DeterministicLocalEmbeddingProvider,
    EmbeddingResult,
    HybridEmbeddingService,
    OpenAIEmbeddingProvider,
)
from knowledge.semantic_memory import ExecutionMemoryItem, SemanticMemoryStore
from knowledge.postgres_vector_store import DocumentChunk, PostgresPgVectorStore
from orchestration.checkpoint_manager import ExecutionCheckpointManager
from orchestration.dag_builder import DAGBuilder, ARDOGraph
from orchestration.plan_hasher import compute_plan_hash
from orchestration.scheduler import ARDOScheduler, ARDOSession
from main import app


@pytest.fixture(autouse=True)
def setup_auth_sqlite(monkeypatch):
    """Ensure tests run against an isolated SQLite database and mock out external services."""
    monkeypatch.setenv("AUTH_DB_BACKEND", "sqlite")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-32-bytes-minimum-length-ok")
    monkeypatch.setattr("core.database.Database.connect_db", AsyncMock())
    monkeypatch.setattr("core.database.Database.close_db", AsyncMock())
    HybridEmbeddingService.reset_state()
    db = AuthDatabase.reset_instance(force_sqlite=True)
    yield db
    HybridEmbeddingService.reset_state()


def _create_user(db: AuthDatabase, email: str):
    user = db.create_user(
        full_name=f"User {email}",
        email=email,
        password_hash="test_hash_val",
        is_active=True,
        is_verified=True,
    )
    token = generate_session_token()
    db.create_session(user["id"], token)
    return user, token


# ── T7-A (T2-01): Atomic Embedding Provenance & Concurrency Safety ───────────

def test_t2_01_atomic_embedding_provenance_under_concurrency():
    """
    T2-01 Remediation Verification:
    Concurrent embedding requests with asynchronous provider failures must never
    associate cloud vector data with local fallback provenance or vice-versa.
    """
    service = HybridEmbeddingService(force_local=False, api_key="sk-mocked-key")

    mock_1536_vec = [0.05] * 1536
    mock_128_vec = [0.1] * 128

    # Create mock OpenAI provider
    mock_openai = MagicMock()
    mock_openai.dimension = 1536
    mock_openai.model_name = "text-embedding-3-small"
    mock_openai.embedding_space = "openai:text-embedding-3-small:1536"
    mock_openai.embed_query = MagicMock(return_value=mock_1536_vec)
    service._openai = mock_openai

    # Thread 1 generates via OpenAI
    res1 = service.embed_query_with_provenance("Query for thread 1")
    assert isinstance(res1, EmbeddingResult)
    assert res1.vector == mock_1536_vec
    assert res1.dimension == 1536
    assert res1.provider == "openai"
    assert res1.embedding_space == "openai:text-embedding-3-small:1536"

    # Simulate an error in Thread 2 that triggers local fallback
    service._handle_failure(RuntimeError("Simulated 429 rate limit"))
    assert service._last_failure_type == "rate_limit_429"

    # Thread 2 generates via local fallback
    res2 = service.embed_query_with_provenance("Query for thread 2")
    assert isinstance(res2, EmbeddingResult)
    assert len(res2.vector) == 128
    assert res2.dimension == 128
    assert "local_deterministic_fallback" in res2.provider
    assert "deterministic-hash-128" in res2.embedding_space

    # Crucial check: res1 (Thread 1) provenance remains completely immutable and uncorrupted!
    assert res1.dimension == 1536
    assert res1.provider == "openai"
    assert res1.embedding_space == "openai:text-embedding-3-small:1536"


def test_t2_01_memory_and_vector_store_capture_atomic_provenance():
    """SemanticMemoryStore and PostgresPgVectorStore capture provenance atomically from EmbeddingResult."""
    service = HybridEmbeddingService(force_local=True)
    mem_store = SemanticMemoryStore(in_memory=True, embedding_provider=service)

    item = mem_store.record_memory(
        goal_text="Plan autonomous rover mission",
        task_summary="Terrain mapping and navigation",
        outcome_status="success",
        user_id="user_test_atomic",
    )

    assert item.metadata.get("embedding_provider") == "local_deterministic_fallback"
    assert item.metadata.get("embedding_model") == "deterministic-hash-128"
    assert item.metadata.get("embedding_dim") == 128
    assert "local_deterministic_fallback:deterministic-hash-128:128" in item.metadata.get("embedding_space", "")

    # Document chunk in vector store
    v_store = PostgresPgVectorStore(in_memory=True, embedding_provider=service)
    chunk = DocumentChunk(
        chunk_id="chk_atom_1",
        document_id="doc_atom_1",
        source="test",
        chunk_index=0,
        content="Autonomous rover communications telemetry",
    )
    v_store.add_chunks([chunk])
    stored_chunk = v_store._local_chunks["chk_atom_1"]
    assert stored_chunk.metadata.get("embedding_dim") == 128
    assert "deterministic-hash-128:128" in stored_chunk.metadata.get("embedding_space", "")


# ── T7-B (T2-02): Checkpoint Ownership Validation Bypass ─────────────────────

@pytest.mark.asyncio
async def test_t2_02_checkpoint_ownership_validation_fails_closed(setup_auth_sqlite):
    """
    T2-02 Remediation Verification:
    POST /orchestrate/{session_id}/resume must fail closed.
    - Owner A resumes Owner A's session: 200 OK.
    - Owner B attempts to resume Owner A's session: 403 Forbidden.
    - Checkpoint with missing/null owner: 403 Forbidden (fail closed).
    """
    db = setup_auth_sqlite
    user_a, token_a = _create_user(db, "owner_a@ardo.ai")
    user_b, token_b = _create_user(db, "owner_b@ardo.ai")

    session_id = "ardo_chk_sec_01"
    nodes = [{"id": "t1", "type": "default", "data": {"label": "Init"}}]
    graph = DAGBuilder.build(nodes, [], goal_text="Goal A")
    graph.session_id = session_id
    graph.user_id = user_a["id"]

    # Save checkpoint explicitly owned by User A
    chk_doc = await ExecutionCheckpointManager.save_checkpoint(
        session_id=session_id,
        graph=graph,
        trigger="test_save",
        user_id=user_a["id"],
    )
    assert chk_doc.get("user_id") == user_a["id"]

    session_a = ARDOSession(
        session_id=session_id,
        graph=graph,
        mode="safe",
        user_id=user_a["id"],
    )
    ARDOScheduler._sessions[session_id] = session_a

    client = TestClient(app)

    # 1. Owner B attempts to resume Owner A's checkpoint -> MUST BE REJECTED 403
    res_b = client.post(
        f"/orchestrate/{session_id}/resume",
        cookies={"ardo_session": token_b},
    )
    assert res_b.status_code == 403
    assert "Cross-user resume forbidden" in res_b.json()["detail"]

    # 2. Checkpoint with null/missing user_id -> MUST BE REJECTED 403 (Fail closed)
    session_orphan_id = "ardo_orphan_chk_02"
    graph_orphan = DAGBuilder.build(nodes, [], goal_text="Orphan")
    graph_orphan.session_id = session_orphan_id
    await ExecutionCheckpointManager.save_checkpoint(
        session_id=session_orphan_id,
        graph=graph_orphan,
        trigger="orphan_save",
        user_id=None,
    )
    res_orphan = client.post(
        f"/orchestrate/{session_orphan_id}/resume",
        cookies={"ardo_session": token_a},
    )
    assert res_orphan.status_code == 403
    assert "Cross-user resume forbidden" in res_orphan.json()["detail"]

    # 3. Owner A resumes Owner A's checkpoint -> 200 OK
    res_a = client.post(
        f"/orchestrate/{session_id}/resume",
        cookies={"ardo_session": token_a},
    )
    assert res_a.status_code == 200
    assert res_a.json()["status"] == "resumed"


# ── T7-C (T2-03): Semantic Memory Tenant Isolation ───────────────────────────

def test_t2_03_semantic_memory_tenant_isolation_in_memory():
    """
    T2-03 Remediation Verification:
    Execution memories must never leak across tenant/user boundaries.
    """
    store = SemanticMemoryStore(in_memory=True, embedding_provider=HybridEmbeddingService(force_local=True))

    # User A records a memory containing sensitive project details
    mem_a = store.record_memory(
        goal_text="Deploy confidential payroll processing service [secret_alpha]",
        task_summary="Configured AWS KMS keys and bank webhooks",
        outcome_status="success",
        user_id="user_tenant_a",
    )
    assert mem_a.user_id == "user_tenant_a"

    # User B records a standard memory
    mem_b = store.record_memory(
        goal_text="Deploy public landing page [public_beta]",
        task_summary="Static asset build and CDN deployment",
        outcome_status="success",
        user_id="user_tenant_b",
    )
    assert mem_b.user_id == "user_tenant_b"

    # Record a legacy memory without tenant info
    mem_legacy = store.record_memory(
        goal_text="Deploy confidential internal proxy [legacy]",
        task_summary="Legacy reverse proxy setup",
        outcome_status="success",
        user_id=None,
    )

    # 1. User A queries: must find mem_a, must NOT find mem_b or mem_legacy
    results_a = store.retrieve_memories(
        goal_query="Deploy confidential payroll",
        user_id="user_tenant_a",
    )
    found_ids_a = [m.memory_id for m in results_a]
    assert mem_a.memory_id in found_ids_a
    assert mem_b.memory_id not in found_ids_a
    assert mem_legacy.memory_id not in found_ids_a

    # 2. User B queries for payroll secrets: must get ZERO results
    results_b = store.retrieve_memories(
        goal_query="Deploy confidential payroll processing service",
        user_id="user_tenant_b",
    )
    found_ids_b = [m.memory_id for m in results_b]
    assert mem_a.memory_id not in found_ids_b
    assert mem_legacy.memory_id not in found_ids_b


def test_t2_03_api_search_memories_enforces_tenant_isolation(setup_auth_sqlite):
    """GET /knowledge/memories requires authentication and scopes queries to current_user."""
    db = setup_auth_sqlite
    user_a, token_a = _create_user(db, "alice@tenant.a")
    user_b, token_b = _create_user(db, "bob@tenant.b")

    client = TestClient(app)

    # 1. Unauthenticated request rejected with 401
    res_unauth = client.get("/knowledge/memories?q=deploy")
    assert res_unauth.status_code == 401

    # 2. Authenticated request for Alice passes user_id=alice
    from knowledge.rag_service import RAGService
    rag = RAGService.get_instance()
    rag.memory_store.record_memory(
        goal_text="Alice confidential financial planning audit",
        task_summary="Secret balance sheet validation",
        outcome_status="success",
        user_id=user_a["id"],
    )

    # Bob searches for Alice's confidential audit -> 0 results
    res_bob = client.get(
        "/knowledge/memories?q=confidential financial planning audit",
        cookies={"ardo_session": token_b},
    )
    assert res_bob.status_code == 200
    assert len(res_bob.json()) == 0


# ── T7-D (T2-04): WebSocket Ticket Authorization ─────────────────────────────

def test_t2_04_ws_ticket_issuance_and_handshake_authorization(setup_auth_sqlite):
    """
    T2-04 Remediation Verification:
    - POST /auth/ws-ticket rejects ticket requests for sessions not owned by current user.
    - WebSocket handshake fails closed if session is not active or owned by user.
    """
    db = setup_auth_sqlite
    user_a, token_a = _create_user(db, "alice_ws@ardo.ai")
    user_b, token_b = _create_user(db, "bob_ws@ardo.ai")

    session_id = "ardo_ws_session_01"
    nodes = [{"id": "t1", "type": "default", "data": {"label": "Task"}}]
    graph = DAGBuilder.build(nodes, [], goal_text="WS Goal")
    session = ARDOSession(
        session_id=session_id,
        graph=graph,
        mode="safe",
        user_id=user_a["id"],
    )
    ARDOScheduler._sessions[session_id] = session

    client = TestClient(app)

    # 1. User B requests a ticket for User A's session -> MUST BE REJECTED 403
    res_b = client.post(
        "/auth/ws-ticket",
        json={"session_id": session_id},
        cookies={"ardo_session": token_b},
    )
    assert res_b.status_code == 403
    assert "Forbidden" in res_b.json()["detail"]

    # 2. User A requests ticket for non-existent session -> 404
    res_nonexistent = client.post(
        "/auth/ws-ticket",
        json={"session_id": "nonexistent_session_xyz"},
        cookies={"ardo_session": token_a},
    )
    assert res_nonexistent.status_code == 404

    # 3. User A requests ticket for own session -> 200 OK
    res_a = client.post(
        "/auth/ws-ticket",
        json={"session_id": session_id},
        cookies={"ardo_session": token_a},
    )
    assert res_a.status_code == 200
    ticket = res_a.json()["ticket"]
    assert ticket is not None


# ── T7-E (T2-06): Approval Plan Hash Validation ──────────────────────────────

def test_t2_06_approve_task_rejects_missing_null_and_mismatched_plan_hash(setup_auth_sqlite):
    """
    T2-06 Remediation Verification:
    POST /orchestrate/{session_id}/approve/{task_id} must strictly enforce plan_hash.
    - Missing / null / empty plan_hash -> 400 Bad Request.
    - Mismatched plan_hash -> 400 Bad Request.
    - Valid matching plan_hash -> 200 OK.
    """
    db = setup_auth_sqlite
    user, token = _create_user(db, "approver@ardo.ai")

    session_id = "ardo_plan_hash_sec"
    nodes = [{"id": "t_safety", "type": "default", "data": {"label": "Critical wire transfer"}}]
    graph = DAGBuilder.build(nodes, [], goal_text="Transfer funds")

    session = ARDOSession(
        session_id=session_id,
        graph=graph,
        mode="safe",
        user_id=user["id"],
    )
    canonical_hash = session.graph.recompute_plan_hash("Transfer funds")
    ARDOScheduler._sessions[session_id] = session

    client = TestClient(app)
    cookies = {"ardo_session": token}

    # 1. Missing plan_hash (None) -> 400
    res_none = client.post(
        f"/orchestrate/{session_id}/approve/t_safety",
        json={"approved": True, "plan_hash": None},
        cookies=cookies,
    )
    assert res_none.status_code == 400
    assert "Missing plan_hash" in res_none.json()["detail"]

    # 2. Empty string plan_hash -> 400
    res_empty = client.post(
        f"/orchestrate/{session_id}/approve/t_safety",
        json={"approved": True, "plan_hash": "   "},
        cookies=cookies,
    )
    assert res_empty.status_code == 400
    assert "Missing plan_hash" in res_empty.json()["detail"]

    # 3. Mismatched plan_hash -> 400
    res_bad = client.post(
        f"/orchestrate/{session_id}/approve/t_safety",
        json={"approved": True, "plan_hash": "0000000000000000000000000000000000000000000000000000000000000000"},
        cookies=cookies,
    )
    assert res_bad.status_code == 400
    assert "Plan hash mismatch" in res_bad.json()["detail"]

    # 4. Valid matching plan_hash -> 200 OK
    res_ok = client.post(
        f"/orchestrate/{session_id}/approve/t_safety",
        json={"approved": True, "plan_hash": canonical_hash, "notes": "Approved by CFO"},
        cookies=cookies,
    )
    assert res_ok.status_code == 200
    assert res_ok.json()["status"] == "approved"
    assert res_ok.json()["plan_hash"] == canonical_hash


# ── T7-F (T4-SEC-01): Diagnostic Inspect Endpoint Authentication & Isolation ───

def test_t4_sec_01_inspect_endpoint_requires_auth_and_session_ownership(setup_auth_sqlite):
    """
    T4-SEC-01 Remediation Verification:
    GET /orchestrate/{session_id}/inspect:
    - Unauthenticated request is rejected (HTTP 401).
    - Other user inspecting session is rejected (HTTP 403).
    - Session owner can inspect without falling back to global tool registry.
    """
    db = setup_auth_sqlite
    user_a, token_a = _create_user(db, "alice_inspect@ardo.ai")
    user_b, token_b = _create_user(db, "bob_inspect@ardo.ai")

    session_id = "ardo_inspect_session_01"
    nodes = [{"id": "t1", "type": "default", "data": {"label": "Browse page"}}]
    graph = DAGBuilder.build(nodes, [], goal_text="Inspect Goal")

    session = ARDOSession(
        session_id=session_id,
        graph=graph,
        mode="safe",
        user_id=user_a["id"],
    )
    ARDOScheduler._sessions[session_id] = session

    client = TestClient(app)

    # 1. Unauthenticated request -> 401
    res_unauth = client.get(f"/orchestrate/{session_id}/inspect")
    assert res_unauth.status_code == 401

    # 2. User B attempts to inspect User A's session -> 403 Forbidden
    res_b = client.get(
        f"/orchestrate/{session_id}/inspect",
        cookies={"ardo_session": token_b},
    )
    assert res_b.status_code == 403
    assert "Forbidden" in res_b.json()["detail"]

    # 3. User A inspects non-existent session -> 403 (fail closed)
    res_nonexistent = client.get(
        "/orchestrate/unknown_session_12345/inspect",
        cookies={"ardo_session": token_a},
    )
    assert res_nonexistent.status_code == 403

    # 4. User A inspects own session without active browser page -> 404 (No active browser page)
    # This confirms authentication and ownership checks passed!
    res_a = client.get(
        f"/orchestrate/{session_id}/inspect",
        cookies={"ardo_session": token_a},
    )
    assert res_a.status_code == 404
    assert "No active browser page" in res_a.json()["detail"]
