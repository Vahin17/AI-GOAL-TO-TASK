"""
verify_phase13_semantic_memory.py
==================================
Phase 13 focused standalone verification script for SemanticMemoryStore.

Run from backend/ directory:
    $env:PYTHONPATH = "."
    python tests\verify_phase13_semantic_memory.py

Proves:
  1. PostgreSQL connection is live (not local fallback)
  2. record_memory() inserts a real pgvector embedding into execution_memories
  3. retrieve_memories() returns the inserted row via pgvector cosine similarity
  4. The exact inserted memory is found in the same-instance retrieval
  5. A BRAND-NEW SemanticMemoryStore instance can retrieve the same row
     (proves persistence in PostgreSQL, not local in-process memory)
  6. Direct SQL confirms: row exists, embedding IS NOT NULL, dim = 128
  7. No local fallback was triggered during any of the above steps
"""

import io
import logging
import sys
import time

from dotenv import load_dotenv

# load_dotenv() finds backend/.env automatically when run from backend/
load_dotenv()

# ── Reliable log capture for fallback detection ─────────────────────────────
_log_buf = io.StringIO()
_log_handler = logging.StreamHandler(_log_buf)
_log_handler.setLevel(logging.WARNING)
_sem_logger = logging.getLogger("knowledge.semantic_memory")
_sem_logger.addHandler(_log_handler)
_sem_logger.setLevel(logging.WARNING)

from knowledge.semantic_memory import SemanticMemoryStore, ExecutionMemoryItem


def _fallback_triggered() -> bool:
    markers = ("using local", "pg retrieve error", "pg record failed", "fallback")
    captured = _log_buf.getvalue().lower()
    return any(m in captured for m in markers)


def _hr():
    print("-" * 60)


FAIL = 0  # count failures; exit non-zero if any

def check(condition: bool, label: str, detail: str = ""):
    global FAIL
    status = "PASS" if condition else "FAIL"
    msg = f"  [{status}] {label}"
    if not condition and detail:
        msg += f"\n         {detail}"
    print(msg)
    if not condition:
        FAIL += 1
    return condition


# ============================================================================
# STEP 1 — Import & instantiate
# ============================================================================
_hr()
print("Step 1 — Import & connect to live PostgreSQL")
_hr()

sm = SemanticMemoryStore()

check(sm._is_pg_connected,  "PostgreSQL connected (_is_pg_connected = True)")
check(sm._pg_conn is not None, "_pg_conn is not None")
check(sm.dim == 128,        f"vector dimension = {sm.dim}  (expected 128)")

if not sm._is_pg_connected:
    print()
    print("ABORT: PostgreSQL is not connected.")
    print("Ensure POSTGRES_URI is set in backend/.env and the Docker container is running.")
    sys.exit(1)

# ============================================================================
# STEP 2 — Insert test memory
# ============================================================================
_hr()
print("Step 2 — Insert test execution memory")
_hr()

TEST_PHRASE = "Phase 13 PostgreSQL semantic memory persistence test for AI goal planning."

item = sm.record_memory(
    goal_text=TEST_PHRASE,
    plan_id="plan_p13_verify",
    session_id="sess_p13_" + str(int(time.time())),
    memory_type="successful_plan",
    task_summary="Phase 13 standalone verification: pgvector INSERT path",
    outcome_status="success",
    execution_metrics={"tasks_completed": 5, "duration_s": 12.3},
    safety_summary={"blocked": 0},
    metadata={"phase": "13", "test": "verify_semantic_memory"},
)

check(bool(item.memory_id), f"memory_id assigned  = {item.memory_id!r}")
check(item.goal_text == TEST_PHRASE, "goal_text matches test phrase")
check(not _fallback_triggered(), "no fallback triggered during INSERT")

MEMORY_ID = item.memory_id
print(f"  memory_id: {MEMORY_ID}")

# ============================================================================
# STEP 3 — Same-instance retrieval via pgvector
# ============================================================================
_hr()
print("Step 3 — Same-instance pgvector similarity retrieval")
_hr()

