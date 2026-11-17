"""
test_embedding_dimension_compatibility.py — Architectural Tests for Vector Dimension Safety
=============================================================================================
Phase 2/3: Targeted verification for:
1. Local 128-dimensional embedding generation.
2. OpenAI 1536-dimensional embedding generation (mocked).
3. Correct provider selection.
4. OpenAI failure and quota-exhaustion fallback.
5. Preventing incompatible vectors from being stored (stored as NULL with full-text indexing).
6. Preventing incompatible vectors from being compared (routed to tsvector full-text search).
7. Correct query-to-storage model compatibility.
8. Existing semantic-memory retrieval behavior.
9. Schema introspection and dimension discovery.
"""

import json
import math
import time
import pytest
from unittest.mock import MagicMock, patch

from knowledge.embeddings import (
    EmbeddingProvider,
    DeterministicLocalEmbeddingProvider,
    OpenAIEmbeddingProvider,
    HybridEmbeddingService,
)
from knowledge.semantic_memory import SemanticMemoryStore, ExecutionMemoryItem
from knowledge.postgres_vector_store import PostgresPgVectorStore
from knowledge.knowledge_schemas import DocumentChunk


# ─────────────────────────────────────────────────────────────────────────────
# 1. Embedding Generation & Dimensionality
# ─────────────────────────────────────────────────────────────────────────────

def test_local_embedding_generation_128_dim():
    """DeterministicLocalEmbeddingProvider must output exactly 128-dim L2-normalized vectors."""
    provider = DeterministicLocalEmbeddingProvider(dim=128)
    assert provider.dimension == 128
    assert provider.active_dimension == 128
    assert provider.model_name == "deterministic-hash-128"

    text = "Deploy FastAPI web application to staging cluster"
    vec = provider.embed_query(text)
    assert len(vec) == 128
    assert all(isinstance(v, float) for v in vec)

    # L2-normalization check
    norm = math.sqrt(sum(v * v for v in vec))
    assert pytest.approx(norm, rel=1e-3) == 1.0

    # Determinism: identical text produces identical vector
    vec2 = provider.embed_query(text)
    assert vec == vec2


def test_openai_embedding_generation_1536_dim_mocked():
    """OpenAIEmbeddingProvider must output exactly 1536-dim vectors."""
    mock_1536_vec = [0.01 * (i % 10) for i in range(1536)]

    with patch("openai.OpenAI") as mock_openai_cls:
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_item = MagicMock()
        mock_item.embedding = mock_1536_vec
        mock_resp = MagicMock()
        mock_resp.data = [mock_item]
        mock_client.embeddings.create.return_value = mock_resp

        provider = OpenAIEmbeddingProvider(api_key="sk-test-fake")
        assert provider.dimension == 1536
        assert provider.model_name == "text-embedding-3-small"

        vec = provider.embed_query("Search for documentation")
        assert len(vec) == 1536
        assert vec == mock_1536_vec


# ─────────────────────────────────────────────────────────────────────────────
# 2. Provider Selection & Fallback Behavior
# ─────────────────────────────────────────────────────────────────────────────

def test_hybrid_provider_selection_logic():
    """HybridEmbeddingService selects provider based on configuration and availability."""
    # Reset any prior test leak
    HybridEmbeddingService._openai_quota_exhausted = False

    # Force local mode
    local_service = HybridEmbeddingService(force_local=True)
    assert local_service.active_provider == "local_deterministic_fallback"
    assert local_service.active_model == "deterministic-hash-128"
    assert local_service.active_dimension == 128
    assert local_service.is_using_cloud is False

    # Cloud mode with valid key
    with patch("openai.OpenAI") as mock_openai_cls:
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        cloud_service = HybridEmbeddingService(api_key="sk-test-mocked", force_local=False)
        assert cloud_service.active_provider == "openai"
        assert cloud_service.active_model == "text-embedding-3-small"
        assert cloud_service.active_dimension == 1536
        assert cloud_service.is_using_cloud is True


