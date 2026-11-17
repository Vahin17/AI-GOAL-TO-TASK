# ARDO — T7 Owner Review: Security Verification & Evidence Reconciliation

**Date:** 2026-11-16  
**Auditor:** ARDO Architecture & Security Review (Read-Only Audit)  
**Branch:** `stabilization-2-closure`  
**Base Commit / Current HEAD:** `914d61a05b90c12679632b8c70c73667f5ae4539`  
**Target Milestone:** Milestone M3 (Phase 3 Security Sign-Off)  
**Audit Purpose:** Independent, repository-grounded verification of Task T7 implementation against the T7 authorization scope and completion claims prior to owner approval.  
**Audit Mode:** STRICTLY READ-ONLY (Zero code changes, zero test modifications, zero file discards, zero git commits/pushes).  

---

## 1. Executive Summary & Recommended Owner Decision

A strict, read-only audit of the working tree, source code, and test suites was conducted to evaluate the completion claims made in `docs/roadmap/evidence/T7_SECURITY_APPROVAL_HARDENING_REPORT.md`.

### Recommended Owner Decision: **HOLD MILESTONE M3 APPROVAL**

**Rationale:**
While Task T7 made substantial, high-value progress (introducing atomic `EmbeddingResult`, adding session ownership checks to HTTP routes, enforcing WebSocket ticket bindings, and removing singleton browser fallbacks), the audit identified **four architectural defects and evidence gaps** where the security boundary either fails open internally, permits unscoped data retrieval, or lacks required data minimization:

1. **DEFECT 1 (Checkpoint Scheduler Bypass):** `ARDOScheduler.resume()` uses `if user_id is not None and chk_user is not None and chk_user != user_id:`. If an internal caller invokes `resume()` with `user_id=None` or if a checkpoint lacks a `user_id` (`chk_user=None`), ownership verification is completely bypassed.
2. **DEFECT 2 (Unscoped Semantic Memory Leakage in Planning):** `backend/ai/orchestrator.py:72` calls `rag_service.get_relevant_context_for_goal(sanitized_goal)` without passing `user_id`. In `SemanticMemoryStore.retrieve_memories()`, `if user_id:` evaluates to `False`, executing an **unscoped cross-tenant query** that retrieves and injects past execution memories belonging to ANY user into the active LLM planning prompt.
3. **DEFECT 3 (Inspection Endpoint Data Minimization Deficit):** `GET /orchestrate/{session_id}/inspect` calls `EnvironmentPerceptionEngine.inspect()`, which extracts `node.value` for all interactive elements in `_DOM_DISTILLATION_SCRIPT` without masking `type="password"` or sensitive credential fields. Plaintext secrets entered into form fields are returned across the wire.
4. **DEFECT 4 (Cached Plan Hash & Fail-Open on None in Approval API):** In `POST /orchestrate/{session_id}/approve/{task_id}`, the router checks `if expected_hash and body.plan_hash.strip() != expected_hash:`. If `session.graph.plan_hash` is `None` or empty, validation is bypassed. Furthermore, the endpoint compares against cached `session.graph.plan_hash` rather than dynamically recomputing via `session.graph.recompute_plan_hash()`.

Milestone M3 should be placed on **HOLD** until a targeted **T7.1 Remediation Pass** resolves these four specific issues. Task T8 (Live Acceptance Trials) must remain unauthorized.

---

## 2. Finding-by-Finding Security Verification

### A. Finding T2-02 — Checkpoint Ownership & Resumption
**Inspected Files:**
- `backend/orchestration/scheduler.py` (lines 368–461)
- `backend/orchestration/checkpoint_manager.py` (lines 124–215, 549–554)
- `backend/orchestration/runtime_engine.py` (lines 1335–1349, 1505–1525)
- `backend/api/router.py` (lines 401–454)

#### Inspection Findings:
1. **HTTP API Route (`router.py:401–425`):**
   - Requires `current_user: Dict[str, Any] = Depends(get_current_user)`.
   - Checks active in-memory session: `if active_session and (not active_session.user_id or active_session.user_id != current_user["id"]): raise HTTPException(403)`.
   - Checks database checkpoint: `if checkpoint: chk_user = checkpoint.get("user_id"); if not chk_user or chk_user != current_user["id"]: raise HTTPException(403)`.
   - **Status:** **VERIFIED** at the FastAPI HTTP boundary. Fails closed against missing user, missing checkpoint owner, and mismatched owner.
