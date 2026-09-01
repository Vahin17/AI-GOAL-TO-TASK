"""
safety_policy.py — Phase 5: Deterministic Safety Policy Engine
===============================================================
The authoritative enforcement layer of the Hybrid Safety Engine.

This module evaluates a strongly-typed SafetyContext against explicit,
named policy rules and produces a final SafetyDecision.

Critical Invariants:
    1. The policy engine NEVER reads raw task text for primary decisions.
       It operates on the TYPED SafetyContext fields from semantic analysis.
    2. Hard boundaries CANNOT be overridden by LLM output or operator approval.
       CRITICAL (BLOCK) tasks are terminal — no frontend button bypasses them.
    3. Keyword signals (HIGH_RISK_KEYWORDS) may serve as WEAK fallback
       evidence only when SafetyContext is unavailable (UNKNOWN handling).
    4. Every policy has a stable PolicyID for auditability.

Policy Hierarchy:
    BLOCK-level policies   → CRITICAL risk (no approval possible)
    APPROVAL-level policies → HIGH risk (operator approval required)
    LOGGING-level policies  → MEDIUM risk
    All others             → LOW risk (ALLOW)
"""

from __future__ import annotations

import logging
from typing import List, Tuple

from orchestration.safety_schemas import (
    SafetyContext,
    SafetyDecision,
    RiskLevel,
    ExecutionDecision,
    PolicyID,
    ActionType,
    TargetCategory,
    Scope,
    Reversibility,
    DestructivePotential,
    DataSensitivity,
    AmbiguityLevel,
    Environment,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Policy Definitions
# Each policy has: ID, description, triggering_conditions, effect
# ─────────────────────────────────────────────────────────────────────────────

class PolicyCatalog:
    """
    Registry of all deterministic safety policies.
    Each entry describes the policy and its trigger logic in docstrings.
    The actual evaluation is performed in SafetyPolicyEngine.evaluate().
    """

    POLICIES = {
        PolicyID.PROTECTED_SYSTEM_RESOURCE: {
            "description": "Task targets OS-critical system or boot files.",
            "trigger": "target_category in [OS_SYSTEM_FILE, OS_BOOT_FILE] OR system_critical=True",
            "risk_contribution": RiskLevel.CRITICAL,
            "execution_constraint": ExecutionDecision.BLOCK,
        },
        PolicyID.IRREVERSIBLE_USER_DATA: {
            "description": "Task permanently destroys user-controlled data with no recovery path.",
            "trigger": "destructive_potential in [HIGH, CRITICAL] AND reversibility=NONE AND scope=USER",
            "risk_contribution": RiskLevel.HIGH,
            "execution_constraint": ExecutionDecision.REQUIRE_APPROVAL,
        },
        PolicyID.PRIVILEGED_OPERATION: {
            "description": "Task requires elevated privileges to execute.",
            "trigger": "privilege_required=True AND (destructive_potential >= MEDIUM OR system_critical=True)",
            "risk_contribution": RiskLevel.HIGH,
            "execution_constraint": ExecutionDecision.REQUIRE_APPROVAL,
        },
        PolicyID.EXTERNAL_SIDE_EFFECT: {
            "description": "Task transmits data to or triggers actions in external services.",
            "trigger": "external_side_effect=True AND action in [TRANSMIT, DEPLOY, EXECUTE]",
            "risk_contribution": RiskLevel.HIGH,
            "execution_constraint": ExecutionDecision.REQUIRE_APPROVAL,
        },
        PolicyID.DESTRUCTIVE_AMBIGUOUS_TARGET: {
            "description": "Destructive action on an ambiguous or unknown target.",
            "trigger": "destructive_potential >= MEDIUM AND ambiguity in [HIGHLY, CONTRADICTORY]",
            "risk_contribution": RiskLevel.HIGH,
            "execution_constraint": ExecutionDecision.REQUIRE_APPROVAL,
        },
        PolicyID.CREDENTIAL_MODIFICATION: {
            "description": "Task modifies security credentials, tokens, or authentication config.",
            "trigger": "data_sensitivity=CREDENTIAL AND action in [MODIFY, DELETE, WRITE, CONFIGURE]",
            "risk_contribution": RiskLevel.CRITICAL,
            "execution_constraint": ExecutionDecision.BLOCK,
        },
        PolicyID.HIGH_IMPACT_CONFIGURATION: {
            "description": "Task modifies system or application configuration with significant impact.",
            "trigger": "action=CONFIGURE AND (system_critical=True OR scope=SYSTEM)",
            "risk_contribution": RiskLevel.HIGH,
            "execution_constraint": ExecutionDecision.REQUIRE_APPROVAL,
        },
        PolicyID.PRODUCTION_DESTRUCTIVE_OP: {
            "description": "Destructive operation in a production environment.",
            "trigger": "environment=PRODUCTION AND destructive_potential >= HIGH",
            "risk_contribution": RiskLevel.CRITICAL,
            "execution_constraint": ExecutionDecision.BLOCK,
        },
        PolicyID.UNKNOWN_SEMANTIC_CONTEXT: {
            "description": "Semantic analysis unavailable or confidence too low to trust context.",
            "trigger": "semantic_confidence < 0.35 OR context is UNKNOWN",
            "risk_contribution": RiskLevel.UNKNOWN,
            "execution_constraint": ExecutionDecision.REQUIRE_APPROVAL,
        },
        PolicyID.LOW_CONFIDENCE_HIGH_IMPACT: {
            "description": "Low confidence combined with potentially high-impact action.",
            "trigger": "semantic_confidence < 0.6 AND destructive_potential >= MEDIUM",
            "risk_contribution": RiskLevel.HIGH,
            "execution_constraint": ExecutionDecision.REQUIRE_APPROVAL,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Weak Lexical Signals (demoted fallback only)
# ─────────────────────────────────────────────────────────────────────────────

# These keywords are NEVER primary safety determinants.
# They serve only as weak evidence when SafetyContext fields are UNKNOWN.
_WEAK_BLOCK_SIGNALS = [
    "boot", "kernel", "system32", "etc/passwd", "etc/shadow",
    "ntldr", "grub", "/dev/sd", "format c:", "rm -rf /",
]

_WEAK_APPROVAL_SIGNALS = [
    "delete", "remove", "drop", "destroy", "wipe",
    "overwrite", "truncate", "purge", "erase",
]


def _weak_lexical_risk(task_name: str) -> Tuple[RiskLevel, List[PolicyID]]:
    """
    Weak fallback evaluation for UNKNOWN contexts only.
    MUST NOT be called when a valid SafetyContext is available.
    """
    label = task_name.lower()
    for sig in _WEAK_BLOCK_SIGNALS:
        if sig in label:
            return RiskLevel.CRITICAL, [PolicyID.PROTECTED_SYSTEM_RESOURCE]
    for sig in _WEAK_APPROVAL_SIGNALS:
        if sig in label:
            return RiskLevel.HIGH, [PolicyID.DESTRUCTIVE_AMBIGUOUS_TARGET]
    return RiskLevel.UNKNOWN, [PolicyID.UNKNOWN_SEMANTIC_CONTEXT]
# Ordered set helpers for DestructivePotential comparisons
# (enum members cannot use >= natively without IntEnum)
_MEDIUM_PLUS = {DestructivePotential.MEDIUM, DestructivePotential.HIGH, DestructivePotential.CRITICAL}
_HIGH_PLUS   = {DestructivePotential.HIGH, DestructivePotential.CRITICAL}


# ─────────────────────────────────────────────────────────────────────────────

def _risk_to_decision(risk: RiskLevel, triggered: List[PolicyID]) -> ExecutionDecision:
    """
    Map risk level to execution decision using deterministic policy constraints.
    If any triggered policy enforces BLOCK, that takes precedence.
    """
    # Absolute BLOCK policies always win
    block_policies = {PolicyID.PROTECTED_SYSTEM_RESOURCE, PolicyID.CREDENTIAL_MODIFICATION,
                      PolicyID.PRODUCTION_DESTRUCTIVE_OP}
    if any(p in block_policies for p in triggered):
        return ExecutionDecision.BLOCK

    if risk == RiskLevel.CRITICAL:
        return ExecutionDecision.BLOCK
    if risk == RiskLevel.HIGH:
        return ExecutionDecision.REQUIRE_APPROVAL
    if risk == RiskLevel.UNKNOWN:
        return ExecutionDecision.REQUIRE_APPROVAL   # Conservative default
    if risk == RiskLevel.MEDIUM:
        return ExecutionDecision.ALLOW_WITH_LOGGING
    return ExecutionDecision.ALLOW


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic Policy Engine
# ─────────────────────────────────────────────────────────────────────────────

class SafetyPolicyEngine:
    """
    Evaluates a SafetyContext against all deterministic policies.
    Returns a fully populated SafetyDecision with explanation.

    Usage:
        engine = SafetyPolicyEngine()
        decision = engine.evaluate(ctx, task_name)
    """

    def evaluate(
        self,
        ctx: SafetyContext,
        task_name: str = "",
    ) -> SafetyDecision:
        """
        Evaluate a task's SafetyContext against all policies.
        
        This is the ONLY function that determines execution eligibility.
        No LLM result can override this output.
        """
        triggered: List[PolicyID] = []
        risk_level = RiskLevel.LOW

        # ── Evaluate each policy ───────────────────────────────────────────

        # POLICY_PROTECTED_SYSTEM_RESOURCE
        if (ctx.target_category in (TargetCategory.OS_SYSTEM_FILE, TargetCategory.OS_BOOT_FILE)
                or ctx.system_critical):
            triggered.append(PolicyID.PROTECTED_SYSTEM_RESOURCE)
            risk_level = _upgrade_risk(risk_level, RiskLevel.CRITICAL)

        # POLICY_CREDENTIAL_MODIFICATION
        if (ctx.data_sensitivity == DataSensitivity.CREDENTIAL
                and ctx.action in (ActionType.MODIFY, ActionType.DELETE,
                                   ActionType.WRITE, ActionType.CONFIGURE)):
            triggered.append(PolicyID.CREDENTIAL_MODIFICATION)
            risk_level = _upgrade_risk(risk_level, RiskLevel.CRITICAL)

        # POLICY_PRODUCTION_DESTRUCTIVE_OP
        if (ctx.environment == Environment.PRODUCTION
                and ctx.destructive_potential in (DestructivePotential.HIGH, DestructivePotential.CRITICAL)):
            triggered.append(PolicyID.PRODUCTION_DESTRUCTIVE_OP)
            risk_level = _upgrade_risk(risk_level, RiskLevel.CRITICAL)

        # POLICY_IRREVERSIBLE_USER_DATA
        if (ctx.destructive_potential in (DestructivePotential.HIGH, DestructivePotential.CRITICAL)
                and ctx.reversibility == Reversibility.NONE
                and ctx.scope == Scope.USER):
            triggered.append(PolicyID.IRREVERSIBLE_USER_DATA)
            risk_level = _upgrade_risk(risk_level, RiskLevel.HIGH)

        # POLICY_PRIVILEGED_OPERATION
        if (ctx.privilege_required
                and ctx.destructive_potential in (DestructivePotential.MEDIUM, DestructivePotential.HIGH, DestructivePotential.CRITICAL)):
            triggered.append(PolicyID.PRIVILEGED_OPERATION)
            risk_level = _upgrade_risk(risk_level, RiskLevel.HIGH)

        # POLICY_EXTERNAL_SIDE_EFFECT
        if (ctx.external_side_effect
                and ctx.action in (ActionType.TRANSMIT, ActionType.DEPLOY, ActionType.EXECUTE)):
            triggered.append(PolicyID.EXTERNAL_SIDE_EFFECT)
            risk_level = _upgrade_risk(risk_level, RiskLevel.HIGH)

        # POLICY_DESTRUCTIVE_AMBIGUOUS_TARGET
        if (ctx.destructive_potential in _MEDIUM_PLUS
                and ctx.ambiguity in (AmbiguityLevel.HIGHLY, AmbiguityLevel.CONTRADICTORY)):
            triggered.append(PolicyID.DESTRUCTIVE_AMBIGUOUS_TARGET)
            risk_level = _upgrade_risk(risk_level, RiskLevel.HIGH)

        # POLICY_HIGH_IMPACT_CONFIGURATION
        if (ctx.action == ActionType.CONFIGURE
                and (ctx.system_critical or ctx.scope == Scope.SYSTEM)):
            triggered.append(PolicyID.HIGH_IMPACT_CONFIGURATION)
            risk_level = _upgrade_risk(risk_level, RiskLevel.HIGH)

        # POLICY_UNKNOWN_SEMANTIC_CONTEXT
        if ctx.semantic_confidence < 0.35:
            triggered.append(PolicyID.UNKNOWN_SEMANTIC_CONTEXT)
            risk_level = _upgrade_risk(risk_level, RiskLevel.UNKNOWN)

        # POLICY_LOW_CONFIDENCE_HIGH_IMPACT
        elif (ctx.semantic_confidence < 0.6
              and ctx.destructive_potential in _MEDIUM_PLUS):
            triggered.append(PolicyID.LOW_CONFIDENCE_HIGH_IMPACT)
            risk_level = _upgrade_risk(risk_level, RiskLevel.HIGH)

        # ── Handle completely unknown context (lexical fallback) ───────────
        if (ctx.action == ActionType.UNKNOWN
                and ctx.target_category == TargetCategory.UNKNOWN
                and not triggered):
            lex_risk, lex_policies = _weak_lexical_risk(task_name)
            triggered.extend(lex_policies)
            risk_level = _upgrade_risk(risk_level, lex_risk)

        # ── Handle MEDIUM-risk scenarios (no specific policy) ─────────────
        if not triggered and ctx.destructive_potential == DestructivePotential.MEDIUM:
            risk_level = _upgrade_risk(risk_level, RiskLevel.MEDIUM)

        # ── Decision mapping ──────────────────────────────────────────────
        decision = _risk_to_decision(risk_level, triggered)

        # ── Generate structured explanation (from policy data, NOT LLM) ──
        explanation = _build_explanation(ctx, risk_level, decision, triggered)

        result = SafetyDecision(
            task_id=ctx.task_id,
            task_name=task_name,
            risk_level=risk_level,
            execution_decision=decision,
            triggered_policy_ids=triggered,
            semantic_context=ctx,
            semantic_confidence=ctx.semantic_confidence,
            explanation=explanation,
        )

        logger.info(
            f"[PolicyEngine] task={ctx.task_id} risk={risk_level.value} "
            f"decision={decision.value} policies={[p.value for p in triggered]}"
        )
        return result

    def evaluate_unknown(self, task_id: str, task_name: str) -> SafetyDecision:
        """
        Produce a SafetyDecision for tasks where semantic analysis failed entirely.
        Uses lexical signals as weak fallback. Explicitly labeled UNKNOWN.
        """
        ctx = SafetyContext.unknown(task_id)
        lex_risk, lex_policies = _weak_lexical_risk(task_name)

        explanation = (
            f"Semantic analysis unavailable for '{task_name}'. "
            f"Weak lexical signals suggest {lex_risk.value} risk. "
            "Applying conservative require-approval policy."
        )
        decision = _risk_to_decision(lex_risk, lex_policies)

        return SafetyDecision(
            task_id=task_id,
            task_name=task_name,
            risk_level=lex_risk,
            execution_decision=decision,
            triggered_policy_ids=lex_policies,
            semantic_context=ctx,
            semantic_confidence=0.0,
            explanation=explanation,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Private Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _upgrade_risk(current: RiskLevel, candidate: RiskLevel) -> RiskLevel:
    """
    Risk levels form a strict ordering: LOW < MEDIUM < HIGH < CRITICAL.
    UNKNOWN is treated conservatively as requiring approval (between MEDIUM and HIGH).
    Only upgrade, never downgrade.
    """
    order = {
        RiskLevel.LOW: 0,
        RiskLevel.MEDIUM: 1,
        RiskLevel.UNKNOWN: 2,
        RiskLevel.HIGH: 3,
        RiskLevel.CRITICAL: 4,
    }
    if order.get(candidate, 0) > order.get(current, 0):
        return candidate
    return current


def _build_explanation(
    ctx: SafetyContext,
    risk: RiskLevel,
    decision: ExecutionDecision,
    policies: List[PolicyID],
) -> str:
    """
    Build a human-readable explanation from structured policy data.
    MUST NOT call an LLM — derived entirely from ctx + policy results.
    """
    action_str = ctx.action.value.capitalize()
    target_str = ctx.target if ctx.target and ctx.target != "unknown" else ctx.target_category.value.replace("_", " ")

    policy_descs = [
        PolicyCatalog.POLICIES.get(p, {}).get("description", p.value)
        for p in policies
    ]

    reason_parts = []
    if ctx.reversibility == Reversibility.NONE:
        reason_parts.append("action is irreversible")
    if ctx.system_critical:
        reason_parts.append("target is system-critical")
    if ctx.privilege_required:
        reason_parts.append("requires elevated privileges")
    if ctx.external_side_effect:
        reason_parts.append("produces external side effects")
    if ctx.ambiguity in (AmbiguityLevel.HIGHLY, AmbiguityLevel.CONTRADICTORY):
        reason_parts.append("target is ambiguous")
    if ctx.semantic_confidence < 0.5:
        reason_parts.append(f"semantic confidence low ({ctx.semantic_confidence:.0%})")

    base = f"{action_str} on '{target_str}' — Risk: {risk.value.upper()}, Decision: {decision.value.upper()}."
    if reason_parts:
        base += f" Factors: {'; '.join(reason_parts)}."
    if policy_descs:
        base += f" Policies triggered: {'; '.join(policy_descs)}."
    return base