def test_openai_failure_and_quota_exhaustion_fallback():
    """OpenAI 429 quota exhaustion switches to local provider without crashing."""
    HybridEmbeddingService._openai_quota_exhausted = False

    with patch("openai.OpenAI") as mock_openai_cls:
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        # Simulate 429 RateLimitError
        mock_client.embeddings.create.side_effect = Exception("Error 429: You exceeded your current quota.")

        service = HybridEmbeddingService(api_key="sk-test-mocked", force_local=False)
        assert service.is_using_cloud is True

        # First query triggers quota exhaustion fallback
        vec = service.embed_query("Perform plan generation")
        assert len(vec) == 128
        assert service.is_using_cloud is False
        assert service.active_dimension == 128
        assert "quota_exhausted" in service.active_provider

        # Subsequent queries stay on local fallback without attempting OpenAI
        vec2 = service.embed_query("Another query")
        assert len(vec2) == 128

    HybridEmbeddingService._openai_quota_exhausted = False


# ─────────────────────────────────────────────────────────────────────────────
# 3. Preventing Incompatible Vectors in PostgreSQL Storage
# ─────────────────────────────────────────────────────────────────────────────

def test_semantic_memory_prevents_incompatible_vector_insertion():
    """
    When DB column is 1536-dim but active provider is 128-dim:
    record_memory() must NOT send 128-dim Vector to PostgreSQL (which throws error).
    Instead, it inserts with embedding=NULL and records provenance metadata.
    """
    mock_pg_conn = MagicMock()
    mock_cur = MagicMock()
    mock_pg_conn.cursor.return_value.__enter__.return_value = mock_cur

    local_provider = DeterministicLocalEmbeddingProvider(dim=128)
    store = SemanticMemoryStore(in_memory=True, embedding_provider=local_provider)
    store._is_pg_connected = True
    store._pg_conn = mock_pg_conn
    # Simulate DB schema initialized with 1536 dimensions (e.g. from production OpenAI)
    store.db_vector_dim = 1536

    item = store.record_memory(
        goal_text="Execute safe deployment",
        task_summary="Validated tests and staging",
        outcome_status="success",
    )

    # Assert record_memory succeeded
    assert item is not None
    assert item.goal_text == "Execute safe deployment"

    # Assert provenance recorded in metadata
    meta = item.metadata
    assert meta["embedding_provider"] == "local_deterministic_fallback"
    assert meta["embedding_model"] == "deterministic-hash-128"
    assert meta["embedding_dim"] == 128
    assert meta["embedding_stored"] is False
    assert meta["embedding_dim_mismatch"] is True

    # Assert SQL INSERT was called with embedding = None (NULL)
    mock_cur.execute.assert_called_once()
    sql_args = mock_cur.execute.call_args[0]
    inserted_values = sql_args[1]
    # The last inserted value is the embedding parameter
    assert inserted_values[-1] is None, f"Expected embedding=None, got: {inserted_values[-1]}"


def test_knowledge_store_prevents_incompatible_vector_insertion():
    """
    When DB column is 1536-dim but chunk has 128-dim embedding:
    PostgresPgVectorStore must insert chunk with embedding=None to prevent PostgreSQL 22000 error.
    """
    mock_pg_conn = MagicMock()
    mock_cur = MagicMock()
    mock_pg_conn.cursor.return_value.__enter__.return_value = mock_cur

    local_provider = DeterministicLocalEmbeddingProvider(dim=128)
    store = PostgresPgVectorStore(in_memory=True, embedding_provider=local_provider)
    store._is_pg_connected = True
    store._pg_conn = mock_pg_conn
    store.db_vector_dim = 1536  # DB has 1536

    chunk = DocumentChunk(
        chunk_id="c_test_01",
        document_id="d_test_01",
        source="test.md",
        content="Testing dimension safety in knowledge chunks",
    )

    with patch("psycopg2.extras.execute_values") as mock_execute_values:
        added = store.add_chunks([chunk])
        assert added == 1

        # Check chunk_rows passed to execute_values
        assert mock_execute_values.call_count == 2
        chunk_rows = mock_execute_values.call_args_list[1][0][2]
        assert len(chunk_rows) == 1
        # Last column in chunk row is embedding
        stored_emb = chunk_rows[0][-1]
        assert stored_emb is None, f"Expected chunk embedding=None, got: {stored_emb}"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Preventing Incompatible Vectors in PostgreSQL Querying
