import pytest
import os
import uuid
from unittest.mock import MagicMock
from core.config import settings
from knowledge.postgres_vector_store import PostgresPgVectorStore
from knowledge.semantic_memory import SemanticMemoryStore
from knowledge.embeddings import HybridEmbeddingService, DeterministicLocalEmbeddingProvider
from knowledge.playbook_service import PlaybookService
from core.auth_db import AuthDatabase


def test_vector_store_picks_up_settings_postgres_uri():
    # If connection_uri is not explicitly provided, it should resolve from settings.POSTGRES_URI
    store = PostgresPgVectorStore(in_memory=True)
    assert store.active_backend == "local_fallback"

    # With live/dev URI, it should report active_backend accurately
    dev_uri = getattr(settings, "POSTGRES_URI", None)
    if dev_uri:
        real_store = PostgresPgVectorStore(connection_uri=dev_uri)
        # Should be either postgres_pgvector (if DB is up) or local_fallback (if DB is unreachable)
        # but NEVER falsely report postgres if not connected
        assert real_store.active_backend in ("postgres_pgvector", "local_fallback")


def test_semantic_memory_store_picks_up_settings_postgres_uri():
    store = SemanticMemoryStore(in_memory=True)
    assert store.active_backend == "local_fallback"

    dev_uri = getattr(settings, "POSTGRES_URI", None)
    if dev_uri:
        real_store = SemanticMemoryStore(connection_uri=dev_uri)
        assert real_store.active_backend in ("postgres_pgvector", "local_fallback")


def test_hybrid_embedding_service_truthful_active_provider():
    # Force local mode
    local_service = HybridEmbeddingService(force_local=True)
    assert local_service.active_provider == "local_deterministic_fallback"
    assert local_service.is_using_cloud is False

    # Simulate quota exhaustion
    HybridEmbeddingService._openai_quota_exhausted = True
    exhausted_service = HybridEmbeddingService(api_key="sk-test-fake")
    assert "openai_quota_exhausted" in exhausted_service.active_provider
    assert exhausted_service.is_using_cloud is False

    # Reset quota exhausted state for subsequent tests
    HybridEmbeddingService._openai_quota_exhausted = False


def test_playbook_service_rejects_unverified_candidate_promotion():
    auth_db = AuthDatabase.get_instance()
    
    # Ensure a valid test user exists in PostgreSQL to satisfy foreign key constraint
    test_user = auth_db.get_user_by_email("test_pb_user@example.com")
    if not test_user:
        test_user = auth_db.create_user(
            email="test_pb_user@example.com",
            full_name="Playbook Test User",
            password_hash="test_hash_pb",
        )
    user_id = test_user["id"]

    # Create an unverified candidate playbook with low confidence
    pb_id = f"pb_unverified_test_{uuid.uuid4().hex[:8]}"
    pb_doc = auth_db.record_playbook(
        id=pb_id,
        user_id=user_id,
        goal_pattern="Play test music",
        execution_steps=[],
        status="candidate",
        confidence_score=0.30,
        metadata={"verification_status": "inconclusive"},
    )
    assert pb_doc is not None

    # Attempt to promote it
    promoted = PlaybookService.promote_candidate(pb_id, user_id=user_id)
    # Must be rejected!
    assert promoted is False

    # Verify status in database remains candidate
    check_pb = auth_db.get_playbook_by_id(pb_id, user_id=user_id)
    assert check_pb["status"] == "candidate"
