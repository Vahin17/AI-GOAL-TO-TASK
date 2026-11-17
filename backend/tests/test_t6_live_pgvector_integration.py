"""
test_t6_live_pgvector_integration.py — Task T6 Live PostgreSQL / pgvector Integration Suite
============================================================================================
Authoritative live integration suite verifying:
1. PostgreSQL connectivity and pgvector extension status on live Docker container.
2. Actual schema column types, vector dimensions (typmod=128), and HNSW indexes.
3. Persistent writes and reads of knowledge chunks across fresh service instances.
4. Persistent writes and reads of execution memories across fresh service instances.
5. Embedding provenance (provider:model:dimension) persisted in JSONB metadata.
6. Incompatible embedding-space records excluded from vector similarity retrieval.
7. Dimension mismatch safeguards: stores with embedding=NULL and enables full-text fallback.
8. Connection and write failures surfaced truthfully without claiming false PostgreSQL persistence.

Run with:
    pytest backend/tests/test_t6_live_pgvector_integration.py --run-live -v
"""

from __future__ import annotations

import time
import uuid
from unittest.mock import MagicMock
import psycopg2
import psycopg2.extras
import pytest

from core.config import settings
from knowledge.embeddings import (
    DeterministicLocalEmbeddingProvider,
    HybridEmbeddingService,
)
from knowledge.postgres_vector_store import DocumentChunk, PostgresPgVectorStore
from knowledge.semantic_memory import ExecutionMemoryItem, SemanticMemoryStore

pytestmark = pytest.mark.live_db


@pytest.fixture(scope="module")
def pg_uri() -> str:
    uri = settings.POSTGRES_URI
    assert uri, "POSTGRES_URI must be configured"
    return uri


@pytest.fixture(scope="module")
def pg_conn(pg_uri: str):
    """Module-level live PostgreSQL connection."""
    conn = psycopg2.connect(pg_uri, connect_timeout=5)
    conn.autocommit = True
    yield conn
    conn.close()


# ── Area A: Database and Schema Verification ────────────────────────────────

def test_live_pg_connection_and_pgvector_extension(pg_conn):
    """PostgreSQL is live (v16+) and pgvector extension is installed and active."""
    with pg_conn.cursor() as cur:
        cur.execute("SELECT version();")
        version_str = cur.fetchone()[0]
        assert "PostgreSQL" in version_str
        assert "16." in version_str

        cur.execute("SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';")
        ext = cur.fetchone()
        assert ext is not None, "pgvector extension is not installed in the database"
        assert ext[0] == "vector"
        assert ext[1] is not None, f"pgvector version: {ext[1]}"