# ─────────────────────────────────────────────────────────────────────────────

def test_semantic_memory_prevents_incompatible_vector_comparison():
    """
    When DB column is 1536-dim but query vector is 128-dim:
    retrieve_memories() must NOT execute pgvector distance query (which crashes).
    Instead, it routes to _retrieve_memories_tsvector (full-text search).
    """
    local_provider = DeterministicLocalEmbeddingProvider(dim=128)
    store = SemanticMemoryStore(in_memory=True, embedding_provider=local_provider)
    store._is_pg_connected = True
    store._pg_conn = MagicMock()
    store.db_vector_dim = 1536  # DB has 1536

    # Mock _retrieve_memories_tsvector
    mock_ts_result = [
        ExecutionMemoryItem(
            memory_id="mem_ts_01",
            goal_text="Execute safe deployment",
            task_summary="Validated staging",
            relevance_score=0.85,
        )
    ]
    store._retrieve_memories_tsvector = MagicMock(return_value=mock_ts_result)

    results = store.retrieve_memories("safe deployment", top_k=2)
    assert len(results) == 1
    assert results[0].memory_id == "mem_ts_01"
    store._retrieve_memories_tsvector.assert_called_once_with(
        goal_query="safe deployment",
        top_k=2,
        min_score=0.35,
        memory_type=None,
    )


def test_knowledge_store_bypasses_vector_query_on_dimension_mismatch():
    """
    When DB column is 1536-dim but query is 128-dim:
    query_vector() returns [] without executing invalid vector SQL.
    """
    mock_pg_conn = MagicMock()
    local_provider = DeterministicLocalEmbeddingProvider(dim=128)
    store = PostgresPgVectorStore(in_memory=True, embedding_provider=local_provider)
    store._is_pg_connected = True
    store._pg_conn = mock_pg_conn
    store.db_vector_dim = 1536

    res = store.query_vector("Some query")
    assert res == []
    # Cursor should never have been created for vector distance SQL
    mock_pg_conn.cursor.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# 5. Schema Introspection
# ─────────────────────────────────────────────────────────────────────────────

def test_schema_introspection_parses_vector_dimension():
    """_inspect_db_schema parses vector(1536) and vector(128) correctly."""
    mock_pg_conn = MagicMock()
    mock_cur = MagicMock()
    mock_pg_conn.cursor.return_value.__enter__.return_value = mock_cur

    store = SemanticMemoryStore(in_memory=True)
    store._is_pg_connected = True
    store._pg_conn = mock_pg_conn

    # Case 1: Database has vector(1536)
    mock_cur.fetchone.return_value = ("vector(1536)",)
    store._inspect_db_schema()
    assert store.db_vector_dim == 1536

    # Case 2: Database has vector(128)
    mock_cur.fetchone.return_value = ("vector(128)",)
    store._inspect_db_schema()
    assert store.db_vector_dim == 128


# ─────────────────────────────────────────────────────────────────────────────
# 6. Strict Embedding-Model Identity Enforcement
# ─────────────────────────────────────────────────────────────────────────────

def test_strict_identity_same_model_same_dim_permitted():
    """When query and stored vector share the same model and dimension, retrieval is permitted."""
    provider = DeterministicLocalEmbeddingProvider(dim=128)
    store = SemanticMemoryStore(in_memory=True, embedding_provider=provider)

    store.record_memory(
        goal_text="Deploy backend microservice to production",
        task_summary="Configured helm charts and secrets",
        outcome_status="success",
    )

    results = store.retrieve_memories("Deploy backend microservice", top_k=1, min_score=0.1)
    assert len(results) == 1
    assert "Deploy backend microservice" in results[0].goal_text
    assert results[0].metadata.get("embedding_space") == provider.embedding_space


