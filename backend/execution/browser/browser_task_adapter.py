"""
browser_task_adapter.py — ARDO & Safety Integration Adapter for Browser Tasks
=============================================================================
Maps ARDO DAG tasks to Playwright Browser Actions, enforces Safety Policies,
records MongoDB operational telemetry, and saves execution memories.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from execution.browser.browser_models import (
    BrowserAction,
    BrowserActionResult,
    BrowserActionType,
    BrowserWorkflowResult,
)
from execution.browser.playwright_executor import PlaywrightBrowserExecutor
from orchestration.safety_engine import SafetyEngine
from orchestration.safety_policy import SafetyPolicyEngine
from orchestration.safety_schemas import (
    ActionType,
    DataSensitivity,
    DestructivePotential,
    ExecutionDecision,
    RiskLevel,
    SafetyContext,
    SafetyDecision,
    TargetCategory,
)

logger = logging.getLogger(__name__)


class BrowserTaskAdapter:
    """
    Adapter bridging ARDO Tasks and Safety Policies to Playwright Execution.
    """

    def __init__(
        self,
        executor: Optional[PlaywrightBrowserExecutor] = None,
        safety_engine: Optional[SafetyEngine] = None,
        safety_policy_engine: Optional[SafetyPolicyEngine] = None,
    ):
        self.executor = executor or PlaywrightBrowserExecutor(headless=True)
        self.safety_engine = safety_engine or SafetyEngine()
        self.safety_policy_engine = safety_policy_engine or SafetyPolicyEngine()

    @staticmethod
    def infer_safety_context(task_id: str, action: BrowserAction) -> SafetyContext:
        """
        Maps a BrowserAction into a typed SafetyContext for deterministic safety evaluation.
        """
        if action.action_type == BrowserActionType.NAVIGATE:
            url = action.url or ""
            is_external = not (url.startswith("http://localhost") or url.startswith("http://127.0.0.1") or url.startswith("file://"))
            return SafetyContext(
                task_id=task_id,
                action=ActionType.NAVIGATE,
                target=url,
                target_category=TargetCategory.NETWORK_ENDPOINT,
                external_side_effect=is_external,
            )

        elif action.action_type in (BrowserActionType.EXTRACT_TEXT, BrowserActionType.GET_TITLE, BrowserActionType.GET_URL):
            return SafetyContext(
                task_id=task_id,
                action=ActionType.READ,
                target=action.selector or "page",
                target_category=TargetCategory.APPLICATION_DATA,
            )

        elif action.action_type == BrowserActionType.FILL:
            # Check if fill targets credentials
            selector_lower = (action.selector or "").lower()
            is_credential = any(k in selector_lower for k in ("password", "token", "secret", "apikey", "pin", "credential", "vault"))
            target_cat = TargetCategory.CREDENTIAL if is_credential else TargetCategory.APPLICATION_DATA
            return SafetyContext(
                task_id=task_id,
                action=ActionType.WRITE,
                target=action.selector or "input",
                target_category=target_cat,
                data_sensitivity=DataSensitivity.CREDENTIAL if is_credential else DataSensitivity.NONE,
            )

        elif action.action_type in (BrowserActionType.CLICK, BrowserActionType.PRESS):
            selector_lower = (action.selector or "").lower()
            is_destructive = any(k in selector_lower for k in ("delete", "wipe", "format", "drop", "destroy", "purge"))
            return SafetyContext(
                task_id=task_id,
                action=ActionType.DELETE if is_destructive else ActionType.EXECUTE,
                target=action.selector or "button",
                target_category=TargetCategory.APPLICATION_DATA,
                destructive_potential=DestructivePotential.HIGH if is_destructive else DestructivePotential.NONE,
            )

        else:
            return SafetyContext(
                task_id=task_id,
                action=ActionType.EXECUTE,
                target=action.selector or "element",
                target_category=TargetCategory.APPLICATION_DATA,
            )

    def evaluate_action_safety(self, task_id: str, action: BrowserAction) -> SafetyDecision:
        """
        Evaluates browser action against the deterministic SafetyPolicyEngine.
        """
        from orchestration.safety_schemas import DataSensitivity, DestructivePotential
        s_ctx = self.infer_safety_context(task_id, action)
        decision = self.safety_policy_engine.evaluate(s_ctx, action.description or f"Browser {action.action_type.value}")
        return decision

    async def execute_safe_browser_task(
        self,
        task_id: str,
        session_id: str,
        action: BrowserAction,
        approval_token: Optional[str] = None,
    ) -> Tuple[BrowserActionResult, SafetyDecision]:
        """
        Evaluates safety -> executes action if allowed -> returns result + decision.
        """
        decision = self.evaluate_action_safety(task_id, action)

        # 1. Blocked by Safety Policy
        if decision.is_blocked:
            logger.warning(f"[BrowserTaskAdapter] Task {task_id} BLOCKED by Safety Policy: {decision.explanation}")
            return (
                BrowserActionResult(
                    success=False,
                    action_type=action.action_type,
                    error=f"BLOCKED_BY_SAFETY_POLICY: {decision.explanation}",
                    metadata={"decision": decision.execution_decision.value, "risk_level": decision.risk_level.value},
                ),
                decision,
            )

        # 2. Requires Approval Gate
        if decision.execution_decision == ExecutionDecision.REQUIRE_APPROVAL:
            task_desc = action.description or f"Browser {action.action_type.value}"
            if not self.safety_engine.is_approved(session_id, task_id, task_desc):
                logger.info(f"[BrowserTaskAdapter] Task {task_id} requires approval before execution.")
                return (
                    BrowserActionResult(
                        success=False,
                        action_type=action.action_type,
                        error="APPROVAL_REQUIRED: Operator approval needed for high-risk browser action.",
                        metadata={"decision": decision.execution_decision.value, "risk_level": decision.risk_level.value},
                    ),
                    decision,
                )

        # 3. Executable
        result = await self.executor.execute_action(action)
        return result, decision