2. **Internal Scheduler Boundary (`scheduler.py:390–396`):**
   ```python
   chk_user = checkpoint.get("user_id")
   if user_id is not None and chk_user is not None and chk_user != user_id:
       raise PermissionError(f"Cross-user checkpoint resume forbidden: checkpoint owned by {chk_user}, requested by {user_id}")

   if existing and user_id is not None and existing.user_id is not None and existing.user_id != user_id:
       raise PermissionError(f"Cross-user session resume forbidden: session owned by {existing.user_id}, requested by {user_id}")
   ```
   - **Defect Identified:** The condition requires `user_id is not None` AND `chk_user is not None`.
     - Case 1: If caller passes `user_id=None` (the default parameter), both checks evaluate to `False`, bypassing ownership validation entirely.
     - Case 2: If caller passes `user_id="alice"` and the checkpoint is an unowned/legacy checkpoint (`chk_user=None`), `chk_user is not None` evaluates to `False`, allowing Alice to resume an unowned/legacy checkpoint without restriction.
     - Case 3: There is no separate, explicit, restricted authorization path for "system-owned" checkpoints; `user_id` simply defaults to `None`.
3. **Session Retrieval (`scheduler.py:452–460`):**
   ```python
   if user_id is not None and (not session.user_id or session.user_id != user_id):
       return None
   ```
   - When `user_id` is passed, it fails closed against null/mismatched `session.user_id`. When `user_id` is `None`, it returns the session unconditionally.

**Status for T2-02:** **DEFECT IDENTIFIED** (HTTP API is protected, but internal scheduler method `ARDOScheduler.resume()` fails open when `user_id=None` or `chk_user=None`).

---

### B. Finding T2-03 — Semantic Memory Tenant Isolation
**Inspected Files:**
- `backend/knowledge/semantic_memory.py` (lines 180–235, 290–445, 457–515)
- `backend/knowledge/rag_service.py` (lines 250–283)
- `backend/ai/orchestrator.py` (lines 28–100)
- `backend/orchestration/runtime_engine.py` (lines 1350–1475)
- `backend/api/router.py` (lines 165–260, 690–710)

#### Inspection Findings:
1. **Memory Persistence (`semantic_memory.py:180–235`):**
   - `record_memory(..., user_id: Optional[str] = None)` persists `user_id` inside `item.metadata["user_id"]`.
   - `RuntimeEngine._persist_semantic_memory()` correctly passes `user_id=self.user_id` to `rag.record_plan_outcome()`.
   - **Status:** **VERIFIED** on the persistence path.
2. **Direct Memory Search Endpoint (`router.py:695–705`):**
   - `GET /knowledge/memories` requires `Depends(get_current_user)` and passes `user_id=current_user["id"]`.
   - **Status:** **VERIFIED** on direct memory queries.
3. **Planner RAG Retrieval Path (`orchestrator.py:72` & `router.py:191, 240`):**
   - `POST /orchestrate` authenticates `current_user`, but line 240 calls `plan = await generate_task_graph(request.goal)` **without passing `user_id`**.
   - `POST /orchestrate/plan` (line 191) has no authentication dependency and calls `generate_task_graph(request.goal)` without `user_id`.
   - In `backend/ai/orchestrator.py:72`:
     ```python
     rag_context, provenance_obj = rag_service.get_relevant_context_for_goal(sanitized_goal)
     ```
     `user_id` is omitted, defaulting to `None`.
   - In `backend/knowledge/rag_service.py:273`:
     `self.memory_store.retrieve_memories(..., user_id=user_id)` passes `user_id=None`.
   - In `backend/knowledge/semantic_memory.py:357–359`:
     ```python
     if user_id:
         where_clauses.append("metadata->>'user_id' = %s")
         filter_params.append(user_id)
     ```
     Because `user_id` is `None`, `metadata->>'user_id' = %s` is **NOT** added to the SQL query.
   - **Impact:** When the LLM generates a plan, `retrieve_memories` runs an **unfiltered query across all tenants**, retrieving confidential memories, internal URLs, and task summaries belonging to other users and injecting them into the prompt.