def test_strict_identity_different_model_same_dim_blocked():
    """
    Two distinct models with the SAME vector dimension (128) MUST NOT be compared.
    Records from Model A must be blocked when queried with Model B.
    """
    # Provider A
    provider_a = DeterministicLocalEmbeddingProvider(dim=128)
    # Provider B: custom provider with same dimension (128) but different model name / space
    class MockAlternativeProvider(EmbeddingProvider):
        @property
        def dimension(self) -> int:
            return 128
        @property
        def model_name(self) -> str:
            return "alternative-model-128"
        @property
        def active_provider(self) -> str:
            return "alternative_provider"
        def embed_texts(self, texts):
            return [[0.1] * 128 for _ in texts]
        def embed_query(self, query):
            return [0.1] * 128

    provider_b = MockAlternativeProvider()
    assert provider_a.dimension == provider_b.dimension == 128
    assert provider_a.embedding_space != provider_b.embedding_space

    # Store memory under Provider A
    store_a = SemanticMemoryStore(in_memory=True, embedding_provider=provider_a)
    item = store_a.record_memory(
        goal_text="Kubernetes cluster hardening",
        task_summary="Applied network policies",
        outcome_status="success",
    )
    assert item.metadata["embedding_space"] == provider_a.embedding_space

    # Attempt retrieval using Provider B on same store
    store_a.embedding_provider = provider_b
    results = store_a.retrieve_memories("Kubernetes cluster hardening", top_k=2, min_score=0.0)
    # MUST be blocked because embedding_space does not match!
    assert len(results) == 0, f"Expected 0 results due to model incompatibility, got: {len(results)}"


def test_strict_identity_same_model_different_dim_blocked():
    """Even if provider name is identical, different dimension embedding space is blocked."""
    provider_128 = DeterministicLocalEmbeddingProvider(dim=128)
    provider_64 = DeterministicLocalEmbeddingProvider(dim=64)

    store = SemanticMemoryStore(in_memory=True, embedding_provider=provider_128)
    store.record_memory(
        goal_text="Database index optimization",
        task_summary="Created B-tree indices on foreign keys",
        outcome_status="success",
    )

    # Query with 64-dim provider
    store.embedding_provider = provider_64
    results = store.retrieve_memories("Database index optimization", top_k=2, min_score=0.0)
    assert len(results) == 0


def test_strict_identity_missing_model_identity_excluded_from_vector_retrieval():
    """
    Legacy records with missing/null embedding_space MUST be excluded from vector similarity.
    No NULL provenance matching is permitted.
    """
    provider = DeterministicLocalEmbeddingProvider(dim=128)
    store = SemanticMemoryStore(in_memory=True, embedding_provider=provider)

    # 1. Local in-memory store test
    legacy_item = ExecutionMemoryItem(
        goal_text="Legacy migration task",
        task_summary="Pre-dating provenance architecture",
        outcome_status="success",
        metadata={},  # missing embedding_space
    )
    store._local_memories[legacy_item.memory_id] = legacy_item
    store._local_embeddings[legacy_item.memory_id] = provider.embed_query(legacy_item.goal_text)

    # Vector retrieval must exclude legacy_item
    results = store.retrieve_memories("Legacy migration task", top_k=5, min_score=0.0)
    matching_ids = [r.memory_id for r in results]
    assert legacy_item.memory_id not in matching_ids, "Legacy record without embedding_space must be excluded from vector similarity!"

    # 2. PostgreSQL SQL clause test
    mock_pg_conn = MagicMock()
    mock_cur = MagicMock()
    mock_pg_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_cur.fetchall.return_value = []

    pg_store = SemanticMemoryStore(in_memory=True, embedding_provider=provider)
    pg_store._is_pg_connected = True
    pg_store._pg_conn = mock_pg_conn
    pg_store.db_vector_dim = 128

    pg_store.retrieve_memories("Test query", top_k=3)

    # Verify SQL WHERE clause enforces:
    # 1. embedding IS NOT NULL
    # 2. metadata->>'embedding_space' = %s
    assert mock_cur.execute.call_count >= 1
    sql_text = mock_cur.execute.call_args_list[0][0][0]
    sql_params = mock_cur.execute.call_args_list[0][0][1]

    assert "embedding IS NOT NULL" in sql_text
    assert "metadata->>'embedding_space' = %s" in sql_text
    assert provider.embedding_space in sql_params


