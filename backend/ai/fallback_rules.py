import re
import uuid
from typing import List, Optional
from schemas.models import TaskNode, TaskEdge, PlanGraphMetadata, PlanGraphResponse


def decompose_goal_heuristically(goal: str) -> List[str]:
    """
    Decomposes a compound goal into ordered actionable tasks.
    Splits on commas, semicolons, 'and', 'then', and sequential transition phrases.
    """
    if not goal or not goal.strip():
        return ["Initialization Phase", "Execution Phase", "Verification Phase"]

    clean_goal = goal.strip().rstrip(".")

    # Split by:
    # 1. Commas or semicolons
    # 2. "then" or "and then"
    # 3. "and" followed by an action verb
    pattern = r'[,;]|\b(?:then|and\s+then)\b|\band\s+(?=(?:open|search|click|find|update|edit|take|capture|extract|verify|fill|navigate|visit|press|submit|select|give|get|show|read|close)\b)'
    raw_steps = re.split(pattern, clean_goal, flags=re.IGNORECASE)

    subtasks = []
    for step in raw_steps:
        cleaned = step.strip()
        if cleaned:
            # Capitalize first letter
            cleaned = cleaned[0].upper() + cleaned[1:]
            subtasks.append(cleaned)

    # If splitting yielded meaningful steps (>= 2), return them
    if len(subtasks) >= 2:
        return subtasks

    # If single step mentions browser actions or web operations
    lower = clean_goal.lower()
    browser_keywords = (
        "dashboard", "navigate", "open", "visit", "go to", "browse",
        "search", "update", "screenshot", "click", "fill", "type", "tab", "http"
    )
    if any(kw in lower for kw in browser_keywords):
        inferred = []
        if "dashboard" in lower and not lower.startswith(("open", "navigate", "visit", "go to")):
            inferred.append("Open the internal dashboard")
        inferred.append(clean_goal[0].upper() + clean_goal[1:])
        if "screenshot" not in lower:
            inferred.append("Take a verification screenshot")
        return inferred

    # Fallback to generic 3-phase flow for purely abstract goals
    return [
        "Initialization Phase",
        "Execution Phase",
        "Verification Phase",
    ]


async def generate_fallback_plan(goal: str) -> PlanGraphResponse:
    """
    Constructs a deterministic, rule-based safe graph layout when the generative provider
    fails or is unconfigured. Dynamically decomposes multi-step goals into executable
    tasks so that deterministic browser automation can proceed without an external LLM key.
    """
    subtasks = decompose_goal_heuristically(goal)

    nodes: List[TaskNode] = []
    edges: List[TaskEdge] = []
    prev_node: Optional[TaskNode] = None

    for i, label in enumerate(subtasks):
        node_id = f"task_{uuid.uuid4().hex[:8]}"
        is_first = (i == 0)
        is_last = (i == len(subtasks) - 1)
        node_type = "input" if is_first else ("output" if is_last else "default")

        node = TaskNode(
            id=node_id,
            type=node_type,
            position={"x": 250.0, "y": 50.0 + i * 130.0},
            data={"label": label},
        )
        nodes.append(node)

        if prev_node:
            edge = TaskEdge(
                id=f"e_{prev_node.id}_{node.id}",
                source=prev_node.id,
                target=node.id,
            )
            edges.append(edge)
        prev_node = node

    metadata = PlanGraphMetadata(
        intent=f"Heuristic Fallback Plan for: {goal}",
        confidence=1.0,
        reasoning_summary=f"Deterministic fallback pipeline decomposed goal into {len(nodes)} actionable execution steps."
    )

    return PlanGraphResponse(
        nodes=nodes,
        edges=edges,
        metadata=metadata,
    )