4. **Legacy Ownerless Records:**
   - In `_retrieve_memories_tsvector()`, when `user_id` is provided, `metadata->>'user_id' = %s` excludes legacy records lacking `user_id`. When `user_id` is `None`, legacy records leak to all callers.

**Status for T2-03:** **DEFECT IDENTIFIED** (Planning pipeline does not thread `user_id` into `RAGService`, resulting in unscoped cross-tenant memory injection into LLM prompts).

---

### C. Finding T2-04 — WebSocket Ticket Lifecycle & Authorization
**Inspected Files:**
- `backend/core/security.py` (lines 158–198)
- `backend/api/auth.py` (lines 908–969)
- `backend/api/router.py` (lines 790–854)

#### Inspection Findings:
1. **Ticket Bound to Authenticated User:**
   - In `auth.py:964`: `ws_ticket_manager.issue_ticket(user_id=current_user["id"], session_id=payload.session_id)`.
   - Ticket is tied to `current_user["id"]`. **VERIFIED**.
2. **Ticket Bound to Intended Session:**
   - Ticket creation verifies caller owns `session_id` via `ARDOScheduler._sessions`, `_session_history`, or MongoDB `task_runtime_state`. Rejects other users' sessions with HTTP 403.
   - In `core/security.py:188`: `redeem_ticket()` verifies `if sid != expected_session_id: return None`. **VERIFIED**.
3. **Expired Tickets Rejected:**
   - In `core/security.py:186`: `if time.time() > expires_at: return None`. **VERIFIED** in code.
4. **Reused / Replayed Tickets Rejected (Single-Use):**
   - In `core/security.py:182`: `data = self._tickets.pop(ticket, None)`. The ticket is popped on first check; any second attempt returns `None`. **VERIFIED** in code (and tested in `test_phase4_1_deployment_readiness_gate.py:346`).
5. **Invalid Tickets Rejected:**
   - In `router.py:815`: `if not user_id: await websocket.close(code=1008); return`. **VERIFIED**.
6. **Session Mismatch & Missing Session Owner Rejected:**
   - In `router.py:832`: `if not active_session or not active_session.user_id or active_session.user_id != user_id: await websocket.close(code=1008); return`.
   - Fails closed if session is missing, unowned, or owned by another user. **VERIFIED**.
7. **Ticket Secrets Excluded from Logs:**
   - `auth.py` does not log the generated ticket.
   - `router.py` only logs `logger.info(f"[WS] Client connected to session: {session_id} (user={user_id})")`. Ticket token is never written to logs. **VERIFIED**.

**Status for T2-04:** **VERIFIED** in implementation. (Evidence test suite omissions documented in Section 3).

---

### D. Finding T2-06 — Approval Plan Hash Validation
**Inspected Files:**
- `backend/api/router.py` (lines 305–399)
- `backend/orchestration/dag_builder.py` (lines 166–201, 374–385)
- `backend/orchestration/runtime_engine.py` (lines 557–586)
- `backend/core/auth_db.py` (lines 280–330)

#### Inspection Findings:
1. **Matching Hash Approval:**
   - `POST /orchestrate/{session_id}/approve/{task_id}` with correct hash approves task and records in `AuthDatabase`. **VERIFIED**.
2. **Missing, Null, or Empty Hash Rejection:**
   - In `router.py:325`: `if not body.plan_hash or not body.plan_hash.strip(): raise HTTPException(400)`. **VERIFIED**.
3. **Mismatched Hash Rejection:**
   - In `router.py:330`: `if expected_hash and body.plan_hash.strip() != expected_hash: raise HTTPException(400)`. **VERIFIED** when `expected_hash` is present.
4. **Defect in Approval Endpoint Hash Comparison (`router.py:324–334`):**
   - **Fail-Open on Missing Expected Hash:** Line 330 checks `if expected_hash and ...`. If `session.graph.plan_hash` is `None` or empty, `expected_hash` is falsy, and **the mismatch check is skipped**. Any non-empty string in `body.plan_hash` is accepted!
   - **Cached vs. Dynamic Hash:** Line 324 reads cached `expected_hash = getattr(session.graph, "plan_hash", None)`. If the graph was mutated in memory after creation, the approval endpoint does not detect the change because it does not call `session.graph.recompute_plan_hash()`.
