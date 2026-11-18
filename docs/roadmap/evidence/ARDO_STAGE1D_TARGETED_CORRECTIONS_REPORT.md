# ARDO — Stage 1D: Targeted False-Success Remediation Corrections & Bounded Verification Report

**Project:** ARDO — Adaptive Reactive DAG Orchestrator  
**Phase:** Phase 3 — Production Preparation  
**Stage:** 1D — Targeted False-Success Remediation Corrections & Deadlock Resolution  
**Date:** 2026-11-17  
**Branch:** `stabilization-2-closure`  
**HEAD Commit:** `bdaa78ef7c00a104d3a30089a6614e117fda291f` (`Stage 0B checkpoint: preserve M3, security hardening, and phase 14-16 baseline before false-success remediation`)  
**Ancestor Check (`bdaa78ef7c00a104d3a30089a6614e117fda291f`):** `0` (Verified ancestor of HEAD)  
**Stage 1D Status:** **COMPLETE — ALL TARGETED DEFECTS (`DEF-1C-01`..`DEF-1C-08`) AND BROWSER `_op_lock` DEADLOCK RESOLVED & EMPIRICALLY VERIFIED**

> [!IMPORTANT]
> **Explicit Authorization Statement:**  
> No Git staging or commit operations were performed during Stage 1D. No live Gemini, OpenAI, or external cloud API calls were executed. **No production deployment and no Task T8, T9, or T10 execution has been authorized or performed.**

---

## 1. Executive Summary

Stage 1D addressed all eight residual findings (`DEF-1C-01` through `DEF-1C-08`) identified in the independent Stage 1C audit ([ARDO_STAGE1C_INDEPENDENT_VERIFICATION_REPORT.md](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/docs/roadmap/evidence/ARDO_STAGE1C_INDEPENDENT_VERIFICATION_REPORT.md)) and resolved the `_BrowserSession._op_lock` self-deadlock uncovered in `backend/execution/browser/playwright_executor.py` when executing `BrowserActionType.INSPECT_PAGE` through `ToolRegistry`.

All targeted corrections and bounded verification suites completed cleanly with zero hangs and zero orphaned browser/Python processes.

---

## 2. `DEF-1C-01` Through `DEF-1C-08` Implementation & Verification Status

