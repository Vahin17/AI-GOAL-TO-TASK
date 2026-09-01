"""
weight_optimizer.py — ARDO Metric-Driven Weight Optimization Foundation
=======================================================================
Stores execution outcomes, tracks success/failure trends, and suggests
future weight recalibration for the PriorityEngine.

Current status: Foundation layer (Phase 1)
    - Outcome storage and trend tracking: IMPLEMENTED
    - Efficiency statistics: IMPLEMENTED
    - Weight suggestion heuristics: IMPLEMENTED
    - ML-driven optimisation: PLANNED for future phase

Academic justification:
    This module demonstrates "future adaptive optimization support" —
    an architecturally sound foundation for evolving ARDO from a
    fixed-weight scheduler toward a self-optimising system.

    The approach is inspired by:
    - Reinforcement Learning from execution feedback (reward shaping)
    - Bayesian optimization of hyperparameters (weight coefficients)
    - A/B testing of scheduling configurations in production systems

Why this matters now (even without ML):
    By recording all execution outcomes with associated weights, we
    accumulate the dataset needed for future ML training. The system
    is "future-ready" without prematurely adding ML complexity.
"""

from __future__ import annotations

import time
import json
import logging
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from orchestration.priority_engine import PriorityWeights

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Execution Outcome Record
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ExecutionOutcome:
    """
    Records what happened when a task executed under the current weights.
    Used to correlate scheduling decisions with actual outcomes.
    """
    task_id: str
    task_name: str
    session_id: str
    weights_used: Dict[str, float]    # Weight snapshot at time of scheduling
    priority_score: float
    factor_breakdown: Dict[str, float]
    outcome: str                      # "success" | "failure" | "retry_success"
    retry_count: int
    actual_execution_time: float
    estimated_execution_time: float
    wait_time: float                  # How long it waited before executing
    timestamp: float = field(default_factory=time.time)

    @property
    def time_accuracy(self) -> float:
        """How accurate was the execution time estimate? 1.0 = perfect."""
        if self.estimated_execution_time <= 0:
            return 0.0
        ratio = self.actual_execution_time / self.estimated_execution_time
        # Perfect = 1.0, penalise over or under estimation
        return max(0.0, 1.0 - abs(ratio - 1.0))

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "task_name": self.task_name,
            "session_id": self.session_id,
            "weights_used": self.weights_used,
            "priority_score": self.priority_score,
            "factor_breakdown": self.factor_breakdown,
            "outcome": self.outcome,
            "retry_count": self.retry_count,
            "actual_execution_time": self.actual_execution_time,
            "estimated_execution_time": self.estimated_execution_time,
            "wait_time": self.wait_time,
            "time_accuracy": self.time_accuracy,
            "timestamp": self.timestamp,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Weight Suggestion
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class WeightSuggestion:
    """A suggested adjustment to the priority weight coefficients."""
    suggested_weights: Dict[str, float]
    rationale: str
    confidence: float           # How confident the optimizer is [0–1]
    based_on_n_outcomes: int
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "suggested_weights": self.suggested_weights,
            "rationale": self.rationale,
            "confidence": self.confidence,
            "based_on_n_outcomes": self.based_on_n_outcomes,
            "timestamp": self.timestamp,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Weight Optimizer
# ─────────────────────────────────────────────────────────────────────────────