5. **Runtime Engine Defense-in-Depth (`runtime_engine.py:557–586`):**
   - When a gated task is dispatched, `RuntimeEngine` calls `current_plan_hash = self.graph.recompute_plan_hash(self.goal_id)`.
   - It checks `auth_db.is_plan_approved(session_id, current_plan_hash, task_id)`.
   - If the plan was mutated, the recomputed hash does not match the approval record, and `RuntimeEngine` quarantines the task branch with `event_type="task_approval_invalidated"`. **VERIFIED**.

**Status for T2-06:** **DEFECT IDENTIFIED** at the API endpoint level (`expected_hash` fail-open on `None` and cached vs recomputed discrepancy), mitigated at the runtime execution level by `RuntimeEngine`.

---

### E. Finding T4-SEC-01 — Inspection Endpoint Security & Data Minimization
**Inspected Files:**
- `backend/api/router.py` (lines 127–160)
- `backend/execution/browser/environment_perception.py` (lines 40–165, 241–310)

#### Inspection Findings:
1. **Authentication & Ownership:**
   - `GET /orchestrate/{session_id}/inspect` requires `current_user: Dict[str, Any] = Depends(get_current_user)`.
   - Verifies `if not active_session or not active_session.user_id or active_session.user_id != current_user["id"]: raise HTTPException(403)`. **VERIFIED**.
2. **Global Singleton Fallback Removal:**
   - The insecure fallback to `ToolRegistry.get_tool("browser")` was removed completely. Only `active_session.browser_adapter` is inspected. **VERIFIED**.
3. **Response Minimization & Sensitive Data Redaction:**
   - In `EnvironmentPerceptionEngine._DOM_DISTILLATION_SCRIPT`:
     ```javascript
     const value = (node.value !== undefined && tag !== 'button') ? String(node.value) : null;
     const inputType = (tag === 'input') ? (node.type || 'text') : null;
     ```
   - Neither `_DOM_DISTILLATION_SCRIPT` nor `inspect_session_page()` masks password or sensitive input fields.
   - If a page has `<input type="password" value="Secret123">`, the plaintext value `"Secret123"` is extracted, serialized into `PageSnapshot`, and returned in the HTTP response.
   - No field-level redaction exists for sensitive selectors (`password`, `secret`, `token`, `ssn`, `card`).
4. **Sensitive Data Logging:**
   - The route handler does not print or log the snapshot content. However, unredacted credentials exist in the serialized response payload.

**Status for T4-SEC-01:** **DEFECT IDENTIFIED** (Authentication and tenant checks verified; response minimization and password/credential value masking were NOT implemented).

---

### F. Finding T2-01 — Atomic Embedding Provenance
**Inspected Files:**
- `backend/knowledge/embeddings.py` (lines 25–70, 425–500)
- `backend/knowledge/postgres_vector_store.py` (lines 200–215, 320–335)
- `backend/knowledge/semantic_memory.py` (lines 215–225, 305–320)

#### Inspection Findings:
1. **Atomic Provenance Binding:**
   - `EmbeddingResult` is a `@dataclass(frozen=True)` holding `vector`, `provider`, `model`, `dimension`, and `embedding_space`.
   - Both `embed_query_with_provenance()` and `embed_texts_with_provenance()` generate the vector and seal the generating provider's metadata inside the same immutable instance. **VERIFIED**.
2. **Provider Switching Race Prevention:**
   - In `HybridEmbeddingService`, if OpenAI succeeds, the OpenAI metadata is sealed with the vector. If OpenAI fails and falls back to local deterministic embedding, the local metadata is sealed with the local vector.
   - Vector dimension and `embedding_space` cannot desynchronize. **VERIFIED**.
3. **Call Site Adoption:**
   - `postgres_vector_store.py` and `semantic_memory.py` write and read `res.embedding_space` directly from `EmbeddingResult`. No production caller queries mutable `service.active_provider` or `service.embedding_space` post-generation. **VERIFIED**.
