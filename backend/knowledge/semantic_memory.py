"""
semantic_memory.py — Semantic Memory Foundation for Historical Execution
==========================================================================
Phase 7C: Provides structured semantic memory storage for past execution outcomes,
successful plan patterns, failed attempts, and operator feedback.

Key Principles:
    1. Separation of Concerns:
       - Static Knowledge (Playbooks, SOPs) is stored in knowledge_chunks.
       - Historical Execution Memory (past plans, task outcomes) is stored in execution_memories.
       - The system distinguishes between authoritative domain rules and experiential memory.
    2. Strict Safety Invariant:
       - Historical memory of a task having succeeded in the past NEVER bypasses current
         deterministic safety policy rules.
       - Authority: Deterministic Safety Policy Engine > LLM > RAG / Memory.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
import uuid
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from knowledge.embeddings import EmbeddingProvider, HybridEmbeddingService

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Semantic Memory Schemas
# ─────────────────────────────────────────────────────────────────────────────

class ExecutionMemoryItem(BaseModel):
    """
    A single historical execution memory record.
    """
    memory_id:         str = Field(default_factory=lambda: f"mem_{uuid.uuid4().hex[:12]}")
    user_id:           Optional[str] = None
    goal_text:         str
    plan_id:           str = ""
    session_id:        str = ""
    memory_type:       str = "successful_plan"  # "successful_plan", "failed_plan", "procedure", "feedback"
    task_summary:      str = ""
    outcome_status:    str = "success"          # "success", "failed", "blocked"
    execution_metrics: Dict[str, Any] = Field(default_factory=dict)
    safety_summary:    Dict[str, Any] = Field(default_factory=dict)
    metadata:          Dict[str, Any] = Field(default_factory=dict)
    relevance_score:   float = 0.0
    created_at:        float = Field(default_factory=time.time)


MEMORY_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS execution_memories (
    memory_id VARCHAR(64) PRIMARY KEY,
    goal_text TEXT NOT NULL,
    plan_id VARCHAR(64) DEFAULT '',
    session_id VARCHAR(64) DEFAULT '',
    memory_type VARCHAR(32) NOT NULL,
    task_summary TEXT NOT NULL,
    outcome_status VARCHAR(32) NOT NULL,
    execution_metrics JSONB DEFAULT '{{}}',
    safety_summary JSONB DEFAULT '{{}}',
    metadata JSONB DEFAULT '{{}}',
    embedding vector({dim}),
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_exec_memories_embedding_hnsw 
ON execution_memories USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_exec_memories_type ON execution_memories(memory_type);
CREATE INDEX IF NOT EXISTS idx_exec_memories_status ON execution_memories(outcome_status);
"""


# ─────────────────────────────────────────────────────────────────────────────
# SemanticMemoryStore
# ─────────────────────────────────────────────────────────────────────────────

