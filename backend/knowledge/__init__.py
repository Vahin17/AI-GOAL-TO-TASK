"""
knowledge — Context-Aware Knowledge & Semantic Memory Package
=============================================================
Phase 7: PostgreSQL + pgvector Vector Store, Semantic Memory Foundation,
and RAG grounding pipeline.
"""

from knowledge.knowledge_schemas import (
    Document,
    DocumentChunk,
    RetrievedChunk,
    RetrievalResult,
    KnowledgeProvenance,
    IngestionReport,
)
from knowledge.chunking import TextChunker
from knowledge.embeddings import (
    EmbeddingProvider,
    DeterministicLocalEmbeddingProvider,
    OpenAIEmbeddingProvider,
    HybridEmbeddingService,
)
from knowledge.vector_store_base import VectorStore
from knowledge.postgres_vector_store import PostgresPgVectorStore
from knowledge.semantic_memory import SemanticMemoryStore, ExecutionMemoryItem
from knowledge.retriever import KnowledgeRetriever
from knowledge.context_assembler import ContextAssembler
from knowledge.rag_service import RAGService

__all__ = [
    "Document",
    "DocumentChunk",
    "RetrievedChunk",
    "RetrievalResult",
    "KnowledgeProvenance",
    "IngestionReport",
    "TextChunker",
    "EmbeddingProvider",
    "DeterministicLocalEmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "HybridEmbeddingService",
    "VectorStore",
    "PostgresPgVectorStore",
    "SemanticMemoryStore",
    "ExecutionMemoryItem",
    "KnowledgeRetriever",
    "ContextAssembler",
    "RAGService",
]
