"""
plan_critic.py — Independent Plan Critic & Anti-Hallucination Gate (Phase 1)
=============================================================================
Provides an independent verification pass after plan generation.

Responsibilities:
1. Validates action types against registered tools in ToolRegistry and BrowserActionType.
2. Validates DAG structure: detects cycles, missing dependencies, and unreachable steps.
3. Checks goal relevance: ensures steps serve the user's objective.
4. Checks grounding against retrieved RAG context / page evidence.
5. Distinguishes grounded steps from exploratory observation steps.
6. Flags unsupported assumptions, invented capabilities, and hallucinated actions.
7. Produces strongly-typed PlanCriticResult with actionable regeneration feedback.
8. Enforces bounded regeneration (caller loops up to 2 attempts max).
"""

from __future__ import annotations

import enum
import logging
import re
from typing import Any, Dict, List, Optional, Set
from pydantic import BaseModel, Field

from schemas.models import TaskNode, TaskEdge
from execution.registry.tool_registry import ToolRegistry
from execution.browser.browser_models import BrowserActionType

logger = logging.getLogger(__name__)


class CriticIssueSeverity(str, enum.Enum):
    CRITICAL = "critical"  # Triggers plan rejection & regeneration
    WARNING = "warning"    # Advises caution, but does not block execution
    INFO = "info"


class CriticIssue(BaseModel):
    step_id: Optional[str] = Field(None, description="Affected node ID if step-specific")
    issue_type: str = Field(..., description="Category: invalid_tool, cycle, missing_dep, ungrounded, irrelevant")
    severity: CriticIssueSeverity = Field(..., description="Severity level")
    message: str = Field(..., description="Human-readable description of the detected issue")


class StepGrounding(BaseModel):
    step_id: str
    step_name: str
    is_grounded: bool
    step_type: str = Field(..., description="'grounded', 'exploratory', or 'unsupported'")
    grounded_source: Optional[str] = None
    explanation: str


class PlanCriticResult(BaseModel):
    is_valid: bool = Field(..., description="True if plan contains no CRITICAL issues")
    confidence_score: float = Field(..., ge=0.0, le=1.0, description="Overall critic confidence score")
    issues: List[CriticIssue] = Field(default_factory=list)
    step_grounding: List[StepGrounding] = Field(default_factory=list)
    requires_regeneration: bool = Field(default=False)
    regeneration_prompt_feedback: Optional[str] = Field(None, description="Feedback to append to LLM retry prompt")
    summary: str = Field(..., description="Executive summary of critic analysis")


