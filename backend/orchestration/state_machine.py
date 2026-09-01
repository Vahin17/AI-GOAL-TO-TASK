"""
state_machine.py — ARDO Formal Execution State Machine
=======================================================
Provides deterministic, validated state transitions for every task node
in the ARDO orchestration graph. Rejecting invalid transitions makes the
runtime robust and production-grade — no task can silently skip states.

Allowed State Transitions:
    pending         → waiting
    pending         → approval_needed
    waiting         → active
    waiting         → blocked           (dependency failed)
    active          → completed
    active          → failed
    active          → retrying
    active          → approval_needed
    retrying        → active
    retrying        → failed            (max retries reached)
    failed          → blocked           (branch isolation)
    approval_needed → active            (operator approved)
    approval_needed → blocked           (operator rejected)
    blocked         → (terminal — no exit without manual reset)
    completed       → (terminal)
"""

from enum import Enum
from typing import Dict, Set, Tuple
import logging

logger = logging.getLogger(__name__)


class TaskState(str, Enum):
    PENDING          = "pending"
    WAITING          = "waiting"
    ACTIVE           = "active"
    RETRYING         = "retrying"
    COMPLETED        = "completed"
    FAILED           = "failed"
    BLOCKED          = "blocked"
    APPROVAL_NEEDED  = "approval_needed"


# Adjacency map: state → set of valid target states
_VALID_TRANSITIONS: Dict[TaskState, Set[TaskState]] = {
    TaskState.PENDING: {
        TaskState.WAITING,
        TaskState.APPROVAL_NEEDED,
    },
    TaskState.WAITING: {
        TaskState.ACTIVE,
        TaskState.BLOCKED,
    },
    TaskState.ACTIVE: {
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.RETRYING,
        TaskState.APPROVAL_NEEDED,
    },
    TaskState.RETRYING: {
        TaskState.ACTIVE,
        TaskState.FAILED,
    },
    TaskState.FAILED: {
        TaskState.BLOCKED,
    },
    TaskState.APPROVAL_NEEDED: {
        TaskState.ACTIVE,
        TaskState.BLOCKED,
    },
    # Terminal states — no outgoing transitions
    TaskState.COMPLETED: set(),
    TaskState.BLOCKED:   set(),
}


class InvalidStateTransitionError(Exception):
    """Raised when an attempted state transition is not permitted."""
    pass


class TaskStateMachine:
    """
    Enforces formal state transitions on ARDO task nodes.
    Each transition is logged for explainability and audit trails.
    """

    @staticmethod
    def validate(current: TaskState, target: TaskState, task_id: str = "unknown") -> None:
        """
        Validate a proposed state transition.

        Args:
            current:  The node's current TaskState.
            target:   The proposed next TaskState.
            task_id:  Task identifier for logging.

        Raises:
            InvalidStateTransitionError: If the transition is not in the allowed map.
        """
        allowed = _VALID_TRANSITIONS.get(current, set())
        if target not in allowed:
            msg = (
                f"[StateMachine] INVALID TRANSITION: task={task_id} "
                f"{current.value} → {target.value}. "
                f"Allowed: {[s.value for s in allowed] or 'none (terminal)'}"
            )
            logger.error(msg)
            raise InvalidStateTransitionError(msg)

        logger.debug(
            f"[StateMachine] VALID: task={task_id} {current.value} → {target.value}"
        )

    @staticmethod
    def transition(
        current: TaskState, target: TaskState, task_id: str = "unknown"
    ) -> TaskState:
        """
        Validate and perform a state transition.

        Args:
            current:  Current TaskState.
            target:   Desired next TaskState.
            task_id:  Task identifier for logging.

        Returns:
            The new TaskState (= target) if valid.

        Raises:
            InvalidStateTransitionError: If not permitted.
        """
        TaskStateMachine.validate(current, target, task_id)
        logger.info(
            f"[StateMachine] TRANSITION: task={task_id} "
            f"{current.value} → {target.value}"
        )
        return target

    @staticmethod
    def is_terminal(state: TaskState) -> bool:
        """Returns True if the state has no valid outgoing transitions."""
        return len(_VALID_TRANSITIONS.get(state, set())) == 0

    @staticmethod
    def is_active_or_retrying(state: TaskState) -> bool:
        return state in (TaskState.ACTIVE, TaskState.RETRYING)

    @staticmethod
    def is_complete(state: TaskState) -> bool:
        return state == TaskState.COMPLETED

    @staticmethod
    def is_failed_or_blocked(state: TaskState) -> bool:
        return state in (TaskState.FAILED, TaskState.BLOCKED)

    @staticmethod
    def get_allowed_transitions(state: TaskState) -> Tuple[TaskState, ...]:
        return tuple(_VALID_TRANSITIONS.get(state, set()))
