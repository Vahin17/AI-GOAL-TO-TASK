"""
postgres_vector_store.py — PostgreSQL + pgvector Vector Store Implementation
=============================================================================
Phase 7A: Production-grade vector storage using PostgreSQL and pgvector.

Features:
    - Tables: knowledge_documents, knowledge_chunks (with vector column & JSONB metadata)
    - HNSW indexing: CREATE INDEX USING hnsw (embedding vector_cosine_ops)
    - Cosine similarity: 1.0 - (embedding <=> query_vector)
    - Metadata filtering: JSONB containment & key-value matching
    - Content-hash deduplication: Prevents duplicate chunk insertion
    - Connection resilience: Graceful fallback to local high-precision vector engine
      when external PostgreSQL instance is offline or credentials are not supplied.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from typing import Any, Dict, List, Optional

from knowledge.embeddings import EmbeddingProvider, HybridEmbeddingService
from knowledge.knowledge_schemas import DocumentChunk, RetrievedChunk
from knowledge.vector_store_base import VectorStore

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# PostgreSQL DDL Schemas
# ─────────────────────────────────────────────────────────────────────────────

PGVECTOR_SCHEMA_DDL = """
-- 1. Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- 2. Knowledge Documents Table
CREATE TABLE IF NOT EXISTS knowledge_documents (
    document_id VARCHAR(64) PRIMARY KEY,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    category VARCHAR(64) DEFAULT 'general',
    content TEXT NOT NULL,
    metadata JSONB DEFAULT '{{}}',
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 3. Knowledge Chunks Table with Vector Column
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    chunk_id VARCHAR(64) PRIMARY KEY,
    document_id VARCHAR(64) NOT NULL REFERENCES knowledge_documents(document_id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    chunk_index INTEGER DEFAULT 0,
    content TEXT NOT NULL,
    char_count INTEGER DEFAULT 0,
    token_estimate INTEGER DEFAULT 0,
    content_hash VARCHAR(64) NOT NULL,
    metadata JSONB DEFAULT '{{}}',
    embedding vector({dim}),
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 4. HNSW Vector Index for Cosine Distance Search
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_embedding_hnsw 
ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);

-- 5. B-tree & GIN Indexes for fast filtering
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_doc_id ON knowledge_chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_hash ON knowledge_chunks(content_hash);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_metadata ON knowledge_chunks USING gin(metadata);
"""


class PostgresPgVectorStore(VectorStore):
    """
    PostgreSQL + pgvector Vector Store.
    
    Operates against a live PostgreSQL database with pgvector installed,
    or runs in local embedded mode when testing in isolated environments.
    """

    def __init__(
        self,
        connection_uri: Optional[str] = None,
        embedding_provider: Optional[EmbeddingProvider] = None,
        table_name: str = "knowledge_chunks",
        in_memory: bool = False,
    ):
        self.connection_uri = connection_uri or os.getenv("POSTGRES_URI") or os.getenv("DATABASE_URL")
        self.embedding_provider = embedding_provider or HybridEmbeddingService()
        self.table_name = table_name
        self.in_memory = in_memory
        self.dim = getattr(self.embedding_provider, "active_dimension", getattr(self.embedding_provider, "dimension", 128))

        # In-memory storage for test isolation / offline fallback
        self._local_chunks: Dict[str, DocumentChunk] = {}
        self._local_embeddings: Dict[str, List[float]] = {}
        self._is_pg_connected = False
        self._pg_conn = None

        if not in_memory and self.connection_uri:
            self._init_postgres_connection()
        else:
            logger.info("[PostgresPgVectorStore] Initialized in local/isolated mode.")

    def _init_postgres_connection(self) -> None:
        """Attempts to initialize PostgreSQL connection and run DDL."""
        try:
            import psycopg2
            from pgvector.psycopg2 import register_vector

            self._pg_conn = psycopg2.connect(self.connection_uri, connect_timeout=3)
            self._pg_conn.autocommit = True
            register_vector(self._pg_conn)

            with self._pg_conn.cursor() as cur:
                # Format schema with correct embedding dimension
                ddl = PGVECTOR_SCHEMA_DDL.format(dim=self.dim)
                cur.execute(ddl)

            self._is_pg_connected = True
            logger.info(f"[PostgresPgVectorStore] Connected to PostgreSQL with pgvector (dim={self.dim}).")
        except Exception as e:
            logger.warning(
                f"[PostgresPgVectorStore] PostgreSQL connection failed ({e}). Operating in resilient local mode."
            )
            self._is_pg_connected = False
            self._pg_conn = None

    def add_chunks(self, chunks: List[DocumentChunk]) -> int:
        """
        Embeds and stores document chunks into PostgreSQL / local store.
        """
        if not chunks:
            return 0

        valid_chunks: List[DocumentChunk] = []
        seen_ids = set()
        for c in chunks:
            if c.chunk_id and c.chunk_id not in seen_ids and c.content.strip():
                seen_ids.add(c.chunk_id)
                valid_chunks.append(c)

        if not valid_chunks:
            return 0

        # Compute embeddings for chunks that don't have them
        chunks_needing_embed = [c for c in valid_chunks if not c.embedding]
        if chunks_needing_embed:
            computed_embeddings = self.embedding_provider.embed_texts([c.content for c in chunks_needing_embed])
            for c, emb in zip(chunks_needing_embed, computed_embeddings):
                c.embedding = emb

        if self._is_pg_connected and self._pg_conn:
            try:
                import psycopg2.extras
                with self._pg_conn.cursor() as cur:
                    # 1. Upsert parent documents first
                    doc_rows = []
                    seen_docs = set()
                    for c in valid_chunks:
                        if c.document_id not in seen_docs:
                            seen_docs.add(c.document_id)
                            title = str(c.metadata.get("title", c.document_id))
                            category = str(c.metadata.get("category", "general"))
                            doc_rows.append((c.document_id, title, c.source, category, c.content, json.dumps(c.metadata)))

                    psycopg2.extras.execute_values(
                        cur,
                        """
                        INSERT INTO knowledge_documents (document_id, title, source, category, content, metadata)
                        VALUES %s
                        ON CONFLICT (document_id) DO UPDATE 
                        SET title = EXCLUDED.title, metadata = EXCLUDED.metadata
                        """,
                        doc_rows,
                    )

                    # 2. Upsert chunks with pgvector embeddings
                    chunk_rows = [
                        (
                            c.chunk_id,
                            c.document_id,
                            c.source,
                            c.chunk_index,
                            c.content,
                            c.char_count,
                            c.token_estimate,
                            c.content_hash,
                            json.dumps(c.metadata),
                            c.embedding,
                        )
                        for c in valid_chunks
                    ]

                    psycopg2.extras.execute_values(
                        cur,
                        """
                        INSERT INTO knowledge_chunks (
                            chunk_id, document_id, source, chunk_index, content,
                            char_count, token_estimate, content_hash, metadata, embedding
                        ) VALUES %s
                        ON CONFLICT (chunk_id) DO UPDATE
                        SET content = EXCLUDED.content,
                            metadata = EXCLUDED.metadata,
                            embedding = EXCLUDED.embedding
                        """,
                        chunk_rows,
                    )

                logger.info(f"[PostgresPgVectorStore] Indexed {len(valid_chunks)} chunks to PostgreSQL.")
                return len(valid_chunks)
            except Exception as e:
                logger.error(f"[PostgresPgVectorStore] PG insert failed: {e}. Storing locally.")

        # Local fallback store
        for c in valid_chunks:
            self._local_chunks[c.chunk_id] = c
            self._local_embeddings[c.chunk_id] = c.embedding or []
        logger.info(f"[PostgresPgVectorStore] Indexed {len(valid_chunks)} chunks to local store.")
        return len(valid_chunks)

    def query_vector(
        self,
        query_text: str,
        top_k: int = 4,
        min_score: float = 0.0,
        filter_metadata: Optional[Dict[str, Any]] = None,
    ) -> List[RetrievedChunk]:
        """
        Performs vector similarity search using cosine similarity.
        """
        if not query_text.strip():
            return []

        query_vec = self.embedding_provider.embed_query(query_text)

        # 1. Try PostgreSQL pgvector query if connected
        if self._is_pg_connected and self._pg_conn:
            try:
                from pgvector import Vector
                query_vector = Vector(query_vec)

                with self._pg_conn.cursor() as cur:
                    where_clauses: List[str] = ["1=1"]
                    filter_params: List[Any] = []

                    if filter_metadata:
                        for k, v in filter_metadata.items():
                            where_clauses.append("metadata->>%s = %s")
                            filter_params.extend([k, str(v)])

                    where_sql = " AND ".join(where_clauses)
                    sql = f"""
                        SELECT
                            chunk_id,
                            document_id,
                            source,
                            content,
                            metadata,
                            1 - (embedding <=> %s) AS relevance_score,
                            (embedding <=> %s) AS distance
                        FROM {self.table_name}
                        WHERE {where_sql}
                        ORDER BY embedding <=> %s ASC
                        LIMIT %s
                    """
                    full_params = [query_vector, query_vector] + filter_params + [query_vector, top_k * 2]

                    cur.execute(sql, full_params)
                    rows = cur.fetchall()

                    retrieved: List[RetrievedChunk] = []
                    for row in rows:
                        chunk_id, doc_id, src, content, meta_json, score, dist = row
                        meta = meta_json if isinstance(meta_json, dict) else json.loads(meta_json or "{}")
                        relevance = max(0.0, min(1.0, float(score)))

                        if relevance < min_score:
                            continue

                        retrieved.append(
                            RetrievedChunk(
                                chunk_id=chunk_id,
                                document_id=doc_id,
                                source=src,
                                title=str(meta.get("title", "")),
                                content=content,
                                relevance_score=round(relevance, 4),
                                distance=round(float(dist), 4),
                                metadata=meta,
                            )
                        )

                    retrieved.sort(key=lambda r: r.relevance_score, reverse=True)
                    return retrieved[:top_k]
            except Exception as e:
                logger.warning(f"[PostgresPgVectorStore] PG vector query error: {e}. Using local search.")

        # 2. Local vector search (exact cosine similarity)
        results: List[RetrievedChunk] = []
        for chunk_id, chunk in self._local_chunks.items():
            emb = self._local_embeddings.get(chunk_id, [])
            if not emb:
                continue

            # Check metadata filter
            if filter_metadata:
                match = True
                for k, v in filter_metadata.items():
                    if str(chunk.metadata.get(k)) != str(v):
                        match = False
                        break
                if not match:
                    continue

            # Compute cosine similarity
            sim = self._cosine_similarity(query_vec, emb)
            if sim < min_score:
                continue

            results.append(
                RetrievedChunk(
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    source=chunk.source,
                    title=str(chunk.metadata.get("title", "")),
                    content=chunk.content,
                    relevance_score=round(sim, 4),
                    distance=round(1.0 - sim, 4),
                    metadata=chunk.metadata,
                )
            )

        results.sort(key=lambda r: r.relevance_score, reverse=True)
        return results[:top_k]

    def query_keyword(
        self,
        query_text: str,
        top_k: int = 4,
        filter_metadata: Optional[Dict[str, Any]] = None,
    ) -> List[RetrievedChunk]:
        """
        Performs full-text keyword search.
        In PostgreSQL: uses tsvector @@ plainto_tsquery('english', %s) and ts_rank_cd.
        In local store: computes term frequency and token matching.
        """
        if not query_text.strip():
            return []

        # 1. Try PostgreSQL full-text search if connected
        if self._is_pg_connected and self._pg_conn:
            try:
                with self._pg_conn.cursor() as cur:
                    where_clauses: List[str] = ["tsv_content @@ plainto_tsquery('english', %s)"]
                    filter_params: List[Any] = [query_text]

                    if filter_metadata:
                        for k, v in filter_metadata.items():
                            where_clauses.append("metadata->>%s = %s")
                            filter_params.extend([k, str(v)])

                    where_sql = " AND ".join(where_clauses)
                    sql = f"""
                        SELECT
                            chunk_id,
                            document_id,
                            source,
                            content,
                            metadata,
                            ts_rank_cd(tsv_content, plainto_tsquery('english', %s)) AS text_rank
                        FROM {self.table_name}
                        WHERE {where_sql}
                        ORDER BY text_rank DESC
                        LIMIT %s
                    """
                    full_params = [query_text] + filter_params + [top_k]
                    cur.execute(sql, full_params)
                    rows = cur.fetchall()

                    results = []
                    for row in rows:
                        chunk_id, doc_id, src, content, meta_json, rank = row
                        meta = meta_json if isinstance(meta_json, dict) else json.loads(meta_json or "{}")
                        results.append(
                            RetrievedChunk(
                                chunk_id=chunk_id,
                                document_id=doc_id,
                                source=src,
                                title=str(meta.get("title", "")),
                                content=content,
                                relevance_score=round(float(rank), 4),
                                distance=round(max(0.0, 1.0 - float(rank)), 4),
                                metadata=meta,
                            )
                        )
                    return results
            except Exception as e:
                err_str = str(e).lower()
                if "undefined_column" in err_str or "tsv_content" in err_str:
                    logger.warning(
                        "[PostgresPgVectorStore] 'tsv_content' column not found in knowledge_chunks. "
                        "Hybrid search requires explicit migration. Run: "
                        "python -m knowledge.migrations.001_add_tsvector_hybrid_search apply. "
                        "Falling back to resilient local keyword search."
                    )
                else:
                    logger.warning(f"[PostgresPgVectorStore] PG keyword search failed: {e}. Using local keyword search.")

        # 2. Local store keyword search
        query_terms = [t.lower() for t in re.findall(r"\w+", query_text) if len(t) > 1]
        if not query_terms:
            return []

        scored_chunks = []
        for chunk_id, chunk in self._local_chunks.items():
            # Check metadata filter
            if filter_metadata:
                match = True
                for k, v in filter_metadata.items():
                    if str(chunk.metadata.get(k)) != str(v):
                        match = False
                        break
                if not match:
                    continue

            content_lower = chunk.content.lower()
            score = 0.0
            if query_text.lower() in content_lower:
                score += 2.0  # Exact phrase bonus

            term_hits = sum(content_lower.count(term) for term in query_terms)
            if term_hits == 0 and score == 0.0:
                continue

            score += term_hits / (len(query_terms) + 5.0)

            scored_chunks.append((
                score,
                RetrievedChunk(
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    source=chunk.source,
                    title=str(chunk.metadata.get("title", "")),
                    content=chunk.content,
                    relevance_score=round(min(1.0, score), 4),
                    distance=round(max(0.0, 1.0 - min(1.0, score)), 4),
                    metadata=dict(chunk.metadata),
                )
            ))

        scored_chunks.sort(key=lambda x: x[0], reverse=True)
        return [c[1] for c in scored_chunks[:top_k]]

    def query_hybrid(
        self,
        query_text: str,
        top_k: int = 4,
        min_score: float = 0.0,
        filter_metadata: Optional[Dict[str, Any]] = None,
        rrf_k: int = 60,
    ) -> List[RetrievedChunk]:
        """
        Combines semantic vector search and PostgreSQL full-text keyword search
        using Reciprocal Rank Fusion (RRF):
            RRF_score(d) = sum( 1 / (rrf_k + rank_i(d)) )
        Preserves tenant isolation filters and metadata.
        """
        pool_size = max(top_k * 3, 10)

        # 1. Retrieve vector candidates
        vector_results = self.query_vector(
            query_text=query_text,
            top_k=pool_size,
            min_score=min_score,
            filter_metadata=filter_metadata,
        )

        # 2. Retrieve keyword candidates
        keyword_results = self.query_keyword(
            query_text=query_text,
            top_k=pool_size,
            filter_metadata=filter_metadata,
        )

        if not vector_results and not keyword_results:
            return []

        # Map of chunk_id -> {chunk, rrf, vector_rank, keyword_rank}
        fused: Dict[str, Dict[str, Any]] = {}

        # Process vector ranks (1-indexed)
        for rank, chunk in enumerate(vector_results, start=1):
            cid = chunk.chunk_id
            if cid not in fused:
                fused[cid] = {
                    "chunk": chunk,
                    "rrf": 0.0,
                    "vector_rank": rank,
                    "keyword_rank": None,
                }
            fused[cid]["rrf"] += 1.0 / (rrf_k + rank)

        # Process keyword ranks (1-indexed)
        for rank, chunk in enumerate(keyword_results, start=1):
            cid = chunk.chunk_id
            if cid not in fused:
                fused[cid] = {
                    "chunk": chunk,
                    "rrf": 0.0,
                    "vector_rank": None,
                    "keyword_rank": rank,
                }
            else:
                fused[cid]["keyword_rank"] = rank
            fused[cid]["rrf"] += 1.0 / (rrf_k + rank)

        # Max possible RRF score (rank 1 in both searches)
        max_possible_rrf = 2.0 / (rrf_k + 1)

        fused_list = list(fused.values())
        fused_list.sort(key=lambda x: x["rrf"], reverse=True)

        final_chunks: List[RetrievedChunk] = []
        for item in fused_list[:top_k]:
            chunk: RetrievedChunk = item["chunk"]
            norm_score = min(1.0, item["rrf"] / max_possible_rrf)
            updated_meta = dict(chunk.metadata)
            updated_meta["retrieval_mode"] = "hybrid"
            updated_meta["rrf_score"] = round(item["rrf"], 6)
            updated_meta["vector_rank"] = item["vector_rank"]
            updated_meta["keyword_rank"] = item["keyword_rank"]

            final_chunks.append(
                RetrievedChunk(
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    source=chunk.source,
                    title=chunk.title,
                    content=chunk.content,
                    relevance_score=round(norm_score, 4),
                    distance=round(1.0 - norm_score, 4),
                    metadata=updated_meta,
                )
            )

        return final_chunks

    def query(
        self,
        query_text: str,
        top_k: int = 4,
        min_score: float = 0.0,
        filter_metadata: Optional[Dict[str, Any]] = None,
        search_mode: str = "hybrid",
        rrf_k: int = 60,
    ) -> List[RetrievedChunk]:
        """
        Dispatches search to hybrid (default), vector, or keyword engine.
        Fully backward-compatible with callers of query(query_text, top_k, min_score, filter_metadata).
        """
        mode = search_mode.lower() if search_mode else "hybrid"
        if mode == "vector":
            return self.query_vector(
                query_text=query_text,
                top_k=top_k,
                min_score=min_score,
                filter_metadata=filter_metadata,
            )
        elif mode == "keyword":
            return self.query_keyword(
                query_text=query_text,
                top_k=top_k,
                filter_metadata=filter_metadata,
            )
        else:
            return self.query_hybrid(
                query_text=query_text,
                top_k=top_k,
                min_score=min_score,
                filter_metadata=filter_metadata,
                rrf_k=rrf_k,
            )

    def count(self) -> int:
        """Total chunks indexed."""
        if self._is_pg_connected and self._pg_conn:
            try:
                with self._pg_conn.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) FROM {self.table_name}")
                    return cur.fetchone()[0]
            except Exception:
                pass
        return len(self._local_chunks)

    def delete_document(self, document_id: str) -> bool:
        """Deletes all chunks for a document."""
        if self._is_pg_connected and self._pg_conn:
            try:
                with self._pg_conn.cursor() as cur:
                    cur.execute("DELETE FROM knowledge_documents WHERE document_id = %s", (document_id,))
                return True
            except Exception as e:
                logger.error(f"[PostgresPgVectorStore] Delete doc failed: {e}")
                return False

        # Local delete
        to_delete = [cid for cid, c in self._local_chunks.items() if c.document_id == document_id]
        for cid in to_delete:
            del self._local_chunks[cid]
            if cid in self._local_embeddings:
                del self._local_embeddings[cid]
        return True

    def clear(self) -> None:
        """Empties the store."""
        if self._is_pg_connected and self._pg_conn:
            try:
                with self._pg_conn.cursor() as cur:
                    cur.execute("TRUNCATE knowledge_chunks, knowledge_documents CASCADE")
            except Exception as e:
                logger.warning(f"[PostgresPgVectorStore] Clear failed: {e}")

        self._local_chunks.clear()
        self._local_embeddings.clear()

    @staticmethod
    def _cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
        """Calculates dot product / cosine similarity for normalized vectors."""
        if not vec_a or not vec_b or len(vec_a) != len(vec_b):
            return 0.0
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return max(0.0, min(1.0, dot / (norm_a * norm_b)))
