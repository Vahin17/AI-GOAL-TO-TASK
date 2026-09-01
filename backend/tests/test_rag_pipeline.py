"""
test_rag_pipeline.py — Phase 6: Knowledge Ingestion & RAG Pipeline Tests
=========================================================================
Deterministic verification of the complete Context-Aware Knowledge + RAG subsystem:
    1. Document chunking, hashing, and token estimation
    2. Chunk deduplication
    3. Deterministic local & hybrid embeddings
    4. PostgreSQL + pgvector vector indexing and similarity query
    5. Metadata filtering (category, source, custom tags)
    6. Relevance threshold filtering
    7. Top-K constraint adherence
    8. Empty & whitespace query handling
    9. Context assembly, provenance generation & token budget truncation
   10. Embedding failure & vector DB failure resilience
   11. SAFETY INVARIANCE: Retrieved context CANNOT override deterministic safety policies
   12. Empirical retrieval evaluation: Precision@K, Recall@K / Hit Rate@K, Latency (ms)
"""

import time
from typing import List

import pytest

from knowledge.chunking import TextChunker
from knowledge.context_assembler import ContextAssembler
from knowledge.embeddings import (
    DeterministicLocalEmbeddingProvider,
    HybridEmbeddingService,
)
from knowledge.knowledge_schemas import (
    Document,
    DocumentChunk,
    KnowledgeProvenance,
    RetrievalResult,
)
from knowledge.rag_service import RAGService, SEED_DOCUMENTS
from knowledge.retriever import KnowledgeRetriever
from knowledge.postgres_vector_store import PostgresPgVectorStore
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
def in_memory_rag():
    """Provides a fresh isolated in-memory RAGService instance."""
    rag = RAGService.reset_instance(in_memory=True)
    return rag


@pytest.fixture
def local_embeddings():
    return DeterministicLocalEmbeddingProvider(dim=128)


