"""
browser_agent.py — BrowserAgent: Iterative Observe-Reason-Act Loop (Phase 3)
=============================================================================
Implements the first real BrowserAgent for ARDO.

The agent follows a strict OBSERVE → REASON → ACT → OBSERVE AGAIN loop:

    BrowserTask
        ↓
    observe()                   ← EnvironmentPerceptionEngine
        ↓
    while not finished:
        ask_gemini(task, obs)   ← GeminiProvider.query()
        parse_decision()        ← _parse_gemini_decision()
        if FINISH → return completed result
        if FAIL   → return failed result
        if RECOVER → re-observe, continue
        if EXECUTE:
            resolve element ref → css_selector
            build BrowserAction
            execute via BrowserTaskAdapter (safety + Playwright)
            build RecentActionRecord
            observe(recent_action=record) → new observation
    ↓
    MAX ACTIONS → return failed result

IMPORTANT DESIGN RULES (maintained throughout):
- One BrowserAction per loop iteration. Never batch.
- Element refs resolved fresh from the CURRENT observation.
  Old refs from prior observations are NEVER carried forward.
- max_actions enforced strictly; loop cannot exceed it.
- No hardcoded site knowledge (no URL dicts, no named selectors).
- No simulation / fake success. Real execution only.
- Gemini is called with json_mode=True; response parsed to BrowserDecision.
- The agent does NOT reimplement Playwright — delegates to BrowserTaskAdapter.

BrowserAgent is the REASONING ORCHESTRATOR.
BrowserTaskAdapter / PlaywrightBrowserExecutor are the EXECUTION LAYER.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from core.config import settings

from execution.browser.semantic_verifier import SemanticVerifier

from execution.browser.agent_contracts import (
    BrowserActionDecision,
    BrowserAgentOutcome,
    BrowserAgentResult,
    BrowserDecision,
    BrowserDecisionType,
    BrowserObservation,
    BrowserTask,
    BrowserVerification,
    BrowserVerificationStatus,
    RecentActionRecord,
)
from execution.browser.agent_prompts import (
    BROWSER_AGENT_SYSTEM_PROMPT,
    build_user_prompt,
)
from execution.browser.browser_models import (
    BrowserAction,
    BrowserActionResult,
    BrowserActionType,
)
from execution.browser.browser_task_adapter import BrowserTaskAdapter
from execution.browser.environment_perception import EnvironmentPerceptionEngine
from execution.browser.perception_models import ObservedElement
from ai.gemini_provider import (
    GeminiQuotaExceededError,
    GeminiAuthenticationError,
    GeminiInvalidRequestError,
    GeminiRateLimitError,
    GeminiServiceUnavailableError,
    GeminiProviderError,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# JSON keys expected in Gemini's structured decision response
_KEY_DECISION    = "decision"
_KEY_REASON      = "reason"
_KEY_ACTION      = "action"
_KEY_ACTION_TYPE = "action_type"
_KEY_ELEM_REF    = "element_ref"
_KEY_URL         = "url"
_KEY_VALUE       = "value"
_KEY_KEY         = "key"
_KEY_NEW_TAB     = "new_tab"


# ─────────────────────────────────────────────────────────────────────────────
# BrowserAgent
# ─────────────────────────────────────────────────────────────────────────────

class BrowserAgent:
    """
    Iterative browser reasoning agent.

    Accepts a BrowserTask and drives a real Playwright browser through
    repeated observe/reason/act cycles until the task is finished, failed,
    or the action budget is exhausted.

    Usage::

        agent = BrowserAgent(
            llm_provider=gemini_provider,
            adapter=browser_task_adapter,
        )
        result: BrowserAgentResult = await agent.run(task)

    Dependencies (all injected so they can be mocked in tests):
        llm_provider:   Any object with an async query(prompt, system_instruction, json_mode) -> str.
                        In production this is GeminiProvider.
        adapter:        BrowserTaskAdapter — handles safety checks + Playwright execution.
        perception:     EnvironmentPerceptionEngine — builds BrowserObservations from live pages.
    """

    def __init__(
        self,
        llm_provider: Any,
        adapter: Optional[BrowserTaskAdapter] = None,
        perception: Optional[EnvironmentPerceptionEngine] = None,
        event_callback: Optional[Any] = None,
    ) -> None:
        self._llm = llm_provider
        self._adapter = adapter or BrowserTaskAdapter()
        self._perception = perception or EnvironmentPerceptionEngine()
        self._event_callback = event_callback
        self._llm_call_count: int = 0

    def _emit_event(self, event_type: str, task: BrowserTask, details: Dict[str, Any]) -> None:
        """Helper to safely broadcast structured observability events."""
        if self._event_callback:
            try:
                self._event_callback(event_type, task, details)
            except Exception as e:
                logger.warning(f"[BrowserAgent] Event callback notification error: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    async def run(self, task: BrowserTask) -> BrowserAgentResult:
        """
        Execute a BrowserTask through the iterative observe-reason-act loop.

        Args:
            task: The BrowserTask describing what the agent must accomplish.

        Returns:
            BrowserAgentResult describing the final outcome, all actions taken,
            verification status, and timing.
        """
        run_start = time.perf_counter()
        actions_taken: List[BrowserActionDecision] = []
        self._actions_taken = actions_taken
        observation: Optional[BrowserObservation] = None
        last_error: Optional[str] = None
        consecutive_recovery_count: int = 0
        max_recovery_budget: int = 3

        logger.info(
            f"[BrowserAgent] Starting task_id={task.task_id} "
            f"objective='{task.objective}' max_actions={task.max_actions}"
        )

        # ── STEP 0: Ensure browser executor is started & initial observation ─
        try:
            if hasattr(self._adapter, "ensure_started"):
                res = self._adapter.ensure_started()
                if inspect.isawaitable(res):
                    await res
            elif hasattr(self._adapter, "executor") and self._adapter.executor:
                if not getattr(self._adapter.executor, "is_running", False) and hasattr(self._adapter.executor, "start"):
                    res = self._adapter.executor.start()
                    if inspect.isawaitable(res):
                        await res
            observation = await self._observe(task, actions_taken_count=0)
            if "initial_tab_count" not in task.metadata:
                task.metadata["initial_tab_count"] = observation.open_tab_count
            if "initial_tab_refs" not in task.metadata:
                task.metadata["initial_tab_refs"] = [t.tab_ref for t in observation.tabs]
        except Exception as exc:
            logger.error(f"[BrowserAgent] Initial observation failed: {exc}")
            return self._make_result(
                task=task,
                outcome=BrowserAgentOutcome.FAILED,
                actions_taken=actions_taken,
                error=f"Initial observation failed: {exc}",
                run_start=run_start,
                observation=None,
            )

        # ── MAIN LOOP ──────────────────────────────────────────────────────
        for iteration in range(task.max_actions):

            logger.info(
                f"[BrowserAgent] Iteration {iteration + 1}/{task.max_actions} "
                f"url='{observation.current_url}' elements={len(observation.observed_elements)} "
                f"recoveries={consecutive_recovery_count}/{max_recovery_budget}"
            )

            # ── Check for explicit atomic action fast path (Optimization 2) ─
            decision: Optional[BrowserDecision] = None
            is_explicit: bool = False

            # Fast-path 1: If an action was executed, verify if an atomic task is already fully satisfied
            obj_lower = (task.objective or "").lower()
            is_atomic_task = (
                SemanticVerifier._requires_new_tab(task)
                or SemanticVerifier._requires_screenshot(task)
                or any(obj_lower.startswith(p) for p in ("go to ", "navigate to ", "open url", "visit ", "open "))
            )
            has_subsequent_actions = any(v in obj_lower for v in ("search", "click", "fill", "type", "play", "watch", "download", "select"))
            if len(actions_taken) > 0 and consecutive_recovery_count == 0 and is_atomic_task and not has_subsequent_actions:
                pre_verif = self._verify(task, observation)
                if pre_verif.status == BrowserVerificationStatus.VERIFIED:
                    logger.info(
                        f"[BrowserAgent] Atomic task objective satisfied and contract verified ({pre_verif.verified_by}). "
                        f"Completing task without LLM invocation."
                    )
                    decision = BrowserDecision(
                        decision=BrowserDecisionType.FINISH,
                        reason=f"Task contract verified: {pre_verif.verified_by}",
                    )

            # Fast-path 2: Deterministic search (Issue C)
            if decision is None and consecutive_recovery_count == 0:
                decision = self._detect_deterministic_search_action(task, observation, actions_taken)
                if decision is not None:
                    is_explicit = True

            # Fast-path 3: Atomic explicit action (Navigation, New tab, Screenshot)
            if decision is None and len(actions_taken) == 0 and consecutive_recovery_count == 0:
                decision = self._detect_explicit_action(task, observation)
                if decision is not None:
                    is_explicit = True

            if decision is None:
                # ── Ask Gemini for the next decision ────────────────────────
                try:
                    decision = await self._ask_gemini(task, observation)
                except (GeminiQuotaExceededError, GeminiAuthenticationError, GeminiInvalidRequestError) as exc:
                    logger.error(f"[BrowserAgent] Terminal LLM error at iteration {iteration + 1}: {exc}")
                    if isinstance(exc, GeminiQuotaExceededError) or "quota" in str(exc).lower():
                        last_error = f"LLM provider quota exhausted: {exc}"
                    else:
                        last_error = f"Unrecoverable LLM failure: {exc}"
                    self._emit_event("execution_failed", task, {
                        "reason": last_error,
                        "error_type": "quota_exhausted" if "quota" in str(exc).lower() else "llm_failure",
                    })
                    return self._make_result(
                        task=task,
                        outcome=BrowserAgentOutcome.FAILED,
                        actions_taken=actions_taken,
                        error=last_error,
                        run_start=run_start,
                        observation=observation,
                    )
                except (GeminiRateLimitError, GeminiServiceUnavailableError, GeminiProviderError) as exc:
                    logger.error(f"[BrowserAgent] LLM provider error after retries at iteration {iteration + 1}: {exc}")
                    last_error = f"LLM provider error: {exc}"
                    self._emit_event("execution_failed", task, {
                        "reason": last_error,
                    })
                    return self._make_result(
                        task=task,
                        outcome=BrowserAgentOutcome.FAILED,
                        actions_taken=actions_taken,
                        error=last_error,
                        run_start=run_start,
                        observation=observation,
                    )
                except Exception as exc:
                    exc_str = str(exc).lower()
                    if any(q in exc_str for q in ("quota", "429", "resource_exhausted", "free_tier", "unauthorized", "auth", "service unavailable")):
                        logger.error(f"[BrowserAgent] Unrecoverable Gemini failure detected at iteration {iteration + 1}: {exc}")
                        last_error = f"Unrecoverable LLM failure: {exc}"
                        self._emit_event("execution_failed", task, {
                            "reason": last_error,
                        })
                        return self._make_result(
                            task=task,
                            outcome=BrowserAgentOutcome.FAILED,
                            actions_taken=actions_taken,
                            error=last_error,
                            run_start=run_start,
                            observation=observation,
                        )

                    logger.error(f"[BrowserAgent] Gemini call failed at iteration {iteration + 1}: {exc}")
                    last_error = f"LLM decision failed: {exc}"
                    consecutive_recovery_count += 1
                    if consecutive_recovery_count > max_recovery_budget or iteration == task.max_actions - 1:
                        self._emit_event("recovery_exhausted", task, {
                            "reason": f"Recovery budget ({max_recovery_budget}) exhausted: {last_error}",
                        })
                        return self._make_result(
                            task=task,
                            outcome=BrowserAgentOutcome.FAILED,
                            actions_taken=actions_taken,
                            error=last_error,
                            run_start=run_start,
                            observation=observation,
                        )
                    # Re-observe with failure context to allow recovery
                    self._emit_event("recovery_started", task, {
                        "reason": last_error,
                        "attempt": consecutive_recovery_count,
                        "max_budget": max_recovery_budget,
                    })
                    recent = RecentActionRecord(
                        action_type=BrowserActionType.GET_URL,
                        success=False,
                        error=last_error,
                    )
                    try:
                        observation = await self._observe(
                            task=task,
                            actions_taken_count=len(actions_taken),
                            recent_action=recent,
                        )
                    except Exception:
                        pass
                    continue

            logger.info(
                f"[BrowserAgent] Decision={decision.decision.value} reason='{decision.reason}'"
            )

            # Emit structured DECISION_MADE event
            decision_data = {
                "decision": decision.decision.value,
                "reason": decision.reason,
            }
            if decision.action:
                safe_val = decision.action.value
                if safe_val and ("password" in (decision.action.element_ref or "").lower() or "secret" in (decision.action.element_ref or "").lower()):
                    safe_val = "[REDACTED]"
                decision_data["action"] = {
                    "action_type": decision.action.action_type.value,
                    "element_ref": decision.action.element_ref,
                    "url": decision.action.url,
                    "value": safe_val,
                    "key": decision.action.key,
                    "new_tab": decision.action.new_tab,
                }
            self._emit_event("decision_made", task, decision_data)

            # ── Handle FINISH ────────────────────────────────────────────
            if decision.decision == BrowserDecisionType.FINISH:
                self._emit_event("verification_started", task, {
                    "objective": task.objective,
                    "success_criteria": task.success_criteria,
                    "current_url": observation.current_url,
                })
                verification = self._verify(task, observation)
                logger.info(
                    f"[BrowserAgent] FINISH declared. verification={verification.status.value}"
                )
                self._emit_event("verification_completed", task, {
                    "status": verification.status.value,
                    "failure_reason": verification.failure_reason,
                    "verified_by": getattr(verification, "verified_by", "SemanticVerifier"),
                    "current_url": observation.current_url,
                    "page_title": observation.page_title,
                })
                if verification.status == BrowserVerificationStatus.VERIFIED:
                    return self._make_result(
                        task=task,
                        outcome=BrowserAgentOutcome.COMPLETED,
                        actions_taken=actions_taken,
                        error=None,
                        run_start=run_start,
                        observation=observation,
                        verification=verification,
                    )

                # Verification NOT_VERIFIED: attempt replanning if budget allows
                if iteration < task.max_actions - 1 and consecutive_recovery_count < max_recovery_budget:
                    consecutive_recovery_count += 1
                    logger.warning(
                        f"[BrowserAgent] FINISH declared but verification failed: {verification.failure_reason}. "
                        f"Re-observing and replanning (attempt {consecutive_recovery_count}/{max_recovery_budget})."
                    )
                    self._emit_event("recovery_started", task, {
                        "reason": f"Verification failed: {verification.failure_reason}",
                        "attempt": consecutive_recovery_count,
                        "max_budget": max_recovery_budget,
                    })
                    recent = RecentActionRecord(
                        action_type=BrowserActionType.GET_URL,
                        success=False,
                        error=f"Task verification failed: {verification.failure_reason}. Please replan to satisfy criteria.",
                    )
                    try:
                        observation = await self._observe(
                            task=task,
                            actions_taken_count=len(actions_taken),
                            recent_action=recent,
                        )
                    except Exception:
                        pass
                    continue

                # Budget exhausted or final iteration -> return failed verification result
                self._emit_event("recovery_exhausted", task, {
                    "reason": f"Task verification failed and recovery budget exhausted: {verification.failure_reason}",
                })
                return self._make_result(
                    task=task,
                    outcome=BrowserAgentOutcome.FAILED,
                    actions_taken=actions_taken,
                    error=f"Task verification failed: {verification.failure_reason}",
                    run_start=run_start,
                    observation=observation,
                    verification=verification,
                )

            # ── Handle FAIL ──────────────────────────────────────────────
            if decision.decision == BrowserDecisionType.FAIL:
                logger.warning(
                    f"[BrowserAgent] FAIL declared: {decision.reason}"
                )
                return self._make_result(
                    task=task,
                    outcome=BrowserAgentOutcome.FAILED,
                    actions_taken=actions_taken,
                    error=decision.reason,
                    run_start=run_start,
                    observation=observation,
                )

            # ── Handle RECOVER ───────────────────────────────────────────
            if decision.decision == BrowserDecisionType.RECOVER:
                logger.info(
                    f"[BrowserAgent] RECOVER declared: {decision.reason}. Re-observing."
                )
                consecutive_recovery_count += 1
                if consecutive_recovery_count > max_recovery_budget:
                    self._emit_event("recovery_exhausted", task, {
                        "reason": f"Recovery budget ({max_recovery_budget}) exhausted: {decision.reason}",
                    })
                    return self._make_result(
                        task=task,
                        outcome=BrowserAgentOutcome.FAILED,
                        actions_taken=actions_taken,
                        error=f"Recovery budget ({max_recovery_budget}) exhausted: {decision.reason}",
                        run_start=run_start,
                        observation=observation,
                    )

                self._emit_event("recovery_started", task, {
                    "reason": decision.reason,
                    "attempt": consecutive_recovery_count,
                    "max_budget": max_recovery_budget,
                })

                if decision.action is not None:
                    observation, exec_decision, last_error = await self._execute_and_observe(
                        task=task,
                        observation=observation,
                        decision=decision,
                        actions_taken=actions_taken,
                    )
                    if exec_decision is None:
                        if "BLOCKED" in (last_error or ""):
                            return self._make_result(
                                task=task,
                                outcome=BrowserAgentOutcome.BLOCKED,
                                actions_taken=actions_taken,
                                error=last_error,
                                run_start=run_start,
                                observation=observation,
                            )
                        if iteration == task.max_actions - 1:
                            return self._make_result(
                                task=task,
                                outcome=BrowserAgentOutcome.FAILED,
                                actions_taken=actions_taken,
                                error=last_error,
                                run_start=run_start,
                                observation=observation,
                            )
                    elif exec_decision.success:
                        # ── Verification-First Check after recovery action ────
                        verification = self._verify(task, observation)
                        if self._can_verify_first(task, verification, is_explicit_action=is_explicit):
                            logger.info(
                                f"[BrowserAgent] Verification-first check passed after recovery ({verification.verified_by}). "
                                f"Task {task.task_id} completed without extra LLM FINISH call."
                            )
                            self._emit_event("verification_started", task, {
                                "objective": task.objective,
                                "success_criteria": task.success_criteria,
                                "current_url": observation.current_url,
                            })
                            self._emit_event("verification_completed", task, {
                                "status": verification.status.value,
                                "failure_reason": verification.failure_reason,
                                "verified_by": getattr(verification, "verified_by", "SemanticVerifier"),
                                "current_url": observation.current_url,
                                "page_title": observation.page_title,
                            })
                            return self._make_result(
                                task=task,
                                outcome=BrowserAgentOutcome.COMPLETED,
                                actions_taken=actions_taken,
                                error=None,
                                run_start=run_start,
                                observation=observation,
                                verification=verification,
                            )
                else:
                    # Pure re-observe recover
                    try:
                        observation = await self._observe(
                            task=task,
                            actions_taken_count=len(actions_taken),
                        )
                    except Exception as exc:
                        logger.error(f"[BrowserAgent] RECOVER re-observe failed: {exc}")
                continue

            # ── Handle EXECUTE ───────────────────────────────────────────
            if decision.decision == BrowserDecisionType.EXECUTE:
                if decision.action is None:
                    logger.warning("[BrowserAgent] EXECUTE decision missing action. Treating as FAIL.")
                    return self._make_result(
                        task=task,
                        outcome=BrowserAgentOutcome.FAILED,
                        actions_taken=actions_taken,
                        error="EXECUTE decision returned no action.",
                        run_start=run_start,
                        observation=observation,
                    )

                observation, exec_decision, last_error = await self._execute_and_observe(
                    task=task,
                    observation=observation,
                    decision=decision,
                    actions_taken=actions_taken,
                )
                if exec_decision is None:
                    if "BLOCKED" in (last_error or ""):
                        return self._make_result(
                            task=task,
                            outcome=BrowserAgentOutcome.BLOCKED,
                            actions_taken=actions_taken,
                            error=last_error,
                            run_start=run_start,
                            observation=observation,
                        )

                    # Non-fatal execution error (e.g. stale element reference)
                    consecutive_recovery_count += 1
                    if consecutive_recovery_count > max_recovery_budget or iteration == task.max_actions - 1:
                        self._emit_event("recovery_exhausted", task, {
                            "reason": f"Recovery budget exhausted: {last_error}",
                        })
                        return self._make_result(
                            task=task,
                            outcome=BrowserAgentOutcome.FAILED,
                            actions_taken=actions_taken,
                            error=f"Recovery budget exhausted: {last_error}",
                            run_start=run_start,
                            observation=observation,
                        )
                    logger.warning(
                        f"[BrowserAgent] Action execution failed ({last_error}). "
                        f"Re-observed and replanning (attempt {consecutive_recovery_count}/{max_recovery_budget})."
                    )
                    self._emit_event("recovery_started", task, {
                        "reason": f"Action execution failed: {last_error}",
                        "attempt": consecutive_recovery_count,
                        "max_budget": max_recovery_budget,
                    })
                    continue

                if exec_decision.success:
                    consecutive_recovery_count = 0  # Reset failure count on success

                    # ── Verification-First Check (Optimization 1) ─────────────
                    # If the action succeeded and the task objective/criteria is
                    # already verified by SemanticVerifier, complete immediately
                    # without an extra Gemini FINISH call.
                    verification = self._verify(task, observation)
                    if self._can_verify_first(task, verification, is_explicit_action=is_explicit):
                        logger.info(
                            f"[BrowserAgent] Verification-first check passed ({verification.verified_by}). "
                            f"Task {task.task_id} completed without extra LLM FINISH call."
                        )
                        self._emit_event("verification_started", task, {
                            "objective": task.objective,
                            "success_criteria": task.success_criteria,
                            "current_url": observation.current_url,
                        })
                        self._emit_event("verification_completed", task, {
                            "status": verification.status.value,
                            "failure_reason": verification.failure_reason,
                            "verified_by": getattr(verification, "verified_by", "SemanticVerifier"),
                            "current_url": observation.current_url,
                            "page_title": observation.page_title,
                        })
                        return self._make_result(
                            task=task,
                            outcome=BrowserAgentOutcome.COMPLETED,
                            actions_taken=actions_taken,
                            error=None,
                            run_start=run_start,
                            observation=observation,
                            verification=verification,
                        )
                else:
                    consecutive_recovery_count += 1
                    if consecutive_recovery_count > max_recovery_budget or iteration == task.max_actions - 1:
                        self._emit_event("recovery_exhausted", task, {
                            "reason": f"Recovery budget exhausted after action failure: {exec_decision.error}",
                        })
                        return self._make_result(
                            task=task,
                            outcome=BrowserAgentOutcome.FAILED,
                            actions_taken=actions_taken,
                            error=f"Recovery budget exhausted after action failure: {exec_decision.error}",
                            run_start=run_start,
                            observation=observation,
                        )
                    self._emit_event("recovery_started", task, {
                        "reason": f"Action failed: {exec_decision.error}",
                        "attempt": consecutive_recovery_count,
                        "max_budget": max_recovery_budget,
                    })

                # Continue to next iteration with fresh observation
                continue

        # ── MAX ACTIONS REACHED ────────────────────────────────────
        logger.warning(
            f"[BrowserAgent] Task {task.task_id} reached max_actions={task.max_actions}. "
            f"Returning FAILED (not a success)."
        )
        verification = self._verify(task, observation) if observation else None
        return self._make_result(
            task=task,
            outcome=BrowserAgentOutcome.FAILED,
            actions_taken=actions_taken,
            error=f"Max actions ({task.max_actions}) reached without completing the task.",
            run_start=run_start,
            observation=observation,
            verification=verification,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Internal: Execute one action and return new observation
    # ─────────────────────────────────────────────────────────────────────────

    async def _execute_and_observe(
        self,
        task: BrowserTask,
        observation: BrowserObservation,
        decision: BrowserDecision,
        actions_taken: List[BrowserActionDecision],
    ) -> Tuple[BrowserObservation, Optional[Any], Optional[str]]:
        """
        Execute the action in the decision against the browser, record it, and
        return a fresh observation.

        Returns:
            (new_observation, action_result_or_None, error_or_None)
            If action_result is None the caller should treat this as a hard failure.
        """
        action_decision = decision.action  # BrowserActionDecision

        # ── 1. Resolve element reference → css_selector ─────────────────
        css_selector: Optional[str] = None
        if action_decision.element_ref:
            css_selector = self._resolve_element_ref(observation, action_decision.element_ref)
            if css_selector is None:
                error = (
                    f"Element reference '{action_decision.element_ref}' not found "
                    f"in current observation. Cannot execute action."
                )
                logger.warning(f"[BrowserAgent] {error}")
                # Emit ACTION_STARTED and ACTION_FAILED for stale/unresolved ref
                self._emit_event("action_started", task, {
                    "action_type": action_decision.action_type.value,
                    "element_ref": action_decision.element_ref,
                    "url": action_decision.url,
                })
                self._emit_event("action_failed", task, {
                    "action_type": action_decision.action_type.value,
                    "element_ref": action_decision.element_ref,
                    "url": action_decision.url,
                    "error": error,
                })
                # Build a failure recent_action and re-observe
                recent = RecentActionRecord(
                    action_type=action_decision.action_type,
                    element_ref=action_decision.element_ref,
                    success=False,
                    error=error,
                )
                try:
                    new_obs = await self._observe(
                        task=task,
                        actions_taken_count=len(actions_taken),
                        recent_action=recent,
                    )
                except Exception:
                    new_obs = observation
                return new_obs, None, error

        # ── 2. Build BrowserAction for executor ─────────────────────────
        browser_action = _decision_to_browser_action(action_decision, css_selector, task)

        # ── 3. Emit ACTION_STARTED event ────────────────────────────────
        safe_val = action_decision.value
        if safe_val and ("password" in (action_decision.element_ref or "").lower() or "secret" in (action_decision.element_ref or "").lower()):
            safe_val = "[REDACTED]"
        self._emit_event("action_started", task, {
            "action_type": action_decision.action_type.value,
            "element_ref": action_decision.element_ref,
            "url": action_decision.url,
            "value": safe_val,
            "key": action_decision.key,
            "new_tab": action_decision.new_tab,
        })

        # ── 4. Execute via BrowserTaskAdapter (safety + Playwright) ─────
        t0 = time.perf_counter()
        try:
            action_result, safety_decision = await self._adapter.execute_safe_browser_task(
                task_id=task.task_id,
                session_id=task.session_id,
                action=browser_action,
            )
        except Exception as exc:
            error = f"Execution exception: {exc}"
            logger.error(f"[BrowserAgent] {error}")
            duration_ms = (time.perf_counter() - t0) * 1000.0
            self._emit_event("action_failed", task, {
                "action_type": action_decision.action_type.value,
                "element_ref": action_decision.element_ref,
                "url": action_decision.url,
                "duration_ms": duration_ms,
                "error": error,
            })
            recent = RecentActionRecord(
                action_type=action_decision.action_type,
                element_ref=action_decision.element_ref,
                url=action_decision.url,
                success=False,
                error=error,
            )
            try:
                new_obs = await self._observe(
                    task=task,
                    actions_taken_count=len(actions_taken),
                    recent_action=recent,
                )
            except Exception:
                new_obs = observation
            return new_obs, None, error

        duration_ms = (time.perf_counter() - t0) * 1000.0

        # ── 5. Record the action decision in the taken list ──────────────
        actions_taken.append(action_decision)

        logger.info(
            f"[BrowserAgent] Action executed: {action_decision.action_type.value} "
            f"success={action_result.success} duration={duration_ms:.0f}ms "
            f"url={action_result.current_url}"
        )

        # ── 6. Emit truthful ACTION_COMPLETED / ACTION_FAILED ───────────
        if action_result.success:
            self._emit_event("action_completed", task, {
                "action_type": action_decision.action_type.value,
                "element_ref": action_decision.element_ref,
                "url": action_result.current_url or action_decision.url,
                "duration_ms": duration_ms,
            })
        else:
            logger.warning(
                f"[BrowserAgent] Action {action_decision.action_type.value} failed: "
                f"{action_result.error}"
            )
            self._emit_event("action_failed", task, {
                "action_type": action_decision.action_type.value,
                "element_ref": action_decision.element_ref,
                "url": action_result.current_url or action_decision.url,
                "duration_ms": duration_ms,
                "error": action_result.error or "Action failed",
            })

        # Check if blocked by safety
        if action_result.error and "BLOCKED_BY_SAFETY_POLICY" in action_result.error:
            return observation, None, action_result.error

        # ── 7. Build RecentActionRecord for next observation ─────────────
        recent = RecentActionRecord(
            action_type=action_decision.action_type,
            element_ref=action_decision.element_ref,
            url=action_decision.url,
            value=action_decision.value,
            new_tab=action_decision.new_tab,
            success=action_result.success,
            error=str(action_result.error) if action_result.error is not None and not isinstance(action_result.error, str) and not isinstance(action_result.error, type(None)) else (action_result.error or None),
            duration_ms=duration_ms,
        )

        # ── 8. Re-observe after the action ──────────────────────────────
        try:
            new_obs = await self._observe(
                task=task,
                actions_taken_count=len(actions_taken),
                recent_action=recent,
            )
        except Exception as exc:
            logger.error(f"[BrowserAgent] Post-action observe failed: {exc}")
            new_obs = observation  # Fallback: keep old observation

        return new_obs, action_result, None

    # ─────────────────────────────────────────────────────────────────────────
    # Internal: Observe
    # ─────────────────────────────────────────────────────────────────────────

    async def _observe(
        self,
        task: BrowserTask,
        actions_taken_count: int,
        recent_action: Optional[RecentActionRecord] = None,
    ) -> BrowserObservation:
        """
        Build a fresh BrowserObservation from the current browser state.

        Uses the executor's active page (via the adapter) and the
        EnvironmentPerceptionEngine to produce a structured observation with
        semantic element references.
        """
        page = self._get_page()
        context = self._get_context()

        obs = await self._perception.observe(
            page=page,
            session_id=task.session_id,
            task_id=task.task_id,
            context=context,
            actions_taken_count=actions_taken_count,
            recent_action=recent_action,
        )

        compact_obs = {
            "url": obs.current_url,
            "title": obs.page_title,
            "active_tab_index": obs.active_tab_index,
            "open_tab_count": obs.open_tab_count,
            "elements_count": len(obs.observed_elements),
            "visible_text_snippet": (obs.page_text_summary[:200] + "...") if obs.page_text_summary and len(obs.page_text_summary) > 200 else (obs.page_text_summary or ""),
        }
        self._emit_event("observation_received", task, compact_obs)
        return obs

    def _get_page(self) -> Any:
        """Return the active Playwright page from the executor, or None."""
        try:
            if hasattr(self._adapter, "get_page"):
                page = self._adapter.get_page()
                if page is not None:
                    return page
            session = getattr(self._adapter.executor, "_session", None)
            if session is None:
                session = self._adapter.executor
            page = getattr(session, "_page", None)
            return page
        except Exception:
            return None

    def _get_context(self) -> Any:
        """Return the active Playwright browser context from the executor, or None."""
        try:
            if hasattr(self._adapter, "get_context"):
                ctx = self._adapter.get_context()
                if ctx is not None:
                    return ctx
            session = getattr(self._adapter.executor, "_session", None)
            if session is None:
                session = self._adapter.executor
            return getattr(session, "_context", None)
        except Exception:
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # Internal: Deterministic Search & Explicit Action Fast Paths (Issue C)
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def _extract_search_intent(cls, task: BrowserTask) -> Optional[str]:
        """
        Extracts search query if task objective or metadata specifies a search action.
        Returns None if not a search task or query cannot be extracted.
        """
        if task.metadata:
            for k in ("search_query", "query", "search_term"):
                if task.metadata.get(k):
                    return str(task.metadata[k]).strip()

        obj = (task.objective or "").strip()
        obj_lower = obj.lower()

        # If the task includes video selection, playback, or downstream interactions (e.g. "and play the song"),
        # do not fast-path as a simple search task; preserve full reasoning loop.
        if any(w in obj_lower for w in ("play", "click", "watch", "select", "download", "buy", "order")):
            return None

        # Compound multi-step task (e.g. "Open X and search for Y") preserves full dynamic reasoning
        if re.search(r'\b(?:and|then)\s+search\b', obj_lower) or (obj_lower.startswith("open ") and " and " in obj_lower):
            return None

        # Regex matching on search intent:
        patterns = [
            r'(?:inside|on|at|in)\s+[a-zA-Z0-9_\.\-]+\s+search\s+(?:for\s+)?["\']?([^"\'\n\r]+?)["\']?(?:\s+(?:and|then)\s+.*)?$',
            r'search\s+(?:for\s+)?["\']?([^"\'\n\r]+?)["\']?(?:\s+(?:on|in|inside)\s+[a-zA-Z0-9_\.\-]+)?(?:\s+(?:and|then)\s+.*)?$',
            r'find\s+(?:for\s+)?["\']?([^"\'\n\r]+?)["\']?(?:\s+(?:on|in|inside)\s+[a-zA-Z0-9_\.\-]+)?(?:\s+(?:and|then)\s+.*)?$',
        ]
        for pat in patterns:
            m = re.search(pat, obj, re.IGNORECASE)
            if m:
                query = m.group(1).strip().rstrip(".,;!?")
                query = re.sub(r'^(?:for|the|song|video)\s+', '', query, flags=re.IGNORECASE).strip()
                query = re.sub(r'\s+(?:song|video)$', '', query, flags=re.IGNORECASE).strip()
                if query and len(query) > 1:
                    return query
        return None

    def _find_search_input(self, observation: BrowserObservation) -> Optional[ObservedElement]:
        """
        Locates the appropriate search input element in the current observation
        using semantic attributes (role, tag, placeholder, aria-label, name, id).
        """
        best_candidate: Optional[ObservedElement] = None
        best_score: int = 0
        for el in observation.observed_elements:
            tag = (el.tag or "").lower()
            role = (el.role or "").lower()
            input_type = (getattr(el, "input_type", None) or "").lower()
            placeholder = (el.placeholder or "").lower()
            aria_label = (el.aria_label or "").lower()
            name = (el.name or "").lower()
            el_id = (el.element_id or "").lower()
            selector = (el.css_selector or "").lower()

            # Must be an input or textarea or searchbox
            if tag not in ("input", "textarea") and role not in ("searchbox", "textbox"):
                continue

            score = 0
            if role == "searchbox" or input_type == "search":
                score += 5
            if name in ("q", "query", "search_query", "search", "k", "s", "search_term"):
                score += 4
            if "search" in aria_label:
                score += 3
            if "search" in placeholder:
                score += 3
            if "search" in el_id:
                score += 2
            if "search" in selector:
                score += 2
            if tag == "input" and input_type in ("text", ""):
                score += 1

            if score > best_score:
                best_score = score
                best_candidate = el

        if best_score >= 2:
            return best_candidate
        return None

    def _find_search_submit_button(self, observation: BrowserObservation) -> Optional[ObservedElement]:
        """
        Locates an explicit search submit control if present.
        """
        for el in observation.observed_elements:
            tag = (el.tag or "").lower()
            role = (el.role or "").lower()
            aria_label = (el.aria_label or "").lower()
            text = (el.text or "").lower()
            el_id = (el.element_id or "").lower()
            if tag in ("button", "input") or role == "button":
                if (
                    "search" in aria_label
                    or "search" in text
                    or "search" in el_id
                    or el_id in ("search-icon-legacy", "search-button")
                ):
                    return el
        return None

    def _is_search_results_state(self, query: str, observation: BrowserObservation) -> bool:
        """
        Verifies if current page state represents an active search results view for query.
        """
        url_lower = (observation.current_url or "").lower()
        is_search_url = any(k in url_lower for k in ("search_query=", "?q=", "&q=", "/search", "/results", "query="))

        q_tokens = [t.lower() for t in re.split(r'\s+', query) if len(t) > 1]
        if not q_tokens:
            return False

        url_has_query = all(tok in url_lower or tok.replace(" ", "+") in url_lower for tok in q_tokens)
        title_lower = (observation.page_title or "").lower()
        title_has_query = any(tok in title_lower for tok in q_tokens)
        text_lower = (observation.page_text_summary or "").lower()
        text_has_query = all(tok in text_lower for tok in q_tokens)

        return (is_search_url and (url_has_query or title_has_query)) or (url_has_query and (title_has_query or text_has_query))

    def _detect_deterministic_search_action(
        self,
        task: BrowserTask,
        observation: BrowserObservation,
        actions_taken: List[BrowserActionDecision],
    ) -> Optional[BrowserDecision]:
        """
        Deterministic search execution: locate input -> fill query -> submit via Enter -> verify results.
        Saves multi-turn Gemini calls for standard search operations.
        """
        query = self._extract_search_intent(task)
        if not query:
            return None

        # 1. If we already took actions and reached verified search results state, finish
        if len(actions_taken) > 0 and self._is_search_results_state(query, observation):
            return BrowserDecision(
                decision=BrowserDecisionType.FINISH,
                reason=f"Deterministic search: verified search results for query '{query}' at '{observation.current_url}'",
            )

        # 2. Locate search input
        search_input = self._find_search_input(observation)
        if not search_input:
            return None

        # 3. Check if query is filled
        input_val = (search_input.value or "").strip().lower()
        has_filled = (
            input_val == query.strip().lower()
            or any(
                a.action_type == BrowserActionType.FILL and (a.value or "").strip().lower() == query.strip().lower()
                for a in actions_taken
            )
        )

        if not has_filled:
            return BrowserDecision(
                decision=BrowserDecisionType.EXECUTE,
                reason=f"Deterministic search: fill query '{query}' into {search_input.ref}",
                action=BrowserActionDecision(
                    action_type=BrowserActionType.FILL,
                    element_ref=search_input.ref,
                    value=query,
                ),
            )

        # 4. Query is filled. Submit using Enter or submit button
        has_pressed_enter = any(
            a.action_type == BrowserActionType.PRESS and (a.key or "").lower() == "enter"
            for a in actions_taken
        )
        if not has_pressed_enter:
            return BrowserDecision(
                decision=BrowserDecisionType.EXECUTE,
                reason=f"Deterministic search: submit query '{query}' via Enter on {search_input.ref}",
                action=BrowserActionDecision(
                    action_type=BrowserActionType.PRESS,
                    element_ref=search_input.ref,
                    key="Enter",
                ),
            )

        # 5. Fallback submit button click if Enter did not transition URL
        submit_btn = self._find_search_submit_button(observation)
        has_clicked_submit = any(
            a.action_type == BrowserActionType.CLICK and submit_btn and a.element_ref == submit_btn.ref
            for a in actions_taken
        )
        if submit_btn and not has_clicked_submit:
            return BrowserDecision(
                decision=BrowserDecisionType.EXECUTE,
                reason=f"Deterministic search: click submit button ({submit_btn.ref}) for query '{query}'",
                action=BrowserActionDecision(
                    action_type=BrowserActionType.CLICK,
                    element_ref=submit_btn.ref,
                ),
            )

        return None

    def _detect_explicit_action(
        self, task: BrowserTask, observation: BrowserObservation
    ) -> Optional[BrowserDecision]:
        """
        Generic detection of an explicit atomic browser action specified by the task contract.

        Only triggers when the action is completely unambiguous and explicit (e.g. concrete URL
        navigation with explicit scheme, or standalone screenshot request), avoiding an unnecessary Gemini call.
        Does NOT trigger for dynamic reasoning tasks (element interaction, form filling, search).
        Never uses hardcoded website tables or selectors.
        """
        obj_lower = (task.objective or "").strip().lower()
        crit_lower = (task.success_criteria or "").strip().lower()

        # Dynamic keywords indicate a task requiring UI reasoning/interaction -> bypass fast path
        dynamic_keywords = (
            "search", "find", "click", "play", "fill", "type", "submit",
            "select", "enter", "form", "login", "buy", "purchase", "send", "message",
            "interact", "choose", "check"
        )
        if any(kw in obj_lower for kw in dynamic_keywords):
            return None

        # 1. Explicit Standalone Screenshot action
        is_standalone_screenshot = (
            obj_lower in ("screenshot", "take screenshot", "take a screenshot", "capture screen", "capture screenshot")
            or re.search(r'^(?:please\s+)?(?:take\s+(?:a\s+)?)?screenshot\b', obj_lower)
            or re.search(r'^(?:please\s+)?(?:capture\s+(?:the\s+)?)?screen(?:shot)?\b', obj_lower)
            or (crit_lower in ("screenshot", "screenshot captured", "screenshot artifact present") and not obj_lower.startswith("http"))
        )
        if is_standalone_screenshot:
            expected_urls = [u for u in SemanticVerifier._extract_target_urls(task) if u.startswith("http://") or u.startswith("https://")]
            if expected_urls:
                url_match, _ = SemanticVerifier._verify_url(expected_urls, observation.current_url or "")
                if not url_match:
                    pass  # Must navigate first
                else:
                    return BrowserDecision(
                        decision=BrowserDecisionType.EXECUTE,
                        reason="Explicit screenshot action specified in task contract",
                        action=BrowserActionDecision(
                            action_type=BrowserActionType.SCREENSHOT,
                        ),
                    )
            else:
                return BrowserDecision(
                    decision=BrowserDecisionType.EXECUTE,
                    reason="Explicit screenshot action specified in task contract",
                    action=BrowserActionDecision(
                        action_type=BrowserActionType.SCREENSHOT,
                    ),
                )

        # 2. Explicit Navigation / New Tab action
        new_tab = SemanticVerifier._requires_new_tab(task)
        if re.search(r'^(?:please\s+)?(?:open|create)\s+(?:a\s+)?new\s+(?:browser\s+)?tab\b', obj_lower) or obj_lower in ("new tab", "open new tab", "open a new tab", "create new tab", "create a new tab"):
            initial_count = task.metadata.get("initial_tab_count", 1)
            tab_count = observation.open_tab_count
            if len(observation.tabs) > 0:
                tab_count = max(tab_count, len(observation.tabs))
            if len(self._actions_taken) > 0 and tab_count > initial_count:
                return BrowserDecision(
                    decision=BrowserDecisionType.FINISH,
                    reason="New tab created and active",
                )
            return BrowserDecision(
                decision=BrowserDecisionType.EXECUTE,
                reason="Explicit new tab navigation requested in task contract",
                action=BrowserActionDecision(
                    action_type=BrowserActionType.NAVIGATE,
                    url="about:blank",
                    new_tab=True,
                ),
            )

        # Check for explicit destination URLs in task metadata / plan contract
        explicit_url = (
            task.metadata.get("expected_url")
            or task.metadata.get("destination_url")
            or task.metadata.get("url")
            or getattr(task, "expected_url", None)
        )
        if not explicit_url:
            url_match_obj = re.search(r'(https?://[^\s\'"<>]+|file://[^\s\'"<>]+)', task.objective or "")
            if url_match_obj and any(obj_lower.startswith(p) for p in ("open ", "navigate", "go to", "visit", "load")):
                explicit_url = url_match_obj.group(0).rstrip(".,;)")
            elif task.metadata.get("action_type") == "navigate":
                domain_match = re.search(r'\b([a-zA-Z0-9-]+\.(?:com|org|net|io|dev|edu|gov|html?)(?:/[^\s]*)?)\b', task.objective or "", re.IGNORECASE)
                if domain_match:
                    explicit_url = f"https://{domain_match.group(1).rstrip('.,;')}"

        if explicit_url:
            norm_target = SemanticVerifier._normalize_url(str(explicit_url))
            url_match, _ = SemanticVerifier._verify_url([norm_target], observation.current_url or "")
            if not url_match:
                return BrowserDecision(
                    decision=BrowserDecisionType.EXECUTE,
                    reason=f"Explicit destination URL '{norm_target}' specified in task contract",
                    action=BrowserActionDecision(
                        action_type=BrowserActionType.NAVIGATE,
                        url=norm_target,
                        new_tab=new_tab,
                    ),
                )
            else:
                return BrowserDecision(
                    decision=BrowserDecisionType.FINISH,
                    reason=f"Already on destination URL '{norm_target}'",
                )

        return None

    # ─────────────────────────────────────────────────────────────────────────
    # Internal: Ask Gemini
    # ─────────────────────────────────────────────────────────────────────────

    async def _ask_gemini(
        self, task: BrowserTask, observation: BrowserObservation
    ) -> BrowserDecision:
        """
        Send the current task + observation to Gemini and parse the decision.

        Enforces per-session LLM request budget and structured JSON mode.
        Raises GeminiQuotaExceededError on quota or budget exhaustion.
        """
        max_budget = getattr(settings, "MAX_SESSION_LLM_CALLS", 15)
        if self._llm_call_count >= max_budget:
            logger.error(
                f"[BrowserAgent] Session LLM request budget ({max_budget}) exhausted."
            )
            raise GeminiQuotaExceededError(
                f"LLM request budget ({max_budget}) exhausted for this session."
            )
        self._llm_call_count += 1

        user_prompt = build_user_prompt(
            objective=task.objective,
            success_criteria=task.success_criteria,
            constraints=task.constraints,
            observation_summary=observation.to_agent_summary(),
            actions_taken=observation.actions_taken_count,
            max_actions=task.max_actions,
        )

        raw = await self._llm.query(
            user_prompt,
            system_instruction=BROWSER_AGENT_SYSTEM_PROMPT,
            json_mode=True,
        )

        return _parse_gemini_decision(raw)

    # ─────────────────────────────────────────────────────────────────────────
    # Internal: Simple verification
    # ─────────────────────────────────────────────────────────────────────────

    def _verify(
        self, task: BrowserTask, observation: Optional[BrowserObservation]
    ) -> BrowserVerification:
        """
        Perform observational semantic verification of the post-action browser state.
        Delegates to SemanticVerifier (Phase 5).
        """
        obs = observation
        if obs is not None and obs.recent_action is None and getattr(self, "_actions_taken", None):
            last_act = self._actions_taken[-1]
            obs = obs.model_copy(update={
                "recent_action": RecentActionRecord(
                    action_type=last_act.action_type,
                    element_ref=last_act.element_ref,
                    url=last_act.url,
                    value=last_act.value,
                    new_tab=last_act.new_tab,
                    success=True,
                )
            })
        return SemanticVerifier.verify(task=task, observation=obs)

    @classmethod
    def _is_single_navigation_intent(cls, task: BrowserTask) -> bool:
        """
        Check if the task represents a single atomic navigation intent without
        subsequent dynamic UI interactions (e.g. search, click, fill, login).
        """
        obj_lower = (task.objective or "").strip().lower()
        dynamic_keywords = (
            "search", "find", "click", "play", "fill", "type", "submit",
            "select", "enter", "form", "login", "buy", "purchase", "send", "message",
            "interact", "choose", "check"
        )
        if any(kw in obj_lower for kw in dynamic_keywords):
            return False

        # Check explicit action / navigation indicators in metadata
        if (
            task.metadata.get("destination_url")
            or task.metadata.get("url")
            or getattr(task, "expected_url", None)
            or task.metadata.get("action_type") == "navigate"
        ):
            return True

        nav_patterns = (
            r'^(?:please\s+)?(?:open|navigate|go\s+to|browse\s+to|visit|launch|load)\b',
            r'^(?:new\s+tab|open\s+new\s+tab)\b',
        )
        return any(re.search(pat, obj_lower) for pat in nav_patterns)

    def _can_verify_first(
        self,
        task: BrowserTask,
        verification: BrowserVerification,
        is_explicit_action: bool = False,
    ) -> bool:
        """
        Determine whether a task's success criteria can be evaluated deterministically
        for verification-first completion without an extra Gemini FINISH call.

        Triggers when:
        1. SemanticVerifier status is VERIFIED.
        2. If task was driven by explicit fast-path action and target contract verified.
        3. For search tasks, when results URL/query state is confirmed.
        4. If task objective requires dynamic interactions (click, play, select),
           do NOT early-exit on intermediate navigation alone.
        """
        if verification.status != BrowserVerificationStatus.VERIFIED:
            return False

        # If it was an explicit fast-path action that executed and was verified
        if is_explicit_action:
            query = self._extract_search_intent(task)
            if query:
                if getattr(verification, "observed_url", None):
                    obs_mock = BrowserObservation(
                        session_id=task.session_id,
                        task_id=task.task_id,
                        current_url=verification.observed_url,
                        page_title=verification.observed_title or "",
                    )
                    if self._is_search_results_state(query, obs_mock):
                        return True
            else:
                return True

        # Check search task completion
        query = self._extract_search_intent(task)
        if query and getattr(verification, "observed_url", None):
            obs_mock = BrowserObservation(
                session_id=task.session_id,
                task_id=task.task_id,
                current_url=verification.observed_url,
                page_title=verification.observed_title or "",
            )
            if self._is_search_results_state(query, obs_mock):
                return True

        # If explicit success criteria was provided on the task and satisfied
        if task.success_criteria:
            return True

        # If task objective explicitly requires dynamic interactions, never early exit on navigation alone
        obj_lower = (task.objective or "").lower()
        dynamic_keywords = (
            "click", "play", "select", "form", "login", "interact", "choose"
        )
        if any(kw in obj_lower for kw in dynamic_keywords):
            return False

        v_by = getattr(verification, "verified_by", "") or ""
        if (
            "URL destination verification" in v_by
            or "screenshot artifact verification" in v_by
            or "tab state verification" in v_by
            or "task objective satisfied" in v_by
            or ("success criteria verified" in v_by and "General success criteria" not in v_by)
        ):
            return True

        # For single explicit navigation tasks, a non-blank destination URL observed by SemanticVerifier is sufficient
        if self._is_single_navigation_intent(task) and v_by == "non-blank destination URL observed":
            return True

        return False

    # ─────────────────────────────────────────────────────────────────────────
    # Internal: Result builder
    # ─────────────────────────────────────────────────────────────────────────

    def _make_result(
        self,
        task: BrowserTask,
        outcome: BrowserAgentOutcome,
        actions_taken: List[BrowserActionDecision],
        error: Optional[str],
        run_start: float,
        observation: Optional[BrowserObservation],
        verification: Optional[BrowserVerification] = None,
    ) -> BrowserAgentResult:
        duration_ms = (time.perf_counter() - run_start) * 1000.0
        return BrowserAgentResult(
            task_id=task.task_id,
            session_id=task.session_id,
            outcome=outcome,
            objective=task.objective,
            actions_taken=list(actions_taken),
            verification=verification,
            error=error,
            actions_count=len(actions_taken),
            duration_ms=duration_ms,
            final_url=observation.current_url if observation else None,
            final_title=observation.page_title if observation else None,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Module-level helpers
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_element_ref(
    observation: BrowserObservation, ref: str
) -> Optional[str]:
    """
    Resolve a semantic element reference (e.g. "elem_003") to its CSS selector
    using the CURRENT observation's observed_elements list.

    IMPORTANT: This always reads from the observation provided, never from a
    cached or previous observation. Element refs are per-observation ephemeral.

    Args:
        observation: The current BrowserObservation containing ObservedElements.
        ref:         The semantic element reference string.

    Returns:
        CSS selector string if found, else None.
    """
    for el in observation.observed_elements:
        if el.ref == ref:
            return el.css_selector
    return None


# Expose as instance method alias for testability
BrowserAgent._resolve_element_ref = staticmethod(_resolve_element_ref)  # type: ignore[attr-defined]


def _parse_gemini_decision(raw: str) -> BrowserDecision:
    """
    Parse Gemini's JSON response string into a BrowserDecision.

    Raises ValueError if the response cannot be parsed or the decision
    type is unrecognised.

    Args:
        raw: Raw string returned by GeminiProvider.query() in json_mode.

    Returns:
        BrowserDecision populated from the JSON.
    """
    # Strip common markdown fences that some models add despite json_mode
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # Drop first and last fence line
        inner = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(inner).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Gemini returned non-JSON response: {exc}\nRaw: {raw[:300]}")

    raw_decision = data.get(_KEY_DECISION, "").strip().lower()
    reason = data.get(_KEY_REASON, "no reason provided")

    try:
        decision_type = BrowserDecisionType(raw_decision)
    except ValueError:
        raise ValueError(
            f"Unrecognised decision type '{raw_decision}'. "
            f"Must be one of: {[d.value for d in BrowserDecisionType]}"
        )

    # Parse action block (may be absent for finish/fail)
    action_data = data.get(_KEY_ACTION)
    action: Optional[BrowserActionDecision] = None

    if action_data and isinstance(action_data, dict):
        raw_action_type = action_data.get(_KEY_ACTION_TYPE, "").strip().lower()
        try:
            action_type = BrowserActionType(raw_action_type)
        except ValueError:
            raise ValueError(
                f"Unrecognised action_type '{raw_action_type}'. "
                f"Must be one of: {[a.value for a in BrowserActionType]}"
            )

        action = BrowserActionDecision(
            action_type=action_type,
            element_ref=action_data.get(_KEY_ELEM_REF) or None,
            url=action_data.get(_KEY_URL) or None,
            value=action_data.get(_KEY_VALUE) or None,
            key=action_data.get(_KEY_KEY) or None,
            new_tab=bool(action_data.get(_KEY_NEW_TAB, False)),
        )

    return BrowserDecision(
        decision=decision_type,
        action=action,
        reason=reason,
    )


def _decision_to_browser_action(
    action_decision: BrowserActionDecision,
    css_selector: Optional[str],
    task: BrowserTask,
) -> BrowserAction:
    """
    Convert a BrowserActionDecision (agent contract) into a BrowserAction
    (executor contract), filling in the resolved CSS selector.

    Args:
        action_decision: The agent's decision about what action to take.
        css_selector:    CSS selector resolved from the element_ref, or None.
        task:            BrowserTask providing context for the description.

    Returns:
        BrowserAction ready for execution via BrowserTaskAdapter.
    """
    return BrowserAction(
        action_type=action_decision.action_type,
        selector=css_selector,
        value=action_decision.value,
        url=action_decision.url,
        key=action_decision.key,
        new_tab=action_decision.new_tab,
        description=(
            f"BrowserAgent: {action_decision.action_type.value}"
            + (f" on {action_decision.element_ref}" if action_decision.element_ref else "")
            + f" [task={task.task_id}]"
        ),
        metadata={
            "agent": "BrowserAgent",
            "task_id": task.task_id,
            "session_id": task.session_id,
            "element_ref": action_decision.element_ref,
            "new_tab": action_decision.new_tab,
        },
    )
