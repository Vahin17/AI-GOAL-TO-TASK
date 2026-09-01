"""
priority_engine.py — ARDO Weighted Priority Scoring Engine
===========================================================
Computes a dynamic priority score for every ready task in the ARDO graph.
Tasks with higher scores are scheduled first.

Academic Grounding:
-------------------
ARDO's priority algorithm is inspired by three established approaches:

1. HEFT (Heterogeneous Earliest Finish Time) — Topcuoglu et al. (2002)
   HEFT computes upward rank values based on computation cost and
   communication cost along the longest path. ARDO adapts this concept
   by using dependency depth as a proxy for "upward rank" — tasks on the
   critical path (deepest dependency chains) receive higher weights.

2. Weighted Priority Scheduling — used in real-time OS scheduling
   (EDF, WFQ, WRR). Each scheduling factor gets a configurable weight
   coefficient, allowing the orchestrator to be tuned for different
   workload profiles without changing the algorithm structure.

3. Risk-Aware Reactive Orchestration — inspired by modern AI browser
   agents and workflow engines (Temporal.io, Prefect, Airflow) that
   integrate safety/risk signals into scheduling decisions, not just
   execution order.

Factor Rationale:
-----------------
- dependency_weight = 0.30 (HIGHEST)
  Tasks deeper in the dependency chain block the most downstream work.
  Scheduling them first minimises total waiting time for dependent tasks.
  This mirrors the "critical path" insight from HEFT.

- safety_weight = 0.25 (NEAR-PRIMARY)
  Safety is treated as a first-class scheduling concern, not an
  afterthought. High-risk tasks are deprioritised, keeping the early
  execution queue low-risk. This prevents catastrophic failures early
  in the workflow when recovery options are most available.

- task_criticality = 0.20
  Tasks explicitly marked critical (e.g., system setup, auth) are
  elevated. This replaces manual prioritisation.

- execution_cost = 0.15
  Lower-cost tasks are preferred when scores are otherwise equal —
  this improves overall throughput by completing quick tasks early
  and freeing up scheduling bandwidth.

- retry_confidence = 0.10 (LOWEST)
  Retry confidence modulates scheduling order but has the lowest weight
  to prevent a single failed-then-retried task from permanently
  dominating the queue. This avoids retry bias.

Coefficient Balance:
    Sum of weights = 1.00 (normalised)
    No single factor exceeds 30% to prevent factor domination.
    This ensures diverse task characteristics all contribute to scheduling.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, TYPE_CHECKING

if TYPE_CHECKING:
    from orchestration.dag_builder import ARDOGraph, ARDOTaskNode

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Configurable Weight Coefficients
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PriorityWeights:
    """
    Configurable weight coefficients for the ARDO priority formula.
    All weights must sum to 1.0 for the score to remain in [0, 1].

    Can be overridden at runtime by the weight_optimizer module when
    adaptive self-tuning is enabled in future iterations.
    """
    dependency: float = 0.30   # Influence of dependency depth (critical path)
    safety:     float = 0.25   # Inverse of risk_score (safer = higher)
    criticality: float = 0.20  # Task importance flag
    execution_cost: float = 0.15  # Inverse of estimated time (cheaper = higher)
    retry_confidence: float = 0.10  # Degrades with failures, improves with successes

    def validate(self) -> None:
        total = (
            self.dependency + self.safety + self.criticality +
            self.execution_cost + self.retry_confidence
        )
        if abs(total - 1.0) > 0.001:
            raise ValueError(
                f"PriorityWeights must sum to 1.0 (got {total:.3f}). "
                "Adjust weights to maintain a normalised score range."
            )


DEFAULT_WEIGHTS = PriorityWeights()


# ─────────────────────────────────────────────────────────────────────────────
# Priority Engine
# ─────────────────────────────────────────────────────────────────────────────

class PriorityEngine:
    """
    Computes dynamic priority scores for ARDO task nodes.

    The score is a weighted sum of five independent factors, each
    normalised to [0, 1] before weighting. This ensures no single factor
    can dominate the final score unless explicitly weighted to do so.

    Usage:
        engine = PriorityEngine()
        score, breakdown = engine.score(node, graph)
        queue = engine.build_execution_queue(graph)
    """

    def __init__(self, weights: PriorityWeights = DEFAULT_WEIGHTS):
        weights.validate()
        self.weights = weights
        # Depth cache: task_id → dependency depth (computed once per graph)
        self._depth_cache: Dict[str, int] = {}

    def score(
        self, node: "ARDOTaskNode", graph: "ARDOGraph"
    ) -> tuple[float, Dict[str, float]]:
        """
        Compute priority score and factor breakdown for a single task node.

        Returns:
            (final_score, factor_breakdown)
            factor_breakdown maps factor name → weighted contribution.
        """
        w = self.weights

        # Factor 1: Dependency depth (critical path influence)
        depth = self._get_depth(node.task_id, graph)
        max_depth = max((self._get_depth(tid, graph) for tid in graph.nodes), default=1)
        dep_factor = depth / max_depth if max_depth > 0 else 0.0

        # Factor 2: Safety (inverse of risk — safer tasks score higher)
        safety_factor = 1.0 - node.risk_score

        # Factor 3: Task criticality (heuristic: tasks with many successors = critical)
        successor_count = len(graph.successors(node.task_id))
        max_successors = max(
            (len(graph.successors(tid)) for tid in graph.nodes), default=1
        )
        criticality_factor = successor_count / max_successors if max_successors > 0 else 0.5

        # Factor 4: Execution cost (inverse of time — cheaper tasks score higher)
        max_time = max(
            (n.estimated_execution_time for n in graph.nodes.values()), default=1.0
        )
        cost_factor = 1.0 - (node.estimated_execution_time / max_time) if max_time > 0 else 0.5

        # Factor 5: Retry confidence (degrades with failures)
        confidence_factor = max(0.0, min(1.0, node.retry_confidence))

        # Weighted sum
        final_score = (
            w.dependency     * dep_factor       +
            w.safety         * safety_factor    +
            w.criticality    * criticality_factor +
            w.execution_cost * cost_factor      +
            w.retry_confidence * confidence_factor
        )
        final_score = round(min(1.0, max(0.0, final_score)), 4)

        breakdown = {
            "dependency":      round(w.dependency * dep_factor, 4),
            "safety":          round(w.safety * safety_factor, 4),
            "criticality":     round(w.criticality * criticality_factor, 4),
            "execution_cost":  round(w.execution_cost * cost_factor, 4),
            "retry_confidence": round(w.retry_confidence * confidence_factor, 4),
        }

        return final_score, breakdown

    def build_execution_queue(self, graph: "ARDOGraph") -> List[str]:
        """
        Score all nodes and return a sorted execution queue (highest score first).
        Excludes terminal nodes (completed, blocked).
        """
        from orchestration.state_machine import TaskState

        self._depth_cache.clear()  # Invalidate cache for fresh graph state
        scored: List[tuple[float, str]] = []

        for task_id, node in graph.nodes.items():
            if node.status in (TaskState.COMPLETED, TaskState.BLOCKED):
                continue
            score, _ = self.score(node, graph)
            # Write computed score back to node
            graph.nodes[task_id].priority_score = score
            scored.append((score, task_id))

        # Sort descending by score; tie-break by task_id for determinism
        scored.sort(key=lambda x: (-x[0], x[1]))
        queue = [task_id for _, task_id in scored]

        logger.info(
            f"[PriorityEngine] Execution queue built: "
            + ", ".join(f"{tid}({s:.2f})" for s, tid in scored[:5])
            + ("..." if len(scored) > 5 else "")
        )
        return queue

    # ── Private helpers ────────────────────────────────────────────────────

    def _get_depth(self, task_id: str, graph: "ARDOGraph") -> int:
        """
        Compute the longest dependency chain depth for a node.
        Depth 0 = root node (no predecessors).
        Cached to avoid recomputation on large graphs.
        """
        if task_id in self._depth_cache:
            return self._depth_cache[task_id]

        preds = graph.predecessors(task_id)
        if not preds:
            self._depth_cache[task_id] = 0
            return 0

        depth = 1 + max(self._get_depth(p, graph) for p in preds)
        self._depth_cache[task_id] = depth
        return depth
