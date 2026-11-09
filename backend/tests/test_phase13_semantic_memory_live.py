"""
test_phase13_semantic_memory_live.py -- Phase 13: Hardened Semantic Memory Live Verification
=============================================================================================
Proves SemanticMemoryStore uses live PostgreSQL + pgvector:
  - records an execution memory (INSERT with real embedding vector)
  - retrieves it via cosine similarity from pgvector (no numeric[] cast error)
  - persists across a fresh SemanticMemoryStore instance
  - never falls back to the local in-process store during the above operations

Run from backend/ with:
    pytest tests/test_phase13_semantic_memory_live.py -v
"""

import io
import logging
import time
import pytest

from dotenv import load_dotenv

# load_dotenv() picks up backend/.env when pytest runs from the backend/ directory.
load_dotenv()

from knowledge.semantic_memory import SemanticMemoryStore, ExecutionMemoryItem


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def live_store():
    """
    Instantiate a SemanticMemoryStore that MUST connect to live PostgreSQL.
    Skips the entire module if the live DB is unavailable.
    """
    store = SemanticMemoryStore()
    if not store._is_pg_connected:
        pytest.skip(
            "Live PostgreSQL is not available -- skipping live semantic-memory "
            "verification. Ensure POSTGRES_URI is set and Docker container is running."
        )
    return store


@pytest.fixture(scope="module")
def test_phrase():
    return "Phase 13 PostgreSQL semantic memory persistence test for AI goal planning."


# ---------------------------------------------------------------------------
# Helper: capture WARNING-level logs from SemanticMemoryStore
# ---------------------------------------------------------------------------

def _make_log_capture():
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setLevel(logging.WARNING)
    sem_logger = logging.getLogger("knowledge.semantic_memory")
    sem_logger.addHandler(h)
    sem_logger.setLevel(logging.WARNING)
    return buf, h, sem_logger


def _release_log_capture(buf, h, sem_logger):
    sem_logger.removeHandler(h)
    return buf.getvalue()


def _fallback_triggered(log_output: str) -> bool:
    fallback_markers = (
        "Using local",
        "PG retrieve error",
        "PG record failed",
        "fallback",
    )
    lo = log_output.lower()
    return any(m.lower() in lo for m in fallback_markers)


# ---------------------------------------------------------------------------
# Test 1 -- Connection state
# ---------------------------------------------------------------------------

def test_live_pg_connection(live_store):
    """PostgreSQL connection must be active on instantiation."""
    assert live_store._is_pg_connected, "FAIL: _is_pg_connected is False"
    assert live_store._pg_conn is not None, "FAIL: _pg_conn is None"
    assert live_store.dim == 128, f"FAIL: unexpected dim={live_store.dim}"


# ---------------------------------------------------------------------------
# Test 2 -- INSERT via record_memory (embedding persisted as vector type)
# ---------------------------------------------------------------------------

def test_record_memory_inserts_into_postgres(live_store, test_phrase):
    """
    record_memory() must INSERT a row into execution_memories with a real
    pgvector embedding -- no numeric[] type-cast error, no local fallback.
    """
    buf, h, sem_logger = _make_log_capture()

    item = live_store.record_memory(
        goal_text=test_phrase,
        plan_id="plan_p13_live_test",
        session_id="sess_p13_" + str(int(time.time())),
        memory_type="successful_plan",
        task_summary="Phase 13 pgvector semantic memory hardened verification -- INSERT path",
        outcome_status="success",
        execution_metrics={"tasks_completed": 5, "duration_s": 12.3},
        safety_summary={"blocked": 0},
        metadata={"phase": "13", "test": "semantic_memory_pgvector_live"},
    )

    log_output = _release_log_capture(buf, h, sem_logger)

    assert item.memory_id, "FAIL: record_memory returned item with no memory_id"
    assert item.goal_text == test_phrase, "FAIL: goal_text mismatch"

    assert not _fallback_triggered(log_output), (
        f"FAIL: local fallback triggered during record_memory. Log: {log_output!r}"
    )

    # Stash memory_id on the store object so subsequent tests can use it
    live_store._phase13_memory_id = item.memory_id


# ---------------------------------------------------------------------------
# Test 3 -- Same-instance similarity retrieval via pgvector <=>
# ---------------------------------------------------------------------------

