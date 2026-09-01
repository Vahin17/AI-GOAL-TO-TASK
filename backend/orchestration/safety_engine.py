"""
safety_engine.py — Phase 5: Hybrid Safety Engine
==================================================
Orchestrates the full safety evaluation pipeline:

1. SafetyAnalyzer → Batch LLM semantic analysis → {task_id: SafetyContext}
2. SafetyPolicyEngine → Deterministic evaluation → SafetyDecision per task
3. ApprovalBindings → Task-specific, hash-bound operator approvals
4. MongoDB Persistence → Full SafetyRecord with schema_version=2

This module is the SINGLE integration point between the safety pipeline
and the rest of the system (orchestrator, DAGBuilder, RuntimeEngine).

CRITICAL Rules:
    - The engine NEVER blocks task execution directly.
      It returns SafetyDecisions which the DAGBuilder and RuntimeEngine consume.
    - BLOCKED tasks (CRITICAL risk) are set to TaskState.BLOCKED at DAG build time.
      They can NEVER be transitioned to ACTIVE by operator approval.
    - APPROVAL_NEEDED tasks must have a valid, unmutatated ApprovalBinding
      before RuntimeEngine dispatches them.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from ai.safety_analyzer import SafetyAnalyzer
from orchestration.safety_policy import SafetyPolicyEngine
from orchestration.safety_schemas import (
    SafetyContext,
    SafetyDecision,
    SafetyRecord,
    ApprovalBinding,
    RiskLevel,
    ExecutionDecision,
    ApprovalStatus,
    PolicyID,
)

logger = logging.getLogger(__name__)


class SafetyEngine:
    """
    Hybrid Safety Engine — Central coordinator.

    Usage in orchestrator.py:
        engine = SafetyEngine(db=db)
        decisions = await engine.evaluate_plan(goal, plan_nodes, session_id, goal_id, plan_id)
        # decisions: {task_id: SafetyDecision}
    """

    def __init__(self, db=None):
        self.analyzer = SafetyAnalyzer()
        self.policy_engine = SafetyPolicyEngine()
        self.db = db
        # session_id → {task_id → ApprovalBinding}
        self._approval_bindings: Dict[str, Dict[str, ApprovalBinding]] = {}

    # ── Public API ─────────────────────────────────────────────────────────

    async def evaluate_plan(
        self,
        goal: str,
        plan_nodes: List[Dict],   # [{"task_id": ..., "task_name": ...}]
        session_id: str,
        goal_id: str,
        plan_id: str,
    ) -> Dict[str, SafetyDecision]:
        """
        Evaluate all tasks in a plan and return {task_id: SafetyDecision}.
        Persists SafetyRecord for each task to MongoDB.
        """
        # Step 1: Batch semantic analysis
        tasks_for_analysis = [
            {"task_id": n["task_id"], "task_name": n.get("task_name", n.get("label", ""))}
            for n in plan_nodes
        ]
        contexts = await self.analyzer.analyze_plan(goal, tasks_for_analysis)

        # Step 2: Deterministic policy evaluation per task
        decisions: Dict[str, SafetyDecision] = {}
        for node in plan_nodes:
            tid = node["task_id"]
            task_name = node.get("task_name", node.get("label", ""))
            ctx = contexts.get(tid, SafetyContext.unknown(tid))
            decision = self.policy_engine.evaluate(ctx, task_name)
            decisions[tid] = decision

            # Step 3: Persist safety record
            if self.db:
                await self._persist_safety_record(
                    decision, session_id, goal_id, plan_id, task_name
                )

        return decisions

    async def record_approval(
        self,
        session_id: str,
        task_id: str,
        task_name: str,
        safety_record_id: str,
        approved: bool,
    ) -> bool:
        """
        Record an operator approval/denial.
        Returns True if the approval was successfully applied (hash still valid).
        Returns False if the task content has changed (invalidated approval).

        Note: BLOCK decisions CANNOT be approved — this method will return False.
        """
        binding = self._get_binding(session_id, task_id)
        if not binding:
            logger.warning(
                f"[SafetyEngine] No binding found for session={session_id} task={task_id}"
            )
            return False

        current_hash = SafetyRecord.compute_content_hash(task_name)

        if approved:
            result = binding.approve(current_hash)
            if not result:
                logger.warning(
                    f"[SafetyEngine] Approval INVALIDATED for task={task_id}. "
                    "Task content changed after evaluation."
                )
            else:
                logger.info(f"[SafetyEngine] Task={task_id} approved by operator.")

            if self.db:
                await self._update_approval_record(
                    session_id, task_id, safety_record_id,
                    approved=result, binding=binding
                )
            return result
        else:
            binding.deny()
            logger.info(f"[SafetyEngine] Task={task_id} denied by operator.")
            if self.db:
                await self._update_approval_record(
                    session_id, task_id, safety_record_id,
                    approved=False, binding=binding
                )
            return False

    def register_approval_binding(
        self,
        session_id: str,
        task_id: str,
        safety_record_id: str,
        task_content_hash: str,
    ) -> ApprovalBinding:
        """Register a new approval binding for a task that needs approval."""
        binding = ApprovalBinding(
            session_id=session_id,
            task_id=task_id,
            safety_record_id=safety_record_id,
            task_content_hash=task_content_hash,
        )
        if session_id not in self._approval_bindings:
            self._approval_bindings[session_id] = {}
        self._approval_bindings[session_id][task_id] = binding
        return binding

    def is_approved(self, session_id: str, task_id: str, task_name: str) -> bool:
        """Check if a task has a valid, non-mutated approval."""
        binding = self._get_binding(session_id, task_id)
        if not binding or binding.status != ApprovalStatus.APPROVED:
            return False
        current_hash = SafetyRecord.compute_content_hash(task_name)
        if current_hash != binding.task_content_hash:
            binding.status = ApprovalStatus.INVALID
            logger.warning(
                f"[SafetyEngine] Approval INVALIDATED on check for task={task_id}."
            )
            return False
        return True

    def is_denied(self, session_id: str, task_id: str) -> bool:
        """Check if a task was explicitly denied by operator."""
        binding = self._get_binding(session_id, task_id)
        return binding is not None and binding.status == ApprovalStatus.DENIED

    # ── Private helpers ────────────────────────────────────────────────────

    def _get_binding(self, session_id: str, task_id: str) -> Optional[ApprovalBinding]:
        return self._approval_bindings.get(session_id, {}).get(task_id)

    async def _persist_safety_record(
        self,
        decision: SafetyDecision,
        session_id: str,
        goal_id: str,
        plan_id: str,
        task_name: str,
    ) -> None:
        if not self.db:
            return
        content_hash = SafetyRecord.compute_content_hash(task_name)
        record = SafetyRecord(
            session_id=session_id,
            goal_id=goal_id,
            plan_id=plan_id,
            task_id=decision.task_id,
            original_task=task_name,
            task_content_hash=content_hash,
            semantic_context=decision.semantic_context.model_dump() if decision.semantic_context else None,
            semantic_confidence=decision.semantic_confidence,
            risk_level=decision.risk_level,
            execution_decision=decision.execution_decision,
            triggered_policy_ids=[p.value for p in decision.triggered_policy_ids],
            explanation=decision.explanation,
            approval_required=decision.approval_required,
            approval_status=ApprovalStatus.PENDING if decision.approval_required else ApprovalStatus.APPROVED,
        )
        try:
            await self.db.safety_flags.insert_one(record.to_mongo_dict())
        except Exception as e:
            logger.error(f"[SafetyEngine] Failed to persist safety record: {e}")

    async def _update_approval_record(
        self,
        session_id: str,
        task_id: str,
        safety_record_id: str,
        approved: bool,
        binding: ApprovalBinding,
    ) -> None:
        if not self.db:
            return
        update = {
            "$set": {
                "approval_status": binding.status.value,
                "approved_at": binding.approved_at,
                "denied_at": binding.denied_at,
            }
        }
        try:
            await self.db.safety_flags.update_one(
                {"session_id": session_id, "task_id": task_id},
                update,
            )
        except Exception as e:
            logger.error(f"[SafetyEngine] Failed to update approval record: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Module-level singleton getter
# ─────────────────────────────────────────────────────────────────────────────

_engine_cache: Dict[str, SafetyEngine] = {}


def get_safety_engine(db=None) -> SafetyEngine:
    """Get or create a SafetyEngine instance for the current session."""
    key = id(db)
    if key not in _engine_cache:
        _engine_cache[key] = SafetyEngine(db=db)
    return _engine_cache[key]
