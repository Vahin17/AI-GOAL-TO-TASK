"""
rag_service.py — Central Knowledge & Semantic Memory Service Coordinator
========================================================================
Phase 7: PostgreSQL + pgvector vector store & Semantic Memory Coordinator.

Responsibilities:
    1. Static Knowledge Management: Uses PostgresPgVectorStore for playbooks/SOPs.
    2. Semantic Memory Management: Uses SemanticMemoryStore for historical plans/failures.
    3. Dual Retrieval & Grounding: Combines static guidelines and experiential memory.
    4. Auto-seeding: Automatically seeds standard operational playbooks.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from core.config import settings
from knowledge.chunking import TextChunker
from knowledge.context_assembler import ContextAssembler
from knowledge.embeddings import HybridEmbeddingService
from knowledge.knowledge_schemas import Document, IngestionReport, KnowledgeProvenance
from knowledge.postgres_vector_store import PostgresPgVectorStore
from knowledge.retriever import KnowledgeRetriever
from knowledge.semantic_memory import ExecutionMemoryItem, SemanticMemoryStore
from knowledge.vector_store_base import VectorStore

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Seed Knowledge Base
# ─────────────────────────────────────────────────────────────────────────────

SEED_DOCUMENTS = [
    Document(
        document_id="doc_playbook_web_app",
        title="Web Application Architecture & Development Playbook",
        source="knowledge://playbooks/web_application_architecture.md",
        category="playbook",
        content="""
# Web Application Architecture & Development Playbook

When decomposing a web application goal into execution tasks, standard production patterns require:

1. **Frontend Architecture**:
   - Modern component-based frontend (Next.js, React, or Vite).
   - Component decomposition: Layout, navigation, core features, state management.
   - Dark/light responsive design with accessible styling.

2. **Backend API Design**:
   - FastAPI or Express REST/GraphQL endpoints with strict request/response schemas.
   - Database connection pooling and CRUD operations.
   - Health check endpoints and structured logging.

3. **Data Layer**:
   - Schema definitions, migrations, and index optimization.
   - Separation between transactional data and audit/telemetry logs.

4. **Testing and Verification**:
   - Unit tests for core domain logic, integration tests for API endpoints.
   - Automated linting and type checking before deployment.
""",
        metadata={"priority": "high", "domain": "software_engineering"},
    ),
    Document(
        document_id="doc_playbook_deployment",
        title="Safe Deployment & Release Engineering Guidelines",
        source="knowledge://playbooks/safe_deployment_guidelines.md",
        category="playbook",
        content="""
# Safe Deployment & Release Engineering Guidelines

Standard safe deployment sequence for production environments:

1. **Pre-flight Validation**:
   - Verify all automated test suites pass (unit, integration, regression).
   - Run dependency vulnerability and security scanner checks.
   - Build reproducible release artifacts.

2. **Staging Verification**:
   - Deploy first to staging environment before touching production.
   - Verify smoke tests against staging endpoints.
   - Verify database migrations in staging.

3. **Production Rollout**:
   - Use canary or blue-green deployment strategies where feasible.
   - Maintain rollback scripts and snapshot backups.
   - Monitor real-time telemetry, latency, and error rates during rollout.
""",
        metadata={"priority": "critical", "domain": "devops"},
    ),
    Document(
        document_id="doc_playbook_maintenance",
        title="System Maintenance & Safe Cleanup Procedures",
        source="knowledge://playbooks/system_maintenance_cleanup.md",
        category="playbook",
        content="""
# System Maintenance & Safe Cleanup Procedures

Standard operating procedures for system maintenance and disk space recovery:

1. **Safe Transient Cleanup**:
   - Temporary build artifacts (node_modules/.cache, target/debug, dist/, pycache).
   - Test run outputs and expired scratch directories.
   - Browser cache and non-essential session cookies.
   - User recycle bin contents (with confirmation if large volume).

2. **Protected Resources (NEVER Delete during routine maintenance)**:
   - Operating system boot files, kernel configs, and system binaries.
   - User project source files and active version-controlled repositories.
   - Production databases, live credential stores, and authentication tokens.
""",
        metadata={"priority": "high", "domain": "sysadmin"},
    ),
    Document(
        document_id="doc_playbook_testing",
        title="Empirical Validation & Test Automation Standard",
        source="knowledge://playbooks/test_automation_standard.md",
        category="playbook",
        content="""
# Empirical Validation & Test Automation Standard

Core principles for automated test generation in planning engines:

1. **Deterministic Verification**:
   - Every complex task requires explicit automated tests with reproducible assertions.
   - Fault injection tests (network latency, database outage) prove runtime resilience.
   - Invariant testing verifies that circular DAGs and invalid states are impossible.

2. **Performance Benchmarking**:
   - Measure actual execution latency, throughput, and memory consumption.
   - Avoid mock-only verification for critical path algorithms.