def test_same_instance_retrieval(live_store, test_phrase):
    """
    retrieve_memories() on the same store instance must return the inserted
    memory via a live pgvector cosine-similarity query (Vector adapter, not numeric[]).
    """
    memory_id = getattr(live_store, "_phase13_memory_id", None)
    if not memory_id:
        pytest.skip("Depends on test_record_memory_inserts_into_postgres running first")

    buf, h, sem_logger = _make_log_capture()

    results = live_store.retrieve_memories(test_phrase, top_k=1000, min_score=0.0)

    log_output = _release_log_capture(buf, h, sem_logger)

    assert isinstance(results, list), f"FAIL: results is {type(results).__name__}, not list"
    assert len(results) >= 1, "FAIL: no results returned from PostgreSQL"

    for r in results:
        assert 0.0 <= r.relevance_score <= 1.0, (
            f"FAIL: relevance_score={r.relevance_score} out of range"
        )

    found_same = any(r.memory_id == memory_id for r in results)
    assert found_same, (
        f"FAIL: inserted memory_id={memory_id!r} not found in same-instance retrieval. "
        f"Got IDs: {[r.memory_id for r in results]}"
    )
    print(f"  same-instance retrieval: PASS (memory_id={memory_id})")

    assert not _fallback_triggered(log_output), (
        f"FAIL: local fallback triggered during retrieve_memories. Log: {log_output!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 -- Cross-instance persistence (the "new instance" test)
# ---------------------------------------------------------------------------

def test_new_instance_persistence(live_store, test_phrase):
    """
    A brand-new SemanticMemoryStore must retrieve the previously inserted memory.
    This proves the row is in PostgreSQL, not only in the original Python object.
    """
    memory_id = getattr(live_store, "_phase13_memory_id", None)
    if not memory_id:
        pytest.skip("Depends on test_record_memory_inserts_into_postgres running first")

    buf, h, sem_logger = _make_log_capture()

    sm2 = SemanticMemoryStore()
    assert sm2._is_pg_connected, "FAIL: fresh SemanticMemoryStore not PG connected"

    results2 = sm2.retrieve_memories(test_phrase, top_k=1000, min_score=0.0)

    log_output = _release_log_capture(buf, h, sem_logger)

    found_new = any(r.memory_id == memory_id for r in results2)
    assert found_new, (
        f"FAIL: memory_id={memory_id!r} NOT found via new SemanticMemoryStore instance -- "
        f"data is NOT persisted in PostgreSQL. Got IDs: {[r.memory_id for r in results2]}"
    )
    print(f"  new-instance persistence: PASS (memory_id={memory_id})")

    assert not _fallback_triggered(log_output), (
        f"FAIL: local fallback triggered on new instance. Log: {log_output!r}"
    )


# ---------------------------------------------------------------------------
# Test 5 -- Direct PostgreSQL row verification (strongest evidence)
# ---------------------------------------------------------------------------

def test_direct_postgres_row_verification(live_store):
    """
    Directly queries execution_memories via psycopg2 to confirm:
    - the row exists
    - memory_id, goal_text, outcome_status are correct
    - embedding IS NOT NULL
    - embedding dimension == store.dim (128)
    """
    memory_id = getattr(live_store, "_phase13_memory_id", None)
    if not memory_id:
        pytest.skip("Depends on test_record_memory_inserts_into_postgres running first")

    with live_store._pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                memory_id,
                goal_text,
                outcome_status,
                embedding IS NOT NULL AS has_embedding,
                vector_dims(embedding)  AS embedding_dimensions
            FROM execution_memories
            WHERE memory_id = %s
            """,
            (memory_id,),
        )
        row = cur.fetchone()

    assert row is not None, (
        f"FAIL: memory_id={memory_id!r} not found in execution_memories table"
    )

    db_memory_id, db_goal, db_status, has_embedding, vec_dim = row

    assert db_memory_id == memory_id, f"FAIL: memory_id mismatch {db_memory_id!r}"
    assert "Phase 13" in db_goal, f"FAIL: test phrase not in db goal_text: {db_goal!r}"
    assert db_status == "success", f"FAIL: outcome_status={db_status!r}"
    assert has_embedding is True, "FAIL: embedding IS NULL in database"
    assert vec_dim == live_store.dim, (
        f"FAIL: embedding_dimensions={vec_dim}, expected {live_store.dim}"
    )
    print(
        f"  direct DB verification: PASS "
        f"(has_embedding=True, vec_dim={vec_dim}, status={db_status})"
    )


# ---------------------------------------------------------------------------
# Test 6 -- Summary / final VERIFIED gate
# ---------------------------------------------------------------------------

def test_phase13_semantic_memory_live_summary(live_store, test_phrase):
    """
    Prints a concise verification summary and asserts all conditions are met.
    Only prints VERIFIED when all pass.
    """
    memory_id = getattr(live_store, "_phase13_memory_id", None)

    pg_connected = live_store._is_pg_connected
    vec_dim = live_store.dim

    same_results = live_store.retrieve_memories(test_phrase, top_k=1000, min_score=0.0)
    same_found = any(r.memory_id == memory_id for r in same_results)

    sm3 = SemanticMemoryStore()
    new_results = sm3.retrieve_memories(test_phrase, top_k=1000, min_score=0.0)
    new_found = any(r.memory_id == memory_id for r in new_results)

    db_has_embedding = False
    db_vec_dim = None
    if memory_id:
        with live_store._pg_conn.cursor() as cur:
            cur.execute(
                "SELECT embedding IS NOT NULL, vector_dims(embedding) "
                "FROM execution_memories WHERE memory_id = %s",
                (memory_id,),
            )
            row = cur.fetchone()
            if row:
                db_has_embedding, db_vec_dim = row

    print()
    print("-" * 60)
    print("Phase 13 Semantic Memory Live Verification Summary")
    print("-" * 60)
    print(f"  memory_id                : {memory_id}")
    print(f"  PostgreSQL connected     : {pg_connected}")
    print(f"  vector dimension         : {vec_dim}")
    print(f"  same-instance retrieval  : {same_found}")
    print(f"  new-instance retrieval   : {new_found}")
    print(f"  database embedding       : {db_has_embedding}")
    print(f"  database embedding dim   : {db_vec_dim}")
    print(f"  fallback triggered       : False  (asserted in prior tests)")
    print("-" * 60)

    assert pg_connected,      "FAIL: PostgreSQL not connected"
    assert memory_id,         "FAIL: no memory_id recorded"
    assert same_found,        "FAIL: same-instance retrieval failed"
    assert new_found,         "FAIL: new-instance persistence failed"
    assert db_has_embedding,  "FAIL: embedding NULL in database"
    assert db_vec_dim == vec_dim, f"FAIL: db dim={db_vec_dim} != {vec_dim}"

    print("LIVE PostgreSQL + pgvector semantic memory: VERIFIED")
    print()