def test_schema_dimensions_and_hnsw_indexes(pg_conn):
    """knowledge_chunks and execution_memories tables have vector(128) columns and HNSW indexes."""
    with pg_conn.cursor() as cur:
        # Check column dimensions
        for tbl in ("knowledge_chunks", "execution_memories"):
            cur.execute(
                """
                SELECT a.attname, a.atttypmod
                FROM pg_attribute a
                JOIN pg_class c ON a.attrelid = c.oid
                JOIN pg_namespace n ON c.relnamespace = n.oid
                WHERE n.nspname = 'public'
                  AND c.relname = %s
                  AND a.attname = 'embedding'
                  AND NOT a.attisdropped;
                """,
                (tbl,),
            )
            row = cur.fetchone()
            assert row is not None, f"embedding column not found on table {tbl}"
            assert row[1] == 128, f"Table {tbl}.embedding has typmod={row[1]}, expected 128"

        # Check HNSW indexes
        cur.execute(
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE tablename IN ('knowledge_chunks', 'execution_memories')
              AND indexdef ILIKE '%hnsw%';
            """
        )
        hnsw_indexes = cur.fetchall()
        idx_names = [row[0] for row in hnsw_indexes]
        assert "idx_knowledge_chunks_embedding_hnsw" in idx_names
        assert "idx_exec_memories_embedding_hnsw" in idx_names


# ── Area B: Persistent Writes and Reads ──────────────────────────────────────

def test_live_knowledge_chunk_persistence_and_retrieval(pg_conn):
    """Write knowledge chunk, reinitialize store instance, and retrieve via vector similarity."""
    test_run_id = f"t6_test_{uuid.uuid4().hex[:8]}"
    doc_id = f"doc_{test_run_id}"
    chunk_id = f"chk_{test_run_id}"

    provider = HybridEmbeddingService(force_local=True)
    store1 = PostgresPgVectorStore(table_name="knowledge_chunks", embedding_provider=provider)
    assert store1._is_pg_connected is True
    assert store1.active_backend == "postgres_pgvector"

    chunk = DocumentChunk(
        chunk_id=chunk_id,
        document_id=doc_id,
        source="t6_live_audit",
        chunk_index=0,
        content=f"Planetary exploration satellite trajectory planning and telemetry verification [{test_run_id}].",
        metadata={"test_run_id": test_run_id, "classification": "verified_live"},
    )

    try:
        inserted = store1.add_chunks([chunk])
        assert inserted == 1

        # Re-initialize a brand new store instance (fresh connection)
        store2 = PostgresPgVectorStore(table_name="knowledge_chunks", embedding_provider=provider)
        assert store2._is_pg_connected is True

        results = store2.query_vector(
            query_text=f"Planetary exploration satellite trajectory [{test_run_id}]",
            top_k=5,
            min_score=0.2,
        )
        assert len(results) >= 1
        matched = [r for r in results if r.chunk_id == chunk_id]
        assert len(matched) == 1
        assert matched[0].document_id == doc_id
        assert matched[0].relevance_score > 0.0

    finally:
        # Clean up
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM knowledge_chunks WHERE document_id = %s;", (doc_id,))


def test_live_execution_memory_persistence_and_retrieval(pg_conn):
    """Write execution memory, reinitialize store instance, and retrieve via vector search."""
    test_run_id = f"t6_mem_{uuid.uuid4().hex[:8]}"
    provider = HybridEmbeddingService(force_local=True)

    store1 = SemanticMemoryStore(embedding_provider=provider)
    assert store1._is_pg_connected is True
    assert store1.active_backend == "postgres_pgvector"

    item = store1.record_memory(
        goal_text=f"Execute automated flight reservation workflow [{test_run_id}]",
        plan_id=f"plan_{test_run_id}",
        session_id=f"sess_{test_run_id}",
        memory_type="successful_plan",
        task_summary=f"Automated navigation and seat reservation verified [{test_run_id}]",
        outcome_status="success",
        metadata={"test_run_id": test_run_id},
    )

    try:
        assert item.memory_id is not None

        # Direct SQL verification of row and embedding presence
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                SELECT memory_id, vector_dims(embedding), metadata->>'embedding_space'
                FROM execution_memories
                WHERE memory_id = %s;
                """,
                (item.memory_id,),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == item.memory_id
            assert row[1] == 128
            assert "deterministic-hash-128" in row[2]

        # Reinitialize fresh store instance
        store2 = SemanticMemoryStore(embedding_provider=provider)
        assert store2._is_pg_connected is True

        results = store2.retrieve_memories(
            goal_query=f"flight reservation workflow [{test_run_id}]",
            top_k=5,
        )
        assert len(results) >= 1
        matched = [r for r in results if r.memory_id == item.memory_id]
        assert len(matched) == 1
        assert matched[0].outcome_status == "success"

    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM execution_memories WHERE memory_id = %s;", (item.memory_id,))


# ── Area C: Embedding Identity & Compatibility ──────────────────────────────

def test_live_incompatible_embedding_space_excluded(pg_conn):
    """Vectors from an incompatible embedding space are strictly excluded from vector retrieval."""
    test_run_id = f"t6_incompat_{uuid.uuid4().hex[:8]}"
    provider = HybridEmbeddingService(force_local=True)
    store = SemanticMemoryStore(embedding_provider=provider)

    # Insert row directly with mismatched embedding_space
    mem_id = f"mem_{test_run_id}"
    fake_vec = [0.05] * 128
    with pg_conn.cursor() as cur:
        from pgvector import Vector
        cur.execute(
            """
            INSERT INTO execution_memories (
                memory_id, goal_text, memory_type, task_summary, outcome_status,
                metadata, embedding
            ) VALUES (
                %s, %s, 'successful_plan', 'Incompatible space task', 'success',
                %s, %s
            );
            """,
            (
                mem_id,
                f"Perform quantum cryptographic handshake [{test_run_id}]",
                psycopg2.extras.Json({
                    "embedding_space": "other_model:v2:128",
                    "test_run_id": test_run_id,
                }),
                Vector(fake_vec),
            ),
        )

    try:
        # Query using local provider (space local_deterministic_fallback:deterministic-hash-128:128)
        results = store.retrieve_memories(
            goal_query=f"quantum cryptographic handshake [{test_run_id}]",
            top_k=10,
        )
        # Incompatible space must NOT match in vector similarity
        matched = [r for r in results if r.memory_id == mem_id]
        assert len(matched) == 0, "Incompatible embedding space was incorrectly returned in vector search"

    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM execution_memories WHERE memory_id = %s;", (mem_id,))


def test_live_dimension_mismatch_inserts_null_and_enables_fulltext_fallback(pg_conn):
    """When generated dimension does not match DB schema, embedding=NULL is inserted without SQL error."""
    test_run_id = f"t6_mismatch_{uuid.uuid4().hex[:8]}"

    # Mock provider that reports dimension 1536 and returns 1536-dim vectors
    class Mock1536Provider:
        dimension = 1536
        active_dimension = 1536
        active_provider = "mock_openai"
        active_model = "model3"
        model_name = "model3"
        embedding_space = "mock_openai:model3:1536"

        def embed_query(self, text: str):
            return [0.1] * 1536

        def embed_texts(self, texts):
            return [[0.1] * 1536 for _ in texts]

    mock_provider = Mock1536Provider()

    store = SemanticMemoryStore(embedding_provider=mock_provider)
    assert store._is_pg_connected is True
    assert store.dim == 1536
    assert store.db_vector_dim == 128

    # Record memory — must not raise PostgreSQL 22000 dimension mismatch error!
    item = store.record_memory(
        goal_text=f"Deploy microservice container orchestration [{test_run_id}]",
        memory_type="successful_plan",
        task_summary="Deploy microservice task",
        outcome_status="success",
        metadata={"test_run_id": test_run_id},
    )

    try:
        assert item.memory_id is not None

        # Verify in PostgreSQL: embedding is NULL, row exists, metadata records mismatch
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                SELECT memory_id, embedding IS NULL, metadata->>'embedding_dim_mismatch'
                FROM execution_memories
                WHERE memory_id = %s;
                """,
                (item.memory_id,),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[1] is True, "Embedding should be NULL due to dimension mismatch guard"
            assert row[2] == "true"

        # Verify full-text search fallback retrieves the NULL-embedding row
        results = store.retrieve_memories(
            goal_query=f"Deploy microservice container orchestration [{test_run_id}]",
            top_k=5,
        )
        assert len(results) >= 1
        matched = [r for r in results if r.memory_id == item.memory_id]
        assert len(matched) == 1
        assert matched[0].memory_id == item.memory_id

    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM execution_memories WHERE memory_id = %s;", (item.memory_id,))


# ── Area D: Failure Behavior & Truthful Reporting ────────────────────────────

def test_connection_failure_surfaces_accurately_without_false_persistence():
    """When PostgreSQL is unreachable, store truthfully reports local fallback and does not claim PG success."""
    bad_uri = "postgresql://invalid_user:bad_pass@127.0.0.1:5439/nonexistent_db"
    store = SemanticMemoryStore(connection_uri=bad_uri)

    assert store._is_pg_connected is False
    assert store.active_backend == "local_fallback"

    item = store.record_memory(
        goal_text="Local fallback offline memory test",
        memory_type="successful_plan",
        task_summary="Local offline task",
        outcome_status="success",
    )
    assert item.memory_id in store._local_memories
    # Local fallback is truthful
    assert store.active_backend == "local_fallback"
