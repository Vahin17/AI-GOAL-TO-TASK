"""
test_plan_critic.py — Unit Tests for Independent PlanCritic & Anti-Hallucination Gate
======================================================================================
Validates:
- Valid plans passing critic verification with high confidence
- Detection and rejection of cyclic dependencies
- Detection and rejection of edges referencing non-existent nodes
- Detection and rejection of invented/forbidden capabilities (e.g. shell, format disk)
- Rejection of unregistered tools
- Correct categorization of grounded, exploratory, and unsupported steps
- Actionable regeneration feedback generation
"""

import pytest
from schemas.models import TaskNode, TaskEdge
from ai.plan_critic import PlanCritic, PlanCriticResult, CriticIssueSeverity


def _make_node(node_id: str, label: str, tool_name: str = "", action_type: str = "") -> TaskNode:
    data = {"label": label}
    if tool_name or action_type:
        data["tool_call"] = {"tool_name": tool_name, "action_type": action_type}
    return TaskNode(
        id=node_id,
        type="default",
        position={"x": 0.0, "y": 0.0},
        data=data,
    )


def _make_edge(source: str, target: str) -> TaskEdge:
    return TaskEdge(
        id=f"e_{source}_{target}",
        source=source,
        target=target,
    )


def test_valid_plan_passes_critic():
    """A standard valid goal and linear plan pass critic verification."""
    goal = "Navigate to github.com and take a screenshot"
    nodes = [
        _make_node("node_1", "Navigate to github.com", action_type="navigate"),
        _make_node("node_2", "Inspect page elements to confirm loaded", action_type="inspect_page"),
        _make_node("node_3", "Take screenshot of github homepage", action_type="screenshot"),
    ]
    edges = [
        _make_edge("node_1", "node_2"),
        _make_edge("node_2", "node_3"),
    ]

    result: PlanCriticResult = PlanCritic.evaluate_grounding_and_relevance(goal, nodes, edges)
    assert result.is_valid is True
    assert result.requires_regeneration is False
    assert result.confidence_score >= 0.8
    assert len(result.step_grounding) == 3
    # Check exploratory step categorization
    inspect_step = next(s for s in result.step_grounding if s.step_id == "node_2")
    assert inspect_step.step_type == "exploratory"


def test_cycle_detected_and_rejected():
    """A plan containing a dependency cycle must be rejected with CRITICAL issue."""
    goal = "Perform workflow"
    nodes = [
        _make_node("step_a", "Step A"),
        _make_node("step_b", "Step B"),
        _make_node("step_c", "Step C"),
    ]
    edges = [
        _make_edge("step_a", "step_b"),
        _make_edge("step_b", "step_c"),
        _make_edge("step_c", "step_a"),  # Cycle: A -> B -> C -> A
    ]

    result = PlanCritic.evaluate_grounding_and_relevance(goal, nodes, edges)
    assert result.is_valid is False
    assert result.requires_regeneration is True
    assert any(i.issue_type == "cycle_detected" for i in result.issues)
    assert "Cycle detected" in result.regeneration_prompt_feedback


def test_missing_dependency_node_rejected():
    """An edge referencing a non-existent node ID is rejected."""
    goal = "Search web"
    nodes = [
        _make_node("n1", "Navigate to google.com"),
    ]
    edges = [
        _make_edge("n1", "non_existent_node_99"),
    ]

    result = PlanCritic.evaluate_grounding_and_relevance(goal, nodes, edges)
    assert result.is_valid is False
    assert any(i.issue_type == "missing_dependency" for i in result.issues)


def test_invented_capability_rejected():
    """Invented dangerous capabilities like execute_arbitrary_shell are flagged and rejected."""
    goal = "Clean workspace"
    nodes = [
        _make_node("n1", "Execute arbitrary shell command to purge disk", tool_name="execute_arbitrary_shell"),
    ]
    edges = []

    result = PlanCritic.evaluate_grounding_and_relevance(goal, nodes, edges)
    assert result.is_valid is False
    assert any(i.issue_type == "invalid_tool" for i in result.issues)


def test_unregistered_tool_rejected():
    """A step specifying an unregistered tool is flagged as critical."""
    goal = "Send message"
    nodes = [
        _make_node("n1", "Send telegram alert", tool_name="telegram_bot_unregistered"),
    ]
    edges = []

    result = PlanCritic.evaluate_grounding_and_relevance(goal, nodes, edges)
    assert result.is_valid is False
    assert any(i.issue_type == "unregistered_tool" for i in result.issues)


def test_empty_plan_rejected():
    """An empty plan with 0 nodes is rejected."""
    result = PlanCritic.evaluate_grounding_and_relevance("Do something", [], [])
    assert result.is_valid is False
    assert result.requires_regeneration is True


def test_rag_grounded_steps_recognized():
    """Steps aligned with retrieved RAG context are tagged as grounded knowledge_base."""
    goal = "Troubleshoot system authentication error"
    rag_context = "Procedure for handling authentication failures in Playwright using session cookies."
    nodes = [
        _make_node("n1", "Inspect Playwright session cookies for authentication failures"),
    ]
    edges = []

    result = PlanCritic.evaluate_grounding_and_relevance(goal, nodes, edges, rag_context=rag_context)
    assert result.is_valid is True
    assert result.step_grounding[0].is_grounded is True
    assert result.step_grounding[0].grounded_source == "knowledge_base"

