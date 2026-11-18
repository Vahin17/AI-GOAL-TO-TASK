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
    A single historical execution memory record with explicit lifecycle, provenance, and trust state.
    """
    memory_id:           str = Field(default_factory=lambda: f"mem_{uuid.uuid4().hex[:12]}")
    user_id:             Optional[str] = None
    goal_text:           str
    plan_id:             str = ""
    session_id:          str = ""
    memory_type:         str = "successful_plan"  # "successful_plan", "failed_plan", "procedure", "feedback", "user_instruction"
    task_summary:        str = ""
    outcome_status:      str = "success"          # "success", "failed", "blocked", "unverified", "incomplete", "quarantined", "invalidated", "expired"
    source_type:         str = "execution"        # "execution", "user_instruction", "user_preference", "operator_feedback"
    trust_status:        str = "approved"         # "candidate", "approved", "rejected", "quarantined", "expired", "invalidated", "superseded"
    verification_status: str = "verified"         # "verified", "unverified", "failed", "inconclusive", "explicit_user"
    evidence_hashes:     List[str] = Field(default_factory=list)
    execution_metrics:   Dict[str, Any] = Field(default_factory=dict)
    safety_summary:      Dict[str, Any] = Field(default_factory=dict)
    metadata:            Dict[str, Any] = Field(default_factory=dict)
    relevance_score:     float = 0.0
    created_at:          float = Field(default_factory=time.time)
    updated_at:          float = Field(default_factory=time.time)
    expires_at:          Optional[float] = None
    superseded_by:       Optional[str] = None
    version:             int = 1


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
        Embeds and stores a new historical execution outcome record with tenant isolation,
        evidence-backed eligibility gating, deduplication, and supersession resolution.
        """
        meta = dict(metadata or {})
        clean_uid = str(user_id).strip() if user_id is not None and str(user_id).strip() else None
        if clean_uid:
            meta["user_id"] = clean_uid

        source_type = str(meta.get("source_type") or ("user_instruction" if memory_type == "user_instruction" else "execution"))
        meta["source_type"] = source_type

        effective_status = str(outcome_status or "success").strip().lower()
        effective_mtype = str(memory_type or "successful_plan").strip()

        # Stage 3 Eligibility Enforcement:
        # 1. Execution memories claiming "success" or "successful_plan" MUST NOT come from
        #    deterministic fallback plans, unverified runs, incomplete runs, or evidence-free runs.
        if source_type == "execution":
            is_fallback = bool(meta.get("is_fallback", False)) or meta.get("planning_mode") == "deterministic_fallback"
            ver_status = str(meta.get("verification_status", "")).strip().lower()
            evidence_free = bool(meta.get("evidence_free", False))
            if effective_status == "success" and (
                is_fallback
                or evidence_free
                or ver_status in ("unverified", "failed", "inconclusive", "rejected")
            ):
                logger.warning(
                    f"[SemanticMemoryStore] Downgrading ineligible 'success' memory for goal '{goal_text[:60]}' "
                    f"(is_fallback={is_fallback}, verification_status='{ver_status}', evidence_free={evidence_free}) to 'unverified'."
                )
                effective_status = "unverified"
                effective_mtype = "failed_plan"
                meta["downgraded_from_success"] = True

        # Derive trust_status and verification_status deterministically
        explicit_trust = meta.get("trust_status")
        if explicit_trust in ("candidate", "approved", "rejected", "quarantined", "expired", "invalidated", "superseded"):
            trust_status = str(explicit_trust)
        elif effective_status == "quarantined" or meta.get("quarantine_reason"):
            trust_status = "quarantined"
        elif effective_status in ("rejected", "invalidated"):
            trust_status = str(effective_status)
        elif effective_status == "expired":
            trust_status = "expired"
        elif source_type in ("user_instruction", "user_preference"):
            trust_status = "approved"
        elif effective_status == "success":
            trust_status = "approved"
        else:
            # Failed, blocked, unverified, incomplete, or paused execution records stay non-authoritative ("candidate")
            trust_status = "candidate"

        if source_type in ("user_instruction", "user_preference"):
            verification_status = str(meta.get("verification_status") or "explicit_user")
        elif effective_status == "success" and trust_status == "approved":
            verification_status = str(meta.get("verification_status") or "verified")
        else:
            verification_status = str(meta.get("verification_status") or ("unverified" if effective_status == "unverified" else effective_status))

        evidence_hashes: List[str] = list(meta.get("evidence_hashes") or [])
        expires_at: Optional[float] = float(meta["expires_at"]) if meta.get("expires_at") is not None else None

        meta["trust_status"] = trust_status
        meta["verification_status"] = verification_status
        if evidence_hashes:
            meta["evidence_hashes"] = evidence_hashes
        if expires_at is not None:
            meta["expires_at"] = expires_at

        # Deduplication & Contradiction/Supersession Resolution in local cache
        normalized_goal = goal_text.strip().lower()
        max_prior_version = 0
        for existing_id, existing in list(self._local_memories.items()):
            existing_uid = existing.user_id or (existing.metadata.get("user_id") if existing.metadata else None)
            if existing_uid != clean_uid:
                continue
            # Exact session + plan + goal + outcome deduplication
            if (
                session_id
                and plan_id
                and existing.session_id == session_id
                and existing.plan_id == plan_id
                and existing.goal_text.strip().lower() == normalized_goal
                and existing.outcome_status == effective_status
            ):
                logger.info(
                    f"[SemanticMemoryStore] Deduplicated repeated memory write for session='{session_id}', plan='{plan_id}' -> '{existing_id}'."
                )
                return existing

            if existing.goal_text.strip().lower() == normalized_goal:
                max_prior_version = max(max_prior_version, getattr(existing, "version", 1))

        now_ts = time.time()
        version_num = max_prior_version + 1 if max_prior_version > 0 else int(meta.get("version", 1))
        meta["version"] = version_num
        meta["updated_at"] = now_ts

        item = ExecutionMemoryItem(
            user_id=clean_uid,
            goal_text=goal_text,
            plan_id=plan_id,
            session_id=session_id,
            memory_type=effective_mtype,
            task_summary=task_summary,
            outcome_status=effective_status,
            source_type=source_type,
            trust_status=trust_status,
            verification_status=verification_status,
            evidence_hashes=evidence_hashes,
            execution_metrics=execution_metrics or {},
            safety_summary=safety_summary or {},
            metadata=meta,
            created_at=now_ts,
            updated_at=now_ts,
            expires_at=expires_at,
            version=version_num,
        )

        # Supersede older active memories for the same tenant & goal when a newer verified/approved memory arrives
        if trust_status == "approved" and effective_status == "success":
            for existing_id, existing in list(self._local_memories.items()):
                existing_uid = existing.user_id or (existing.metadata.get("user_id") if existing.metadata else None)
                if existing_uid != clean_uid:
                    continue
                if existing.goal_text.strip().lower() == normalized_goal and existing.trust_status not in ("quarantined", "superseded", "invalidated"):
                    existing.trust_status = "superseded"
                    existing.superseded_by = item.memory_id
                    existing.updated_at = now_ts
                    existing.metadata["trust_status"] = "superseded"
                    existing.metadata["superseded_by"] = item.memory_id
                    existing.metadata["updated_at"] = now_ts

        embed_text = f"{goal_text} | {task_summary}"
        try:
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
        except Exception as emb_err:
            logger.warning(f"[SemanticMemoryStore] Embedding failed during record_memory: {emb_err}. Recording without vector.")
            embedding = []
            vec_dim = 0
            provider_name = "error"
            model_name = "none"
            embedding_space = "none:none:0"
            item.metadata["embedding_stored"] = False
            item.metadata["embedding_error"] = str(emb_err)

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
                if embedding and (self.db_vector_dim is None or vec_dim == self.db_vector_dim):
                    pg_vector_val = Vector(embedding)
                else:
                    item.metadata["embedding_stored"] = False
                    if vec_dim > 0 and self.db_vector_dim is not None and vec_dim != self.db_vector_dim:
                        item.metadata["embedding_dim_mismatch"] = True
                        logger.warning(
                            f"[SemanticMemoryStore] Dimension mismatch: generated {vec_dim}-dim vector, "
                            f"but DB schema expects {self.db_vector_dim}-dim. Storing memory with embedding=NULL for hybrid text search."
                        )

                with self._pg_conn.cursor() as cur:
                    if trust_status == "approved" and effective_status == "success" and max_prior_version > 0:
                        if clean_uid:
                            cur.execute(
                                """
                                UPDATE execution_memories
                                SET metadata = jsonb_set(
                                    jsonb_set(COALESCE(metadata, '{}'::jsonb), '{trust_status}', '"superseded"'::jsonb),
                                    '{superseded_by}', to_jsonb(%s::text)
                                )
                                WHERE LOWER(TRIM(goal_text)) = %s
                                  AND metadata->>'user_id' = %s
                                  AND outcome_status != 'quarantined'
                                """,
                                (item.memory_id, normalized_goal, clean_uid),
                            )
                        else:
                            cur.execute(
                                """
                                UPDATE execution_memories
                                SET metadata = jsonb_set(
                                    jsonb_set(COALESCE(metadata, '{}'::jsonb), '{trust_status}', '"superseded"'::jsonb),
                                    '{superseded_by}', to_jsonb(%s::text)
                                )
                                WHERE LOWER(TRIM(goal_text)) = %s
                                  AND metadata->>'user_id' IS NULL
                                  AND outcome_status != 'quarantined'
                                """,
                                (item.memory_id, normalized_goal),
                            )

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
                self._local_memories[item.memory_id] = item
                if embedding:
                    self._local_embeddings[item.memory_id] = embedding
                logger.info(f"[SemanticMemoryStore] Recorded memory '{item.memory_id}' in PostgreSQL.")
                return item
            except Exception as e:
                logger.error(f"[SemanticMemoryStore] PG record failed: {e}. Falling back to local.")

        self._local_memories[item.memory_id] = item
        if embedding:
            self._local_embeddings[item.memory_id] = embedding
        logger.info(f"[SemanticMemoryStore] Recorded memory '{item.memory_id}' in local store.")
        return item

    @staticmethod
    def _is_memory_retrievable(
        outcome_status: str,
        meta_dict: Dict[str, Any],
        approved_only: bool = True,
        requested_memory_type: Optional[str] = None,
    ) -> bool:
        """
        Determines whether a stored memory item is eligible for active planning/RAG retrieval.
        Excludes quarantined, rejected, invalidated, expired, and superseded memories.
        When approved_only=True (default unless explicitly querying failed_plan), excludes
        unverified/failed/incomplete candidate memories.
        """
        status_lower = str(outcome_status or "").strip().lower()
        if status_lower in ("quarantined", "rejected", "invalidated", "expired"):
            return False
        if meta_dict.get("quarantine_reason"):
            return False

        trust = str(meta_dict.get("trust_status") or "approved").strip().lower()
        if trust in ("quarantined", "rejected", "invalidated", "expired", "superseded"):
            return False

        expires_at = meta_dict.get("expires_at")
        if expires_at is not None:
            try:
                if float(expires_at) <= time.time():
                    return False
            except (TypeError, ValueError):
                pass

        if approved_only and requested_memory_type != "failed_plan":
            if trust == "candidate":
                return False
            if status_lower in ("unverified", "incomplete", "paused", "failed", "blocked"):
                return False

        return True

    @staticmethod
    def _hydrate_memory_item(
        mid: str,
        goal: str,
        pid: str,
        sid: str,
        mtype: str,
        summary: str,
        status: str,
        metrics: Any,
        safety: Any,
        meta_dict: Dict[str, Any],
        score_val: float,
    ) -> ExecutionMemoryItem:
        return ExecutionMemoryItem(
            memory_id=mid,
            user_id=meta_dict.get("user_id"),
            goal_text=goal,
            plan_id=pid,
            session_id=sid,
            memory_type=mtype,
            task_summary=summary,
            outcome_status=status,
            source_type=str(meta_dict.get("source_type", "execution")),
            trust_status=str(meta_dict.get("trust_status", "approved" if status == "success" else "candidate")),
            verification_status=str(meta_dict.get("verification_status", "verified" if status == "success" else status)),
            evidence_hashes=list(meta_dict.get("evidence_hashes") or []),
            execution_metrics=metrics if isinstance(metrics, dict) else json.loads(metrics or "{}"),
            safety_summary=safety if isinstance(safety, dict) else json.loads(safety or "{}"),
            metadata=meta_dict,
            relevance_score=round(score_val, 4),
            expires_at=float(meta_dict["expires_at"]) if meta_dict.get("expires_at") is not None else None,
            superseded_by=meta_dict.get("superseded_by"),
            version=int(meta_dict.get("version", 1)),
        )

    def retrieve_memories(
        self,
        goal_query: str,
        top_k: int = 3,
        min_score: float = 0.35,
        memory_type: Optional[str] = None,
        user_id: Optional[str] = None,
        approved_only: bool = True,
    ) -> List[ExecutionMemoryItem]:
        """
        Retrieves top-K most similar past execution memories for a target goal with tenant isolation
        and strict trust/expiration/supersession filtering.
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
                ts_results = [
                    r for r in ts_results
                    if self._is_memory_retrievable(r.outcome_status, r.metadata, approved_only=approved_only, requested_memory_type=memory_type)
                ]
                if ts_results:
                    return ts_results[:top_k]

            try:
                from pgvector import Vector  # adapt Python list → pgvector type
                query_vector = Vector(query_vec)

                with self._pg_conn.cursor() as cur:
                    where_clauses: List[str] = [
                        "embedding IS NOT NULL",
                        "metadata->>'embedding_space' = %s",
                        "outcome_status NOT IN ('quarantined', 'rejected', 'invalidated', 'expired')",
                        "metadata->>'quarantine_reason' IS NULL",
                        "(metadata->>'expires_at' IS NULL OR (metadata->>'expires_at')::double precision > %s)",
                    ]
                    filter_params: List[Any] = [query_space, time.time()]

                    # AUD-S3-04: Apply eligibility and trust filtering at SQL level before LIMIT
                    if approved_only and memory_type != "failed_plan":
                        where_clauses.append("outcome_status NOT IN ('unverified', 'incomplete', 'paused', 'failed', 'blocked')")
                        where_clauses.append("COALESCE(metadata->>'trust_status', 'approved') NOT IN ('candidate', 'quarantined', 'rejected', 'invalidated', 'expired', 'superseded')")
                    else:
                        where_clauses.append("COALESCE(metadata->>'trust_status', 'approved') NOT IN ('quarantined', 'rejected', 'invalidated', 'expired', 'superseded')")

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
                    full_params = [query_vector] + filter_params + [query_vector, max(top_k * 3, 10)]
                    cur.execute(sql, full_params)
                    rows = cur.fetchall()

                    results: List[ExecutionMemoryItem] = []
                    for row in rows:
                        mid, goal, pid, sid, mtype, summary, status, metrics, safety, meta, score, cat = row
                        score_val = max(0.0, min(1.0, float(score)))
                        if score_val < min_score:
                            continue

                        meta_dict = meta if isinstance(meta, dict) else json.loads(meta or "{}")
                        if not self._is_memory_retrievable(status, meta_dict, approved_only=approved_only, requested_memory_type=memory_type):
                            continue

                        results.append(
                            self._hydrate_memory_item(
                                mid, goal, pid, sid, mtype, summary, status, metrics, safety, meta_dict, score_val
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
                    ts_results = [
                        r for r in ts_results
                        if self._is_memory_retrievable(r.outcome_status, r.metadata, approved_only=approved_only, requested_memory_type=memory_type)
                    ]
                    if ts_results:
                        return ts_results[:top_k]
                    return []
            except Exception as e:
                logger.warning(f"[SemanticMemoryStore] PG retrieve error: {e}. Using local.")

        # Local search
        results = []
        for mid, mem in self._local_memories.items():
            meta_dict = dict(mem.metadata or {})
            if getattr(mem, "trust_status", None):
                meta_dict.setdefault("trust_status", mem.trust_status)
            if getattr(mem, "expires_at", None) is not None:
                meta_dict.setdefault("expires_at", mem.expires_at)

            if not self._is_memory_retrievable(
                getattr(mem, "outcome_status", ""),
                meta_dict,
                approved_only=approved_only,
                requested_memory_type=memory_type,
            ):
                continue

            if memory_type and mem.memory_type != memory_type:
                continue

            if clean_uid:
                mem_user = getattr(mem, "user_id", None) or meta_dict.get("user_id")
                clean_mem_user = str(mem_user).strip() if mem_user is not None else ""
                if not clean_mem_user or clean_mem_user != clean_uid:
                    continue
            else:
                mem_user = getattr(mem, "user_id", None) or meta_dict.get("user_id")
                if mem_user:
                    continue

            # Strict embedding-space compatibility:
            # Exclude records without embedding_space or with incompatible embedding_space.
            mem_space = meta_dict.get("embedding_space")
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
                    "outcome_status NOT IN ('quarantined', 'rejected', 'invalidated', 'expired')",
                    "metadata->>'quarantine_reason' IS NULL",
                    "(metadata->>'expires_at' IS NULL OR (metadata->>'expires_at')::double precision > %s)",
                ]
                filter_params: List[Any] = [goal_query, time.time()]

                # AUD-S3-04: Apply eligibility and trust filtering at SQL level before LIMIT
                if memory_type != "failed_plan":
                    where_clauses.append("outcome_status NOT IN ('unverified', 'incomplete', 'paused', 'failed', 'blocked')")
                    where_clauses.append("COALESCE(metadata->>'trust_status', 'approved') NOT IN ('candidate', 'quarantined', 'rejected', 'invalidated', 'expired', 'superseded')")
                else:
                    where_clauses.append("COALESCE(metadata->>'trust_status', 'approved') NOT IN ('quarantined', 'rejected', 'invalidated', 'expired', 'superseded')")

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
                full_params = [goal_query] + filter_params + [max(top_k * 3, 10)]
                cur.execute(sql, full_params)
                rows = cur.fetchall()

                results: List[ExecutionMemoryItem] = []
                for row in rows:
                    mid, goal, pid, sid, mtype, summary, status, metrics, safety, meta, rank, cat = row
                    score_val = max(0.1, min(1.0, float(rank) * 2.0 if rank > 0 else 0.1))
                    if min_score > 0 and score_val < min_score:
                        continue
                    meta_dict = meta if isinstance(meta, dict) else json.loads(meta or "{}")
                    if not self._is_memory_retrievable(status, meta_dict, approved_only=True, requested_memory_type=memory_type):
                        continue
                    results.append(
                        self._hydrate_memory_item(
                            mid, goal, pid, sid, mtype, summary, status, metrics, safety, meta_dict, score_val
                        )
                    )
                return results[:top_k]
        except Exception as e:
            logger.warning(f"[SemanticMemoryStore] tsvector retrieval error: {e}. Falling back to local.")
            return []

    def get_memory_by_id(self, memory_id: str, user_id: str) -> Optional[ExecutionMemoryItem]:
        """
        Tenant-isolated direct memory lookup by ID.
        Rejects missing/blank user_id, cross-tenant IDs, and quarantined memories.
        """
        clean_uid = str(user_id).strip() if user_id is not None else ""
        clean_mid = str(memory_id).strip() if memory_id is not None else ""
        if not clean_uid or not clean_mid:
            return None

        if self._is_pg_connected and self._pg_conn:
            try:
                with self._pg_conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT
                            memory_id, goal_text, plan_id, session_id, memory_type,
                            task_summary, outcome_status, execution_metrics, safety_summary,
                            metadata
                        FROM execution_memories
                        WHERE memory_id = %s
                          AND metadata->>'user_id' = %s
                          AND outcome_status != 'quarantined'
                        """,
                        (clean_mid, clean_uid),
                    )
                    row = cur.fetchone()
                    if row:
                        mid, goal, pid, sid, mtype, summary, status, metrics, safety, meta = row
                        meta_dict = meta if isinstance(meta, dict) else json.loads(meta or "{}")
                        if meta_dict.get("quarantine_reason") or meta_dict.get("trust_status") == "quarantined":
                            return None
                        return self._hydrate_memory_item(
                            mid, goal, pid, sid, mtype, summary, status, metrics, safety, meta_dict, 1.0
                        )
            except Exception as e:
                logger.warning(f"[SemanticMemoryStore] get_memory_by_id PG error: {e}")

        mem = self._local_memories.get(clean_mid)
        if not mem:
            return None
        mem_user = getattr(mem, "user_id", None) or (mem.metadata.get("user_id") if mem.metadata else None)
        if str(mem_user or "").strip() != clean_uid:
            return None
        if mem.outcome_status == "quarantined" or mem.trust_status == "quarantined" or (mem.metadata and mem.metadata.get("quarantine_reason")):
            return None
        return mem.model_copy()

    def approve_memory(self, memory_id: str, user_id: str) -> bool:
        """
        Promotes a candidate memory to 'approved' for the owning tenant if it is not quarantined,
        fallback-derived, or evidence-free execution.
        """
        mem = self.get_memory_by_id(memory_id, user_id=user_id)
        if not mem:
            return False
        if mem.metadata.get("is_fallback") or mem.metadata.get("evidence_free"):
            return False
        return self.update_memory(
            memory_id=memory_id,
            user_id=user_id,
            metadata_updates={"trust_status": "approved", "verification_status": "verified"},
            outcome_status="success",
        )

    def update_memory(
        self,
        memory_id: str,
        user_id: str,
        task_summary: Optional[str] = None,
        metadata_updates: Optional[Dict[str, Any]] = None,
        outcome_status: Optional[str] = None,
    ) -> bool:
        """
        Authorized tenant-isolated memory update. Increments version and updates timestamp.
        Cannot un-quarantine a quarantined memory.
        """
        clean_uid = str(user_id).strip() if user_id is not None else ""
        clean_mid = str(memory_id).strip() if memory_id is not None else ""
        if not clean_uid or not clean_mid:
            return False

        existing = self.get_memory_by_id(clean_mid, user_id=clean_uid)
        if not existing:
            return False

        now_ts = time.time()
        new_meta = dict(existing.metadata or {})
        if metadata_updates:
            for k, v in metadata_updates.items():
                if k in ("user_id", "quarantine_reason"):
                    continue
                new_meta[k] = v

        new_version = int(existing.version or 1) + 1
        new_meta["version"] = new_version
        new_meta["updated_at"] = now_ts
        new_summary = task_summary if task_summary is not None else existing.task_summary
        new_status = outcome_status if outcome_status is not None else existing.outcome_status
        new_trust = str(new_meta.get("trust_status") or existing.trust_status)

        updated_pg = False
        if self._is_pg_connected and self._pg_conn:
            try:
                with self._pg_conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE execution_memories
                        SET task_summary = %s,
                            outcome_status = %s,
                            metadata = %s
                        WHERE memory_id = %s
                          AND metadata->>'user_id' = %s
                          AND outcome_status != 'quarantined'
                        """,
                        (new_summary, new_status, json.dumps(new_meta), clean_mid, clean_uid),
                    )
                    updated_pg = cur.rowcount > 0
            except Exception as e:
                logger.warning(f"[SemanticMemoryStore] update_memory PG error: {e}")

        if clean_mid in self._local_memories:
            local_item = self._local_memories[clean_mid]
            local_item.task_summary = new_summary
            local_item.outcome_status = new_status
            local_item.trust_status = new_trust
            local_item.verification_status = str(new_meta.get("verification_status", local_item.verification_status))
            local_item.metadata = new_meta
            local_item.updated_at = now_ts
            local_item.version = new_version
            if new_meta.get("expires_at") is not None:
                local_item.expires_at = float(new_meta["expires_at"])
            return True

        return updated_pg

    def invalidate_memory(self, memory_id: str, user_id: str, reason: str = "invalidated") -> bool:
        """
        Tenant-isolated memory invalidation/revocation. Marks memory as invalidated so it is never retrieved.
        """
        return self.update_memory(
            memory_id=memory_id,
            user_id=user_id,
            metadata_updates={"trust_status": "invalidated", "invalidation_reason": reason},
            outcome_status="invalidated",
        )

    def delete_memory(self, memory_id: str, user_id: str) -> bool:
        """
        Tenant-isolated memory deletion. Refuses to delete quarantined audit records.
        """
        clean_uid = str(user_id).strip() if user_id is not None else ""
        clean_mid = str(memory_id).strip() if memory_id is not None else ""
        if not clean_uid or not clean_mid:
            return False

        existing = self.get_memory_by_id(clean_mid, user_id=clean_uid)
        if not existing:
            return False

        deleted = False
        if self._is_pg_connected and self._pg_conn:
            try:
                with self._pg_conn.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM execution_memories
                        WHERE memory_id = %s
                          AND metadata->>'user_id' = %s
                          AND outcome_status != 'quarantined'
                        """,
                        (clean_mid, clean_uid),
                    )
                    deleted = cur.rowcount > 0
            except Exception as e:
                logger.warning(f"[SemanticMemoryStore] delete_memory PG error: {e}")

        if clean_mid in self._local_memories:
            self._local_memories.pop(clean_mid, None)
            self._local_embeddings.pop(clean_mid, None)
            deleted = True

        return deleted

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
