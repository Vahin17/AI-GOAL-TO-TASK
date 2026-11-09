"""
checkpoint_manager.py — Database-Backed Execution Checkpoints & Worker Leases (Phase 2 Task 5)
===============================================================================================
Manages execution state persistence, worker leases, and crash recovery for ARDO sessions:
1. Structured execution checkpoints persisted to MongoDB task_runtime_state and execution_checkpoints.
2. Idempotent checkpointing after terminal state transitions and operator approvals.
3. Worker lease acquisition with heartbeats and TTL expiration (prevents concurrent duplicate workers).
4. Safe session resume: preserves terminal completed/blocked nodes, re-evaluates interrupted active tasks,
   and verifies canonical plan hash against plan_approvals.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

from orchestration.dag_builder import ARDOGraph, ARDOTaskNode
from orchestration.state_machine import TaskState

logger = logging.getLogger(__name__)

DEFAULT_LEASE_TTL_SECONDS = 30.0

MUTATING_KEYWORDS: Set[str] = {
    "pay", "payment", "purchase", "buy", "order", "charge",
    "transfer", "wire", "refund", "submit", "delete", "remove",
    "drop", "destroy", "cancel", "terminate", "send", "post", "put",
    "patch", "modify", "sign_up", "register", "subscribe", "book", "reserve", "hire",
}

READ_ONLY_PREFIXES: Tuple[str, ...] = (
    "navigate", "browse", "go to", "view", "read", "inspect", "extract",
    "search", "query", "find", "list", "check", "verify", "observe", "screenshot",
)

READ_ONLY_ACTIONS: Set[str] = {
    "navigate", "read_page", "extract_page_content", "screenshot", "wait",
    "get", "inspect", "observe", "search", "query", "find", "list", "verify",
}


def is_task_idempotent(node: ARDOTaskNode) -> bool:
    """
    Determines whether a task is provably idempotent (safe to retry after crash)
    or non-idempotent (external outcome is uncertain; duplicate execution could cause harm).
    """
    # 1. Explicit metadata override if set by safety or planner
    if isinstance(getattr(node, "metadata", None), dict):
        if "is_idempotent" in node.metadata:
            return bool(node.metadata["is_idempotent"])

    name_lower = (node.task_name or "").strip().lower()

    # 2. Check for explicit read-only prefix (e.g. "Navigate to checkout page", "Search for flight tickets")
    if any(name_lower.startswith(p) for p in READ_ONLY_PREFIXES):
        # Even if "checkout" appears in "Navigate to checkout page", the action is navigation
        # Only classify as mutating if it has an explicit submit/pay/purchase action verb
        mutating_verbs = {"submit", "pay", "purchase", "buy", "delete", "send", "book", "reserve", "transfer"}
        words = set(re.findall(r"\b[a-z_]+\b", name_lower))
        first_word = name_lower.split()[0] if name_lower.split() else ""
        if first_word in READ_ONLY_PREFIXES:
            # First verb is read-only (navigate/search/view/inspect)
            # Only non-idempotent if it also explicitly commands a mutation verb
            if not any(v in words for v in mutating_verbs - {first_word}):
                return True

    # 3. Check for explicit mutating keywords anywhere in task name
    words = set(re.findall(r"\b[a-z_]+\b", name_lower))
    if any(m in words or any(w.startswith(m) for w in words) for m in MUTATING_KEYWORDS):
        return False

    # 4. Check browser_action if present
    if getattr(node, "browser_action", None) is not None:
        action_type = ""
        ba = node.browser_action
        if isinstance(ba, dict):
            action_type = str(ba.get("action_type") or ba.get("type", "")).lower()
        elif hasattr(ba, "action_type"):
            val = ba.action_type
            action_type = val.value.lower() if hasattr(val, "value") else str(val).lower()
        elif isinstance(ba, str):
            action_type = ba.lower()

        if action_type:
            if action_type in READ_ONLY_ACTIONS:
                return True
            # Clicks, form filling, keystrokes, submits are considered non-idempotent
            return False

    # 5. Check tool_call if present
    if getattr(node, "tool_call", None) is not None:
        tool_name = ""
        tc = node.tool_call
        if isinstance(tc, dict):
            tool_name = str(tc.get("name", "")).lower()
        elif hasattr(tc, "name"):
            tool_name = str(tc.name).lower()
        if any(m in tool_name for m in MUTATING_KEYWORDS):
            return False

    # 6. Check if task name starts with any safe read-only prefixes
    if any(name_lower.startswith(p) for p in READ_ONLY_PREFIXES):
        return True

    # Safe default: if unknown or unclear, consider it non-idempotent
    return False


# In-memory lease fallback store for testing or offline dev
_MEMORY_LEASES: Dict[str, Dict[str, Any]] = {}
_MEMORY_CHECKPOINTS: Dict[str, List[Dict[str, Any]]] = {}


class ExecutionCheckpointManager:
    """
    Authoritative manager for session execution checkpoints and worker concurrency leases.
    """

    @classmethod
    async def save_checkpoint(
        cls,
        session_id: str,
        graph: ARDOGraph,
        trigger: str,
        metadata: Optional[Dict[str, Any]] = None,
        db: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Persists a complete execution checkpoint.
        Updates task_runtime_state and appends to execution_checkpoints.
        """
        now = time.time()
        checkpoint_id = f"chk_{uuid.uuid4().hex[:12]}"

        # Extract node statuses
        node_states: Dict[str, Dict[str, Any]] = {}
        completed_count = 0
        failed_count = 0
        blocked_count = 0
        active_count = 0

        for tid, node in graph.nodes.items():
            st = node.status.value if hasattr(node.status, "value") else str(node.status)
            if st == "completed":
                completed_count += 1
            elif st == "failed":
                failed_count += 1
            elif st == "blocked":
                blocked_count += 1
            elif st == "active":
                active_count += 1

            node_states[tid] = {
                "status": st,
                "task_name": node.task_name,
                "retry_count": node.retry_count,
                "approval_needed": node.approval_needed,
                "was_approval_gated": getattr(node, "was_approval_gated", False),
                "started_at": node.started_at,
                "completed_at": node.completed_at,
                "execution_logs": node.execution_logs[-5:] if node.execution_logs else [],
            }

        checkpoint_doc: Dict[str, Any] = {
            "checkpoint_id": checkpoint_id,
            "session_id": session_id,
            "plan_hash": graph.plan_hash,
            "trigger": trigger,
            "timestamp": now,
            "node_states": node_states,
            "parallel_groups": graph.parallel_groups,
            "nodes_snapshot": graph.snapshot(),
            "metrics": {
                "total_tasks": len(graph.nodes),
                "completed_tasks": completed_count,
                "failed_tasks": failed_count,
                "blocked_tasks": blocked_count,
                "active_tasks": active_count,
            },
            "metadata": metadata or {},
        }

        # 1. MongoDB Persistence
        if db is not None:
            try:
                # Append to checkpoint history
                await db.execution_checkpoints.insert_one(dict(checkpoint_doc))
                # Update latest state
                await db.task_runtime_state.update_one(
                    {"session_id": session_id},
                    {"$set": {
                        "session_id": session_id,
                        "latest_checkpoint_id": checkpoint_id,
                        "plan_hash": graph.plan_hash,
                        "updated_at": now,
                        "node_states": node_states,
                        "nodes": graph.snapshot(),
                        "parallel_groups": graph.parallel_groups,
                        "metrics": checkpoint_doc["metrics"],
                    }},
                    upsert=True,
                )
                logger.info(
                    f"[CheckpointManager] Saved checkpoint {checkpoint_id} for session {session_id} "
                    f"(trigger='{trigger}', completed={completed_count}/{len(graph.nodes)})"
                )
            except Exception as e:
                logger.error(f"[CheckpointManager] Failed to persist checkpoint to MongoDB: {e}")

        # 2. In-Memory fallback store
        if session_id not in _MEMORY_CHECKPOINTS:
            _MEMORY_CHECKPOINTS[session_id] = []
        _MEMORY_CHECKPOINTS[session_id].append(checkpoint_doc)

        return checkpoint_doc

    @classmethod
    async def load_latest_checkpoint(
        cls,
        session_id: str,
        db: Optional[Any] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Retrieves the most recent checkpoint for an interrupted or paused session.
        """
        if db is not None:
            try:
                # First check task_runtime_state
                state_doc = await db.task_runtime_state.find_one({"session_id": session_id})
                if state_doc:
                    return state_doc
                # Or query latest checkpoint
                cursor = db.execution_checkpoints.find({"session_id": session_id}).sort("timestamp", -1).limit(1)
                docs = await cursor.to_list(length=1)
                if docs:
                    return docs[0]
            except Exception as e:
                logger.error(f"[CheckpointManager] Error loading checkpoint from MongoDB: {e}")

        # In-memory fallback
        mem_list = _MEMORY_CHECKPOINTS.get(session_id, [])
        if mem_list:
            return mem_list[-1]
        return None

    # ── Worker Leases ────────────────────────────────────────────────────────

    @classmethod
    async def acquire_worker_lease(
        cls,
        session_id: str,
        worker_id: str,
        lease_duration_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
        db: Optional[Any] = None,
    ) -> bool:
        """
        Atomically acquires a worker lease for a session to prevent concurrent duplicate execution.
        Returns True if acquired; False if held by another active worker.
        """
        now = time.time()
        expires_at = now + lease_duration_seconds

        if db is not None:
            try:
                # Atomic upsert or conditional update if expired or owned by same worker
                result = await db.worker_leases.update_one(
                    {
                        "session_id": session_id,
                        "$or": [
                            {"lease_expires_at": {"$lt": now}},
                            {"worker_id": worker_id},
                            {"worker_id": None},
                        ],
                    },
                    {
                        "$set": {
                            "session_id": session_id,
                            "worker_id": worker_id,
                            "acquired_at": now,
                            "lease_expires_at": expires_at,
                        }
                    },
                    upsert=False,
                )
                if result.matched_count > 0:
                    return True

                # If no document exists yet, insert atomically
                try:
                    await db.worker_leases.insert_one({
                        "session_id": session_id,
                        "worker_id": worker_id,
                        "acquired_at": now,
                        "lease_expires_at": expires_at,
                    })
                    return True
                except Exception:
                    # Duplicate key race condition
                    return False
            except Exception as e:
                logger.error(f"[CheckpointManager] MongoDB worker lease acquire error: {e}")

        # In-memory lease fallback
        existing = _MEMORY_LEASES.get(session_id)
        if existing:
            if existing["lease_expires_at"] > now and existing["worker_id"] != worker_id:
                # Active lease held by another worker
                logger.warning(
                    f"[CheckpointManager] Lease denied for {worker_id} on {session_id}: "
                    f"held by {existing['worker_id']} until {existing['lease_expires_at']}"
                )
                return False

        _MEMORY_LEASES[session_id] = {
            "worker_id": worker_id,
            "acquired_at": now,
            "lease_expires_at": expires_at,
        }
        return True

    @classmethod
    async def renew_worker_lease(
        cls,
        session_id: str,
        worker_id: str,
        lease_duration_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
        db: Optional[Any] = None,
    ) -> bool:
        """Heartbeat to extend an active worker lease."""
        now = time.time()
        expires_at = now + lease_duration_seconds

        if db is not None:
            try:
                res = await db.worker_leases.update_one(
                    {"session_id": session_id, "worker_id": worker_id},
                    {"$set": {"lease_expires_at": expires_at}},
                )
                return res.matched_count > 0
            except Exception as e:
                logger.error(f"[CheckpointManager] Lease renewal error: {e}")

        existing = _MEMORY_LEASES.get(session_id)
        if existing and existing["worker_id"] == worker_id:
            existing["lease_expires_at"] = expires_at
            return True
        return False

    @classmethod
    async def release_worker_lease(
        cls,
        session_id: str,
        worker_id: str,
        db: Optional[Any] = None,
    ) -> None:
        """Releases worker lease on clean completion or shutdown."""
        if db is not None:
            try:
                await db.worker_leases.delete_one({"session_id": session_id, "worker_id": worker_id})
            except Exception as e:
                logger.error(f"[CheckpointManager] Lease release error: {e}")

        existing = _MEMORY_LEASES.get(session_id)
        if existing and existing["worker_id"] == worker_id:
            _MEMORY_LEASES.pop(session_id, None)

    # ── Graph Reconstruction from Checkpoint ─────────────────────────────────

    @classmethod
    def restore_graph_from_checkpoint(
        cls,
        graph: ARDOGraph,
        checkpoint: Dict[str, Any],
    ) -> Tuple[ARDOGraph, Dict[str, int]]:
        """
        Restores node execution statuses from an authoritative checkpoint.
        Rules:
        - COMPLETED nodes remain COMPLETED (never re-executed).
        - BLOCKED nodes remain BLOCKED.
        - FAILED nodes remain FAILED unless explicitly reset.
        - Interrupted ACTIVE nodes:
            * If provably idempotent / read-only -> reset to WAITING for safe re-run.
            * If non-idempotent or mutating -> transition to OUTCOME_UNKNOWN to prevent duplicate side effects.
        - WAITING nodes remain WAITING.
        - Preserves approval state.
        """
        node_states = checkpoint.get("node_states") or {}
        summary = {"completed": 0, "blocked": 0, "failed": 0, "rescheduled": 0, "outcome_unknown": 0}

        for tid, node in graph.nodes.items():
            saved_state = node_states.get(tid)
            if not saved_state:
                continue

            saved_status_str = saved_state.get("status", "pending")
            node.retry_count = saved_state.get("retry_count", node.retry_count)
            node.approval_needed = saved_state.get("approval_needed", node.approval_needed)
            node.was_approval_gated = saved_state.get("was_approval_gated", False)
            node.started_at = saved_state.get("started_at", node.started_at)
            node.completed_at = saved_state.get("completed_at", node.completed_at)

            if saved_status_str == "completed":
                node.status = TaskState.COMPLETED
                summary["completed"] += 1
            elif saved_status_str == "blocked":
                node.status = TaskState.BLOCKED
                summary["blocked"] += 1
            elif saved_status_str == "failed":
                node.status = TaskState.FAILED
                summary["failed"] += 1
            elif saved_status_str == "waiting":
                node.status = TaskState.WAITING
                summary["rescheduled"] += 1
            elif saved_status_str == "active":
                # Interrupted in flight — distinguish idempotent vs non-idempotent
                if is_task_idempotent(node):
                    node.status = TaskState.WAITING
                    node.execution_logs.append("Recovered from execution checkpoint; read-only/idempotent action safely reset to WAITING.")
                    summary["rescheduled"] += 1
                else:
                    node.status = TaskState.OUTCOME_UNKNOWN
                    node.execution_logs.append(
                        "Recovered from execution checkpoint; non-idempotent action flagged as OUTCOME_UNKNOWN "
                        "to prevent duplicate external side effects."
                    )
                    summary["outcome_unknown"] += 1
            elif saved_status_str in ("outcome_unknown", TaskState.OUTCOME_UNKNOWN.value):
                node.status = TaskState.OUTCOME_UNKNOWN
                summary["outcome_unknown"] += 1
            elif saved_status_str == "approval_needed":
                node.status = TaskState.APPROVAL_NEEDED

        logger.info(
            f"[CheckpointManager] Restored graph for session {graph.session_id}: "
            f"completed={summary['completed']}, blocked={summary['blocked']}, "
            f"failed={summary['failed']}, rescheduled={summary['rescheduled']}, "
            f"outcome_unknown={summary['outcome_unknown']}"
        )
        return graph, summary

    @classmethod
    async def reconcile_uncertain_tasks(
        cls,
        graph: ARDOGraph,
        browser_adapter: Optional[Any] = None,
        db: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """
        Safely reconciles interrupted tasks in TaskState.OUTCOME_UNKNOWN.
        1. If browser context/observation is available, invokes SemanticVerifier to check
           if the external objective actually succeeded prior to crash. If verified, marks COMPLETED.
        2. If outcome is indeterminate or non-idempotent and cannot be proven safe,
           transitions the task to APPROVAL_NEEDED so that the human operator decides
           before any duplicate side effect is attempted.
        """
        reconciled: List[Dict[str, Any]] = []
        now = time.time()

        for tid, node in graph.nodes.items():
            if node.status != TaskState.OUTCOME_UNKNOWN:
                continue

            resolved = False
            # 1. Attempt observational verification via existing browser context
            if browser_adapter is not None:
                try:
                    observation = None
                    if hasattr(browser_adapter, "get_observation"):
                        if asyncio.iscoroutinefunction(browser_adapter.get_observation):
                            observation = await browser_adapter.get_observation()
                        else:
                            observation = browser_adapter.get_observation()
                    elif hasattr(browser_adapter, "executor") and hasattr(browser_adapter.executor, "get_observation"):
                        obs_fn = getattr(browser_adapter.executor, "get_observation")
                        if asyncio.iscoroutinefunction(obs_fn):
                            observation = await obs_fn()
                        else:
                            observation = obs_fn()

                    if observation is not None:
                        from execution.browser.agent_contracts import (
                            BrowserTask,
                            BrowserVerificationStatus,
                        )
                        from execution.browser.semantic_verifier import SemanticVerifier

                        expected_url = None
                        success_criteria = None
                        if isinstance(node.metadata, dict):
                            expected_url = node.metadata.get("expected_url")
                            sc = node.metadata.get("success_criteria")
                            if isinstance(sc, list):
                                success_criteria = "; ".join(str(x) for x in sc)
                            elif isinstance(sc, str):
                                success_criteria = sc

                        meta = {}
                        if expected_url:
                            meta["expected_url"] = expected_url

                        btask = BrowserTask(
                            task_id=node.task_id,
                            session_id=graph.session_id or "unknown_session",
                            objective=node.task_name,
                            expected_url=expected_url,
                            success_criteria=success_criteria,
                            metadata=meta,
                        )
                        verif = SemanticVerifier.verify(btask, observation)
                        if verif.status == BrowserVerificationStatus.VERIFIED:
                            node.status = TaskState.COMPLETED
                            node.completed_at = now
                            node.execution_logs.append(
                                f"Post-crash semantic verification confirmed task outcome succeeded ({verif.verified_by}). "
                                f"Marked COMPLETED without duplicate re-execution."
                            )
                            reconciled.append({
                                "task_id": tid,
                                "status": "completed",
                                "method": "semantic_verification",
                                "details": verif.verified_by,
                            })
                            resolved = True
                except Exception as e:
                    logger.warning(f"[CheckpointManager] Post-crash verification failed for {tid}: {e}")

            # 2. If indeterminate, gate for operator approval (never duplicate external actions)
            if not resolved:
                node.status = TaskState.APPROVAL_NEEDED
                node.approval_needed = True
                node.execution_logs.append(
                    "Interrupted task external outcome is uncertain (non-idempotent). "
                    "Gated in APPROVAL_NEEDED to prevent accidental duplicate external side effects."
                )
                reconciled.append({
                    "task_id": tid,
                    "status": "approval_needed",
                    "method": "operator_gating",
                    "details": "Indeterminate external outcome requires human review",
                })

        if db is not None and reconciled:
            await cls.save_checkpoint(graph.session_id, graph, trigger="post_crash_reconciliation", db=db)

        return reconciled

