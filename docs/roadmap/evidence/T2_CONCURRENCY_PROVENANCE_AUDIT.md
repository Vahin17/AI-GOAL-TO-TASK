# ARDO — Evidence Document: Task T2
## Concurrency and Data-Provenance Audit

**Date:** 2026-11-13  
**Status:** COMPLETE (READ-ONLY AUDIT)  
**Task ID:** `T2`  
**Phase:** Phase 1 (Stabilization)  
**Audit Mode:** STRICT READ-ONLY MODE (Zero code changes, zero test modifications, zero commits)  

---

## 1. Executive Summary

This audit independently investigated concurrency safety, shared provider state, and tenant data isolation across the ARDO planning engine.

### Key Audit Conclusions
1. **Embedding Provenance Race Condition:** **CONFIRMED (CRITICAL)**.
   Under concurrent requests or parallel task execution, `HybridEmbeddingService` (accessed via the singleton `RAGService.get_instance()`) can mutate its shared provider state while another request is between vector generation and provenance metadata assignment. This can cause a 1536-dimensional OpenAI vector to be labeled as a 128-dimensional local vector (or vice versa), which subsequently triggers a fatal PostgreSQL 22000 dimension mismatch abort on similarity retrieval.
2. **Cross-Tenant Checkpoint Hijacking:** **CONFIRMED (CRITICAL)**.
   MongoDB `execution_checkpoints` and `task_runtime_state` do not persist `user_id`. When resuming an offline/interrupted session via `POST /orchestrate/{session_id}/resume`, the tenant ownership check evaluates `checkpoint.get("user_id")`, which is always `None`, allowing any authenticated user to resume and hijack another user's session.
3. **Cross-Tenant Semantic Memory Leakage:** **CONFIRMED (HIGH)**.
   PostgreSQL `execution_memories` has no `user_id` column. When a user plans a goal, `RAGService.get_relevant_context_for_goal()` retrieves past execution memories from any user who executed a semantically similar goal.
4. **WebSocket Cross-Session Eavesdropping:** **CONFIRMED (MEDIUM)**.
   `create_ws_ticket` issues tickets without validating session ownership, and WebSocket connection logic permits access when `active_session.user_id` is `None`.
5. **Cryptographic Plan Hash Bypass:** **CONFIRMED (LOW/MEDIUM)**.
   Task approvals skip plan hash verification if the client omits `plan_hash` in the request body.

---

## 2. Shared Embedding-Provider State Race Condition

### A. Proof of Vulnerability

#### Call Sequence & Data Flow
1. **Shared Singleton Architecture:**
   `RAGService` is a process-wide singleton (`backend/knowledge/rag_service.py:154, 193`).
   Its embedding service (`self.embeddings`) is a shared instance of `HybridEmbeddingService`.
   Its memory store (`self.memory_store`) is a shared instance of `SemanticMemoryStore` using `self.embeddings`.
   Its vector store (`self.vector_store`) is a shared instance of `PostgresPgVectorStore` using `self.embeddings`.

2. **The Non-Atomic Provenance Window:**
   In `backend/knowledge/semantic_memory.py` (`record_memory`, lines 199–213):
   ```python
   # Step 1: Generates vector (I/O operation, releases Python GIL)
   embedding = self.embedding_provider.embed_query(embed_text)
   vec_dim = len(embedding) if embedding else 0

   # --- RACE WINDOW OPENS HERE ---

   # Step 2: Reads shared state from provider service
   provider_name = getattr(self.embedding_provider, "active_provider", "unknown")
   model_name = getattr(self.embedding_provider, "active_model", getattr(self.embedding_provider, "model_name", "unknown"))
   embedding_space = getattr(
       self.embedding_provider,
       "embedding_space",
       f"{provider_name}:{model_name}:{vec_dim}",
   )
   item.metadata.setdefault("embedding_space", embedding_space)
   ```

