"""
ARDO — Adaptive Reactive DAG Orchestrator
==========================================
Custom orchestration engine for the AI Goal-To-Task Planning Engine.

Modules:
    state_machine       — Formal state transition enforcement
    dag_builder         — DAG construction and validation
    priority_engine     — HEFT-inspired weighted priority scoring
    dependency_manager  — Dependency resolution and parallel grouping
    retry_manager       — Exponential backoff and confidence tracking
    reactive_rescheduler— Event-driven rescheduling
    explainability_engine — Scheduling decision transparency
    stability_guard     — Deadlock + starvation protection
    weight_optimizer    — Outcome tracking for future self-tuning
    execution_monitor   — Metrics and persistence
    runtime_engine      — Main async orchestration loop
    scheduler           — Public session entry point
"""

from orchestration.scheduler import ARDOScheduler

__all__ = ["ARDOScheduler"]