def test_knowledge_store_strict_model_identity_filtering():
    """
    PostgresPgVectorStore.query_vector must exclude chunks with incompatible or missing embedding_space
    in both local in-memory store and PostgreSQL queries.
    """
    provider = DeterministicLocalEmbeddingProvider(dim=128)
    store = PostgresPgVectorStore(in_memory=True, embedding_provider=provider)

    chunk_valid = DocumentChunk(
        chunk_id="chunk_valid",
        document_id="doc_01",
        source="doc.md",
        content="Valid chunk with matching embedding space",
        embedding=provider.embed_query("Valid chunk with matching embedding space"),
        metadata={"embedding_space": provider.embedding_space},
    )
    chunk_incompatible = DocumentChunk(
        chunk_id="chunk_incompatible",
        document_id="doc_02",
        source="doc.md",
        content="Incompatible chunk from alternative provider",
        embedding=provider.embed_query("Incompatible chunk from alternative provider"),
        metadata={"embedding_space": "other_provider:other-model:128"},
    )
    chunk_missing_space = DocumentChunk(
        chunk_id="chunk_no_space",
        document_id="doc_03",
        source="doc.md",
        content="Legacy chunk with no embedding space recorded",
        embedding=provider.embed_query("Legacy chunk with no embedding space recorded"),
        metadata={},  # missing embedding_space
    )

    store._local_chunks = {
        chunk_valid.chunk_id: chunk_valid,
        chunk_incompatible.chunk_id: chunk_incompatible,
        chunk_missing_space.chunk_id: chunk_missing_space,
    }
    store._local_embeddings = {
        chunk_valid.chunk_id: chunk_valid.embedding,
        chunk_incompatible.chunk_id: chunk_incompatible.embedding,
        chunk_missing_space.chunk_id: chunk_missing_space.embedding,
    }

    results = store.query_vector("Valid chunk", top_k=5, min_score=0.0)
    retrieved_ids = [r.chunk_id for r in results]

    assert "chunk_valid" in retrieved_ids
    assert "chunk_incompatible" not in retrieved_ids
    assert "chunk_no_space" not in retrieved_ids

    # Check SQL query generation
    mock_pg_conn = MagicMock()
    mock_cur = MagicMock()
    mock_pg_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_cur.fetchall.return_value = []

    store._is_pg_connected = True
    store._pg_conn = mock_pg_conn
    store.db_vector_dim = 128

    store.query_vector("SQL verification query", top_k=3)
    mock_cur.execute.assert_called_once()
    sql_text = mock_cur.execute.call_args[0][0]
    sql_params = mock_cur.execute.call_args[0][1]

    assert "embedding IS NOT NULL" in sql_text
    assert "metadata->>'embedding_space' = %s" in sql_text
    assert provider.embedding_space in sql_params


def test_legacy_record_without_provenance_retrievable_via_full_text_search():
    """
    Legacy records without embedding_space ARE retrievable via full-text search.
    Full-text search preserves legacy accessibility without corrupting vector space.
    """
    # 1. SemanticMemoryStore._retrieve_memories_tsvector test
    mock_pg_conn = MagicMock()
    mock_cur = MagicMock()
    mock_pg_conn.cursor.return_value.__enter__.return_value = mock_cur

    legacy_row = (
        "mem_legacy_01",
        "Legacy system upgrade",
        "plan_01",
        "sess_01",
        "successful_plan",
        "Completed OS packages update",
        "success",
        "{}",
        "{}",
        "{}",  # metadata has NO embedding_space
        0.82,  # text_rank
        "2026-01-01",
    )
    mock_cur.fetchall.return_value = [legacy_row]

    store = SemanticMemoryStore(in_memory=True)
    store._is_pg_connected = True
    store._pg_conn = mock_pg_conn

    ts_results = store._retrieve_memories_tsvector("system upgrade", top_k=2)
    assert len(ts_results) == 1
    assert ts_results[0].memory_id == "mem_legacy_01"
    assert ts_results[0].metadata.get("embedding_space") is None

    # 2. PostgresPgVectorStore keyword search allows legacy chunks
    kv_store = PostgresPgVectorStore(in_memory=True)
    legacy_chunk = DocumentChunk(
        chunk_id="chunk_legacy_01",
        document_id="doc_legacy_01",
        source="legacy_manual.md",
        content="Guide to legacy system migration and rollback procedures",
        metadata={},  # no embedding_space
    )
    kv_store._local_chunks[legacy_chunk.chunk_id] = legacy_chunk

    kw_results = kv_store.query_keyword("migration procedures", top_k=2)
    assert len(kw_results) == 1
    assert kw_results[0].chunk_id == "chunk_legacy_01"