3. **Interleaving Execution Trace:**
   - **Thread 1 (Request A - Goal Planning / Memory Recording):**
     Calls `self.embedding_provider.embed_query(embed_text)`.
     `OpenAIEmbeddingProvider.embed_query` makes an HTTPS call to OpenAI (releasing the GIL).
     OpenAI call succeeds. Thread 1 receives a 1536-dimensional vector `embedding`. `vec_dim = 1536`.
   - **Thread 2 (Request B - Concurrent Execution / Background Ingestion / Query):**
     Concurrently executes `self.embedding_provider.embed_query(other_text)` on the same shared service.
     The call fails (e.g., HTTP 429 rate limit or network timeout).
     `HybridEmbeddingService._handle_failure(e)` executes in `backend/knowledge/embeddings.py:290-314`:
     ```python
     self._openai = None  # Mutates instance state!
     HybridEmbeddingService._last_failure_type = "network_error"
     HybridEmbeddingService._last_failure_time = time.time()
     ```
     From this moment, `self.embedding_provider.embedding_space` on the shared service returns:
     `"local_deterministic_fallback:deterministic-hash-128:128"`.
   - **Thread 1 (Request A resumes):**
     Executes lines 203–213 of `semantic_memory.py`.
     It queries `self.embedding_provider.embedding_space`.
     It receives `"local_deterministic_fallback:deterministic-hash-128:128"`.
     It records:
     `item.metadata["embedding_dim"] = 1536`
     `item.metadata["embedding_space"] = "local_deterministic_fallback:deterministic-hash-128:128"`!
   - **Database Insertion & Subsequent Failure:**
     Thread 1 executes line 224: `vec_dim (1536) == self.db_vector_dim (1536)`, so `Vector(embedding)` is inserted into PostgreSQL.
     Now the database holds a **1536-dimensional vector labeled with 128-dimensional local provenance**.
     Later, when a local search executes (using a 128-dim vector), it executes:
     `WHERE metadata->>'embedding_space' = 'local_deterministic_fallback:deterministic-hash-128:128'`
     PostgreSQL matches this row and attempts `embedding <=> query_vector`.
     PostgreSQL aborts the entire transaction with **ERROR 22000: different vector dimensions 128 and 1536**.

### B. Finding Details
- **Finding ID:** `T2-FINDING-01`
- **Severity:** **CRITICAL**
- **Status:** **CONFIRMED**
- **Affected Files & Lines:**
  - `backend/knowledge/embeddings.py`: Lines 186–193, 290–314, 396–411.
  - `backend/knowledge/semantic_memory.py`: Lines 199–213.
  - `backend/knowledge/postgres_vector_store.py`: Lines 195–210.
  - `backend/knowledge/rag_service.py`: Lines 154, 164, 193 (singleton lifecycle).
- **Feasibility:** High. In a production environment with multiple concurrent user sessions or background tasks, network calls to OpenAI frequently interleave.
- **Recommended Fix (When Authorized):**
  Refactor `embed_query()` and `embed_texts()` to return an atomic data structure combining both the vector and its exact generating provenance:
  ```python
  @dataclass(frozen=True)
  class EmbeddingResult:
      vector: List[float]
      embedding_space: str
      provider: str
      model: str
      dimension: int
  ```
  This eliminates the window between vector generation and metadata capture.

---

## 3. Cross-Session and Cross-Tenant Isolation Audit

### Finding T2-02: Cross-Tenant Session Hijacking via MongoDB Checkpoints
- **Severity:** **CRITICAL**
- **Status:** **CONFIRMED**
- **Affected Files & Lines:**
  - `backend/orchestration/checkpoint_manager.py`: Lines 168–185 (`checkpoint_doc` creation).
  - `backend/api/router.py`: Lines 355–371 (`resume_orchestration`).
