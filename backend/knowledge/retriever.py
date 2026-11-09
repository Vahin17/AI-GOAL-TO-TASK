"""
retriever.py — Knowledge Retriever & Search Controller
======================================================
Coordinates query execution, latency measurement, threshold filtering,
and produces strongly-typed RetrievalResult objects.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from knowledge.knowledge_schemas import RetrievalResult, RetrievedChunk
from knowledge.vector_store_base import VectorStore

logger = logging.getLogger(__name__)


class KnowledgeRetriever:
    """
    High-level search interface over the vector store.
    """

    def __init__(
        self,
        vector_store: VectorStore,
        default_top_k: int = 4,
        default_min_score: float = 0.4,
    ):
        self.vector_store = vector_store
        self.default_top_k = default_top_k
        self.default_min_score = default_min_score

    def retrieve(
        self,
        query: str,
        top_k: Optional[int] = None,
        min_score: Optional[float] = None,
        filter_metadata: Optional[Dict[str, Any]] = None,
        search_mode: str = "hybrid",
        rrf_k: int = 60,
    ) -> RetrievalResult:
        """
        Retrieves relevant document chunks for a natural language query using
        hybrid (vector + tsvector full-text keyword with RRF), vector-only, or keyword-only search.
        """
        start_time = time.perf_counter()
        k = top_k if top_k is not None else self.default_top_k
        threshold = min_score if min_score is not None else self.default_min_score

        if not query.strip():
            return RetrievalResult(
                query=query,
                chunks=[],
                total_candidates=0,
                filtered_count=0,
                retrieval_latency_ms=0.0,
                status="empty",
            )

        try:
            # Check if vector store supports search_mode and rrf_k
            import inspect
            sig = inspect.signature(self.vector_store.query)
            if "search_mode" in sig.parameters:
                matched_chunks = self.vector_store.query(
                    query_text=query,
                    top_k=k,
                    min_score=threshold,
                    filter_metadata=filter_metadata,
                    search_mode=search_mode,
                    rrf_k=rrf_k,
                )
            else:
                matched_chunks = self.vector_store.query(
                    query_text=query,
                    top_k=k,
                    min_score=threshold,
                    filter_metadata=filter_metadata,
                )

            latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
            status = "success" if matched_chunks else "empty"

            return RetrievalResult(
                query=query,
                chunks=matched_chunks,
                total_candidates=len(matched_chunks),
                filtered_count=len(matched_chunks),
                retrieval_latency_ms=latency_ms,
                status=status,
            )

        except Exception as e:
            latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
            logger.error(f"[KnowledgeRetriever] Retrieval failed for '{query[:30]}': {e}")
            return RetrievalResult(
                query=query,
                chunks=[],
                total_candidates=0,
                filtered_count=0,
                retrieval_latency_ms=latency_ms,
                status="error",
                error_message=str(e),
            )