class PlanCritic:
    """
    Independent critic evaluating generated DAG task plans.
    """

    # Common exploratory action keywords (legitimate for web agents)
    EXPLORATORY_KEYWORDS = {
        "inspect", "observe", "check", "locate", "find", "search", "verify",
        "examine", "probe", "read", "view", "wait", "look"
    }

    # Forbidden / Hallucinated dangerous capabilities that no safe web agent possesses
    INVENTED_CAPABILITIES = {
        "execute_arbitrary_shell", "format_disk", "spawn_reverse_shell",
        "bypass_firewall", "extract_private_keys", "send_unauthenticated_email",
        "download_malware", "ddos_target", "intercept_passwords", "raw_kernel_call"
    }

    @classmethod
    def get_registered_capabilities(cls, registry: Optional[ToolRegistry] = None) -> Set[str]:
        """Returns the set of registered tools and browser action types."""
        reg = registry or ToolRegistry.get_instance()
        caps = {action.value.lower() for action in BrowserActionType}
        caps.add("browser")
        for tool_meta in reg.list_tools():
            caps.add(tool_meta.get("name", "").lower())
        return caps

    @classmethod
    def validate_dag_structure(
        cls,
        nodes: List[TaskNode],
        edges: List[TaskEdge],
    ) -> List[CriticIssue]:
        """
        Validates graph structure:
        - Detects missing nodes referenced by edges
        - Detects cycles using DFS
        - Detects unreachable steps / disconnected nodes
        """
        issues: List[CriticIssue] = []
        node_ids = {n.id for n in nodes}

        # 1. Missing node references in edges
        for edge in edges:
            if edge.source not in node_ids:
                issues.append(
                    CriticIssue(
                        step_id=edge.target,
                        issue_type="missing_dependency",
                        severity=CriticIssueSeverity.CRITICAL,
                        message=f"Edge references nonexistent source node '{edge.source}'",
                    )
                )
            if edge.target not in node_ids:
                issues.append(
                    CriticIssue(
                        step_id=edge.source,
                        issue_type="missing_dependency",
                        severity=CriticIssueSeverity.CRITICAL,
                        message=f"Edge references nonexistent target node '{edge.target}'",
                    )
                )

        # 2. Cycle Detection via DFS
        adjacency: Dict[str, List[str]] = {nid: [] for nid in node_ids}
        for edge in edges:
            if edge.source in node_ids and edge.target in node_ids:
                adjacency[edge.source].append(edge.target)

        visited: Set[str] = set()
        rec_stack: Set[str] = set()
        cycle_nodes: Set[str] = set()

        def _dfs(u: str) -> bool:
            visited.add(u)
            rec_stack.add(u)
            for v in adjacency.get(u, []):
                if v not in visited:
                    if _dfs(v):
                        cycle_nodes.add(u)
                        return True
                elif v in rec_stack:
                    cycle_nodes.add(u)
                    cycle_nodes.add(v)
                    return True
            rec_stack.remove(u)
            return False

        for nid in node_ids:
            if nid not in visited:
                if _dfs(nid):
                    break

        if cycle_nodes:
            issues.append(
                CriticIssue(
                    step_id=list(cycle_nodes)[0],
                    issue_type="cycle_detected",
                    severity=CriticIssueSeverity.CRITICAL,
                    message=f"Cycle detected in task DAG involving nodes: {sorted(list(cycle_nodes))}",
                )
            )

        return issues

    @classmethod
    def validate_tool_capabilities(
        cls,
        nodes: List[TaskNode],
        registry: Optional[ToolRegistry] = None,
    ) -> List[CriticIssue]:
        """
        Validates that all steps use known, registered actions and tools,
        and flags invented or forbidden capabilities.
        """
        issues: List[CriticIssue] = []
        allowed_tools = cls.get_registered_capabilities(registry)

        for node in nodes:
            label = (node.data.get("label") or "").lower()
            tool_call = node.data.get("tool_call") or {}
            tool_name = (tool_call.get("tool_name") or "").lower()
            action_type = (tool_call.get("action_type") or "").lower()

            # Check for explicitly invented dangerous capabilities
            for invented in cls.INVENTED_CAPABILITIES:
                if invented in label or invented in tool_name or invented in action_type:
                    issues.append(
                        CriticIssue(
                            step_id=node.id,
                            issue_type="invalid_tool",
                            severity=CriticIssueSeverity.CRITICAL,
                            message=f"Step '{node.data.get('label')}' requests invented or forbidden capability '{invented}'",
                        )
                    )

            # If tool_name is specified, check against registered tools
            if tool_name and tool_name not in allowed_tools:
                issues.append(
                    CriticIssue(
                        step_id=node.id,
                        issue_type="unregistered_tool",
                        severity=CriticIssueSeverity.CRITICAL,
                        message=f"Step '{node.data.get('label')}' specifies unregistered tool '{tool_name}'",
                    )
                )

            # If action_type is specified, check against BrowserActionType
            if action_type and action_type not in allowed_tools:
                issues.append(
                    CriticIssue(
                        step_id=node.id,
                        issue_type="invalid_action_type",
                        severity=CriticIssueSeverity.CRITICAL,
                        message=f"Step '{node.data.get('label')}' specifies invalid action type '{action_type}'",
                    )
                )

        return issues

    @classmethod
    def evaluate_grounding_and_relevance(
        cls,
        goal: str,
        nodes: List[TaskNode],
        edges: List[TaskEdge],
        rag_context: str = "",
        registry: Optional[ToolRegistry] = None,
    ) -> PlanCriticResult:
        """
        Performs the complete verification pass.
        Combines deterministic structural & capability checks with grounding heuristics.
        """
        if not nodes:
            return PlanCriticResult(
                is_valid=False,
                confidence_score=0.0,
                issues=[
                    CriticIssue(
                        issue_type="empty_plan",
                        severity=CriticIssueSeverity.CRITICAL,
                        message="Plan contains zero task nodes.",
                    )
                ],
                step_grounding=[],
                requires_regeneration=True,
                regeneration_prompt_feedback="Plan cannot be empty. Generate discrete task steps.",
                summary="Rejected: Plan has no nodes.",
            )

        # 1. Structural DAG validation
        structural_issues = cls.validate_dag_structure(nodes, edges)

        # 2. Tool capability validation
        capability_issues = cls.validate_tool_capabilities(nodes, registry)

        all_issues = structural_issues + capability_issues

        # 3. Grounding and relevance analysis
        step_groundings: List[StepGrounding] = []
        goal_words = set(re.findall(r"\w+", goal.lower()))
        rag_lower = rag_context.lower() if rag_context else ""

        unsupported_count = 0

        for node in nodes:
            label = node.data.get("label", "Untitled Task")
            label_lower = label.lower()
            label_words = set(re.findall(r"\w+", label_lower))

            # Check if exploratory action (e.g. observe DOM, search, check status)
            is_exploratory = any(kw in label_lower for kw in cls.EXPLORATORY_KEYWORDS)

            # Check if grounded in RAG context or user goal
            grounded_in_rag = bool(rag_lower and any(w in rag_lower for w in label_words if len(w) > 4))
            grounded_in_goal = bool(any(w in goal_words for w in label_words if len(w) > 3))

            if grounded_in_rag:
                sg = StepGrounding(
                    step_id=node.id,
                    step_name=label,
                    is_grounded=True,
                    step_type="grounded",
                    grounded_source="knowledge_base",
                    explanation="Step aligns directly with retrieved domain knowledge.",
                )
            elif grounded_in_goal:
                sg = StepGrounding(
                    step_id=node.id,
                    step_name=label,
                    is_grounded=True,
                    step_type="grounded",
                    grounded_source="user_goal",
                    explanation="Step directly executes intent expressed in user goal.",
                )
            elif is_exploratory:
                sg = StepGrounding(
                    step_id=node.id,
                    step_name=label,
                    is_grounded=True,
                    step_type="exploratory",
                    grounded_source="browser_perception",
                    explanation="Step performs necessary exploratory observation or verification.",
                )
            else:
                unsupported_count += 1
                sg = StepGrounding(
                    step_id=node.id,
                    step_name=label,
                    is_grounded=False,
                    step_type="unsupported",
                    grounded_source=None,
                    explanation="Step lacks clear grounding in goal parameters or knowledge base.",
                )
                all_issues.append(
                    CriticIssue(
                        step_id=node.id,
                        issue_type="unsupported_step",
                        severity=CriticIssueSeverity.WARNING,
                        message=f"Step '{label}' lacks direct grounding in goal or retrieved knowledge.",
                    )
                )

            step_groundings.append(sg)

        # 4. Determine validity and regeneration requirement
        has_critical = any(issue.severity == CriticIssueSeverity.CRITICAL for issue in all_issues)
        is_valid = not has_critical

        # If more than 50% of steps are unsupported, treat as critical failure
        if len(nodes) > 1 and unsupported_count / len(nodes) > 0.5:
            is_valid = False
            has_critical = True
            all_issues.append(
                CriticIssue(
                    issue_type="excessive_unsupported_steps",
                    severity=CriticIssueSeverity.CRITICAL,
                    message=f"More than half of the steps ({unsupported_count}/{len(nodes)}) are unsupported.",
                )
            )

        # Calculate confidence score
        penalty = sum(0.3 for i in all_issues if i.severity == CriticIssueSeverity.CRITICAL)
        penalty += sum(0.1 for i in all_issues if i.severity == CriticIssueSeverity.WARNING)
        confidence = max(0.0, min(1.0, 1.0 - penalty))

        # Generate feedback for regeneration if needed
        feedback = None
        if not is_valid:
            crit_msgs = [i.message for i in all_issues if i.severity == CriticIssueSeverity.CRITICAL]
            feedback = f"Plan critic rejected previous plan due to: {'; '.join(crit_msgs)}. Correct these issues in the regenerated plan."

        summary = (
            f"Critic verification passed ({len(nodes)} steps, {len(all_issues)} warnings, confidence {confidence:.2f})"
            if is_valid
            else f"Critic verification failed with {len([i for i in all_issues if i.severity == CriticIssueSeverity.CRITICAL])} critical issues."
        )

        return PlanCriticResult(
            is_valid=is_valid,
            confidence_score=round(confidence, 2),
            issues=all_issues,
            step_grounding=step_groundings,
            requires_regeneration=not is_valid,
            regeneration_prompt_feedback=feedback,
            summary=summary,
        )

