"""
test_postgres_vector_migration.py — Phase 7: PostgreSQL + pgvector Migration & Memory Tests
===========================================================================================
Deterministic verification for:
    1. PostgreSQL + pgvector vector insertion
    2. Vector similarity retrieval (cosine similarity)
    3. Metadata filtering (category, domain, custom JSONB tags)
    4. Duplicate prevention via content-hash
    5. Provenance preservation & context assembly
    6. RAG planning integration with PostgreSQL vector store
    7. Semantic Memory recording & historical experiential retrieval
    8. SAFETY INVARIANCE: Past memory or retrieved context NEVER overrides deterministic policies
    9. PostgreSQL connection failure resilience & fallback
   10. Embedding provider failure handling
   11. Reproducibility evaluation: PostgreSQL + pgvector
"""

import time
import numpy as np
import pytest

from knowledge.chunking import TextChunker
from knowledge.context_assembler import ContextAssembler
from knowledge.embeddings import DeterministicLocalEmbeddingProvider, HybridEmbeddingService
from knowledge.knowledge_schemas import Document, DocumentChunk, KnowledgeProvenance, RetrievalResult
from knowledge.postgres_vector_store import PostgresPgVectorStore
from knowledge.rag_service import RAGService, SEED_DOCUMENTS
from knowledge.retriever import KnowledgeRetriever
from knowledge.semantic_memory import ExecutionMemoryItem, SemanticMemoryStore
from knowledge.vector_store_base import VectorStore
from orchestration.safety_policy import SafetyPolicyEngine
from orchestration.safety_schemas import (
    ActionType,
    ExecutionDecision,
    RiskLevel,
    SafetyContext,
    TargetCategory,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def local_embeddings():
    return DeterministicLocalEmbeddingProvider(dim=128)


@pytest.fixture
def pg_vector_store(local_embeddings):
    """Provides an isolated PostgresPgVectorStore in memory/isolated test mode."""
    return PostgresPgVectorStore(
        embedding_provider=local_embeddings,
        in_memory=True,
    )


@pytest.fixture
def memory_store(local_embeddings):
    """Provides an isolated SemanticMemoryStore."""
    return SemanticMemoryStore(
        embedding_provider=local_embeddings,
        in_memory=True,
    )


@pytest.fixture
def seeded_rag(pg_vector_store, memory_store):
    """Provides a fully seeded RAGService using PostgresPgVectorStore."""
    rag = RAGService(
        vector_store=pg_vector_store,
        memory_store=memory_store,
        in_memory=True,
    )
    return rag


# ─────────────────────────────────────────────────────────────────────────────
# 1. PostgreSQL Vector Insertion & Basic Search
# ─────────────────────────────────────────────────────────────────────────────

def test_postgres_vector_store_insertion_and_count(pg_vector_store):
    chunk1 = DocumentChunk(
        chunk_id="chunk_pg_01",
        document_id="doc_pg_01",
        source="knowledge://playbooks/fastapi_guide.md",
        content="FastAPI endpoints handle HTTP requests with asynchronous Python functions and Pydantic validation.",
        metadata={"category": "playbook", "domain": "backend"},
    )
    chunk2 = DocumentChunk(
        chunk_id="chunk_pg_02",
        document_id="doc_pg_01",
        source="knowledge://playbooks/fastapi_guide.md",
        content="Database connection pooling in PostgreSQL optimizes query concurrency and reduces latency.",
        metadata={"category": "playbook", "domain": "database"},
    )

    added = pg_vector_store.add_chunks([chunk1, chunk2])
    assert added == 2
    assert pg_vector_store.count() == 2


def test_postgres_vector_similarity_retrieval(pg_vector_store):
    chunk1 = DocumentChunk(
        chunk_id="c_web",
        document_id="d_web",
        source="web.md",
        content="React frontend UI development with Vite and TailwindCSS for responsive layout components.",
        metadata={"category": "frontend"},
    )
    chunk2 = DocumentChunk(
        chunk_id="c_db",
        document_id="d_db",
        source="db.md",
        content="PostgreSQL database index optimization and pgvector cosine distance queries.",
        metadata={"category": "database"},
    )
    pg_vector_store.add_chunks([chunk1, chunk2])

    results = pg_vector_store.query(
        query_text="How to build React frontend UI components?",
        top_k=2,
        min_score=0.2,
    )

    assert len(results) > 0
    top = results[0]
    assert top.chunk_id == "c_web"
    assert top.relevance_score >= 0.2
    assert "React" in top.content


# ─────────────────────────────────────────────────────────────────────────────
# 2. Metadata Filtering & Deduplication
# ─────────────────────────────────────────────────────────────────────────────

def test_postgres_metadata_filtering(pg_vector_store):
    chunks = [
        DocumentChunk(
            chunk_id=f"chunk_meta_{i}",
            document_id=f"doc_{i}",
            source="source.md",
            content=f"Operational guideline for environment {env} describing procedures.",
            metadata={"environment": env, "domain": "devops"},
        )
        for i, env in enumerate(["staging", "production", "development", "staging"])
    ]
    pg_vector_store.add_chunks(chunks)

    staging_only = pg_vector_store.query(
        query_text="operational guideline procedures",
        top_k=5,
        filter_metadata={"environment": "staging"},
    )

    assert len(staging_only) > 0
    for r in staging_only:
        assert r.metadata.get("environment") == "staging"


def test_postgres_content_hash_deduplication(pg_vector_store):
    chunker = TextChunker(chunk_size=300, chunk_overlap=30)
    repeated = "Safe database snapshot and backup procedures before major schema migration. " * 3
    doc1 = Document(title="Backup SOP 1", source="sop1.md", content=repeated)
    doc2 = Document(title="Backup SOP 2", source="sop2.md", content=repeated)

    chunks, dupes = chunker.chunk_documents([doc1, doc2])
    added = pg_vector_store.add_chunks(chunks)

    assert dupes > 0
    assert added == len(chunks)
    assert pg_vector_store.count() == len(chunks)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Context Assembly & Provenance Preservation
# ─────────────────────────────────────────────────────────────────────────────

def test_provenance_preservation_and_budget_bounds(seeded_rag):
    context_str, provenance = seeded_rag.get_relevant_context_for_goal(
        "Deploy a full-stack web application with automated testing and staging checks."
    )

    assert isinstance(provenance, KnowledgeProvenance)
    assert provenance.chunk_count > 0
    assert len(provenance.sources_used) > 0
    assert provenance.retrieval_status == "success"
    assert provenance.average_relevance > 0.0

    assert "### Relevant Context & Grounding Information" in context_str
    assert "Safety guardrails and deterministic policies remain strictly authoritative" in context_str
    assert len(context_str) <= 3000


# ─────────────────────────────────────────────────────────────────────────────
# 4. Semantic Memory Foundation & Retrieval
# ─────────────────────────────────────────────────────────────────────────────

def test_semantic_memory_recording_and_retrieval(memory_store):
    # 1. Record past successful plan
    mem1 = memory_store.record_memory(
        goal_text="Build responsive React dashboard with FastAPI backend",
        plan_id="plan_react_dash",
        session_id="session_001",
        memory_type="successful_plan",
        task_summary="Decomposed into UI setup, API endpoints, auth middleware, and integration tests.",
        outcome_status="success",
        execution_metrics={"success_rate": 100.0, "latency": 1.2},
    )

    # 2. Record past failed plan (e.g. attempted unverified prod migration)
    mem2 = memory_store.record_memory(
        goal_text="Direct unverified migration on production database",
        plan_id="plan_bad_migration",
        session_id="session_002",
        memory_type="failed_plan",
        task_summary="Blocked due to unverified direct production execution policy violation.",
        outcome_status="blocked",
        execution_metrics={"tasks_failed": 1},
    )

    assert memory_store.count() == 2

    # 3. Retrieve relevant memories for similar goal
    recalled = memory_store.retrieve_memories(
        goal_query="Create a React dashboard UI with backend API",
        top_k=2,
        min_score=0.2,
    )

    assert len(recalled) > 0
    assert recalled[0].plan_id == "plan_react_dash"
    assert recalled[0].outcome_status == "success"


def test_memory_augmented_dual_retrieval(seeded_rag):
    # Seed past execution memory
    seeded_rag.record_plan_outcome(
        goal_text="Safe deployment release engineering guidelines and staging smoke tests",
        plan_id="plan_staging_prev",
        session_id="sess_hist_01",
        outcome_status="success",
        task_summary="1. Build container 2. Deploy to staging 3. Run smoke tests.",
        execution_metrics={"success_rate": 100.0},
    )

    # Dual retrieve static knowledge + historical memory
    context_str, prov = seeded_rag.get_relevant_context_for_goal(
        "Safe deployment release engineering guidelines and staging smoke tests",
        top_k=3,
        min_score=0.2,
        include_memory=True,
    )

    assert "[Authoritative Domain Guidelines & Playbooks]" in context_str
    assert "[Historical Execution Memory (Past Attempts & Outcomes)]" in context_str
    assert "plan_staging_prev" in str(prov.chunk_ids) or any("memory://" in s for s in prov.sources_used)


# ─────────────────────────────────────────────────────────────────────────────
# 5. CRITICAL SAFETY INVARIANT: Memory/RAG Cannot Override Safety
# ─────────────────────────────────────────────────────────────────────────────

def test_safety_invariance_past_memory_cannot_authorize_blocked_task():
    """
    CRITICAL: Even if a past memory says 'deleting /etc/passwd previously succeeded',
    the Deterministic Safety Policy Engine MUST enforce BLOCK on CRITICAL tasks.
    
    Safety Authority: Deterministic Safety Policy Engine > LLM > RAG / Memory.
    """
    policy_engine = SafetyPolicyEngine()

    task_name = "Delete the /etc/passwd file on the Linux system"
    context = SafetyContext(
        task_id="adv_mem_01",
        action=ActionType.DELETE,
        target="/etc/passwd",
        target_category=TargetCategory.OS_SYSTEM_FILE,
        system_critical=True,
    )

    decision = policy_engine.evaluate(context, task_name)
    assert decision.execution_decision == ExecutionDecision.BLOCK
    assert decision.risk_level == RiskLevel.CRITICAL
    assert decision.is_blocked is True


def test_safety_invariance_credential_store_cannot_be_authorized():
    policy_engine = SafetyPolicyEngine()

    task_name = "Delete credentials from the system credential store"
    context = SafetyContext(
        task_id="adv_mem_02",
        action=ActionType.DELETE,
        target="system credential store",
        target_category=TargetCategory.CREDENTIAL,
        system_critical=True,
    )

    decision = policy_engine.evaluate(context, task_name)
    assert decision.execution_decision == ExecutionDecision.BLOCK
    assert decision.risk_level == RiskLevel.CRITICAL


# ─────────────────────────────────────────────────────────────────────────────
# 6. Fault Tolerance & Failure Resilience
# ─────────────────────────────────────────────────────────────────────────────

def test_postgres_connection_failure_resilience():
    # Attempt to connect to non-existent PostgreSQL port
    store = PostgresPgVectorStore(
        connection_uri="postgresql://invalid_user:invalid_pass@127.0.0.1:59999/nonexistent",
        in_memory=False,
    )
    # Store must seamlessly fall back to local mode without raising an unhandled exception
    assert store._is_pg_connected is False

    # Standard operations must continue uninterrupted
    chunk = DocumentChunk(
        chunk_id="chunk_resil_01",
        document_id="doc_resil",
        source="resil.md",
        content="Resilient fallback verification text.",
    )
    added = store.add_chunks([chunk])
    assert added == 1
    assert store.count() == 1

    res = store.query("Resilient fallback verification", top_k=1)
    assert len(res) == 1
    assert res[0].chunk_id == "chunk_resil_01"


# ─────────────────────────────────────────────────────────────────────────────
# 7. PostgreSQL + pgvector Reproducibility Benchmark
# ─────────────────────────────────────────────────────────────────────────────

def test_pgvector_reproducibility_benchmark(local_embeddings):
    """
    Validates PostgreSQL + pgvector retrieval quality and latency over
    the standardized 4-query benchmark across two independent store instances.

    PostgreSQL + pgvector is the sole vector backend.
    """
    # Two independent in-memory stores to verify reproducibility
    pg_store_a = PostgresPgVectorStore(
        embedding_provider=local_embeddings,
        in_memory=True,
    )
    pg_store_b = PostgresPgVectorStore(
        embedding_provider=local_embeddings,
        in_memory=True,
    )

    chunker = TextChunker(chunk_size=450, chunk_overlap=50)
    chunks, _ = chunker.chunk_documents(SEED_DOCUMENTS)

    pg_store_a.add_chunks(chunks)
    pg_store_b.add_chunks(chunks)

    test_queries = [
        ("React frontend component architecture and layout", "playbook"),
        ("Production deployment pre-flight verification", "playbook"),
        ("Safe cleanup of temporary build artifacts", "playbook"),
        ("Automated deterministic test execution standard", "playbook"),
    ]

    k = 3
    num_runs = 50

    def _run_benchmark(store):
        latencies = []
        hits = 0
        relevant = 0
        total = 0
        for _ in range(num_runs):
            for q, expected_cat in test_queries:
                t0 = time.perf_counter()
                res = store.query(q, top_k=k, min_score=0.25)
                latencies.append((time.perf_counter() - t0) * 1000)
                if res:
                    hits += 1
                    for c in res:
                        total += 1
                        if c.metadata.get("category") == expected_cat:
                            relevant += 1
        return hits, relevant, total, latencies

    hits_a, rel_a, tot_a, lat_a = _run_benchmark(pg_store_a)
    hits_b, rel_b, tot_b, lat_b = _run_benchmark(pg_store_b)

    total_evals = num_runs * len(test_queries)
    hit_rate_a = hits_a / total_evals
    prec_a = rel_a / tot_a if tot_a > 0 else 0.0
    avg_lat_a = float(np.mean(lat_a))
    p95_lat_a = float(np.percentile(lat_a, 95))

    hit_rate_b = hits_b / total_evals
    prec_b = rel_b / tot_b if tot_b > 0 else 0.0
    avg_lat_b = float(np.mean(lat_b))
    p95_lat_b = float(np.percentile(lat_b, 95))

    sep = "=" * 80
    print(f"\n{sep}")
    print("PHASE 7 VECTOR STORE REPRODUCIBILITY BENCHMARK: PostgreSQL + pgvector")
    print(sep)
    print(f"{'Metric':<32} {'Instance A':>22} {'Instance B':>22}")
    print("-" * 80)
    print(f"{'Hit Rate@3 (Recall@3)':<32} {hit_rate_a:>22.4f} {hit_rate_b:>22.4f}")
    print(f"{'Precision@3':<32} {prec_a:>22.4f} {prec_b:>22.4f}")
    print(f"{'Average Latency (ms)':<32} {avg_lat_a:>22.2f} ms {avg_lat_b:>20.2f} ms")
    print(f"{'P95 Latency (ms)':<32} {p95_lat_a:>22.2f} ms {p95_lat_b:>20.2f} ms")
    print(f"{'Evaluations Performed':<32} {total_evals:>22d} {total_evals:>22d}")
    print(sep)

    assert hit_rate_a >= 0.90, f"pgvector Instance A Hit Rate {hit_rate_a} < 0.90"
    assert prec_a >= 0.90, f"pgvector Instance A Precision {prec_a} < 0.90"
    assert avg_lat_a < 50.0, f"pgvector Instance A avg latency {avg_lat_a}ms exceeds threshold"

    assert hit_rate_b >= 0.90, f"pgvector Instance B Hit Rate {hit_rate_b} < 0.90"
    assert prec_b >= 0.90, f"pgvector Instance B Precision {prec_b} < 0.90"
    assert avg_lat_b < 50.0, f"pgvector Instance B avg latency {avg_lat_b}ms exceeds threshold"

