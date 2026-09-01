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
        self.connection_uri = connection_uri or os.getenv("POSTGRES_URI") or os.getenv("DATABASE_URL")
        self.embedding_provider = embedding_provider or HybridEmbeddingService()
        self.in_memory = in_memory
        self.dim = getattr(self.embedding_provider, "active_dimension", getattr(self.embedding_provider, "dimension", 128))

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
            logger.info(f"[SemanticMemoryStore] Connected to PostgreSQL memories table (dim={self.dim}).")
        except Exception as e:
            logger.warning(f"[SemanticMemoryStore] PG connection failed: {e}. Using local store.")
            self._is_pg_connected = False
            self._pg_conn = None

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
    ) -> ExecutionMemoryItem:
        """
        Embeds and stores a new historical execution outcome record.
        """
        item = ExecutionMemoryItem(
            goal_text=goal_text,
            plan_id=plan_id,
            session_id=session_id,
            memory_type=memory_type,
            task_summary=task_summary,
            outcome_status=outcome_status,
            execution_metrics=execution_metrics or {},
            safety_summary=safety_summary or {},
            metadata=metadata or {},
        )

        embed_text = f"{goal_text} | {task_summary}"
        embedding = self.embedding_provider.embed_query(embed_text)

        if self._is_pg_connected and self._pg_conn:
            try:
                from pgvector import Vector  # adapt Python list → pgvector type
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
                            Vector(embedding),  # must be pgvector type, not numeric[]
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
    ) -> List[ExecutionMemoryItem]:
        """
        Retrieves top-K most similar past execution memories for a target goal.
        """
        if not goal_query.strip():
            return []

        query_vec = self.embedding_provider.embed_query(goal_query)

        if self._is_pg_connected and self._pg_conn:
            try:
                from pgvector import Vector  # adapt Python list → pgvector type
                query_vector = Vector(query_vec)

                with self._pg_conn.cursor() as cur:
                    where_clauses: List[str] = ["1=1"]
                    filter_params: List[Any] = []

                    if memory_type:
                        where_clauses.append("memory_type = %s")
                        filter_params.append(memory_type)

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

                        results.append(
                            ExecutionMemoryItem(
                                memory_id=mid,
                                goal_text=goal,
                                plan_id=pid,
                                session_id=sid,
                                memory_type=mtype,
                                task_summary=summary,
                                outcome_status=status,
                                execution_metrics=metrics if isinstance(metrics, dict) else json.loads(metrics or "{}"),
                                safety_summary=safety if isinstance(safety, dict) else json.loads(safety or "{}"),
                                metadata=meta if isinstance(meta, dict) else json.loads(meta or "{}"),
                                relevance_score=round(score_val, 4),
                            )
                        )
                    results.sort(key=lambda r: r.relevance_score, reverse=True)
                    return results[:top_k]
            except Exception as e:
                logger.warning(f"[SemanticMemoryStore] PG retrieve error: {e}. Using local.")

        # Local search
        results = []
        for mid, mem in self._local_memories.items():
            if memory_type and mem.memory_type != memory_type:
                continue

            emb = self._local_embeddings.get(mid, [])
            if not emb:
                continue

            sim = self._cosine_sim(query_vec, emb)
            if sim < min_score:
                continue

            mem_copy = mem.model_copy()
            mem_copy.relevance_score = round(sim, 4)
            results.append(mem_copy)

        results.sort(key=lambda r: r.relevance_score, reverse=True)
        return results[:top_k]

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