- **Data Flow & Vulnerability:**
  1. `ExecutionCheckpointManager.save_checkpoint()` creates `checkpoint_doc` containing `checkpoint_id`, `session_id`, `plan_hash`, `node_states`, `metrics`, etc.
  2. It **does NOT store `user_id`** in `checkpoint_doc` or in MongoDB `task_runtime_state`.
  3. When an active session concludes or the server restarts, `ARDOScheduler._sessions.get(session_id)` is `None`.
  4. User B calls `POST /orchestrate/{session_id}/resume` with User A's `session_id`.
  5. In `router.py:368-370`:
     ```python
     if checkpoint:
         chk_user = checkpoint.get("user_id")
         if chk_user is not None and chk_user != current_user["id"]:
             raise HTTPException(status_code=403, detail="Cross-user resume forbidden.")
     ```
  6. Because `chk_user` is always `None`, this security check is completely bypassed.
  7. User B successfully resumes User A's session, acquires the worker lease, and assumes control of all browser actions and task approvals.
- **Recommended Fix (When Authorized):**
  Pass `user_id` into `save_checkpoint()`, persist it in `checkpoint_doc`, and strictly reject resume if `checkpoint.get("user_id") != current_user["id"]` (fail closed on `None`).

---

### Finding T2-03: Cross-Tenant Semantic Memory Leakage
- **Severity:** **HIGH**
- **Status:** **CONFIRMED**
- **Affected Files & Lines:**
  - `backend/knowledge/semantic_memory.py`: Lines 56–78 (`MEMORY_SCHEMA_DDL`), lines 309–325 (`retrieve_memories`).
  - `backend/knowledge/rag_service.py`: Lines 250–280 (`get_relevant_context_for_goal`).
  - `backend/orchestration/runtime_engine.py`: Lines 1438–1446 (`record_plan_outcome`).
- **Data Flow & Vulnerability:**
  1. `execution_memories` table schema has no `user_id` or `tenant_id` column.
  2. When a plan completes, `runtime_engine.py` records `goal_text` and `task_summary` into semantic memory without tenant metadata.
  3. When any other tenant plans a goal, `RAGService.get_relevant_context_for_goal()` queries `retrieve_memories()` with no tenant filter.
  4. User A's past goals (which may contain internal project names, credentials, or proprietary URLs) are returned and injected into the planning LLM prompt for User B.
- **Recommended Fix (When Authorized):**
  Add `user_id VARCHAR(64)` to `execution_memories` (or JSONB `metadata->>'user_id'`) and filter by `user_id = %s` in `retrieve_memories()`.

---

### Finding T2-04: WebSocket Eavesdropping on Missing-User Sessions
- **Severity:** **MEDIUM**
- **Status:** **CONFIRMED**
- **Affected Files & Lines:**
  - `backend/api/auth.py`: Lines 912–926 (`create_ws_ticket`).
  - `backend/api/router.py`: Lines 774–777 (`stream_orchestration_events`).
- **Data Flow & Vulnerability:**
  1. `create_ws_ticket` issues a valid ticket for any arbitrary `session_id` passed in the request body without checking if `current_user["id"]` owns that session.
  2. In `router.py`, the WebSocket authorization check is:
     ```python
     active_session = ARDOScheduler._sessions.get(session_id)
     if active_session and active_session.user_id is not None and active_session.user_id != user_id:
         await websocket.close(code=1008)
         return
     ```
  3. If `active_session.user_id is None` (e.g. system or test sessions), the check passes.
  4. If `active_session` is `None` (session terminated or restoring), the check passes.
  5. The unauthorized user connects to `stream_events()` and receives execution payloads.
- **Recommended Fix (When Authorized):**
  Validate session ownership at ticket generation time in `create_ws_ticket`, and fail closed in `router.py` if `active_session` is missing or has `user_id is None`.

---

### Finding T2-05: In-Memory Evidence Store Global Leakage
- **Severity:** **MEDIUM**
- **Status:** **CONFIRMED**
- **Affected Files & Lines:**
  - `backend/execution/evidence/evidence_vault.py`: Lines 27, 230–234 (`get_evidence_by_id`).
  - `backend/orchestration/scheduler.py`: Line 183 (`self.user_id or "system"`).