# ─────────────────────────────────────────────────────────────────────────────
# 7. Provider Recovery and Bounded Cooldowns
# ─────────────────────────────────────────────────────────────────────────────

def test_provider_recovery_after_network_failure_and_cooldown():
    """OpenAI recovers after network glitch once cooldown expires and request succeeds."""
    HybridEmbeddingService.reset_state()
    mock_1536_vec = [0.05] * 1536

    with patch("openai.OpenAI") as mock_openai_cls:
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_resp = MagicMock()
        mock_item = MagicMock()
        mock_item.embedding = mock_1536_vec
        mock_resp.data = [mock_item]

        # 1. First query fails with network timeout
        mock_client.embeddings.create.side_effect = ConnectionError("Connection timed out to api.openai.com")

        service = HybridEmbeddingService(api_key="sk-test-valid", force_local=False)
        vec_fallback = service.embed_query("Perform plan generation")

        # Must fall back to local 128-dim vector
        assert len(vec_fallback) == 128
        assert service.is_using_cloud is False
        assert service.active_dimension == 128
        assert "network_cooldown" in service.active_provider
        assert service.embedding_space == "local_deterministic_fallback:deterministic-hash-128:128"

        diag = service.get_diagnostics()
        assert diag["in_cooldown"] is True
        assert diag["last_failure_type"] == "network_error"

        # 2. Second query during cooldown: does NOT call OpenAI client
        mock_client.embeddings.create.reset_mock()
        vec_cooldown = service.embed_query("Second query in cooldown")
        assert len(vec_cooldown) == 128
        mock_client.embeddings.create.assert_not_called()

        # 3. Simulate cooldown expiration (35 seconds later)
        HybridEmbeddingService._last_failure_time = time.time() - 35.0
        assert service._is_in_cooldown() is False

        # Reset mock side effect to succeed
        mock_client.embeddings.create.side_effect = None
        mock_client.embeddings.create.return_value = mock_resp

        # 4. Third query: triggers recovery and succeeds with OpenAI!
        vec_recovered = service.embed_query("Recovered query")
        assert len(vec_recovered) == 1536
        assert vec_recovered == mock_1536_vec
        assert service.is_using_cloud is True
        assert service.active_dimension == 1536
        assert service.active_provider == "openai"
        assert service.embedding_space == "openai:text-embedding-3-small:1536"

        diag_after = service.get_diagnostics()
        assert diag_after["in_cooldown"] is False
        assert diag_after["consecutive_failures"] == 0

    HybridEmbeddingService.reset_state()


def test_permanent_fallback_on_401_unauthorized():
    """401 Unauthorized permanently locks fallback without hammering OpenAI."""
    HybridEmbeddingService.reset_state()

    with patch("openai.OpenAI") as mock_openai_cls:
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.embeddings.create.side_effect = Exception("Error 401: Unauthorized - Incorrect API key provided.")

        service = HybridEmbeddingService(api_key="sk-bad-key", force_local=False)
        vec = service.embed_query("Test query with bad credentials")

        assert len(vec) == 128
        assert service.is_using_cloud is False
        assert "auth_invalid_401" in service.active_provider
        assert service.embedding_space == "local_deterministic_fallback:deterministic-hash-128:128"

        diag = service.get_diagnostics()
        assert diag["auth_invalid"] is True
        assert diag["last_failure_type"] == "auth_401"
        assert diag["in_cooldown"] is True

        # Even after large elapsed time (e.g., 2 hours), 401 NEVER recovers
        HybridEmbeddingService._last_failure_time = time.time() - 7200.0
        assert service._is_in_cooldown() is True

        mock_client.embeddings.create.reset_mock()
        vec_later = service.embed_query("Query hours later")
        assert len(vec_later) == 128
        mock_client.embeddings.create.assert_not_called()

    HybridEmbeddingService.reset_state()

