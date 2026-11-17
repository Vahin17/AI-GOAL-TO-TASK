# ARDO — Evidence Document: Task T1
## Close Embedding Architecture Checkpoint

**Date:** 2026-11-13  
**Status:** COMPLETE (DOCUMENTATION & EVIDENCE ONLY)  
**Task ID:** `T1`  
**Phase:** Phase 1 (Stabilization)  
**Implementation Changes:** ZERO (Strictly documentation and evidence consolidation)  

---

## 1. Executive Summary

Task T1 formalizes and closes the checkpoint for the ARDO Embedding and Semantic Memory Architecture. All dimension and model-space compatibility mechanisms have been implemented and empirically verified across 44 targeted tests.

This checkpoint establishes:
1. Mathematical safety of vector similarity retrieval (segregating disparate models and dimensional projections).
2. Preservation of legacy records lacking model provenance through PostgreSQL full-text search.
3. Accurate documentation of current provider recovery limitations.
4. Truthful disclosure that all database evidence is in-memory and mocked, with live PostgreSQL testing deferred to Task T6.

---

## 2. Empirical Test Execution Evidence

### Test Suite Invocations
All four targeted test modules were executed in a single unified run inside the active Python virtual environment:

```powershell
$env:PYTHONPATH="backend"; .venv\Scripts\python -m pytest `
  backend/tests/test_embedding_dimension_compatibility.py `
  backend/tests/test_vector_store_truthfulness.py `
  backend/tests/test_rag_pipeline.py `
  backend/tests/test_hybrid_retrieval.py -q
```

### Actual Pytest Execution Output
```
............................................                             [100%]
44 passed in 12.15s
```

### Granular Suite Breakdown
| Module | Test Count | Pass Rate | Execution Time | Key Invariants Verified |
| :--- | :---: | :---: | :---: | :--- |
| `test_embedding_dimension_compatibility.py` | 17 | 100% (17/17) | 0.63s | 128-dim local fallback, 1536-dim OpenAI, model identity segregation, legacy tsvector fallback, bounded cooldown recovery, 401 permanent lock. |
| `test_vector_store_truthfulness.py` | 4 | 100% (4/4) | 12.74s | Settings URI inheritance, truthful active provider strings, candidate playbook safety. |
| `test_rag_pipeline.py` | 16 | 100% (16/16) | 1.69s | Chunking, token estimation, cosine similarity, context assembly, budget limits, safety policy invariance. |
| `test_hybrid_retrieval.py` | 7 | 100% (7/7) | 1.29s | Exact keyword matching, semantic vector matching, Reciprocal Rank Fusion (RRF), tenant isolation, metadata preservation. |
| **Consolidated Total** | **44** | **100% (44/44)** | **12.15s** | **Zero test failures, zero regressions.** |

---

## 3. Strict Model-Space Identity & Dimension Safeguards

### A. Embedding Space Identity Definition
Every embedding provider implements an authoritative `embedding_space` property:
$$\text{embedding\_space} = \text{active\_provider} : \text{model\_name} : \text{dimension}$$

- `DeterministicLocalEmbeddingProvider`: `"local_deterministic_fallback:deterministic-hash-128:128"`
- `OpenAIEmbeddingProvider`: `"openai:text-embedding-3-small:1536"`
- `HybridEmbeddingService`: Dynamically reflects the currently active provider's space.

### B. Database Dimension Safeguards
1. **Schema Introspection:** `SemanticMemoryStore` and `PostgresPgVectorStore` inspect live PostgreSQL column definitions using `pg_attribute`:
   ```sql
   SELECT format_type(atttypid, atttypmod)
   FROM pg_attribute
   WHERE attrelid = 'execution_memories'::regclass
     AND attname = 'embedding'
     AND NOT attisdropped;
   ```
2. **Insertion Protection (Zero 22000 Errors):** If the active provider's vector dimension differs from the database column dimension (e.g. 128-dim local vector vs. 1536-dim table column), the record is inserted with `embedding = NULL`. The textual content is safely persisted and indexed via the PostgreSQL generated `tsv_content` column.
3. **Query Protection:** If the query vector's dimension differs from the table column dimension, pgvector cosine search (`<=>`) is bypassed entirely. `SemanticMemoryStore` seamlessly executes `_retrieve_memories_tsvector()`, and `PostgresPgVectorStore` executes keyword search via `query_keyword()`.

### C. Model-Space Provenance & Strict Filtering
1. **Persistence:** Newly generated vectors have `embedding_space` immutably attached to their JSONB `metadata`.
2. **PostgreSQL Filtering:** Vector similarity queries enforce exact matching:
   ```sql
   WHERE embedding IS NOT NULL
     AND metadata->>'embedding_space' = %s
   ```
   - **No NULL Provenance Matching:** Rows lacking `embedding_space` evaluate to `NULL = %s` (`UNKNOWN`), completely preventing unverified vectors from entering similarity comparisons.
3. **Local Filtering:** In-memory dictionary retrieval skips any item where `mem.metadata.get("embedding_space") != query_space`.
4. **Legacy Record Preservation:** Full-text keyword queries search `tsv_content @@ plainto_tsquery(...)` without requiring `embedding_space`. Legacy records remain 100% discoverable through text search and hybrid Reciprocal Rank Fusion (RRF).

---

## 4. PostgreSQL Integration Status Disclosure

> [!IMPORTANT]
> **Actual PostgreSQL / pgvector integration has NOT been executed.**
> All 44 passing tests were executed using in-memory dictionary stores (`in_memory=True`) or mocked `psycopg2` / `pgvector` connections.
> No live PostgreSQL server was contacted. Live DDL, HNSW indexing, and real pgvector cosine distance operations remain to be validated in **Task T6** once a live Docker PostgreSQL container is active.

---

## 5. Documented Provider Recovery Limitations

While `HybridEmbeddingService` successfully implements bounded cooldowns (preventing high-frequency request hammering), the following architectural limitations are formally documented for future enhancement in **Task T5**:

1. **Over-Broad HTTP 429 Classification:**
   The service treats `"quota"`, `"credit"`, `"429"`, and `"rate limit"` identically. It does not distinguish temporary per-minute rate limits (RPM/TPM) from hard account billing stops (`insufficient_quota`).
2. **`Retry-After` Header Ignored:**
   The implementation relies strictly on `str(e).lower()`. It does not inspect HTTP response headers for a `Retry-After` delay.
3. **Fixed 120-Second Cooldown:**
   Under hard credit exhaustion, the service retries every 120 seconds. While this caps retry traffic to 1 request per 2 minutes, it will not permanently halt retries for unrecoverable billing stops.
4. **Authentication (401) Recovery Inflexibility:**
   An HTTP 401 permanently locks the service into local fallback (`_auth_invalid = True`). There is no dynamic `update_api_key()` method; reloading credentials requires restarting the application process.

---

## 6. Exit Gate Verification

- [x] All 44 targeted tests documented with exact command output.
- [x] Strict model identity and dimension safeguards fully detailed.
- [x] Lack of live PostgreSQL testing explicitly disclosed.
- [x] Provider recovery limitations documented for Task T5.
- [x] Zero implementation code modified.

**Task T1 Status:** `done`

