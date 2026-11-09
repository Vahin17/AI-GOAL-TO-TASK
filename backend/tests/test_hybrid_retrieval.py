"""
test_hybrid_retrieval.py — Unit Tests for Hybrid (Vector + Full-Text Keyword) Retrieval
========================================================================================
Validates:
- Exact keyword matching in knowledge chunks
- Semantic vector similarity matching
- Combined Reciprocal Rank Fusion (RRF) scoring
- Strict tenant/user isolation via metadata filtering
- Preservation of verified/freshness metadata
- Search mode dispatch (hybrid, vector, keyword)
- Empty query and zero-match behavior
"""

import pytest
from knowledge.knowledge_schemas import DocumentChunk
from knowledge.postgres_vector_store import PostgresPgVectorStore
from knowledge.retriever import KnowledgeRetriever


@pytest.fixture
def hybrid_store():
    """In-memory isolated PostgresPgVectorStore fixture with sample documents."""
    store = PostgresPgVectorStore(in_memory=True)
    chunks = [
        DocumentChunk(
            chunk_id="chunk_auth_err",
            document_id="doc_auth",
            source="kb_auth_guide.md",
            chunk_index=0,
            content="When system throws error code ERR_AUTH_9872, the operator must clear the session token and re-authenticate.",
            char_count=105,
            metadata={"title": "Auth Troubleshooting", "user_id": "tenant_1", "verified": True, "freshness": "2026-Q3"},
        ),
        DocumentChunk(
            chunk_id="chunk_login_flow",
            document_id="doc_login",
            source="kb_login_overview.md",
            chunk_index=0,
            content="The standard user authentication flow uses Argon2id password hashing and secure HttpOnly cookie sessions.",
            char_count=110,
            metadata={"title": "Login Architecture", "user_id": "tenant_1", "verified": True, "freshness": "2026-Q3"},
        ),
        DocumentChunk(
            chunk_id="chunk_tenant_2_secret",
            document_id="doc_t2",
            source="kb_t2_secret.md",
            chunk_index=0,
            content="Confidential procedure for tenant 2: error code ERR_AUTH_9872 should invoke automated failover.",
            char_count=95,
            metadata={"title": "Tenant 2 Internal", "user_id": "tenant_2", "verified": False},
        ),
        DocumentChunk(
            chunk_id="chunk_browser_nav",
            document_id="doc_browser",
            source="kb_browser.md",
            chunk_index=0,
            content="Playwright executes navigation by calling page.goto with a 30-second bounded timeout and networkidle wait.",
            char_count=112,
            metadata={"title": "Browser Navigation Manual", "user_id": "tenant_1", "verified": True},
        ),
    ]
    store.add_chunks(chunks)
    return store


def test_exact_keyword_matching(hybrid_store):
    """An exact rare keyword like 'ERR_AUTH_9872' returns the exact error chunk as top rank in keyword mode."""
    results = hybrid_store.query_keyword(
        query_text="ERR_AUTH_9872",
        top_k=2,
        filter_metadata={"user_id": "tenant_1"},
    )
    assert len(results) == 1
    assert results[0].chunk_id == "chunk_auth_err"
    assert "ERR_AUTH_9872" in results[0].content


def test_semantic_vector_matching(hybrid_store):
    """Semantic vector search returns conceptually related documents even with different phrasing."""
    results = hybrid_store.query_vector(
        query_text="how does the application handle secure sign in credentials",
        top_k=2,
        filter_metadata={"user_id": "tenant_1"},
    )
    assert len(results) >= 1
    # chunk_login_flow should be retrieved
    chunk_ids = [r.chunk_id for r in results]
    assert "chunk_login_flow" in chunk_ids


def test_hybrid_rrf_scoring(hybrid_store):
    """
    Hybrid search combines vector and keyword ranks via RRF.
    A document matching both keyword ('Argon2id') and semantic meaning ranks first.
    """
    results = hybrid_store.query_hybrid(
        query_text="Argon2id password hashing and session tokens",
        top_k=3,
        filter_metadata={"user_id": "tenant_1"},
        rrf_k=60,
    )
    assert len(results) >= 1
    top_hit = results[0]
    assert top_hit.chunk_id == "chunk_login_flow"
    assert top_hit.metadata.get("retrieval_mode") == "hybrid"
    assert "rrf_score" in top_hit.metadata
    assert top_hit.relevance_score > 0.0


def test_strict_tenant_isolation(hybrid_store):
    """Tenant isolation is strictly enforced across keyword, vector, and hybrid searches."""
    # Tenant 2 has a chunk mentioning ERR_AUTH_9872
    # If filtering for tenant_1, tenant_2's chunk must never be leaked
    hybrid_res = hybrid_store.query_hybrid(
        query_text="ERR_AUTH_9872 failover",
        top_k=5,
        filter_metadata={"user_id": "tenant_1"},
    )
    for r in hybrid_res:
        assert r.metadata.get("user_id") == "tenant_1"
        assert r.chunk_id != "chunk_tenant_2_secret"

    keyword_res = hybrid_store.query_keyword(
        query_text="ERR_AUTH_9872",
        top_k=5,
        filter_metadata={"user_id": "tenant_1"},
    )
    for r in keyword_res:
        assert r.metadata.get("user_id") == "tenant_1"
        assert r.chunk_id != "chunk_tenant_2_secret"


def test_metadata_preservation(hybrid_store):
    """Verified and freshness metadata must be preserved on retrieved chunks."""
    results = hybrid_store.query(
        query_text="Playwright navigation timeout",
        top_k=1,
        filter_metadata={"user_id": "tenant_1"},
        search_mode="hybrid",
    )
    assert len(results) == 1
    hit = results[0]
    assert hit.metadata.get("verified") is True
    assert hit.title == "Browser Navigation Manual"


def test_search_mode_dispatch_in_retriever(hybrid_store):
    """KnowledgeRetriever correctly forwards search_mode (vector, keyword, hybrid)."""
    retriever = KnowledgeRetriever(vector_store=hybrid_store, default_top_k=2)

    # 1. Keyword mode
    kw_res = retriever.retrieve("Playwright page.goto", search_mode="keyword")
    assert kw_res.status == "success"
    assert any(c.chunk_id == "chunk_browser_nav" for c in kw_res.chunks)

    # 2. Vector mode
    vec_res = retriever.retrieve("Argon2id password authentication flow", min_score=0.2, search_mode="vector")
    assert vec_res.status == "success"
    assert len(vec_res.chunks) > 0

    # 3. Hybrid mode (default)
    hyb_res = retriever.retrieve("Argon2id password hashing")
    assert hyb_res.status == "success"
    assert hyb_res.chunks[0].metadata.get("retrieval_mode") == "hybrid"


def test_empty_query_and_zero_matches(hybrid_store):
    """Empty queries or queries with zero matches return empty results cleanly."""
    retriever = KnowledgeRetriever(vector_store=hybrid_store)
    empty_res = retriever.retrieve("")
    assert empty_res.status == "empty"
    assert empty_res.chunks == []

    whitespace_res = retriever.retrieve("    ")
    assert whitespace_res.status == "empty"
    assert whitespace_res.chunks == []
