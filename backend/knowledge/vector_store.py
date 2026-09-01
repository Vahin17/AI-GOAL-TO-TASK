"""
vector_store.py
===============
ChromaDB has been removed from this project.
PostgreSQL + pgvector is now the sole vector storage backend.

This module preserves the public names (ChromaVectorStore, DocumentChunk,
RetrievedChunk) as thin aliases/re-exports of the PostgreSQL implementation
so that legacy imports do not break during the migration period.

Production code should import from knowledge.postgres_vector_store directly.
"""

from __future__ import annotations

# Re-export the PostgreSQL-backed implementation under the legacy name.
# Tests and any remaining callers that use `ChromaVectorStore` will
# automatically use PostgresPgVectorStore (with its local-fallback mode
# when in_memory=True), requiring zero ChromaDB installation.
from knowledge.postgres_vector_store import PostgresPgVectorStore as ChromaVectorStore

# Re-export shared schemas so `from knowledge.vector_store import DocumentChunk`
# continues to work unchanged.
from knowledge.knowledge_schemas import DocumentChunk, RetrievedChunk

__all__ = ["ChromaVectorStore", "DocumentChunk", "RetrievedChunk"]