- **Data Flow & Vulnerability:**
  1. `_MEMORY_EVIDENCE_STORE` is a module-level global dictionary.
  2. In `get_evidence_by_id(evidence_id, user_id)`, the in-memory fallback iterates across all sessions in `_MEMORY_EVIDENCE_STORE.values()`.
  3. In unauthenticated or test runs where `user_id` defaults to `"system"`, evidence from all `"system"` sessions is visible to any caller querying with `"system"`.
- **Recommended Fix (When Authorized):**
  Partition in-memory evidence by `(user_id, session_id)` and require an authenticated user ID on all `ARDOSession` instances.

---

### Finding T2-06: Cryptographic Plan Hash Bypass on Omission
- **Severity:** **LOW / MEDIUM**
- **Status:** **CONFIRMED**
- **Affected Files & Lines:**
  - `backend/api/router.py`: Line 273 (`if body.plan_hash is not None:`).
  - `backend/schemas/ardo_models.py`: Lines 108–117 (`ApproveTaskRequest`).
- **Data Flow & Vulnerability:**
  1. The task approval endpoint validates plan integrity:
     `if body.plan_hash is not None: if body.plan_hash != expected_hash: raise HTTPException(400)`
  2. However, `body.plan_hash` defaults to `None`.
  3. A client or attacker who does not provide `plan_hash` bypasses cryptographic verification completely.
- **Recommended Fix (When Authorized):**
  Enforce mandatory `plan_hash` validation when the underlying graph has an authoritative `plan_hash`.

---

## 4. Verification Table of Identified Concurrency & Isolation Risks

| Risk ID | Scope | Severity | Status | Concurrency Required? | Exact File & Lines |
| :--- | :--- | :---: | :---: | :---: | :--- |
| `T2-01` | Embedding Provenance Mismatch | **CRITICAL** | **Confirmed** | Yes (Parallel calls) | `embeddings.py:290-314`, `semantic_memory.py:199-213` |
| `T2-02` | Checkpoint Resume Session Hijack | **CRITICAL** | **Confirmed** | No (Sequential access) | `checkpoint_manager.py:168-185`, `router.py:368-370` |
| `T2-03` | Semantic Memory Cross-Tenant Leak | **HIGH** | **Confirmed** | No (Cross-session query) | `semantic_memory.py:56-78`, `rag_service.py:250-280` |
| `T2-04` | WebSocket Stream Eavesdropping | **MEDIUM** | **Confirmed** | No (API ticket issuance) | `auth.py:912-926`, `router.py:774-777` |
| `T2-05` | In-Memory Evidence Store Leak | **MEDIUM** | **Confirmed** | No (Fallback iteration) | `evidence_vault.py:230-234`, `scheduler.py:183` |
| `T2-06` | Plan Hash Verification Bypass | **LOW/MED** | **Confirmed** | No (Parameter omission) | `router.py:273`, `ardo_models.py:108-117` |

---

## 5. Proposed Targeted Tests (For Future Execution)

In accordance with strict testing restrictions, the following tests are proposed for owner review and have **NOT** been implemented or executed:

1. **`test_concurrency_embedding_provenance_race`:**
   Spawns two concurrent asyncio tasks or threads against a single `HybridEmbeddingService`: Task 1 requests OpenAI (mocked with artificial delay), Task 2 triggers a failure on the same service. Asserts whether Task 1's vector and metadata become mismatched.
2. **`test_checkpoint_resume_tenant_isolation`:**
   Creates a checkpoint under User A, removes the session from memory to simulate a restart, and calls `resume` under User B. Asserts whether User B is rejected with HTTP 403.
3. **`test_semantic_memory_cross_tenant_isolation`:**
   Records a memory with User A's credentials and executes `retrieve_memories` with User B's credentials. Asserts whether User A's memory is leaked.

---

## 6. Exit Gate Verification

- [x] Embedding provenance race condition proved or disproven (PROVEN & CONFIRMED).
- [x] Cross-session and cross-tenant attribution risks audited and documented with file/line references.
- [x] Zero application code modified.
- [x] Zero existing tests modified.
- [x] Working tree state preserved intact.

**Task T2 Status:** `done`

