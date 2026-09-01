"""
knowledge_schemas.py — Phase 6: Context-Aware Knowledge & RAG Schemas
======================================================================
Defines all data models, request/response contracts, and provenance types
for the knowledge ingestion, vector storage, and retrieval pipeline.

Design Invariants:
    1. Provenance is first-class: Every retrieved chunk and context block
       retains its document_id, chunk_id, source URI, and relevance score.
    2. RAG context is informational ONLY. It cannot override deterministic
       safety policies (SafetyPolicyEngine > LLM > RAG Context).
    3. Types are strictly validated via Pydantic V2.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class Document(BaseModel):
    """
    Top-level knowledge document before chunking.
    Represents a source file, playbook, specification, or guidelines document.
    """
    document_id: str = Field(default_factory=lambda: f"doc_{uuid.uuid4().hex[:10]}")
    title:       str
    source:      str = Field(..., description="Source file path, URL, or identifier")
    category:    str = Field(default="general", description="e.g. playbook, architecture, safety_standard, api_doc")
    content:     str
    metadata:    Dict[str, Any] = Field(default_factory=dict)
    created_at:  float = Field(default_factory=time.time)


class DocumentChunk(BaseModel):
    """
    A single chunk of text derived from a Document, ready for embedding and storage.
    """
    chunk_id:        str = Field(default_factory=lambda: f"chunk_{uuid.uuid4().hex[:12]}")
    document_id:     str
    source:          str
    chunk_index:     int = 0
    content:         str
    char_count:      int = 0
    token_estimate:  int = 0
    content_hash:    str = ""
    metadata:        Dict[str, Any] = Field(default_factory=dict)
    embedding:       Optional[List[float]] = None


class RetrievedChunk(BaseModel):
    """
    A chunk matched during similarity search with relevance scoring.
    """
    chunk_id:        str
    document_id:     str
    source:          str
    title:           str = ""
    content:         str
    relevance_score: float = Field(..., ge=0.0, le=1.0, description="Cosine similarity score (0.0 to 1.0)")
    distance:        float = 0.0
    metadata:        Dict[str, Any] = Field(default_factory=dict)


class RetrievalResult(BaseModel):
    """
    Complete output of a retrieval operation, including metrics and diagnostics.
    """
    query:                str
    chunks:               List[RetrievedChunk] = Field(default_factory=list)
    total_candidates:     int = 0
    filtered_count:       int = 0
    retrieval_latency_ms: float = 0.0
    status:               str = "success"   # "success", "empty", "fallback", "error"
    error_message:        Optional[str] = None


class KnowledgeProvenance(BaseModel):
    """
    Lightweight provenance metadata attached to plans to answer:
    'What knowledge influenced this plan generation?'
    """
    sources_used:         List[str] = Field(default_factory=list, description="Unique source file names/URIs")
    document_ids:         List[str] = Field(default_factory=list)
    chunk_ids:            List[str] = Field(default_factory=list)
    chunk_count:          int = 0
    average_relevance:    float = 0.0
    context_char_count:   int = 0
    retrieval_latency_ms: float = 0.0
    retrieval_status:     str = "success"


class IngestionReport(BaseModel):
    """
    Summary report after batch document ingestion.
    """
    documents_ingested: int = 0
    chunks_created:     int = 0
    chunks_stored:      int = 0
    duplicate_skipped:  int = 0
    duration_ms:        float = 0.0
    errors:             List[str] = Field(default_factory=list)
