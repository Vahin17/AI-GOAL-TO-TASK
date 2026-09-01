"""
explainability_engine.py — ARDO Scheduler Explainability Layer
===============================================================
Every scheduling decision ARDO makes is explained in structured,
human-readable form. This is critical for:

    - Academic transparency (viva / project defense)
    - Operator trust — "why is task X running before task Y?"
    - Debugging unexpected scheduling behavior
    - Explainable AI orchestration principle

Output:
    SchedulingExplanation dataclass per task, surfaced via:
        - /orchestrate/{id}/explain/{task_id} API endpoint
        - OrchestrationMonitor → "Scheduler Decisions" section
        - "Why This Task Now?" active panel (live, updates per transition)
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from orchestration.dag_builder import ARDOGraph, ARDOTaskNode
    from orchestration.priority_engine import PriorityEngine

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Explanation Data Models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SchedulingExplanation:
    """
    Structured explanation of why a task received its priority score
    and why it was selected for execution at a given moment.
    """
    task_id: str
    task_name: str
    final_score: float
    factor_breakdown: Dict[str, float]       # factor_name → weighted contribution
    raw_factors: Dict[str, float]            # factor_name → raw (unweighted) value
    decision_reason: str                     # Human-readable primary reason
    selected_factors: List[str]              # Top factors that influenced the score
    rescheduling_cause: Optional[str] = None # Why the order changed (if applicable)
    why_now: Optional[str] = None            # "Why this task is executing NOW"
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "task_name": self.task_name,
            "final_score": self.final_score,
            "factor_breakdown": self.factor_breakdown,
            "raw_factors": self.raw_factors,
            "decision_reason": self.decision_reason,
            "selected_factors": self.selected_factors,
            "rescheduling_cause": self.rescheduling_cause,
            "why_now": self.why_now,
            "timestamp": self.timestamp,
        }


@dataclass
class ReschedulingEvent:
    """Records why the execution queue was reordered."""
    trigger_task_id: str
    trigger_event: str              # completed | failed | approved | retrying
    previous_queue: List[str]
    new_queue: List[str]
    reason: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "trigger_task_id": self.trigger_task_id,
            "trigger_event": self.trigger_event,
            "previous_queue": self.previous_queue,
            "new_queue": self.new_queue,
            "reason": self.reason,
            "timestamp": self.timestamp,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Explainability Engine
# ─────────────────────────────────────────────────────────────────────────────

class ExplainabilityEngine:
    """
    Generates human-readable scheduling explanations for every ARDO decision.

    Maintains:
        - Per-task explanation history
        - Rescheduling event log (why the queue changed)
        - Active "Why Now?" context for the currently executing task
    """

    def __init__(self):
        # task_id → list of explanations (ordered by time)
        self._explanations: Dict[str, List[SchedulingExplanation]] = {}
        # Global log of rescheduling events
        self._reschedule_log: List[ReschedulingEvent] = []
        # Current "Why Now?" for the active task
        self._active_why_now: Optional[SchedulingExplanation] = None

    def explain_scheduling_decision(
        self,
        node: "ARDOTaskNode",
        graph: "ARDOGraph",
        priority_engine: "PriorityEngine",
        rescheduling_cause: Optional[str] = None,
    ) -> SchedulingExplanation:
        """
        Generate a full scheduling explanation for a task node.

        Args:
            node:               The task node being explained.
            graph:              Current graph state.
            priority_engine:    Used to re-compute factor breakdown.
            rescheduling_cause: If the queue was just reordered, why?

        Returns:
            SchedulingExplanation with all fields populated.
        """
        final_score, factor_breakdown = priority_engine.score(node, graph)

        # Compute raw (unweighted) factor values for transparency
        raw_factors = self._compute_raw_factors(node, graph, priority_engine)

        # Identify top contributing factors
        selected_factors = sorted(
            factor_breakdown.items(), key=lambda x: -x[1]
        )
        top_factors = [k for k, v in selected_factors if v > 0.05]

        # Generate human-readable decision reason
        decision_reason = self._generate_reason(
            node, factor_breakdown, raw_factors, graph
        )

        # Generate "Why Now?" context
        why_now = self._generate_why_now(node, graph, factor_breakdown)

        explanation = SchedulingExplanation(
            task_id=node.task_id,
            task_name=node.task_name,
            final_score=final_score,
            factor_breakdown=factor_breakdown,
            raw_factors=raw_factors,
            decision_reason=decision_reason,
            selected_factors=top_factors,
            rescheduling_cause=rescheduling_cause,
            why_now=why_now,
        )

        # Store in history
        self._explanations.setdefault(node.task_id, []).append(explanation)

        logger.debug(
            f"[Explainability] task={node.task_id} score={final_score:.3f} "
            f"reason='{decision_reason}'"
        )
        return explanation

    def record_reschedule_event(
        self,
        trigger_task_id: str,
        trigger_event: str,
        previous_queue: List[str],
        new_queue: List[str],
        graph: "ARDOGraph",
    ) -> ReschedulingEvent:
        """
        Record and explain a queue reordering event.
        """
        # Generate human-readable reason for the reordering
        moved_up = [tid for tid in new_queue if tid not in previous_queue[:3]]
        moved_down = [tid for tid in previous_queue[:3] if tid in new_queue[3:]]

        reason = (
            f"Queue reordered after '{trigger_event}' on task "
            f"'{graph.get_node(trigger_task_id).task_name if graph.get_node(trigger_task_id) else trigger_task_id}'. "
        )
        if moved_up:
            names = [graph.get_node(t).task_name if graph.get_node(t) else t for t in moved_up[:2]]
            reason += f"Elevated: {', '.join(names)}. "
        if moved_down:
            names = [graph.get_node(t).task_name if graph.get_node(t) else t for t in moved_down[:2]]
            reason += f"Deferred: {', '.join(names)}."

        event = ReschedulingEvent(
            trigger_task_id=trigger_task_id,
            trigger_event=trigger_event,
            previous_queue=previous_queue[:10],
            new_queue=new_queue[:10],
            reason=reason,
        )
        self._reschedule_log.append(event)
        logger.info(f"[Explainability] Reschedule recorded: {reason}")
        return event

    def set_active_why_now(self, explanation: SchedulingExplanation) -> None:
        """Update the currently displayed "Why This Task Now?" context."""
        self._active_why_now = explanation

    def get_active_why_now(self) -> Optional[SchedulingExplanation]:
        return self._active_why_now

    def get_task_explanations(self, task_id: str) -> List[dict]:
        return [e.to_dict() for e in self._explanations.get(task_id, [])]

    def get_reschedule_log(self, limit: int = 20) -> List[dict]:
        return [e.to_dict() for e in self._reschedule_log[-limit:]]

    def get_latest_explanation(self, task_id: str) -> Optional[dict]:
        history = self._explanations.get(task_id, [])
        return history[-1].to_dict() if history else None

    # ── Private helpers ────────────────────────────────────────────────────

    def _compute_raw_factors(
        self, node: "ARDOTaskNode", graph: "ARDOGraph", engine: "PriorityEngine"
    ) -> Dict[str, float]:
        """Compute raw (unweighted) factor values for the explanation."""
        depth = engine._get_depth(node.task_id, graph)
        max_depth = max(
            (engine._get_depth(tid, graph) for tid in graph.nodes), default=1
        )
        succ_count = len(graph.successors(node.task_id))
        max_succ = max((len(graph.successors(t)) for t in graph.nodes), default=1)
        max_time = max((n.estimated_execution_time for n in graph.nodes.values()), default=1.0)

        return {
            "dependency_depth": depth,
            "max_depth_in_graph": max_depth,
            "risk_score": node.risk_score,
            "successor_count": succ_count,
            "estimated_time_seconds": node.estimated_execution_time,
            "retry_confidence": node.retry_confidence,
            "retry_count": node.retry_count,
        }

    def _generate_reason(
        self,
        node: "ARDOTaskNode",
        breakdown: Dict[str, float],
        raw: Dict[str, float],
        graph: "ARDOGraph",
    ) -> str:
        """Generate a concise human-readable explanation for the priority score."""
        parts: List[str] = []

        top_factor = max(breakdown, key=breakdown.get)

        if top_factor == "dependency":
            depth = raw.get("dependency_depth", 0)
            parts.append(
                f"On the critical dependency path (depth {depth}) — "
                "scheduling early minimises downstream waiting time."
            )
        elif top_factor == "safety":
            risk = raw.get("risk_score", 0)
            if risk < 0.2:
                parts.append("Low-risk task — safe to execute early without operator oversight.")
            else:
                parts.append(f"Moderate risk ({risk:.1%}) — elevated but below approval threshold.")
        elif top_factor == "criticality":
            succs = raw.get("successor_count", 0)
            parts.append(
                f"High criticality — unblocks {succs} downstream task(s) upon completion."
            )
        elif top_factor == "execution_cost":
            t = raw.get("estimated_time_seconds", 0)
            parts.append(f"Fast execution (~{t:.0f}s) improves throughput.")
        elif top_factor == "retry_confidence":
            conf = raw.get("retry_confidence", 1.0)
            parts.append(f"Retry confidence at {conf:.0%} — reliable execution history.")

        if node.retry_count > 0:
            parts.append(
                f"Note: {node.retry_count} prior attempt(s). "
                f"Confidence reduced to {node.retry_confidence:.0%}."
            )

        return " ".join(parts) or "Standard scheduling decision based on weighted priority factors."

    def _generate_why_now(
        self,
        node: "ARDOTaskNode",
        graph: "ARDOGraph",
        breakdown: Dict[str, float],
    ) -> str:
        """Generate the 'Why this task NOW?' panel text."""
        reasons: List[str] = []

        # Dependency resolution
        completed_deps = [
            dep_id for dep_id in node.dependency_ids
            if graph.get_node(dep_id) and graph.get_node(dep_id).status.value == "completed"
        ]
        if len(completed_deps) == len(node.dependency_ids) and node.dependency_ids:
            reasons.append(
                f"All {len(completed_deps)} dependenc{'y' if len(completed_deps)==1 else 'ies'} completed."
            )
        elif not node.dependency_ids:
            reasons.append("No dependencies — root task, eligible from start.")

        # Risk
        if node.risk_score < 0.3:
            reasons.append("Low risk score — no approval required.")
        elif node.risk_score >= 0.7:
            reasons.append("High risk — operator approval was granted.")

        # Priority rank
        sorted_nodes = sorted(
            graph.nodes.values(),
            key=lambda n: n.priority_score, reverse=True
        )
        rank = next(
            (i + 1 for i, n in enumerate(sorted_nodes) if n.task_id == node.task_id), "?"
        )
        reasons.append(f"Highest priority among ready tasks (rank #{rank}).")

        # Criticality
        succ_count = len(graph.successors(node.task_id))
        if succ_count > 1:
            reasons.append(f"Completing this unblocks {succ_count} downstream tasks.")

        return "Selected because: " + " ".join(reasons)