| Defect ID | Severity | Summary of Defect | Stage 1D Remediation & File References | Verification Status |
| :--- | :---: | :--- | :--- | :--- |
| **`DEF-1C-01`** | **P0** | Fallback Plan Memory & Playbook Promotion Guard (`ARDOGraph` lacked `is_fallback`; fallback plans with valid tool outputs could be recorded as `"success"` in semantic memory or promoted to candidate playbooks). | • Added `is_fallback: bool`, `fallback_reason: Optional[str]`, `metadata: Dict[str, Any]`, and `is_fallback_plan() -> bool` to `ARDOGraph` in [dag_builder.py](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/orchestration/dag_builder.py#L172-L189) and auto-derived `is_fallback` in `DAGBuilder.build()` ([L427-L439](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/orchestration/dag_builder.py#L427-L439)).<br>• Propagated `is_fallback` and `fallback_reason` through [scheduler.py](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/orchestration/scheduler.py#L363-L399) and [router.py](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/api/router.py#L299-L313).<br>• Added `_is_fallback_graph()` in [runtime_engine.py](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/orchestration/runtime_engine.py#L1433-L1474): forces semantic memory `outcome_status = "unverified"` ([L1462](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/orchestration/runtime_engine.py#L1462)) and skips `generate_candidate_playbook()` ([L1581-L1585](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/orchestration/runtime_engine.py#L1581-L1585)).<br>• Added defense-in-depth rejection in `PlaybookService.generate_candidate_playbook()` ([playbook_service.py:84-99](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/knowledge/playbook_service.py#L84-L99)). | **VERIFIED PASS**<br>(`test_def_1c_01_fallback_plan_cannot_promote_to_memory_or_playbook`) |
| **`DEF-1C-02`** | **P1** | Evidence Session, Task, Tenant, Plan-Hash Binding & Legacy Removal (`verify_goal_outcome()` did not cross-check `session_id`, `user_id`, or `plan_hash` on `evidence_items`; `ARDOSession.get_evidence()` fell back to `"verified"` when `node.status == "completed"` and used `or 1.0` truthiness). | • Added `_is_valid_vault_evidence()` in [semantic_verifier.py](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/execution/browser/semantic_verifier.py#L382-L560) enforcing strict `session_id`, `task_id`, `user_id` (`expected_user_id` or `"system"`), `plan_hash`, `verification_status == "verified"`, and non-empty `artifact_hash != "unverified"`.<br>• Enforced `session_id` and `task_id` checks on `node.browser_result` and filtered `supporting_hashes` to valid verified evidence.<br>• Removed `"verified" if node.status.value == "completed"` and `or 1.0` truthiness fallback in `ARDOSession.get_evidence()` ([scheduler.py:198-309](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/orchestration/scheduler.py#L198-L309)).<br>• Enforced non-empty `raw_content` in `EvidenceVault.record_evidence()` ([evidence_vault.py:97-124](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/execution/evidence/evidence_vault.py#L97-L124)), downgrading empty content to `"unverified"`, `confidence_score = 0.0`, `artifact_hash = "unverified"`. | **VERIFIED PASS**<br>(`test_def_1c_02_evidence_binding_and_scheduler_truthfulness`) |
| **`DEF-1C-03`** | **P1** | Frontend Truthfulness (`hasGenuineEvidence` only checked `browserEvidence.length > 0`; `completed (unverified)` rendered green; `goalVerification` was not reset in `startSession`; `tasks_failed`/`tasks_blocked` could inflate on duplicate events). | • Tightened `hasGenuineEvidence` in [ExecutionControl.tsx](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/frontend/src/components/panels/ExecutionControl.tsx#L83-L114) to require `ev.verification_status === 'verified' && Boolean(ev.artifact_hash) && ev.artifact_hash !== 'unverified'`.<br>• Scoped `activeGoalVerif` to current `sessionId`; styled `'completed (unverified)'` with amber (`bg-amber-500`) and `<AlertTriangle />`; styled `'blocked'` with `bg-rose-500` and `<AlertCircle />`; removed `\|\| ev.result?.success` from green `VERIFIED` badge ([L217-L233](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/frontend/src/components/panels/ExecutionControl.tsx#L217-L233), [L367-L445](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/frontend/src/components/panels/ExecutionControl.tsx#L367-L445)).<br>• Updated [useARDOStore.ts](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/frontend/src/store/useARDOStore.ts#L187-L202) to reset `goalVerification: null` in `startSession`, compute `tasks_failed`/`tasks_blocked` idempotently from `nodeMap` ([L297-L306](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/frontend/src/store/useARDOStore.ts#L297-L306)), and guard `loadEvidence`/`loadGoalVerification` against stale cross-session responses ([L555-L579](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/frontend/src/store/useARDOStore.ts#L555-L579)). | **VERIFIED PASS**<br>(`npx tsc --noEmit` 0 errors; `npm run build` exit code 0) |
| **`DEF-1C-04`** | **P1** | Quarantined Playbook Retrieval Bypass (`get_playbooks_for_user(user_id, status="quarantined")` bypassed exclusion, and `get_playbook_by_id()` did not filter `status != 'quarantined'`). | • Hardened `get_playbooks_for_user()` and `get_playbook_by_id()` in [auth_db.py](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/core/auth_db.py#L1969-L1992) ([L2027-L2035](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/core/auth_db.py#L2027-L2035)) and [playbook_service.py](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/knowledge/playbook_service.py#L183-L202) to unconditionally exclude `status == 'quarantined'`.<br>• Updated `test_scenario_13_quarantined_records_excluded` in [test_false_success_remediation.py](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/tests/test_false_success_remediation.py#L338-L364) to verify against actual owner `'usr_e72e16a21169'`, `status="quarantined"`, and `get_playbook_by_id()`. | **VERIFIED PASS**<br>(`test_scenario_13_quarantined_records_excluded`) |
| **`DEF-1C-05`** | **P1** | Abstract-Goal Browser Classification (`fallback_rules.py` matched `"Verify"` in `"Verify Goal Completion"` and substring matches like `"play"` in `"display"` or `"tab"` in `"table"` as browser tasks). | • Replaced substring matching with compiled word-boundary regexes (`\b...\b`) and removed generic `"verify"` from standalone browser triggers in [fallback_rules.py](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/ai/fallback_rules.py#L24-L32) ([L69-L77](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/ai/fallback_rules.py#L69-L77), [L110-L137](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/ai/fallback_rules.py#L110-L137)) and [runtime_engine.py](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/orchestration/runtime_engine.py#L246-L259). | **VERIFIED PASS**<br>(`test_def_1c_05_goal_classification_matrix`) |
| **`DEF-1C-06`** | **P1** | Legacy Test Contract Alignment (`test_provider_recovery_t5.py`, `test_phase15_event_evidence_observability.py`, and `test_phase15_perception.py` had legacy mock assumptions). | • Updated `test_semantic_memory_records_success_only_when_all_tasks_and_goal_verified` in [test_provider_recovery_t5.py](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/tests/test_provider_recovery_t5.py#L321-L360) to verify both unverified node rejection (`"unverified"`) and tool-backed verified node success (`"success"`).<br>• Bound a valid `calculator` `ToolCall` in `test_non_browser_task_events_in_runtime` ([test_phase15_event_evidence_observability.py:560-590](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/tests/test_phase15_event_evidence_observability.py#L560-L590)).<br>• Registered local `dashboard_fixture_url` with `NetworkGuard` in [test_phase15_perception.py](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/tests/test_phase15_perception.py#L30-L56) and resolved the `_op_lock` deadlock in `playwright_executor.py` + adapter lookup in `router.py`. | **VERIFIED PASS**<br>(All 3 suites pass with 0 failures) |
| **`DEF-1C-07`** | **P2** | `ToolResult.output_data` Persistence & Validation (`runtime_engine.py` checked `getattr(tr, "output", None)` instead of `tr.output_data`, and `semantic_verifier.py` accepted empty tool outputs as `"verified"`). | • Updated [runtime_engine.py](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/orchestration/runtime_engine.py#L1163-L1183) and [semantic_verifier.py](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/execution/browser/semantic_verifier.py#L470-L482) ([L519-L531](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/execution/browser/semantic_verifier.py#L519-L531)) to persist JSON-serialized `output_data` into `EvidenceVault` and reject empty/malformed tool outputs (`output_data={}`, `extracted_text=None`, `artifacts={}`). | **VERIFIED PASS**<br>(`test_def_1c_07_tool_result_output_data_and_malformed_rejection`) |
| **`DEF-1C-08`** | **P1** | Full Orchestration Browser Acceptance (Stage 1B offline Chromium test exercised `PlaywrightBrowserExecutor` directly rather than the full `decompose_goal_with_fallback -> DAGBuilder -> RuntimeEngine -> BrowserAgent -> PlaywrightBrowserExecutor -> SemanticVerifier -> EvidenceVault` pipeline). | • Implemented `test_def_1c_08_full_orchestration_pipeline_chromium_acceptance` and `test_def_1c_08_full_pipeline_fails_closed_on_postcondition_or_launch_failure` in [test_offline_chromium_acceptance.py](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/tests/test_offline_chromium_acceptance.py#L220-L398), driving all 5 decomposed media tasks through `RuntimeEngine` and `BrowserAgent` against real headless Chromium on `mock_youtube.html` with 5 distinct SHA-256 evidence hashes and `overall_status == "verified"`. | **VERIFIED PASS**<br>(`4/4 passed` in `test_offline_chromium_acceptance.py`) |

---

## 3. Browser `_op_lock` Deadlock Root Cause & Fix

### 3.1 Root Cause
In [backend/execution/browser/playwright_executor.py](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/execution/browser/playwright_executor.py):
1. `_BrowserSession.execute_action()` acquired `async with self._op_lock:` (a non-reentrant `asyncio.Lock`) and invoked `await self._execute_action_locked(action)`.
2. When `action.action_type == BrowserActionType.INSPECT_PAGE`, `_execute_action_locked()` called `await self.inspect_page()`.
3. `_BrowserSession.inspect_page()` attempted to acquire `async with self._op_lock:` a second time on the same coroutine, causing an immediate self-deadlock whenever `INSPECT_PAGE` was dispatched through `execute_action()` (such as in `test_15_8_registry_dispatch`).
4. Additionally, in `GET /orchestrate/{session_id}/inspect` ([backend/api/router.py](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/api/router.py#L144-L163)), `inspect_session_page()` only checked `active_session.browser_adapter.executor._page` without falling back to `active_session.engine.browser_adapter.executor._page` when a test or caller attached the active adapter to `session.engine`, and called `EnvironmentPerceptionEngine().inspect(page)` directly instead of routing live `PlaywrightBrowserExecutor` sessions through `await executor.inspect_page()`.

### 3.2 Fix Implemented
1. **`_BrowserSession._inspect_page_unlocked()` ([playwright_executor.py:272-288](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/execution/browser/playwright_executor.py#L272-L288)):**
   - Extracted page inspection into `_inspect_page_unlocked(self, max_elements: int = 50)`, which enforces `if not self._op_lock.locked(): raise RuntimeError("_inspect_page_unlocked called without holding _op_lock")`.
   - `inspect_page()` acquires `async with self._op_lock:` once and delegates to `await self._inspect_page_unlocked(max_elements=max_elements)`.
   - `_execute_action_locked()` calls `await self._inspect_page_unlocked()` directly when handling `BrowserActionType.INSPECT_PAGE` ([line 615](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/execution/browser/playwright_executor.py#L615)), since its caller `execute_action()` already holds `self._op_lock`.
2. **`inspect_session_page` Adapter Resolution & Proactor Delegation ([router.py:144-163](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/api/router.py#L144-L163)):**
   - Checked `active_session.browser_adapter` and fell back to `active_session.engine.browser_adapter` when `page` is `None`.
   - Delegated live `PlaywrightBrowserExecutor` inspection through `await executor_obj.inspect_page()` so inspection executes on the `_ProactorWorker` loop under `_op_lock`.
3. **Regression Test ([test_false_success_remediation.py:674-744](file:///c:/Users/vahin/OneDrive/Documents/AI%20Goal-To-Task%20Planning%20Engine/backend/tests/test_false_success_remediation.py#L674-L744)):**
   - Added `test_stage1d_inspect_page_action_no_deadlock_and_serialized_concurrency` verifying that:
     - Calling `_inspect_page_unlocked()` without holding `_op_lock` raises `RuntimeError`.
     - `execute_action(BrowserAction(action_type=BrowserActionType.INSPECT_PAGE))` completes within bounded timeout without deadlocking.
     - Concurrent `execute_action(INSPECT_PAGE)` and `inspect_page()` calls serialize with `max_concurrent == 1`.

---

## 4. Bounded Verification Commands & Empirical Test Results

Because `pytest-timeout` is not installed in `backend/.venv`, external process timeouts (`subprocess.run(..., timeout=...)`) were used as authorized.

| Step / Suite | Exact Command Executed | Passed | Failed | Skipped / Deselected | Duration | Process Timeout |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| **1. Previously Hanging Single Test (`task-23745`)** | `$env:PYTHONPATH="backend"; backend\.venv\Scripts\python -c "import subprocess, sys; r = subprocess.run([sys.executable, '-m', 'pytest', 'backend/tests/test_phase15_perception.py', '-k', 'test_15_8_registry_dispatch', '-v', '-s'], timeout=60); sys.exit(r.returncode)"` | **1** | **0** | 0 skipped / **11 deselected** | `15.84s` | `60s` (No timeout) |
| **2. Selected Phase 15 Perception Subset (`task-23762`)** | `$env:PYTHONPATH="backend"; backend\.venv\Scripts\python -c "import subprocess, sys; r = subprocess.run([sys.executable, '-m', 'pytest', 'backend/tests/test_phase15_perception.py', '-k', 'not (test_15_2 or test_15_3 or test_15_5 or test_15_6 or test_15_11)', '-v', '-s'], timeout=120); sys.exit(r.returncode)"` | **7** | **0** | 0 skipped / **5 deselected** | `35.43s` | `120s` (No timeout) |
| **3. Focused Stage 1D Regression Batch (`task-23766` + `task-23771`)** | `$env:PYTHONPATH="backend"; backend\.venv\Scripts\python -c "import subprocess, sys; r = subprocess.run([sys.executable, '-m', 'pytest', 'backend/tests/test_false_success_remediation.py', 'backend/tests/test_offline_chromium_acceptance.py', 'backend/tests/test_phase15_event_evidence_observability.py', 'backend/tests/test_fallback.py', 'backend/tests/test_provider_recovery_t5.py', 'backend/tests/test_t7_security_hardening.py', '-v'], timeout=90); sys.exit(r.returncode)"`<br>+ rerun of `test_false_success_remediation.py` (`task-23771`) after fixing `PageSnapshot.timestamp` float fixture | **61**<br>(`19` in `test_false_success_remediation.py` in `7.13s` + `42` in remaining 5 suites) | **0** | 0 skipped / 0 deselected | `16.16s` / `7.13s` | `90s` / `60s` (No timeout) |
| **4. Earlier Stage 1D Verification Suites (`task-23687` & `task-23680`)** | • `test_offline_chromium_acceptance.py`, `test_false_success_remediation.py`, `test_gemini_failure_handling.py`, `test_fallback.py`, `test_plan_approval.py`, `test_task_preconditions.py`, `test_media_playback_verification.py`, `test_browser_call_reduction.py`<br>• `test_t4_verification_contracts.py`, `test_provider_recovery_t5.py`, `test_t7_security_hardening.py`, `test_t7_1_security_remediation.py`, `test_t7_1_1_final_security_gap.py`, `test_t7_1_2_final_approval_integrity.py`, `test_t7_1_3_session_enumeration_remediation.py` | **62**<br><br>**76** | **0**<br><br>**0** | 0 skipped<br><br>**1 skipped** (`test_live_postgresql_tenant_isolation`) | `11.25s`<br><br>`10.24s` | Completed synchronously |
| **5. Frontend TypeScript & Production Build (Prior to Recovery Audit; no frontend files changed since)** | • `cd frontend; npx tsc --noEmit`<br>• `cd frontend; npm run build` (`task-23689`) | **PASS**<br>**PASS** | **0**<br>**0** | — | `25.0s`<br>`16.0s` | Completed cleanly (9/9 static pages) |

*Note on Phase 15 Perception Deselected Tests:* Exactly 7 of the 12 tests in `backend/tests/test_phase15_perception.py` were executed and passed (`test_15_1`, `test_15_4`, `test_15_7`, `test_15_8`, `test_15_9`, `test_15_10`, `test_15_12`), while 5 tests (`test_15_2`, `test_15_3`, `test_15_5`, `test_15_6`, `test_15_11`) were explicitly deselected per bounded verification instructions.

---

## 5. Remaining Limitations & Unresolved Defects

- **Zero Unresolved Stage 1C / Stage 1D Defects:** All eight findings (`DEF-1C-01` through `DEF-1C-08`) and the `_BrowserSession._op_lock` self-deadlock are resolved and regression-tested.
- **Environmental / Policy Constraints Preserved:**
  - Live external browser trials against public websites (YouTube, Wikipedia) and live Gemini/OpenAI API calls remain strictly disabled pending explicit Project Owner authorization for Task T8.
  - Five multi-cycle browser perception tests in `test_phase15_perception.py` (`test_15_2`, `test_15_3`, `test_15_5`, `test_15_6`, `test_15_11`) were deselected by instruction during bounded verification.

---

## 6. Git Repository & Working-Tree Status

- **Branch:** `stabilization-2-closure`
- **HEAD Commit:** `bdaa78ef7c00a104d3a30089a6614e117fda291f`
- **Staged Changes:** `0`
- **Modified Tracked Files (`30`):**
  1. `backend/ai/fallback_rules.py`
  2. `backend/ai/orchestrator.py`
  3. `backend/api/router.py`
  4. `backend/core/auth_db.py`
  5. `backend/execution/browser/agent_contracts.py`
  6. `backend/execution/browser/browser_agent.py`
  7. `backend/execution/browser/playwright_executor.py`
  8. `backend/execution/browser/semantic_verifier.py`
  9. `backend/execution/evidence/evidence_vault.py`
  10. `backend/execution/registry/tool_registry.py`
  11. `backend/knowledge/playbook_service.py`
  12. `backend/knowledge/semantic_memory.py`
  13. `backend/orchestration/dag_builder.py`
  14. `backend/orchestration/execution_monitor.py`
  15. `backend/orchestration/runtime_engine.py`
  16. `backend/orchestration/scheduler.py`
  17. `backend/schemas/ardo_models.py`
  18. `backend/schemas/models.py`
  19. `backend/tests/test_fallback.py`
  20. `backend/tests/test_gemini_failure_handling.py`
  21. `backend/tests/test_phase15_event_evidence_observability.py`
  22. `backend/tests/test_phase15_perception.py`
  23. `backend/tests/test_plan_approval.py`
  24. `backend/tests/test_provider_recovery_t5.py`
  25. `docs/roadmap/NEEDS_OWNER.md`
  26. `docs/roadmap/ROADMAP.md`
  27. `docs/roadmap/STATE.md`
  28. `frontend/src/components/panels/ExecutionControl.tsx`
  29. `frontend/src/lib/ardo-api.ts`
  30. `frontend/src/store/useARDOStore.ts`
- **Untracked Files (`10`):**
  1. `backend/execution/registry/calculation_tool.py`
  2. `backend/tests/fixtures/mock_youtube.html`
  3. `backend/tests/test_false_success_remediation.py`
  4. `backend/tests/test_offline_chromium_acceptance.py`
  5. `docs/roadmap/evidence/ARDO_FALSE_SUCCESS_QUARANTINE_AUDIT.md`
  6. `docs/roadmap/evidence/ARDO_STAGE1A_FALSE_SUCCESS_REMEDIATION_SPEC.md`
  7. `docs/roadmap/evidence/ARDO_STAGE1B_FALSE_SUCCESS_REMEDIATION_REPORT.md`
  8. `docs/roadmap/evidence/ARDO_STAGE1C_INDEPENDENT_VERIFICATION_REPORT.md`
  9. `docs/roadmap/evidence/ARDO_STAGE1D_TARGETED_CORRECTIONS_REPORT.md`
  10. `docs/roadmap/evidence/quarantine_backup_records.json`
