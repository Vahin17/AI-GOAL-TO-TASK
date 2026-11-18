# ARDO — Owner Actions, Authorizations & Dependencies

**Date:** 2026-11-17  
**Status:** ACTIVE (Milestone M3 APPROVED; Awaiting T8 Authorization)  

This document tracks all decisions, environmental prerequisites, and authorizations that explicitly require action from the **Project Owner**.

---

## 1. Formal Approval Gates (Stop Points)

| Milestone | Scope | Prerequisites Before Owner Review | Owner Action Required |
| :--- | :--- | :--- | :--- |
| **Milestone M1** | Stabilization Sign-Off (Phase 1) | T1 (Embedding Checkpoint), T2 (Concurrency Audit), T3 (Phase 14/15 Repairs) complete with empirical test evidence. | **APPROVED by Project Owner.** |
| **Milestone M2** | Functional Reliability Sign-Off (Phase 2) | T4 (Verification Contracts), T5 (Provider Recovery), T6 (Live DB Integration) complete. | **APPROVED by Project Owner.** |
| **Milestone M3** | Security Sign-Off (Phase 3 Gate) | T7 (Security Hardening), T7.1 (Targeted Security Remediation), T7.1.1 (Final Security Gap Verification), T7.1.2 (Final Approval Integrity), and T7.1.3 (Public API Session Enumeration Remediation) complete. | **APPROVED by Project Owner.** |
| **Stage 2 Review & Stage 3 Authorization** | Goal Decomposition & Fallback Transparency Sign-Off | Stage 1 (1A–1E) and Stage 2 (`DEF-S2-01`..`DEF-S2-06`) are complete and empirically verified: 58/58 tests pass in the Stage 2 planning/fallback suite (`test_fallback.py`, `test_phase1_pipeline.py`, `test_plan_critic.py`, `test_input_guard.py`, `test_api.py`), 161/161 tests pass (`1` skipped) in the Stage 1/security/recovery regression suite, and frontend `npx tsc --noEmit` passes (`0` errors). Deliverable in `ARDO_STAGE2_GOAL_DECOMPOSITION_FALLBACK_REPORT.md`. | **APPROVED by Project Owner.** |
| **Stage 3 Review & Stage 4 Authorization** | Protect Memory, Playbooks and RAG Sign-Off | Stage 3 (`DEF-S3-01`..`DEF-S3-05`) is complete and empirically verified across all 52 required scenarios: 119/119 tests pass (`1` skipped) across `test_stage3_memory_playbooks_rag.py` and the 7 RAG/memory/playbook suites, and 130/130 tests pass (`1` skipped) across Stage 1, Stage 2, and M3 security regression suites. Deliverable in `ARDO_STAGE3_MEMORY_PLAYBOOKS_RAG_REPORT.md`. | Review Stage 3 completion report and authorize **Stage 4 — Correct dashboard execution reporting** (or a consolidated Git checkpoint commit if desired). |
| **Final Acceptance (T8..T10)** | Project Completion | All remaining tasks complete; Production Readiness Scorecard delivered with empirical evidence. | Provide explicit authorization and credentials for T8 Live Trials when ready; final project sign-off. |

---

## 2. External Service & Live Credential Authorizations

| Task ID | Resource / Service | Reason Needed | Current Constraint | Owner Action Required |
| :--- | :--- | :--- | :--- | :--- |
| **T8** | Live Google Gemini API | Live browser agent execution trials (Wikipedia, multi-step search). | Live API calls strictly prohibited without owner authorization. | Provide explicit approval and confirm active Gemini API credentials and billing quota prior to T8 execution. |
| **T8** | OpenAI API (Optional) | Cloud embedding live verification. | Live API calls strictly prohibited. | Authorize live OpenAI API testing if cloud provider verification is desired. |

---

## 3. Environment & Infrastructure Prerequisites

| Task ID | Environmental Dependency | Reason Needed | Current Workaround | Owner Action Required |
| :--- | :--- | :--- | :--- | :--- |
| **T6** | Live PostgreSQL container with pgvector | Live validation of DDL, schema discovery via `pg_attribute`, HNSW cosine search, and GIN full-text indexing. | Unit and regression tests run against in-memory dictionary stores and mocked `psycopg2` cursors. | Spin up local Docker PostgreSQL container (or provide connection URI) when T6 is ready to execute. If unavailable, T6 will mark `blocked_environment`. |

---

## 4. Policy & Assertion Change Authorizations

| Scope | Strict Rule | Owner Action Required |
| :--- | :--- | :--- |
| **Test Deletions** | No test may ever be deleted. | Explicit owner approval required if a test is proven permanently obsolete. |
| **Assertion Relaxation** | Assertions may not be loosened or weakened. | Explicit owner approval required if an assertion was fundamentally flawed. |
| **Git Branch Operations** | Do not commit, push, merge, or rebase on `stabilization-2-closure` or `main`. | Owner must provide explicit authorization to create commits or push branches. |

