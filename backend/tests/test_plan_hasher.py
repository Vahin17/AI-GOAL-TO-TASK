"""
test_plan_hasher.py — Unit Tests for Canonical Deterministic Plan Hashing
=========================================================================
Validates:
- Deterministic canonical SHA-256 hash generation
- Order invariance (node/edge list shuffling does not alter hash)
- Layout neutrality (changing React Flow x/y coordinates does not alter hash)
- Mutation sensitivity:
  - Changing node label alters hash
  - Changing tool call or parameter alters hash
  - Adding/removing an edge alters hash
  - Altering dependency connections alters hash
"""

import pytest
from orchestration.plan_hasher import compute_plan_hash
from schemas.models import TaskNode, TaskEdge


def _node(node_id: str, label: str, x: float = 0.0, y: float = 0.0, tool_call: dict = None) -> TaskNode:
    data = {"label": label}
    if tool_call:
        data["tool_call"] = tool_call
    return TaskNode(
        id=node_id,
        type="default",
        position={"x": x, "y": y},
        data=data,
    )


def _edge(source: str, target: str) -> TaskEdge:
    return TaskEdge(
        id=f"e_{source}_{target}",
        source=source,
        target=target,
    )


def test_deterministic_hashing():
    """Identical plan hashed across two distinct invocations returns identical SHA-256."""
    nodes = [
        _node("1", "Open github.com"),
        _node("2", "Search repository"),
    ]
    edges = [_edge("1", "2")]

    hash_1 = compute_plan_hash(nodes, edges, intent="Find repository")
    hash_2 = compute_plan_hash(nodes, edges, intent="Find repository")

    assert isinstance(hash_1, str)
    assert len(hash_1) == 64
    assert hash_1 == hash_2


def test_node_and_edge_order_invariance():
    """List ordering differences do not change the canonical hash."""
    nodes_forward = [
        _node("1", "Step 1"),
        _node("2", "Step 2"),
        _node("3", "Step 3"),
    ]
    nodes_reversed = [
        _node("3", "Step 3"),
        _node("1", "Step 1"),
        _node("2", "Step 2"),
    ]

    edges_forward = [_edge("1", "2"), _edge("2", "3")]
    edges_reversed = [_edge("2", "3"), _edge("1", "2")]

    hash_forward = compute_plan_hash(nodes_forward, edges_forward)
    hash_reversed = compute_plan_hash(nodes_reversed, edges_reversed)

    assert hash_forward == hash_reversed


def test_layout_coordinate_neutrality():
    """Altering UI canvas x/y coordinates does NOT alter the execution hash."""
    nodes_pos_a = [
        _node("1", "Task A", x=0.0, y=0.0),
        _node("2", "Task B", x=0.0, y=150.0),
    ]
    nodes_pos_b = [
        _node("1", "Task A", x=550.0, y=320.0),
        _node("2", "Task B", x=1200.0, y=890.0),
    ]
    edges = [_edge("1", "2")]

    hash_a = compute_plan_hash(nodes_pos_a, edges)
    hash_b = compute_plan_hash(nodes_pos_b, edges)

    assert hash_a == hash_b


def test_label_mutation_changes_hash():
    """Changing any text in a node label strictly alters the hash."""
    nodes_orig = [_node("1", "Click submit button")]
    nodes_mutated = [_node("1", "Click cancel button")]

    hash_orig = compute_plan_hash(nodes_orig, [])
    hash_mutated = compute_plan_hash(nodes_mutated, [])

    assert hash_orig != hash_mutated


def test_tool_parameter_mutation_changes_hash():
    """Altering tool name or parameters strictly alters the hash."""
    nodes_orig = [_node("1", "Navigate", tool_call={"tool_name": "browser", "url": "https://a.com"})]
    nodes_mutated = [_node("1", "Navigate", tool_call={"tool_name": "browser", "url": "https://b.com"})]

    hash_orig = compute_plan_hash(nodes_orig, [])
    hash_mutated = compute_plan_hash(nodes_mutated, [])

    assert hash_orig != hash_mutated


def test_edge_mutation_changes_hash():
    """Adding or modifying an edge changes the hash."""
    nodes = [_node("1", "A"), _node("2", "B"), _node("3", "C")]
    edges_a = [_edge("1", "2"), _edge("2", "3")]
    edges_b = [_edge("1", "3"), _edge("3", "2")]

    hash_a = compute_plan_hash(nodes, edges_a)
    hash_b = compute_plan_hash(nodes, edges_b)

    assert hash_a != hash_b