class SemanticMemoryStore:
    """
    Manages historical execution memories using PostgreSQL + pgvector (with local fallback).
    """

    def __init__(
        self,
        connection_uri: Optional[str] = None,
        embedding_provider: Optional[EmbeddingProvider] = None,
        in_memory: bool = False,
    ):
        if connection_uri:
            self.connection_uri = connection_uri
        else:
            try:
                from core.config import settings
                self.connection_uri = getattr(settings, "POSTGRES_URI", None) or os.getenv("POSTGRES_URI") or os.getenv("DATABASE_URL")
            except Exception:
                self.connection_uri = os.getenv("POSTGRES_URI") or os.getenv("DATABASE_URL")
        self.embedding_provider = embedding_provider or HybridEmbeddingService()
        self.in_memory = in_memory
        self.dim = getattr(self.embedding_provider, "active_dimension", getattr(self.embedding_provider, "dimension", 128))
        self.db_vector_dim: Optional[int] = None
        self._has_tsv_column: bool = False

        self._local_memories: Dict[str, ExecutionMemoryItem] = {}
        self._local_embeddings: Dict[str, List[float]] = {}
        self._is_pg_connected = False
        self._pg_conn = None

        if not in_memory and self.connection_uri:
            self._init_postgres()
        else:
            logger.info("[SemanticMemoryStore] Initialized in local/isolated mode.")

    def _init_postgres(self) -> None:
        try:
            import psycopg2
            from pgvector.psycopg2 import register_vector

            self._pg_conn = psycopg2.connect(self.connection_uri, connect_timeout=3)
            self._pg_conn.autocommit = True
            register_vector(self._pg_conn)

            with self._pg_conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                cur.execute(MEMORY_SCHEMA_DDL.format(dim=self.dim))

            self._is_pg_connected = True
            self._inspect_db_schema()
            logger.info(f"[SemanticMemoryStore] Connected to PostgreSQL memories table (provider_dim={self.dim}, db_dim={self.db_vector_dim}).")
        except Exception as e:
            logger.warning(f"[SemanticMemoryStore] PG connection failed: {e}. Using local store.")
            self._is_pg_connected = False
            self._pg_conn = None

    def _inspect_db_schema(self) -> None:
        """Inspects live PostgreSQL schema to determine actual dimension of embedding and tsvector column presence."""
        if not self._pg_conn or not self._is_pg_connected:
            return
        try:
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT format_type(atttypid, atttypmod)
                    FROM pg_attribute
                    WHERE attrelid = 'execution_memories'::regclass
                      AND attname = 'embedding'
                      AND NOT attisdropped;
                    """
                )
                row = cur.fetchone()
                if row and row[0]:
                    import re
                    match = re.search(r"vector\((\d+)\)", str(row[0]))
                    if match:
                        self.db_vector_dim = int(match.group(1))

                cur.execute(
                    """
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'execution_memories' AND column_name = 'tsv_content';
                    """
                )
                self._has_tsv_column = bool(cur.fetchone())
        except Exception as e:
            logger.debug(f"[SemanticMemoryStore] Could not introspect vector column dimension: {e}")
        if self.db_vector_dim is None:
            self.db_vector_dim = self.dim

    @property
    def active_backend(self) -> str:
        """Truthful indicator of whether Postgres/pgvector or local memory is serving memories."""
        return "postgres_pgvector" if self._is_pg_connected else "local_fallback"

    def record_memory(
        self,
        goal_text: str,
        plan_id: str = "",
        session_id: str = "",
        memory_type: str = "successful_plan",
        task_summary: str = "",
        outcome_status: str = "success",
        execution_metrics: Optional[Dict[str, Any]] = None,
        safety_summary: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
    ) -> ExecutionMemoryItem:
        """
        Embeds and stores a new historical execution outcome record with tenant isolation.
        """
        meta = dict(metadata or {})
        if user_id:
            meta.setdefault("user_id", user_id)

        item = ExecutionMemoryItem(
            user_id=user_id,
            goal_text=goal_text,
            plan_id=plan_id,
            session_id=session_id,
            memory_type=memory_type,
            task_summary=task_summary,
            outcome_status=outcome_status,
            execution_metrics=execution_metrics or {},
            safety_summary=safety_summary or {},
            metadata=meta,
        )

        embed_text = f"{goal_text} | {task_summary}"
        if hasattr(self.embedding_provider, "embed_query_with_provenance"):
            res = self.embedding_provider.embed_query_with_provenance(embed_text)
            embedding = res.vector
            vec_dim = res.dimension
            provider_name = res.provider
            model_name = res.model
            embedding_space = res.embedding_space
        else:
            embedding = self.embedding_provider.embed_query(embed_text)
            vec_dim = len(embedding) if embedding else 0
            provider_name = getattr(self.embedding_provider, "active_provider", "unknown")
            model_name = getattr(self.embedding_provider, "active_model", getattr(self.embedding_provider, "model_name", "unknown"))
            embedding_space = getattr(
                self.embedding_provider,
                "embedding_space",
                f"{provider_name}:{model_name}:{vec_dim}",
            )

        item.metadata.setdefault("embedding_provider", provider_name)
        item.metadata.setdefault("embedding_model", model_name)
        item.metadata.setdefault("embedding_dim", vec_dim)
        item.metadata.setdefault("embedding_space", embedding_space)

        if self._is_pg_connected and self._pg_conn:
            try:
                from pgvector import Vector  # adapt Python list → pgvector type

                # Compatibility verification:
                # If generated embedding dimension does not match live DB column dimension,
                # do NOT pass invalid vector (which triggers PostgreSQL dimension mismatch error).
                # Instead, store with embedding=NULL; the row is safely persisted and indexed via tsv_content!
                pg_vector_val = None
                if self.db_vector_dim is None or vec_dim == self.db_vector_dim:
                    pg_vector_val = Vector(embedding)
                else:
                    item.metadata["embedding_stored"] = False
                    item.metadata["embedding_dim_mismatch"] = True
                    logger.warning(
                        f"[SemanticMemoryStore] Dimension mismatch: generated {vec_dim}-dim vector, "
                        f"but DB schema expects {self.db_vector_dim}-dim. Storing memory with embedding=NULL for hybrid text search."
                    )

                with self._pg_conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO execution_memories (
                            memory_id, goal_text, plan_id, session_id, memory_type,
                            task_summary, outcome_status, execution_metrics, safety_summary,
                            metadata, embedding
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (memory_id) DO NOTHING
                        """,
                        (
                            item.memory_id,
                            item.goal_text,
                            item.plan_id,
                            item.session_id,
                            item.memory_type,
                            item.task_summary,
                            item.outcome_status,
                            json.dumps(item.execution_metrics),
                            json.dumps(item.safety_summary),
                            json.dumps(item.metadata),
                            pg_vector_val,
                        ),
                    )
                logger.info(f"[SemanticMemoryStore] Recorded memory '{item.memory_id}' in PostgreSQL.")
                return item
            except Exception as e:
                logger.error(f"[SemanticMemoryStore] PG record failed: {e}. Falling back to local.")

        self._local_memories[item.memory_id] = item
        self._local_embeddings[item.memory_id] = embedding
        logger.info(f"[SemanticMemoryStore] Recorded memory '{item.memory_id}' in local store.")
        return item

    def retrieve_memories(
        self,
        goal_query: str,
        top_k: int = 3,
        min_score: float = 0.35,
        memory_type: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[ExecutionMemoryItem]:
        """
        Retrieves top-K most similar past execution memories for a target goal with tenant isolation.
        """
        if not goal_query.strip():
            return []

        clean_uid = str(user_id).strip() if user_id is not None else None
        if user_id is not None and not clean_uid:
            logger.warning("[SemanticMemoryStore] Rejecting memory query with empty or whitespace user_id.")
            return []

        if hasattr(self.embedding_provider, "embed_query_with_provenance"):
            res = self.embedding_provider.embed_query_with_provenance(goal_query)
            query_vec = res.vector
            vec_dim = res.dimension
            provider_name = res.provider
            model_name = res.model
            query_space = res.embedding_space
        else:
            query_vec = self.embedding_provider.embed_query(goal_query)
            vec_dim = len(query_vec) if query_vec else 0
            provider_name = getattr(self.embedding_provider, "active_provider", "unknown")
            model_name = getattr(self.embedding_provider, "active_model", getattr(self.embedding_provider, "model_name", "unknown"))
            query_space = getattr(
                self.embedding_provider,
                "embedding_space",
                f"{provider_name}:{model_name}:{vec_dim}",
            )

        if self._is_pg_connected and self._pg_conn:
            # Dimension compatibility check:
            # If query vector dimension does not match DB column dimension,
            # do NOT execute vector cosine query (which causes a PostgreSQL error).
            # Instead, route seamlessly to tsvector full-text search over goal_text & task_summary!
            if self.db_vector_dim is not None and vec_dim != self.db_vector_dim:
                logger.warning(
                    f"[SemanticMemoryStore] Query vector dimension mismatch: query={vec_dim}, DB={self.db_vector_dim}. "
                    f"Executing tsvector full-text search fallback over historical memories."
                )
                ts_kwargs: Dict[str, Any] = {
                    "goal_query": goal_query,
                    "top_k": top_k,
                    "min_score": min_score,
                    "memory_type": memory_type,
                }
                if clean_uid is not None:
                    ts_kwargs["user_id"] = clean_uid
                ts_results = self._retrieve_memories_tsvector(**ts_kwargs)
                if ts_results:
                    return ts_results

            try:
                from pgvector import Vector  # adapt Python list → pgvector type
                query_vector = Vector(query_vec)

                with self._pg_conn.cursor() as cur:
                    where_clauses: List[str] = [
                        "embedding IS NOT NULL",
                        "metadata->>'embedding_space' = %s",
                    ]
                    filter_params: List[Any] = [query_space]

                    if memory_type:
                        where_clauses.append("memory_type = %s")
                        filter_params.append(memory_type)

                    if clean_uid:
                        where_clauses.append("metadata->>'user_id' = %s")
                        filter_params.append(clean_uid)
                    else:
                        where_clauses.append("metadata->>'user_id' IS NULL")

                    where_sql = " AND ".join(where_clauses)
                    sql = f"""
                        SELECT
                            memory_id, goal_text, plan_id, session_id, memory_type,
                            task_summary, outcome_status, execution_metrics, safety_summary,
                            metadata, 1 - (embedding <=> %s) AS score,
                            created_at
                        FROM execution_memories
                        WHERE {where_sql}
                        ORDER BY embedding <=> %s ASC
                        LIMIT %s
                    """
                    # Parameter order exactly matches SQL placeholders:
                    # 1. %s for score      (1 - embedding <=> %s)  — in SELECT
                    # 2. filter_params     (WHERE clauses)          — in WHERE
                    # 3. %s for ORDER BY   (embedding <=> %s)       — in ORDER BY
                    # 4. %s for LIMIT
                    full_params = [query_vector] + filter_params + [query_vector, top_k]
                    cur.execute(sql, full_params)
                    rows = cur.fetchall()

                    results: List[ExecutionMemoryItem] = []
                    for row in rows:
                        mid, goal, pid, sid, mtype, summary, status, metrics, safety, meta, score, cat = row
                        score_val = max(0.0, min(1.0, float(score)))
                        if score_val < min_score:
                            continue

                        meta_dict = meta if isinstance(meta, dict) else json.loads(meta or "{}")
                        results.append(
                            ExecutionMemoryItem(
                                memory_id=mid,
                                user_id=meta_dict.get("user_id"),
                                goal_text=goal,
                                plan_id=pid,
                                session_id=sid,
                                memory_type=mtype,
                                task_summary=summary,
                                outcome_status=status,
                                execution_metrics=metrics if isinstance(metrics, dict) else json.loads(metrics or "{}"),
                                safety_summary=safety if isinstance(safety, dict) else json.loads(safety or "{}"),
                                metadata=meta_dict,
                                relevance_score=round(score_val, 4),
                            )
                        )
                    results.sort(key=lambda r: r.relevance_score, reverse=True)
                    if results:
                        return results[:top_k]

                    # If vector search returned no results, check if legacy memories exist
                    # that can be retrieved via full-text search:
                    ts_kwargs_fallback: Dict[str, Any] = {
                        "goal_query": goal_query,
                        "top_k": top_k,
                        "min_score": min_score,
                        "memory_type": memory_type,
                    }
                    if clean_uid is not None:
                        ts_kwargs_fallback["user_id"] = clean_uid
                    ts_results = self._retrieve_memories_tsvector(**ts_kwargs_fallback)
                    if ts_results:
                        return ts_results
                    return []
            except Exception as e:
                logger.warning(f"[SemanticMemoryStore] PG retrieve error: {e}. Using local.")

        # Local search
        results = []
        for mid, mem in self._local_memories.items():
            if memory_type and mem.memory_type != memory_type:
                continue

            if clean_uid:
                mem_user = getattr(mem, "user_id", None) or (mem.metadata.get("user_id") if mem.metadata else None)
                clean_mem_user = str(mem_user).strip() if mem_user is not None else ""
                if not clean_mem_user or clean_mem_user != clean_uid:
                    continue
            else:
                mem_user = getattr(mem, "user_id", None) or (mem.metadata.get("user_id") if mem.metadata else None)
                if mem_user:
                    continue

            # Strict embedding-space compatibility:
            # Exclude records without embedding_space or with incompatible embedding_space.
            mem_space = mem.metadata.get("embedding_space")
            if not mem_space or mem_space != query_space:
                continue

            emb = self._local_embeddings.get(mid, [])
            if not emb or len(emb) != vec_dim:
                continue

            sim = self._cosine_sim(query_vec, emb)
            if sim < min_score:
                continue

            mem_copy = mem.model_copy()
            mem_copy.relevance_score = round(sim, 4)
            results.append(mem_copy)

        results.sort(key=lambda r: r.relevance_score, reverse=True)
        return results[:top_k]

    def _retrieve_memories_tsvector(
        self,
        goal_query: str,
        top_k: int = 3,
        min_score: float = 0.0,
        memory_type: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[ExecutionMemoryItem]:
        """
        Retrieves memories from PostgreSQL using tsvector full-text search when
        pgvector is dimension-incompatible with the active query embedding.
        """
        if not self._is_pg_connected or not self._pg_conn or not goal_query.strip():
            return []
        clean_ts_uid = str(user_id).strip() if user_id is not None else None
        if user_id is not None and not clean_ts_uid:
            return []
        try:
            tsv_expr = "tsv_content" if getattr(self, "_has_tsv_column", False) else "to_tsvector('english', goal_text || ' ' || COALESCE(task_summary, ''))"
            with self._pg_conn.cursor() as cur:
                where_clauses = [
                    f"{tsv_expr} @@ plainto_tsquery('english', %s)",
                    "(embedding IS NULL OR metadata->>'embedding_space' IS NULL)",
                ]
                filter_params: List[Any] = [goal_query]

                if memory_type:
                    where_clauses.append("memory_type = %s")
                    filter_params.append(memory_type)

                if clean_ts_uid:
                    where_clauses.append("metadata->>'user_id' = %s")
                    filter_params.append(clean_ts_uid)
                else:
                    where_clauses.append("metadata->>'user_id' IS NULL")

                where_sql = " AND ".join(where_clauses)
                sql = f"""
                    SELECT
                        memory_id, goal_text, plan_id, session_id, memory_type,
                        task_summary, outcome_status, execution_metrics, safety_summary,
                        metadata, ts_rank_cd({tsv_expr}, plainto_tsquery('english', %s)) AS text_rank,
                        created_at
                    FROM execution_memories
                    WHERE {where_sql}
                    ORDER BY text_rank DESC
                    LIMIT %s
                """
                full_params = [goal_query] + filter_params + [top_k]
                cur.execute(sql, full_params)
                rows = cur.fetchall()

                results: List[ExecutionMemoryItem] = []
                for row in rows:
                    mid, goal, pid, sid, mtype, summary, status, metrics, safety, meta, rank, cat = row
                    score_val = max(0.1, min(1.0, float(rank) * 2.0 if rank > 0 else 0.1))
                    if min_score > 0 and score_val < min_score:
                        continue
                    meta_dict = meta if isinstance(meta, dict) else json.loads(meta or "{}")
                    results.append(
                        ExecutionMemoryItem(
                            memory_id=mid,
                            user_id=meta_dict.get("user_id"),
                            goal_text=goal,
                            plan_id=pid,
                            session_id=sid,
                            memory_type=mtype,
                            task_summary=summary,
                            outcome_status=status,
                            execution_metrics=metrics if isinstance(metrics, dict) else json.loads(metrics or "{}"),
                            safety_summary=safety if isinstance(safety, dict) else json.loads(safety or "{}"),
                            metadata=meta_dict,
                            relevance_score=round(score_val, 4),
                        )
                    )
                return results
        except Exception as e:
            logger.warning(f"[SemanticMemoryStore] tsvector retrieval error: {e}. Falling back to local.")
            return []

    def count(self) -> int:
        if self._is_pg_connected and self._pg_conn:
            try:
                with self._pg_conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM execution_memories")
                    return cur.fetchone()[0]
            except Exception:
                pass
        return len(self._local_memories)

    def clear(self) -> None:
        if self._is_pg_connected and self._pg_conn:
            try:
                with self._pg_conn.cursor() as cur:
                    cur.execute("TRUNCATE execution_memories CASCADE")
            except Exception as e:
                logger.warning(f"[SemanticMemoryStore] Clear failed: {e}")
        self._local_memories.clear()
        self._local_embeddings.clear()

    @staticmethod
    def _cosine_sim(vec_a: List[float], vec_b: List[float]) -> float:
        if not vec_a or not vec_b or len(vec_a) != len(vec_b):
            return 0.0
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return max(0.0, min(1.0, dot / (norm_a * norm_b)))
