"""
vector_store_base.py — Abstract Base Class for Vector Stores
============================================================
Defines the clean interface contract that any vector database implementation
(PostgreSQL + pgvector, in-memory, etc.) must satisfy.

Decouples the KnowledgeRetriever and ContextAssembler from specific DB engines.
"""

from __future__ import annotations

import abc
from typing import Any, Dict, List, Optional

from knowledge.embeddings import EmbeddingProvider
from knowledge.knowledge_schemas import DocumentChunk, RetrievedChunk


class VectorStore(abc.ABC):
    """
    Abstract vector store contract for indexing and similarity retrieval.
    """

    @abc.abstractmethod
    def add_chunks(self, chunks: List[DocumentChunk]) -> int:
        """
        Embed and persist document chunks. Returns the number of chunks added.
        """
        pass

    @abc.abstractmethod
    def query(
        self,
        query_text: str,
        top_k: int = 4,
        min_score: float = 0.0,
        filter_metadata: Optional[Dict[str, Any]] = None,
    ) -> List[RetrievedChunk]:
        """
        Perform semantic similarity search against the vector index.
        Returns a list of RetrievedChunk sorted by relevance_score descending.
        """
        pass

    @abc.abstractmethod
    def delete_document(self, document_id: str) -> bool:
        """
        Deletes all chunks associated with a document ID.
        """
        pass

    @abc.abstractmethod
    def count(self) -> int:
        """
        Returns the total number of chunks currently stored.
        """
        pass

    @abc.abstractmethod
    def clear(self) -> None:
        """
        Empties all stored chunks in the vector collection/table.
        """
        pass