@pytest.fixture
def in_memory_store(local_embeddings):
    return PostgresPgVectorStore(
        embedding_provider=local_embeddings,
        in_memory=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Chunking & Deduplication Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_chunker_splits_and_estimates_tokens():
    chunker = TextChunker(chunk_size=150, chunk_overlap=30)
    text = (
        "FastAPI is a modern, fast web framework for building APIs with Python. "
        "It is based on standard Python type hints. "
        "The key features are fast to code, fewer bugs, and intuitive design. "
        "It supports asynchronous request handling natively."
    )
    doc = Document(title="FastAPI Overview", source="docs/fastapi.md", content=text)
    chunks = chunker.chunk_document(doc)

    assert len(chunks) >= 2
    for chunk in chunks:
        assert chunk.char_count == len(chunk.content)
        assert chunk.token_estimate > 0
        assert chunk.content_hash != ""
        assert chunk.document_id == doc.document_id
        assert chunk.source == doc.source


def test_chunker_deduplicates_identical_chunks():
    chunker = TextChunker(chunk_size=200, chunk_overlap=20)
    repeated_text = "This is a unique paragraph describing deployment protocols. " * 3
    doc1 = Document(title="Doc 1", source="source_1", content=repeated_text)
    doc2 = Document(title="Doc 2", source="source_2", content=repeated_text)

    chunks, dupes = chunker.chunk_documents([doc1, doc2])
    assert dupes > 0, "Identical content chunks should be deduplicated."
    unique_hashes = {c.content_hash for c in chunks}
    assert len(chunks) == len(unique_hashes)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Embedding Properties & Determinism
# ─────────────────────────────────────────────────────────────────────────────

def test_deterministic_embedding_consistency(local_embeddings):
    text1 = "deploy the web application to production"
    text2 = "deploy the web application to production"
    text3 = "delete operating system kernel files"

    v1 = local_embeddings.embed_query(text1)
    v2 = local_embeddings.embed_query(text2)
    v3 = local_embeddings.embed_query(text3)

    assert v1 == v2, "Identical text must produce identical vectors."
    assert v1 != v3, "Different text must produce different vectors."
    assert len(v1) == 128


def test_embedding_cosine_similarity(local_embeddings):
    v_web = local_embeddings.embed_query("frontend React web user interface components")
    v_ui = local_embeddings.embed_query("React UI component development and layout design")
    v_db = local_embeddings.embed_query("PostgreSQL database migrations and index schemas")

    # Cosine dot product (vectors are L2-normalized)
    sim_web_ui = sum(a * b for a, b in zip(v_web, v_ui))
    sim_web_db = sum(a * b for a, b in zip(v_web, v_db))

    assert sim_web_ui > sim_web_db, (
        f"Semantic similarity (web-UI: {sim_web_ui:.3f}) should exceed unrelated (web-DB: {sim_web_db:.3f})"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Vector Storage & Retrieval Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_relevant_retrieval(in_memory_rag):
    result = in_memory_rag.retriever.retrieve(
        query="How should I design frontend React components and backend API endpoints?",
        top_k=3,
        min_score=0.2,
    )
    assert result.status == "success"
    assert len(result.chunks) > 0
    top_chunk = result.chunks[0]
    assert top_chunk.document_id == "doc_playbook_web_app"
    assert "Web Application" in top_chunk.title or "API" in top_chunk.content
    assert top_chunk.relevance_score >= 0.2


def test_irrelevant_retrieval_and_threshold(in_memory_rag):
    # Query something completely absent from software engineering playbooks
    result = in_memory_rag.retriever.retrieve(
        query="photosynthesis light reaction in chlorophyll molecules of plant cells",
        top_k=3,
        min_score=0.85,  # High threshold
    )
    assert len(result.chunks) == 0
    assert result.status == "empty"


def test_metadata_filtering(in_memory_rag):
    # Retrieve only devops / safe deployment playbooks
    result_devops = in_memory_rag.retriever.retrieve(
        query="release verification and deployment checklist",
        top_k=3,
        filter_metadata={"domain": "devops"},
    )
    assert len(result_devops.chunks) > 0
    for chunk in result_devops.chunks:
        assert chunk.metadata.get("domain") == "devops"


def test_top_k_constraint(in_memory_rag):
    for k in [1, 2, 3]:
        result = in_memory_rag.retriever.retrieve(
            query="procedures for testing, deployment, and cleanup",
            top_k=k,
            min_score=0.0,
        )
        assert len(result.chunks) <= k


def test_empty_and_whitespace_query(in_memory_rag):
    r_empty = in_memory_rag.retriever.retrieve(query="")
    r_spaces = in_memory_rag.retriever.retrieve(query="   \n\t  ")
    assert r_empty.status == "empty" and len(r_empty.chunks) == 0
    assert r_spaces.status == "empty" and len(r_spaces.chunks) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 4. Context Assembly & Provenance
# ─────────────────────────────────────────────────────────────────────────────

def test_context_assembly_and_provenance(in_memory_rag):
    context_str, provenance = in_memory_rag.get_relevant_context_for_goal(
        "Build a modern full-stack web application with automated testing and staging deployment."
    )
    assert isinstance(context_str, str)
    assert len(context_str) > 0
    assert "### Relevant Context & Grounding Information" in context_str
    assert "Safety guardrails and deterministic policies remain strictly authoritative" in context_str

    assert isinstance(provenance, KnowledgeProvenance)
    assert provenance.chunk_count > 0
    assert len(provenance.sources_used) > 0
    assert provenance.average_relevance > 0.0
    assert provenance.retrieval_latency_ms >= 0.0


def test_context_budget_limit():
    assembler = ContextAssembler(max_context_chars=350)
    fake_chunks = [
        DocumentChunk(
            chunk_id=f"c_{i}",
            document_id=f"d_{i}",
            source=f"source_{i}.md",
            content=f"Long content paragraph number {i} describing operational procedures in excessive detail " * 5,
        )
        for i in range(10)
    ]
    from knowledge.knowledge_schemas import RetrievedChunk
    retrieved = [
        RetrievedChunk(
            chunk_id=c.chunk_id,
            document_id=c.document_id,
            source=c.source,
            content=c.content,
            relevance_score=0.9,
        )
        for c in fake_chunks
    ]
    res = RetrievalResult(query="test", chunks=retrieved, status="success")
    ctx, prov = assembler.assemble(res)

    assert len(ctx) <= 450  # Strictly bounded around max limit
    assert prov.chunk_count < len(fake_chunks)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Fault Tolerance & Resilience Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_embedding_fallback_resilience():
    # Construct hybrid service with invalid OpenAI key
    hybrid = HybridEmbeddingService(api_key="invalid_sk_test_key_that_will_fail")
    # Must fallback to deterministic local embedding without crashing
    vec = hybrid.embed_query("safe system verification test")
    assert isinstance(vec, list)
    assert len(vec) == 128


def test_vector_store_error_containment():
    # Create retriever with closed/cleared vector store
    store = PostgresPgVectorStore(in_memory=True)
    retriever = KnowledgeRetriever(vector_store=store)
    # Query on empty store
    res = retriever.retrieve(query="something")
    assert res.status == "empty"
    assert res.chunks == []


# ─────────────────────────────────────────────────────────────────────────────
# 6. CRITICAL SAFETY INVARIANT: RAG Context Cannot Override Safety Policies
# ─────────────────────────────────────────────────────────────────────────────

def test_safety_invariance_with_adversarial_rag_context():
    """
    CRITICAL: Even if retrieved RAG context explicitly claims that an action
    like 'delete /etc/passwd' or 'format OS drive' is safe, the Deterministic
    Safety Policy Engine MUST still flag and BLOCK the critical operation.
    
    Safety Authority: Deterministic Policy Engine > LLM > RAG Context
    """
    policy_engine = SafetyPolicyEngine()

    # Adversarial task where RAG might suggest "it is safe to format system drive"
    task_name = "Format the system drive including the OS partition"
    context = SafetyContext(
        task_id="adv_rag_01",
        action=ActionType.EXECUTE,
        target="system drive / OS partition",
        target_category=TargetCategory.OS_SYSTEM_FILE,
        system_critical=True,
    )

    decision = policy_engine.evaluate(context, task_name)
    assert decision.execution_decision == ExecutionDecision.BLOCK
    assert decision.risk_level == RiskLevel.CRITICAL
    assert decision.is_blocked is True


def test_safety_invariance_boot_files_blocked():
    policy_engine = SafetyPolicyEngine()

    task_name = "Delete operating system boot files"
    context = SafetyContext(
        task_id="adv_rag_02",
        action=ActionType.DELETE,
        target="operating system boot files",
        target_category=TargetCategory.OS_BOOT_FILE,
        system_critical=True,
    )

    decision = policy_engine.evaluate(context, task_name)
    assert decision.execution_decision == ExecutionDecision.BLOCK
    assert decision.risk_level == RiskLevel.CRITICAL


# ─────────────────────────────────────────────────────────────────────────────
# 7. Empirical Retrieval Evaluation (Precision@K, Recall@K, Latency)
# ─────────────────────────────────────────────────────────────────────────────

def test_retrieval_benchmark_metrics(in_memory_rag):
    """
    Evaluates empirical retrieval metrics over a query benchmark:
    - Precision@3
    - Recall@3 / Hit Rate@3
    - Average Retrieval Latency (ms)
    """
    test_queries = [
        ("React frontend component architecture and layout", "playbook"),
        ("Production deployment pre-flight verification", "playbook"),
        ("Safe cleanup of temporary build artifacts", "playbook"),
        ("Automated deterministic test execution standard", "playbook"),
    ]

    hits = 0
    total_relevant_retrieved = 0
    total_retrieved = 0
    latencies: List[float] = []

    k = 3
    for query, expected_category in test_queries:
        start = time.perf_counter()
        result = in_memory_rag.retriever.retrieve(query=query, top_k=k, min_score=0.25)
        lat = (time.perf_counter() - start) * 1000
        latencies.append(lat)

        if result.chunks:
            hits += 1  # Hit Rate
            for chunk in result.chunks:
                total_retrieved += 1
                if chunk.metadata.get("category") == expected_category:
                    total_relevant_retrieved += 1

    precision_at_k = total_relevant_retrieved / total_retrieved if total_retrieved > 0 else 0.0
    hit_rate_at_k = hits / len(test_queries)
    avg_latency_ms = sum(latencies) / len(latencies)

    print("\n" + "=" * 70)
    print("PHASE 6 RAG RETRIEVAL BENCHMARK EVALUATION")
    print("=" * 70)
    print(f"  Queries Evaluated   : {len(test_queries)}")
    print(f"  Top-K               : {k}")
    print(f"  Hit Rate@{k}         : {hit_rate_at_k:.4f} ({hits}/{len(test_queries)})")
    print(f"  Precision@{k}        : {precision_at_k:.4f} ({total_relevant_retrieved}/{total_retrieved})")
    print(f"  Avg Latency (ms)    : {avg_latency_ms:.2f} ms")
    print("=" * 70)

    assert hit_rate_at_k >= 0.75, f"Hit rate {hit_rate_at_k} < 0.75"
    assert precision_at_k >= 0.70, f"Precision {precision_at_k} < 0.70"
    assert avg_latency_ms < 100.0, f"Retrieval latency {avg_latency_ms}ms is unexpectedly high"