""",
        metadata={"priority": "medium", "domain": "testing"},
    ),
]


# ─────────────────────────────────────────────────────────────────────────────
# RAGService
# ─────────────────────────────────────────────────────────────────────────────

class RAGService:
    """
    Central coordinator managing:
    1. Static Knowledge (PostgresPgVectorStore)
    2. Historical Execution Memory (SemanticMemoryStore)
    3. Retrieval & Prompt Grounding
    """

    _instance: Optional["RAGService"] = None

    def __init__(
        self,
        vector_store: Optional[VectorStore] = None,
        memory_store: Optional[SemanticMemoryStore] = None,
        in_memory: bool = False,
        embedding_api_key: Optional[str] = None,
    ):
        self.chunker = TextChunker(chunk_size=450, chunk_overlap=50)
        self.embeddings = HybridEmbeddingService(api_key=embedding_api_key or getattr(settings, "OPENAI_API_KEY", None))
        
        # 1. Static knowledge vector store (PostgreSQL + pgvector)
        self.vector_store = vector_store or PostgresPgVectorStore(
            embedding_provider=self.embeddings,
            in_memory=in_memory,
        )
        self.retriever = KnowledgeRetriever(
            vector_store=self.vector_store,
            default_top_k=getattr(settings, "RAG_TOP_K", 3),
            default_min_score=getattr(settings, "RAG_MIN_SCORE", 0.35),
        )

        # 2. Semantic memory store (PostgreSQL + pgvector)
        self.memory_store = memory_store or SemanticMemoryStore(
            embedding_provider=self.embeddings,
            in_memory=in_memory,
        )

        # 3. Context assembler
        self.assembler = ContextAssembler(
            max_context_chars=getattr(settings, "RAG_MAX_CONTEXT_CHARS", 2500)
        )

        # Seed knowledge if collection/table is fresh
        if self.vector_store.count() == 0:
            self.seed_default_knowledge()

    @classmethod
    def get_instance(cls) -> "RAGService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls, in_memory: bool = True) -> "RAGService":
        """Used in test fixtures for clean isolated instances."""
        cls._instance = cls(in_memory=in_memory)
        return cls._instance

    def seed_default_knowledge(self) -> IngestionReport:
        """Seeds the standard operational documents into the vector store."""
        logger.info(f"[RAGService] Seeding default knowledge base ({len(SEED_DOCUMENTS)} documents)...")
        return self.ingest_documents(SEED_DOCUMENTS)

    def ingest_documents(self, docs: List[Document]) -> IngestionReport:
        """
        Chunks and vectorizes a list of documents into PostgreSQL + pgvector.
        """
        start = time.perf_counter()
        report = IngestionReport(documents_ingested=len(docs))

        try:
            chunks, dupes = self.chunker.chunk_documents(docs)
            report.chunks_created = len(chunks) + dupes
            report.duplicate_skipped = dupes

            stored = self.vector_store.add_chunks(chunks)
            report.chunks_stored = stored
            report.duration_ms = round((time.perf_counter() - start) * 1000, 2)
            logger.info(f"[RAGService] Ingested {len(docs)} docs -> {stored} chunks in {report.duration_ms}ms.")
        except Exception as e:
            report.errors.append(str(e))
            report.duration_ms = round((time.perf_counter() - start) * 1000, 2)
            logger.error(f"[RAGService] Ingestion failed: {e}")

        return report

    def ingest_raw_text(
        self,
        title: str,
        content: str,
        source: str = "manual_input",
        category: str = "general",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> IngestionReport:
        """Helper to ingest a single raw text document."""
        doc = Document(
            title=title,
            content=content,
            source=source,
            category=category,
            metadata=metadata or {},
        )
        return self.ingest_documents([doc])

    def get_relevant_context_for_goal(
        self,
        goal_text: str,
        top_k: Optional[int] = None,
        min_score: Optional[float] = None,
        include_memory: bool = True,
    ) -> Tuple[str, KnowledgeProvenance]:
        """
        Main interface called during task planning:
        1. Retrieves matching static knowledge chunks (Playbooks/SOPs)
        2. Retrieves relevant past execution memories (if enabled)
        3. Formats them as structured markdown context
        """
        knowledge_res = self.retriever.retrieve(
            query=goal_text,
            top_k=top_k,
            min_score=min_score,
        )

        memories: List[ExecutionMemoryItem] = []
        if include_memory and self.memory_store:
            try:
                memories = self.memory_store.retrieve_memories(
                    goal_query=goal_text,
                    top_k=2,
                    min_score=min_score or 0.35,
                )
            except Exception as e:
                logger.warning(f"[RAGService] Memory retrieval warning: {e}")

        return self.assembler.assemble(knowledge_res, memories=memories)

    def record_plan_outcome(
        self,
        goal_text: str,
        plan_id: str,
        session_id: str,
        outcome_status: str,
        task_summary: str,
        execution_metrics: Optional[Dict[str, Any]] = None,
        safety_summary: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ExecutionMemoryItem:
        """
        Records the completed outcome of a planning/orchestration session
        into historical semantic memory.
        """
        mem_type = "successful_plan" if outcome_status == "success" else "failed_plan"
        return self.memory_store.record_memory(
            goal_text=goal_text,
            plan_id=plan_id,
            session_id=session_id,
            memory_type=mem_type,
            task_summary=task_summary,
            outcome_status=outcome_status,
            execution_metrics=execution_metrics,
            safety_summary=safety_summary,
            metadata=metadata,
        )