results = sm.retrieve_memories(TEST_PHRASE, top_k=5, min_score=0.0)

check(isinstance(results, list),  f"results is list  (got {type(results).__name__})")
check(len(results) >= 1,          f"at least 1 result returned  (got {len(results)})")
check(not _fallback_triggered(),  "no fallback triggered during retrieval")

for r in results:
    score_ok = 0.0 <= r.relevance_score <= 1.0
    print(f"  memory_id={r.memory_id!r}  score={r.relevance_score}  goal={r.goal_text[:55]!r}")
    check(score_ok, f"relevance_score in [0,1]  (got {r.relevance_score})")

found_same = any(r.memory_id == MEMORY_ID for r in results)
check(found_same,
      f"exact inserted memory found in same-instance results  (memory_id={MEMORY_ID!r})",
      f"Got IDs: {[r.memory_id for r in results]}")

# Hard assert — must not silently pass
assert found_same, "FAIL: inserted memory not found in same-instance retrieval"

# ============================================================================
# STEP 4 — NEW-INSTANCE persistence test
# ============================================================================
_hr()
print("Step 4 — NEW SemanticMemoryStore instance persistence test")
_hr()

sm2 = SemanticMemoryStore()
check(sm2._is_pg_connected, "new instance is PG connected")

results2 = sm2.retrieve_memories(TEST_PHRASE, top_k=5, min_score=0.0)
check(len(results2) >= 1, f"new instance returns >= 1 result  (got {len(results2)})")

found_new = any(r.memory_id == MEMORY_ID for r in results2)
check(found_new,
      f"exact memory_id found via new instance  (memory_id={MEMORY_ID!r})",
      f"Got IDs: {[r.memory_id for r in results2]}")
check(not _fallback_triggered(), "no fallback during new-instance retrieval")

# Hard assert
assert found_new, "FAIL: memory NOT found via new SemanticMemoryStore instance — not in PostgreSQL"

# ============================================================================
# STEP 5 — Direct SQL row verification
# ============================================================================
_hr()
print("Step 5 — Direct SQL verification (vector_dims, embedding IS NOT NULL)")
_hr()

with sm._pg_conn.cursor() as cur:
    cur.execute(
        """
        SELECT
            memory_id,
            outcome_status,
            embedding IS NOT NULL   AS has_embedding,
            vector_dims(embedding)  AS embedding_dimensions
        FROM execution_memories
        WHERE memory_id = %s
        """,
        (MEMORY_ID,),
    )
    row = cur.fetchone()

check(row is not None, f"row exists in execution_memories  (memory_id={MEMORY_ID!r})")

if row:
    db_id, db_status, has_emb, vec_dim = row
    check(db_id == MEMORY_ID,    f"database memory_id matches  ({db_id!r})")
    check(db_status == "success", f"outcome_status = {db_status!r}  (expected 'success')")
    check(has_emb is True,        f"embedding IS NOT NULL  ({has_emb})")
    check(vec_dim == sm.dim,      f"embedding dimensions = {vec_dim}  (expected {sm.dim})")
    print(f"  -> memory_id={db_id}  status={db_status}  has_embedding={has_emb}  dim={vec_dim}")

# ============================================================================
# Summary
# ============================================================================
_hr()
print("Phase 13 Semantic Memory Verification Summary")
_hr()
print(f"  memory_id                : {MEMORY_ID}")
print(f"  PostgreSQL connected     : {sm._is_pg_connected}")
print(f"  vector dimension         : {sm.dim}")
print(f"  same-instance retrieval  : {found_same}")
print(f"  new-instance retrieval   : {found_new}")
print(f"  embedding in database    : {has_emb if row else 'N/A'}")
print(f"  embedding dimensions     : {vec_dim if row else 'N/A'}")
print(f"  fallback triggered       : {_fallback_triggered()}")
_hr()

if FAIL == 0:
    print("LIVE PostgreSQL + pgvector semantic memory: VERIFIED")
else:
    print(f"VERIFICATION FAILED: {FAIL} check(s) did not pass.")
    sys.exit(1)