4. **Mutable `List[float]` in Frozen Dataclass:**
   - `vector: List[float]` is shallowly frozen. Field reassignment (`res.vector = ...`) is blocked by Python's dataclass runtime.
   - The list itself could theoretically be modified in place (`res.vector[0] = 0.0`), but audit of all call sites in `postgres_vector_store.py` and `semantic_memory.py` confirms vectors are immediately passed to `pgvector.Vector(res.vector)` or serialized without in-place mutation.
   - Practical risk in current codebase: **NEGLIGIBLE / BENIGN**, though conversion to `Tuple[float, ...]` would provide strict deep immutability.

**Status for T2-01:** **VERIFIED**.

---

## 3. Test Evidence Coverage Matrix

The eight tests in `backend/tests/test_t7_security_hardening.py` and existing test suites were audited against each required security scenario:

| Security Scenario | Test Function | Test File | Genuinely Exercises Boundary? | Status |
| :--- | :--- | :--- | :---: | :---: |
| **Atomic embedding provenance under concurrent fallback** | `test_t2_01_atomic_embedding_provenance_under_concurrency` | `test_t7_security_hardening.py` | Yes (simulates thread concurrency & fallback) | **VERIFIED** |
| **Stores capture atomic provenance from EmbeddingResult** | `test_t2_01_memory_and_vector_store_capture_atomic_provenance` | `test_t7_security_hardening.py` | Yes (verifies metadata seals `embedding_space`) | **VERIFIED** |
| **Checkpoint resume with different owner rejected (403)** | `test_t2_02_checkpoint_ownership_validation_fails_closed` | `test_t7_security_hardening.py` | Yes (User B cannot resume User A's checkpoint) | **VERIFIED** |
| **Checkpoint resume with null/missing owner rejected (403)** | `test_t2_02_checkpoint_ownership_validation_fails_closed` | `test_t7_security_hardening.py` | Yes (resuming unowned checkpoint returns 403) | **VERIFIED** |
| **Checkpoint resume with legitimate owner succeeds (200)** | `test_t2_02_checkpoint_ownership_validation_fails_closed` | `test_t7_security_hardening.py` | Yes (Owner A resumes own checkpoint) | **VERIFIED** |
| **Scheduler-level `resume(user_id=None)` bypass** | *None* | *None* | No (never tested with `user_id=None`) | **NOT VERIFIED / DEFECT** |
| **Semantic memory cross-tenant query isolation** | `test_t2_03_semantic_memory_tenant_isolation_in_memory` | `test_t7_security_hardening.py` | Yes (User A cannot see User B's memory) | **VERIFIED** |
| **Semantic memory API search isolation (401/403/scoping)** | `test_t2_03_api_search_memories_enforces_tenant_isolation` | `test_t7_security_hardening.py` | Yes (Bob search returns 0 results for Alice memory) | **VERIFIED** |
| **Planner RAG memory retrieval threads `user_id`** | *None* | *None* | No (orchestrator omits `user_id`) | **NOT VERIFIED / DEFECT** |
| **Retrieval when identity is missing fails closed** | *None* | *None* | No (when `user_id=None`, query is unscoped) | **NOT VERIFIED / DEFECT** |
| **WebSocket ticket issuance requires session ownership (403)** | `test_t2_04_ws_ticket_issuance_and_handshake_authorization` | `test_t7_security_hardening.py` | Yes (Bob cannot issue ticket for Alice session) | **VERIFIED** |
| **WebSocket ticket for non-existent session rejected (404)** | `test_t2_04_ws_ticket_issuance_and_handshake_authorization` | `test_t7_security_hardening.py` | Yes | **VERIFIED** |
| **WebSocket handshake rejects expired ticket** | *Unit only* (`test_phase4_1_...py:346`) | `test_phase4_1_...py` | Partial (unit test checks `redeem_ticket`) | **NOT VERIFIED in T7** |
| **WebSocket handshake rejects reused/replayed ticket** | *Unit only* (`test_phase4_1_...py:346`) | `test_phase4_1_...py` | Partial (unit test checks `redeem_ticket`) | **NOT VERIFIED in T7** |
| **WebSocket handshake rejects session-mismatched ticket** | *None* | *None* | No | **NOT VERIFIED in T7** |
| **Approval rejects missing/empty plan hash (400)** | `test_t2_06_approve_task_rejects_missing_null...` | `test_t7_security_hardening.py` | Yes (missing & empty string return 400) | **VERIFIED** |
| **Approval rejects mismatched plan hash (400)** | `test_t2_06_approve_task_rejects_missing_null...` | `test_t7_security_hardening.py` | Yes (mismatched string returns 400) | **VERIFIED** |
| **Plan mutated after hash creation rejected at runtime** | `test_runtime_engine_verifies_cryptographic_approval` | `test_plan_approval.py` | Yes (tampered graph quarantined by engine) | **VERIFIED** |
| **Approval API rejects approval if graph mutated** | *None* | *None* | No (API compares with cached `graph.plan_hash`) | **NOT VERIFIED** |
| **Inspection endpoint requires auth (401)** | `test_t4_sec_01_inspect_endpoint_requires_auth...` | `test_t7_security_hardening.py` | Yes | **VERIFIED** |
| **Inspection endpoint verifies session ownership (403)** | `test_t4_sec_01_inspect_endpoint_requires_auth...` | `test_t7_security_hardening.py` | Yes (Bob cannot inspect Alice session) | **VERIFIED** |
| **Inspection response masks passwords/secrets** | *None* | *None* | No (unmasked `node.value` returned) | **NOT VERIFIED / DEFECT** |

---

## 4. Reconciliation of Reporting Discrepancies

### A. Live Integration Results (13 vs. 14)
- **Root Cause:**
  - In Task T6, live testing executed two dedicated suites: `test_t6_live_pgvector_integration.py` (7 tests) and `test_phase13_semantic_memory_live.py` (6 tests) = **13 tests**.
  - In Task T7, the live regression sweep included a third test file marked `@pytest.mark.live_db`: `test_candidate_playbook_persistence_integration.py` (1 test).
  - Executing all three suites with `--run-live` yields: $7 + 6 + 1 = \mathbf{14\text{ passed}}$ in 14.69s.
  - When run without `--run-live`, `test_candidate_playbook_persistence_integration.py` skips.
- **Reconciliation:** Both numbers are accurate for their respective scopes. T6 executed 13 tests across 2 files; T7 executed 14 tests across 3 files.

### B. Modified Tracked Files (26 vs. 29)
- **Root Cause:**
  - At the end of Task T5, there were 25 modified tracked files.
  - During early T7 analysis, 26 files were initially noted.
  - T7 implementation required modifying additional tracked files for ownership, DAG builder, and checkpointing:
    1. `backend/ai/gemini_provider.py`
    2. `backend/ai/gemini_rate_limiter.py`
    3. `backend/ai/orchestrator.py`
    4. `backend/api/auth.py`
    5. `backend/api/router.py`
    6. `backend/execution/browser/agent_contracts.py`
    7. `backend/execution/browser/browser_agent.py`
    8. `backend/execution/browser/network_guard.py`
    9. `backend/execution/browser/playwright_executor.py`
    10. `backend/execution/browser/semantic_verifier.py`
    11. `backend/knowledge/embeddings.py`
    12. `backend/knowledge/postgres_vector_store.py`
    13. `backend/knowledge/rag_service.py`
    14. `backend/knowledge/semantic_memory.py`
    15. `backend/orchestration/checkpoint_manager.py`
    16. `backend/orchestration/dag_builder.py`
    17. `backend/orchestration/execution_monitor.py`
    18. `backend/orchestration/runtime_engine.py`
    19. `backend/orchestration/scheduler.py`
    20. `backend/tests/conftest.py`
    21. `backend/tests/test_browser_call_reduction.py`
    22. `backend/tests/test_candidate_playbook_persistence_integration.py`
    23. `backend/tests/test_media_playback_verification.py`
    24. `backend/tests/test_phase13_semantic_memory_live.py`
    25. `backend/tests/test_phase15_acceptance_execution.py`
    26. `backend/tests/test_proactor_transport_resilience.py`
    27. `backend/tests/test_task_preconditions.py`
    28. `backend/tests/test_vector_store_truthfulness.py`
    29. `frontend/src/components/panels/ExecutionControl.tsx`
- **Reconciliation:** The authoritative count from `git status -s` is **29 modified tracked files**.

### C. Pass B / T3 Accounting (30 Passed, 1 Skipped)
- **Root Cause:**
  - Running the 5 regression suites (`test_candidate_playbook_persistence_integration.py`, `test_media_playback_verification.py`, `test_browser_call_reduction.py`, `test_proactor_transport_resilience.py`, `test_task_preconditions.py`):
    - Collected: 31 items.
    - Without `--run-live`: **30 passed, 1 skipped** in 1.36s. The single skipped test is `test_candidate_playbook_and_semantic_memory_live_persistence` which requires `@pytest.mark.live_db`.
    - With `--run-live`: **31 passed, 0 skipped** in 7.81s.
- **Reconciliation:** Exact accounting is 30 passed and 1 skipped offline; 31 passed when live DB is enabled.

### D. Base Commit vs. Actual HEAD
- **Current HEAD (`git rev-parse HEAD`):** `914d61a05b90c12679632b8c70c73667f5ae4539`
- **Base Commit:** `914d61a05b90c12679632b8c70c73667f5ae4539`
- **Reconciliation:** Current HEAD matches the base commit exactly. Zero commits, merges, or branch switches have been performed. All changes remain in the working tree.

### E. Untracked Files Inventory
There are currently **6 untracked Python test/utility files** plus the documentation roadmap directory:
1. `backend/core/retry_utils.py`
2. `backend/tests/test_embedding_dimension_compatibility.py`
3. `backend/tests/test_provider_recovery_t5.py`
4. `backend/tests/test_t4_verification_contracts.py`
5. `backend/tests/test_t6_live_pgvector_integration.py`
6. `backend/tests/test_t7_security_hardening.py`
7. `docs/roadmap/`

---

## 5. Summary of Identified Defects & Recommended Remediation Scope

The following items should form the scope of an authorized **Task T7.1 Targeted Remediation Pass**:

| Defect ID | Severity | File(s) | Description | Required Remediation |
| :--- | :---: | :--- | :--- | :--- |
| **DEF-T7-01** | HIGH | `backend/orchestration/scheduler.py` | `resume()` conditional `if user_id is not None and chk_user is not None` fails open if `user_id` or `chk_user` is None. | Change to fail-closed: require `user_id` parameter; if `not chk_user or chk_user != user_id`, raise `PermissionError`. For internal system tasks, provide explicit `system_override: bool = False`. |
| **DEF-T7-02** | HIGH | `backend/ai/orchestrator.py`, `backend/api/router.py`, `backend/knowledge/semantic_memory.py` | Planning calls `get_relevant_context_for_goal()` without `user_id`, causing `retrieve_memories()` to run unscoped cross-tenant query. | Accept `user_id` in `generate_task_graph()`; forward `current_user["id"]` from `POST /orchestrate`; in `SemanticMemoryStore`, if `user_id` is missing/None on a tenant query, fail closed (return empty list rather than unscoped query). |
| **DEF-T7-03** | MEDIUM | `backend/execution/browser/environment_perception.py` | `_DOM_DISTILLATION_SCRIPT` extracts `node.value` for `type="password"` and credential inputs in plaintext. | Redact `value` (e.g. `[REDACTED]`) when `inputType === 'password'` or element selector matches `password|secret|token|ssn|credit`. |
| **DEF-T7-04** | MEDIUM | `backend/api/router.py` | `approve_task` skips validation if `expected_hash` is None/empty, and compares against cached hash rather than dynamic recomputation. | Enforce `if not expected_hash: expected_hash = session.graph.recompute_plan_hash()`; reject if `expected_hash` cannot be determined; strictly compare `body.plan_hash == expected_hash`. |

---

## 6. Audit Conclusion & Next Steps

1. Task T7 made genuine architectural improvements and resolved the core concurrency race in embedding generation (T2-01).
2. However, Milestone M3 cannot be certified because four security boundaries contain fail-open or data-leakage defects.
3. **Execution is stopped.** No application code or tests have been altered.
4. Awaiting Owner decision on authorizing **Task T7.1 Targeted Remediation Pass**.
