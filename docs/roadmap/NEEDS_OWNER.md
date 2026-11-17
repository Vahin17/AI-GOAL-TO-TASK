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
| **Milestone M3** | Security Sign-Off (Phase 3 Gate) | T7 (Security Hardening), T7.1 (Targeted Security Remediation), T7.1.1 (Final Security Gap Verification), T7.1.2 (Final Approval Integrity), and T7.1.3 (Public API Session Enumeration Remediation) complete. All 6 original findings (T2-01..T2-06, T4-SEC-01), all 4 review findings (DEF-T7-01..DEF-T7-04), and the public API session enumeration oracle remediated fail-closed and validated (9/9 dedicated T7.1.3 tests, 90/90 rerun regression sweep, 99/99 combined security sweep, 14 live PostgreSQL integration tests, 5/5 offline acceptance tasks passing). Reports in `T7_SECURITY_APPROVAL_HARDENING_REPORT.md`, `T7_1_TARGETED_SECURITY_REMEDIATION_REPORT.md`, `T7_1_1_FINAL_SECURITY_GAP_REPORT.md`, `T7_1_2_FINAL_APPROVAL_INTEGRITY_REPORT.md`, `M3_FINAL_OWNER_VERIFICATION_REPORT.md`, and `T7_1_3_SESSION_ENUMERATION_REMEDIATION_REPORT.md`. | **APPROVED by Project Owner.** |
| **Final Acceptance** | Project Completion | All 10 tasks complete; Production Readiness Scorecard delivered with empirical evidence. | Final project sign-off, branch merge, and deployment authorization. |

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

