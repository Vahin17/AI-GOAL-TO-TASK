"""
retry_manager.py — ARDO Retry & Confidence Engine
==================================================
Implements intelligent retry policies with exponential backoff + jitter.
Also tracks retry confidence — a per-task score that degrades on failure
and recovers on success, directly influencing the priority engine.

Design:
    Exponential backoff with full jitter prevents "retry storms" where
    all failed tasks retry simultaneously. Confidence scoring implements
    the execution feedback loop (requirement 7 from ARDO spec).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from orchestration.dag_builder import ARDOTaskNode

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Retry Policy
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RetryPolicy:
    """
    Retry configuration for ARDO task nodes.

    Args:
        max_retries:         Maximum attempts before marking as FAILED.
        base_delay_seconds:  Initial delay before first retry.
        backoff_multiplier:  Each retry delay = base * (multiplier ^ attempt).
        max_delay_seconds:   Cap on any single retry delay.
        jitter:              Whether to add random jitter to avoid thundering herd.
        confidence_decay:    How much retry_confidence decreases per failure.
        confidence_recovery: How much retry_confidence increases per success.
    """
    max_retries:        int   = 3
    base_delay_seconds: float = 2.0
    backoff_multiplier: float = 2.0
    max_delay_seconds:  float = 30.0
    jitter:             bool  = True
    confidence_decay:   float = 0.20
    confidence_recovery: float = 0.10


DEFAULT_POLICY = RetryPolicy()

# Aggressive mode: faster retries
AGGRESSIVE_POLICY = RetryPolicy(
    max_retries=5,
    base_delay_seconds=0.5,
    backoff_multiplier=1.5,
    max_delay_seconds=10.0,
)

# Safe mode: cautious retries
SAFE_POLICY = RetryPolicy(
    max_retries=2,
    base_delay_seconds=5.0,
    backoff_multiplier=3.0,
    max_delay_seconds=60.0,
)


# ─────────────────────────────────────────────────────────────────────────────
# Retry Manager
# ─────────────────────────────────────────────────────────────────────────────

class RetryManager:
    """
    Manages retry state and confidence for ARDO task nodes.

    Tracks:
        - Per-task retry counts (beyond what's stored in ARDOTaskNode)
        - Per-task failure timestamps for backoff calculation
        - Global retry event history for the execution_monitor
    """

    def __init__(self, policy: Optional[RetryPolicy] = None):
        self.policy = policy or DEFAULT_POLICY
        # task_id → timestamp of last failure
        self._last_failure: Dict[str, float] = {}

    @classmethod
    def for_mode(cls, mode: str) -> "RetryManager":
        """Factory: return a RetryManager configured for the given execution mode."""
        policies = {
            "aggressive": AGGRESSIVE_POLICY,
            "safe":       SAFE_POLICY,
            "sequential": DEFAULT_POLICY,
            "parallel":   DEFAULT_POLICY,
        }
        return cls(policy=policies.get(mode, DEFAULT_POLICY))

    def can_retry(self, node: "ARDOTaskNode") -> bool:
        """Returns True if the task has remaining retry attempts."""
        return node.retry_count < node.max_retries

    def get_delay(self, retry_count: int) -> float:
        """
        Compute the next retry delay using exponential backoff with optional jitter.

        Formula:
            delay = min(base * (multiplier ^ retry_count), max_delay)
            if jitter: delay *= random(0.5, 1.0)
        """
        p = self.policy
        delay = p.base_delay_seconds * (p.backoff_multiplier ** retry_count)
        delay = min(delay, p.max_delay_seconds)
        if p.jitter:
            delay *= random.uniform(0.5, 1.0)
        return round(delay, 2)

    def record_failure(self, node: "ARDOTaskNode") -> None:
        """
        Record a task failure: increment retry_count, decay confidence,
        update last failure timestamp.
        """
        node.retry_count += 1
        self._last_failure[node.task_id] = time.monotonic()

        # Decay confidence — implements the execution feedback loop
        node.retry_confidence = round(
            max(0.0, node.retry_confidence - self.policy.confidence_decay), 4
        )
        node.execution_logs.append(
            f"[RETRY {node.retry_count}/{node.max_retries}] "
            f"confidence→{node.retry_confidence:.2f}"
        )
        logger.warning(
            f"[RetryManager] task={node.task_id} retry={node.retry_count} "
            f"confidence={node.retry_confidence:.2f}"
        )

    def record_success(self, node: "ARDOTaskNode") -> None:
        """
        Record a successful execution: recover confidence slightly.
        Confidence never exceeds 1.0.
        """
        node.retry_confidence = round(
            min(1.0, node.retry_confidence + self.policy.confidence_recovery), 4
        )
        self._last_failure.pop(node.task_id, None)
        logger.info(
            f"[RetryManager] task={node.task_id} succeeded. "
            f"confidence→{node.retry_confidence:.2f}"
        )

    async def wait_before_retry(self, node: "ARDOTaskNode") -> None:
        """Async sleep for the computed backoff delay."""
        delay = self.get_delay(node.retry_count)
        logger.info(
            f"[RetryManager] task={node.task_id} backoff={delay:.2f}s "
            f"(attempt {node.retry_count + 1}/{node.max_retries})"
        )
        await asyncio.sleep(delay)