class WeightOptimizer:
    """
    Collects execution outcomes and derives weight recalibration suggestions.

    Phase 1 (current): Heuristic trend analysis
        - High failure rate with high-dependency tasks → increase dependency weight
        - High retry rate for risky tasks → increase safety weight
        - Consistently poor time estimates → adjust execution_cost weight

    Phase 2 (future): Bayesian / RL-based optimisation using the
        accumulated outcomes dataset stored by this module.
    """

    # Minimum outcomes required before making suggestions
    MIN_OUTCOMES_FOR_SUGGESTION = 5

    def __init__(self):
        self._outcomes: List[ExecutionOutcome] = []
        self._suggestions: List[WeightSuggestion] = []

    def record_outcome(self, outcome: ExecutionOutcome) -> None:
        """Store a new execution outcome."""
        self._outcomes.append(outcome)
        logger.debug(
            f"[WeightOptimizer] Recorded outcome: task={outcome.task_id} "
            f"result={outcome.outcome} retries={outcome.retry_count}"
        )
        # Trigger suggestion analysis after sufficient data
        if len(self._outcomes) >= self.MIN_OUTCOMES_FOR_SUGGESTION:
            self._analyse_and_suggest()

    def get_efficiency_stats(self) -> dict:
        """
        Compute execution efficiency statistics across all recorded outcomes.
        """
        if not self._outcomes:
            return {"status": "no_data", "outcome_count": 0}

        successes = [o for o in self._outcomes if o.outcome in ("success", "retry_success")]
        failures = [o for o in self._outcomes if o.outcome == "failure"]
        retried = [o for o in self._outcomes if o.retry_count > 0]

        success_rate = len(successes) / len(self._outcomes)
        retry_rate = len(retried) / len(self._outcomes)

        wait_times = [o.wait_time for o in self._outcomes]
        exec_times = [o.actual_execution_time for o in self._outcomes]
        accuracies = [o.time_accuracy for o in self._outcomes]

        return {
            "outcome_count": len(self._outcomes),
            "success_rate": round(success_rate, 3),
            "failure_rate": round(1 - success_rate, 3),
            "retry_rate": round(retry_rate, 3),
            "avg_wait_time_seconds": round(statistics.mean(wait_times), 2) if wait_times else 0,
            "avg_execution_time_seconds": round(statistics.mean(exec_times), 2) if exec_times else 0,
            "avg_time_estimate_accuracy": round(statistics.mean(accuracies), 3) if accuracies else 0,
            "total_retries": sum(o.retry_count for o in self._outcomes),
        }

    def get_latest_suggestion(self) -> Optional[dict]:
        """Return the most recent weight suggestion, if any."""
        if not self._suggestions:
            return None
        return self._suggestions[-1].to_dict()

    def get_all_suggestions(self) -> List[dict]:
        return [s.to_dict() for s in self._suggestions]

    def get_outcomes_for_db(self, session_id: str) -> List[dict]:
        """Return outcomes for a session, ready for MongoDB persistence."""
        return [
            o.to_dict() for o in self._outcomes
            if o.session_id == session_id
        ]

    # ── Private analysis ───────────────────────────────────────────────────

    def _analyse_and_suggest(self) -> None:
        """
        Heuristic weight analysis based on observed trends.
        Generates a WeightSuggestion if meaningful adjustment is warranted.
        """
        outcomes = self._outcomes
        n = len(outcomes)

        # Current baseline
        from orchestration.priority_engine import PriorityWeights, DEFAULT_WEIGHTS
        current = {
            "dependency":      DEFAULT_WEIGHTS.dependency,
            "safety":          DEFAULT_WEIGHTS.safety,
            "criticality":     DEFAULT_WEIGHTS.criticality,
            "execution_cost":  DEFAULT_WEIGHTS.execution_cost,
            "retry_confidence": DEFAULT_WEIGHTS.retry_confidence,
        }
        suggested = dict(current)
        reasons: List[str] = []
        confidence = 0.3  # Base confidence

        # Analysis 1: High failure rate among high-dependency tasks
        high_dep_failures = [
            o for o in outcomes
            if o.outcome == "failure"
            and o.factor_breakdown.get("dependency", 0) > 0.2
        ]
        if len(high_dep_failures) / n > 0.30:
            # Too many critical-path failures → reduce dependency weight,
            # increase safety weight to be more conservative
            suggested["dependency"] = max(0.20, current["dependency"] - 0.05)
            suggested["safety"] = min(0.35, current["safety"] + 0.05)
            reasons.append(
                "High failure rate on dependency-critical tasks. "
                "Suggest increasing safety weight to deprioritise risky critical tasks."
            )
            confidence += 0.15

        # Analysis 2: Consistently poor time estimates
        accuracies = [o.time_accuracy for o in outcomes]
        avg_accuracy = statistics.mean(accuracies) if accuracies else 1.0
        if avg_accuracy < 0.5:
            # Execution cost estimates are unreliable → reduce their influence
            suggested["execution_cost"] = max(0.05, current["execution_cost"] - 0.05)
            suggested["criticality"] = min(0.30, current["criticality"] + 0.05)
            reasons.append(
                f"Low time estimate accuracy ({avg_accuracy:.0%}). "
                "Suggest reducing execution_cost weight influence."
            )
            confidence += 0.10

        # Analysis 3: High retry rate → increase confidence weight to
        # penalise unreliable tasks more
        retry_rate = sum(1 for o in outcomes if o.retry_count > 0) / n
        if retry_rate > 0.40:
            suggested["retry_confidence"] = min(0.20, current["retry_confidence"] + 0.05)
            reasons.append(
                f"High retry rate ({retry_rate:.0%}). "
                "Increase retry_confidence weight to better penalise unstable tasks."
            )
            confidence += 0.10

        # Normalise suggested weights to sum to 1.0
        total = sum(suggested.values())
        if abs(total - 1.0) > 0.001:
            factor = 1.0 / total
            suggested = {k: round(v * factor, 3) for k, v in suggested.items()}

        # Only emit suggestion if meaningfully different from current
        changed = any(
            abs(suggested[k] - current[k]) > 0.02 for k in current
        )
        if changed and reasons:
            suggestion = WeightSuggestion(
                suggested_weights=suggested,
                rationale=" | ".join(reasons),
                confidence=min(1.0, confidence),
                based_on_n_outcomes=n,
            )
            self._suggestions.append(suggestion)
            logger.info(
                f"[WeightOptimizer] New suggestion: confidence={confidence:.2f} "
                f"based on {n} outcomes."
            )
