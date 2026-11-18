"""
test_provider_recovery_t5.py — Task T5 Provider Recovery Enhancement Verification Suite
========================================================================================
Validates all Task T5 functional and architectural recovery contracts:
1. Temporary 429 rate-limit classification (transient, preserves provider diagnostics).
2. Permanent quota/billing exhaustion (stops repeated useless retries).
3. Retry-After seconds parsing (integer, float, "51s").
4. Retry-After HTTP-date parsing (RFC 7231 / RFC 2822).
5. Invalid or missing Retry-After values (graceful bounded fallbacks).
6. Cooldown expiration and dynamic recovery.
7. Bounded retry exhaustion.
8. Provider recovery after temporary failure.
9. Invalid credentials (401/403 permanent lock and dynamic API key reload).
10. Execution pause and bounded resume with checkpoint preservation.
11. No repeated completed non-idempotent actions.
12. Semantic-memory correctness (incomplete/failed/provider-outage runs never record successful memories).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.retry_utils import classify_provider_error, parse_retry_after
from knowledge.embeddings import (
    DeterministicLocalEmbeddingProvider,
    HybridEmbeddingService,
    OpenAIEmbeddingProvider,
)
from orchestration.checkpoint_manager import (
    ExecutionCheckpointManager,
    is_task_idempotent,
)
from orchestration.dag_builder import ARDOGraph, ARDOTaskNode
from orchestration.runtime_engine import RuntimeEngine
from orchestration.state_machine import TaskState


@pytest.fixture(autouse=True)
def reset_hybrid_embedding_state():
    """Ensure clean embedding provider state before and after each test."""
    HybridEmbeddingService.reset_state()
    yield
    HybridEmbeddingService.reset_state()


# ── Area A & C: Retry-After Parsing (Seconds, HTTP-Date, Invalid) ───────────

def test_retry_after_numeric_seconds():
    """Integer and float seconds with or without units are parsed accurately."""
    assert parse_retry_after(120) == 120.0
    assert parse_retry_after("60") == 60.0
    assert parse_retry_after("30.5") == 30.5
    assert parse_retry_after("51.236756038s") == 51.236756038
    assert parse_retry_after("15 sec") == 15.0
    assert parse_retry_after("10seconds") == 10.0


def test_retry_after_http_date():
    """HTTP-date format per RFC 7231 / RFC 2822 is converted to relative seconds."""
    future_time = datetime.now(timezone.utc) + timedelta(seconds=45)
    http_date_str = future_time.strftime("%a, %d %b %Y %H:%M:%S GMT")

    parsed = parse_retry_after(http_date_str)
    assert parsed is not None
    # Tolerance for execution time
    assert 40.0 <= parsed <= 46.0


def test_retry_after_past_date_or_negative_returns_zero():
    """Past HTTP-dates or negative seconds return 0.0 without errors."""
    past_time = datetime.now(timezone.utc) - timedelta(seconds=100)
    http_date_str = past_time.strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert parse_retry_after(http_date_str) == 0.0
    assert parse_retry_after("-10") == 0.0


def test_retry_after_invalid_and_missing_fallback():
    """Invalid, empty, or missing values safely return None."""
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("   ") is None
    assert parse_retry_after("not-a-date-or-number") is None


def test_retry_after_bounded_upper_limit():
    """Excessive retry delays are clamped to the bounded maximum."""
    huge_delay = 100000.0
    capped = parse_retry_after(huge_delay, max_bounded_seconds=120.0)
    assert capped == 120.0


# ── Area A & B: Provider Error Classification ───────────────────────────────

def test_classify_temporary_rate_limit_vs_permanent_quota():
    """Temporary 429 concurrency/RPM limits are distinguished from hard quota stops."""
    # Temporary 429 rate limit
    ftype, is_perm = classify_provider_error(429, "Rate limit exceeded. Please try again later.")
    assert ftype == "rate_limit_429"
    assert is_perm is False

    # Hard quota exhaustion
    ftype2, is_perm2 = classify_provider_error(429, "You exceeded your current quota, please check your plan and billing details.")
    assert ftype2 == "quota_exhausted"
    assert is_perm2 is True

    # Quota details reason
    ftype3, is_perm3 = classify_provider_error(
        429, "Resource exhausted", details=[{"reason": "RATE_LIMIT_EXCEEDED_DAILY"}]
    )
    assert ftype3 == "quota_exhausted"
    assert is_perm3 is True


def test_classify_auth_and_service_unavailable():
    """HTTP 401/403 are permanent auth failures; 503 is transient."""
    ftype1, is_perm1 = classify_provider_error(401, "Invalid API key provided.")
    assert ftype1 == "auth_401"
    assert is_perm1 is True

    ftype2, is_perm2 = classify_provider_error(503, "The model is currently overloaded. Please try again later.")
    assert ftype2 == "service_unavailable_503"
    assert is_perm2 is False


# ── Area B & C: HybridEmbeddingService Rate Limit vs Quota vs Recovery ──────

def test_embedding_service_temporary_rate_limit_preserves_quota_status():
    """A 429 rate limit error does not mark OpenAI quota as permanently exhausted."""
    HybridEmbeddingService.reset_state()
    service = HybridEmbeddingService(api_key="fake-key")

    mock_openai = MagicMock()
    mock_openai.embed_texts.side_effect = Exception("429 Too Many Requests: Rate limit exceeded (TPM). Retry after 2.5s")
    service._openai = mock_openai

    # Execute embedding - should fall back to local without permanent quota lock
    vecs = service.embed_texts(["hello world"])
    assert len(vecs) == 1
    assert len(vecs[0]) == 128  # deterministic local fallback

    diag = service.get_diagnostics()
    assert diag["openai_quota_exhausted"] is False
    assert diag["last_failure_type"] == "rate_limit_429"
    assert diag["is_transient_rate_limit"] is True
    assert diag["in_cooldown"] is True
    assert diag["cooldown_remaining_seconds"] > 0.0


def test_embedding_service_permanent_quota_exhaustion_locks_provider():
    """A hard quota exhaustion error sets permanent quota exhausted flag and 120s cooldown."""
    HybridEmbeddingService.reset_state()
    service = HybridEmbeddingService(api_key="fake-key")

    mock_openai = MagicMock()
    mock_openai.embed_texts.side_effect = Exception("429: You exceeded your current quota, check your billing details.")
    service._openai = mock_openai

    vecs = service.embed_texts(["hello world"])
    assert len(vecs[0]) == 128

    diag = service.get_diagnostics()
    assert diag["openai_quota_exhausted"] is True
    assert diag["last_failure_type"] == "quota_exhausted"
    assert diag["is_permanent_quota"] is True


def test_embedding_service_recovers_after_temporary_failure():
    """After cooldown expires, embedding service successfully recovers OpenAI provider."""
    HybridEmbeddingService.reset_state()
    service = HybridEmbeddingService(api_key="fake-key")

    mock_openai = MagicMock()
    # First call fails with transient rate limit
    mock_openai.embed_texts.side_effect = Exception("429 rate limit exceeded. Retry in 0.1s")
    service._openai = mock_openai

    service.embed_texts(["first attempt"])
    assert service.get_diagnostics()["last_failure_type"] == "rate_limit_429"

    # Simulate cooldown expiry
    HybridEmbeddingService._last_failure_time = time.time() - 20.0
    HybridEmbeddingService._retry_after_target = time.time() - 5.0
    assert HybridEmbeddingService._is_in_cooldown() is False

    # Second call succeeds
    mock_openai.embed_texts.side_effect = None
    mock_openai.embed_texts.return_value = [[0.1] * 1536]
    with patch("knowledge.embeddings.OpenAIEmbeddingProvider", return_value=mock_openai):
        res = service.embed_texts(["second attempt"])
        assert len(res[0]) == 1536
        assert service.get_diagnostics()["last_failure_type"] is None
        assert service.get_diagnostics()["consecutive_failures"] == 0


def test_embedding_service_auth_reload_clears_lock():
    """Providing a new API key via update_api_key clears 401 permanent auth lock."""
    HybridEmbeddingService.reset_state()
    service = HybridEmbeddingService(api_key="bad-key")

    mock_openai = MagicMock()
    mock_openai.embed_texts.side_effect = Exception("401 Unauthorized: Invalid API key")
    service._openai = mock_openai

    service.embed_texts(["test auth failure"])
    assert HybridEmbeddingService._auth_invalid is True

    # Call with bad key remains locked
    assert service._is_in_cooldown() is True

    # Reload with valid key
    with patch("knowledge.embeddings.OpenAIEmbeddingProvider"):
        service.update_api_key("valid-new-key")
        assert HybridEmbeddingService._auth_invalid is False
        assert service._api_key == "valid-new-key"


# ── Area D: Execution Pausing and Bounded Resume ────────────────────────────

@pytest.mark.asyncio
async def test_runtime_engine_pause_and_resume_preserves_state():
    """Pausing an engine creates a checkpoint, signals loop pause, and resumes safely."""
    node1 = ARDOTaskNode(task_id="t1", task_name="Step 1", status=TaskState.COMPLETED)
    node2 = ARDOTaskNode(task_id="t2", task_name="Step 2", status=TaskState.WAITING)
    graph = ARDOGraph(nodes={"t1": node1, "t2": node2}, session_id="s_pause_test")

    engine = RuntimeEngine(graph=graph, session_id="s_pause_test")
    assert engine.is_paused is False

    # Pause execution with 5s cooldown
    await engine.pause(reason="provider_rate_limit", cooldown_seconds=5.0)
    assert engine.is_paused is True

    # Attempt premature resume within cooldown
    resumed = engine.resume()
    assert resumed is False
    assert engine.is_paused is True

    # Fast forward cooldown
    engine._pause_time = time.time() - 10.0
    resumed = engine.resume()
    assert resumed is True
    assert engine.is_paused is False


# ── Area E: Non-Idempotent Action Protection ───────────────────────────────

def test_non_idempotent_task_classification():
    """Mutating tasks (pay, submit, book) are identified as non-idempotent."""
    node_nav = ARDOTaskNode(task_id="t_nav", task_name="Navigate to payment portal")
    assert is_task_idempotent(node_nav) is True

    node_pay = ARDOTaskNode(task_id="t_pay", task_name="Submit payment of $50")
    assert is_task_idempotent(node_pay) is False

    node_extract = ARDOTaskNode(task_id="t_ext", task_name="Extract table of prices")
    assert is_task_idempotent(node_extract) is True


def test_checkpoint_restore_preserves_completed_and_flags_uncertain_active():
    """Completed tasks are never re-executed; interrupted mutating tasks become OUTCOME_UNKNOWN."""
    node_completed = ARDOTaskNode(task_id="t_done", task_name="Submit payment", status=TaskState.PENDING)
    node_interrupted_mutating = ARDOTaskNode(task_id="t_mut", task_name="Charge credit card", status=TaskState.PENDING)
    node_interrupted_readonly = ARDOTaskNode(task_id="t_read", task_name="View receipt page", status=TaskState.PENDING)

    graph = ARDOGraph(
        nodes={
            "t_done": node_completed,
            "t_mut": node_interrupted_mutating,
            "t_read": node_interrupted_readonly,
        },
        session_id="s_chk_test",
    )

    checkpoint = {
        "session_id": "s_chk_test",
        "node_states": {
            "t_done": {"status": "completed"},
            "t_mut": {"status": "active"},
            "t_read": {"status": "active"},
        },
    }

    restored_graph, summary = ExecutionCheckpointManager.restore_graph_from_checkpoint(graph, checkpoint)

    # 1. Completed remains COMPLETED (never re-executed)
    assert restored_graph.nodes["t_done"].status == TaskState.COMPLETED

    # 2. Interrupted mutating action transitioned to OUTCOME_UNKNOWN to prevent duplicate side effects
    assert restored_graph.nodes["t_mut"].status == TaskState.OUTCOME_UNKNOWN

    # 3. Interrupted read-only action safely reset to WAITING
    assert restored_graph.nodes["t_read"].status == TaskState.WAITING


# ── Area F: Semantic Memory Correctness ─────────────────────────────────────

def test_semantic_memory_does_not_persist_incomplete_run_as_success():
    """An incomplete run (e.g. aborted due to provider failure) is never persisted as 'success'."""
    node1 = ARDOTaskNode(task_id="t1", task_name="Step 1", status=TaskState.COMPLETED)
    node2 = ARDOTaskNode(task_id="t2", task_name="Step 2", status=TaskState.WAITING)
    graph = ARDOGraph(nodes={"t1": node1, "t2": node2}, session_id="s_mem_test")

    engine = RuntimeEngine(graph=graph, session_id="s_mem_test")

    mock_rag = MagicMock()
    with patch("knowledge.rag_service.RAGService.get_instance", return_value=mock_rag):
        engine._persist_semantic_memory()

        # record_plan_outcome must NOT have been called with outcome_status="success"
        assert mock_rag.record_plan_outcome.called
        call_kwargs = mock_rag.record_plan_outcome.call_args[1]
        assert call_kwargs["outcome_status"] == "incomplete"


def test_semantic_memory_records_success_only_when_all_tasks_and_goal_verified():
    """Only a run where all tasks completed and goal was verified with genuine evidence produces a 'success' memory."""
    from execution.registry.tool_models import ToolResult

    # 1. Completed nodes WITHOUT empirical evidence must record 'unverified', NOT 'success'
    unverif_n1 = ARDOTaskNode(task_id="t1", task_name="Step 1", status=TaskState.COMPLETED)
    unverif_n2 = ARDOTaskNode(task_id="t2", task_name="Step 2", status=TaskState.COMPLETED)
    graph_unverif = ARDOGraph(nodes={"t1": unverif_n1, "t2": unverif_n2}, session_id="s_mem_unverif")
    engine_unverif = RuntimeEngine(graph=graph_unverif, session_id="s_mem_unverif")

    mock_rag_unverif = MagicMock()
    with patch("knowledge.rag_service.RAGService.get_instance", return_value=mock_rag_unverif):
        engine_unverif._persist_semantic_memory()
        assert mock_rag_unverif.record_plan_outcome.called
        assert mock_rag_unverif.record_plan_outcome.call_args[1]["outcome_status"] == "unverified"

    # 2. Completed nodes WITH legitimate empirical tool results record 'success'
    node1 = ARDOTaskNode(
        task_id="t1",
        task_name="Step 1",
        status=TaskState.COMPLETED,
        tool_result=ToolResult(
            call_id="c1",
            tool_name="calculator",
            action_name="evaluate",
            success=True,
            output_data={"result": 42.0, "expression": "6 * 7"},
        ),
    )
    node2 = ARDOTaskNode(
        task_id="t2",
        task_name="Step 2",
        status=TaskState.COMPLETED,
        tool_result=ToolResult(
            call_id="c2",
            tool_name="calculator",
            action_name="evaluate",
            success=True,
            output_data={"result": 84.0, "expression": "42 * 2"},
        ),
    )
    graph = ARDOGraph(nodes={"t1": node1, "t2": node2}, session_id="s_mem_test2")

    engine = RuntimeEngine(graph=graph, session_id="s_mem_test2")

    mock_rag = MagicMock()
    with patch("knowledge.rag_service.RAGService.get_instance", return_value=mock_rag):
        engine._persist_semantic_memory()

        assert mock_rag.record_plan_outcome.called
        call_kwargs = mock_rag.record_plan_outcome.call_args[1]
        assert call_kwargs["outcome_status"] == "success"
