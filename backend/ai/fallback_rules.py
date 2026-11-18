import re
import uuid
from typing import Any, Dict, List, Optional, Tuple
from schemas.models import TaskNode, TaskEdge, PlanGraphMetadata, PlanGraphResponse


def _deduplicate_steps(steps: List[str]) -> List[str]:
    """Deduplicates task step labels case-insensitively while preserving original order."""
    seen = set()
    unique: List[str] = []
    for s in steps:
        cleaned = re.sub(r"\s+", " ", s.strip())
        if not cleaned:
            continue
        norm = cleaned.lower().rstrip(".")
        if norm not in seen:
            seen.add(norm)
            unique.append(cleaned)
    return unique


def _extract_math_expression(goal: str) -> Optional[str]:
    """Extracts a safe arithmetic expression if the goal is a calculation request."""
    m = re.search(
        r"\b(?:calculate|compute|evaluate|solve)\s+([0-9\.\s\+\-\*\/\(\)\%\^]+)",
        goal,
        re.IGNORECASE,
    )
    if m:
        expr = m.group(1).strip().replace("^", "**")
        if re.search(r"\d", expr) and re.search(r"[\+\-\*\/\%]", expr):
            return expr
    # Bare arithmetic expression with command prefix or standalone math
    m_bare = re.match(r"^\s*([0-9\(\)\s\.]+\s*[\+\-\*\/\%]\s*[0-9\(\)\s\.\+\-\*\/\%]+)\s*$", goal)
    if m_bare:
        return m_bare.group(1).strip()
    return None


def decompose_goal_heuristically(goal: str) -> List[str]:
    """
    Decomposes a compound goal into ordered actionable tasks.
    Splits on commas, semicolons, 'and', 'then', and sequential transition phrases.
    Recognizes media/browser intents (e.g. YouTube playback) and calculation intents,
    synthesizing discrete, deduplicated executable steps.
    """
    if not goal or not goal.strip():
        raise ValueError("Goal cannot be empty or whitespace for fallback decomposition.")

    clean_goal = goal.strip().rstrip(".")
    lower = clean_goal.lower()

    # 0. Calculation / Mathematical Expression Intent
    math_expr = _extract_math_expression(clean_goal)
    if math_expr:
        return [
            f"Evaluate mathematical expression: {math_expr}",
        ]

    # 1. Media Playback Intent Detection (YouTube / Audio / Video / Song)
    if re.search(r'\b(?:play|song|youtube|listen|watch|video|audio|music|stream)\b', lower):
        # Extract song / media target
        match = re.search(
            r'\b(?:play|listen to|watch|stream)\s+(.+?)(?:\s+on\s+([a-zA-Z0-9_-]+))?$',
            clean_goal,
            re.IGNORECASE,
        )
        platform = "YouTube"
        song_target = ""
        if match:
            song_target = match.group(1).strip()
            if match.group(2):
                platform = match.group(2).strip()
            # Clean trailing noise words like "song", "video", "music", "on youtube"
            song_target = re.sub(r'\b(?:song|video|music)\b', '', song_target, flags=re.IGNORECASE).strip()
            song_target = re.sub(r'\bon\s+youtube\b', '', song_target, flags=re.IGNORECASE).strip()

        if not song_target:
            song_target = re.sub(r'^(?:play|listen to|watch|stream)\s*', '', clean_goal, flags=re.IGNORECASE).strip()
            song_target = re.sub(r'\s+on\s+youtube.*$', '', song_target, flags=re.IGNORECASE).strip()

        search_query = song_target if song_target else clean_goal
        return _deduplicate_steps([
            f"Open {platform}",
            f"Search for '{search_query}'",
            "Identify the appropriate result",
            "Play the selected result",
            "Verify the observable playback outcome",
        ])

    # 2. General compound splitting by conjunctions
    pattern = r'[,;]|\b(?:then|and\s+then)\b|\band\s+(?=(?:open|search|click|find|update|edit|take|capture|extract|verify|fill|navigate|visit|press|submit|select|give|get|show|read|close|play|calculate|compute|evaluate)\b)'
    raw_steps = re.split(pattern, clean_goal, flags=re.IGNORECASE)

    subtasks = []
    for step in raw_steps:
        cleaned = re.sub(r"^\s*(?:in\s+parallel\s*:?|simultaneously\s*:?|concurrently\s*:?)\s*", "", step.strip(), flags=re.IGNORECASE).strip()
        if cleaned:
            cleaned = cleaned[0].upper() + cleaned[1:]
            subtasks.append(cleaned)

    deduped = _deduplicate_steps(subtasks)
    if len(deduped) >= 2:
        return deduped

    # 3. Single-step browser or web operation
    if (
        re.search(
            r'\b(?:dashboard|navigate|open|visit|go\s+to|browse|search|update|screenshot|click|fill|type|tab|web|page|site|url|extract|wikipedia|google)\b',
            lower,
        )
        or "http://" in lower
        or "https://" in lower
        or "file://" in lower
    ):
        inferred = []
        if "dashboard" in lower and not lower.startswith(("open", "navigate", "visit", "go to")):
            inferred.append("Open the internal dashboard")
        inferred.append(clean_goal[0].upper() + clean_goal[1:])
        if "screenshot" not in lower:
            inferred.append("Take a verification screenshot")
        return _deduplicate_steps(inferred)

    # 4. Purely abstract / conceptual goals
    return _deduplicate_steps([
        f"Analyze directive: {clean_goal}",
        "Execute core operations",
        "Verify conclusive outcome",
    ])


async def generate_fallback_plan(
    goal: str,
    is_fallback: bool = True,
    fallback_reason: Optional[str] = None,
    primary_failure_code: Optional[str] = None,
) -> PlanGraphResponse:
    """
    Constructs a deterministic, rule-based safe graph layout when the generative provider
    fails, is unconfigured, or produces a rejected plan.
    Validates DAG structure and tool capabilities before returning.
    """
    if not goal or not goal.strip():
        raise ValueError("Goal cannot be empty or whitespace when generating a fallback plan.")

    clean_goal = goal.strip()
    subtasks = decompose_goal_heuristically(clean_goal)
    if not subtasks:
        raise ValueError("Fallback planner could not produce any actionable steps for goal.")

    nodes: List[TaskNode] = []
    edges: List[TaskEdge] = []

    lower_goal = clean_goal.lower()
    math_expr = _extract_math_expression(clean_goal)
    is_parallel_intent = bool(re.search(r"\b(?:in\s+parallel|simultaneously|concurrently)\b", lower_goal))

    media_or_browser = bool(
        re.search(
            r'\b(?:play|youtube|song|listen|watch|video|audio|music|stream|dashboard|navigate|open|visit|go\s+to|browse|search|screenshot|click|fill|tab|web|page|site|url|wikipedia|google)\b',
            lower_goal,
        )
        or "http://" in lower_goal
        or "https://" in lower_goal
        or "file://" in lower_goal
    )

    for i, label in enumerate(subtasks):
        node_id = f"task_{uuid.uuid4().hex[:8]}"
        is_first = (i == 0)
        is_last = (i == len(subtasks) - 1)
        node_type = "input" if is_first else ("output" if is_last else "default")

        label_lower = label.lower()
        step_math_expr = _extract_math_expression(label) or (math_expr if len(subtasks) == 1 else None)
        is_calc = step_math_expr is not None

        is_browser = (not is_calc) and (
            media_or_browser
            or bool(
                re.search(
                    r'\b(?:open|search|play|navigate|click|youtube|screenshot|visit|browse|wikipedia|google|extract)\b',
                    label_lower,
                )
                or "http://" in label_lower
                or "https://" in label_lower
                or "file://" in label_lower
            )
        )

        resolved_tool = "calculator" if is_calc else ("browser" if is_browser else None)
        tool_call_dict: Optional[Dict[str, Any]] = None
        if is_calc and step_math_expr:
            tool_call_dict = {
                "call_id": f"call_{node_id}",
                "tool_name": "calculator",
                "action_name": "evaluate",
                "arguments": {"expression": step_math_expr},
            }

        node_data: Dict[str, Any] = {
            "label": label,
            "tool": resolved_tool,
            "is_browser_task": is_browser,
            "metadata": {
                "tool": resolved_tool,
                "is_browser_task": is_browser,
                "is_fallback": is_fallback,
                "planning_mode": "deterministic_fallback" if is_fallback else "primary_ai",
            },
        }

        # Enrich browser tasks with deterministic contract parameters
        if is_browser:
            platform_urls = {
                "youtube": "https://www.youtube.com",
                "google": "https://www.google.com",
                "wikipedia": "https://www.wikipedia.org",
                "github": "https://www.github.com",
            }
            dest_url = None
            for p_name, p_url in platform_urls.items():
                if re.search(rf'\b(?:open|navigate\s+to|visit|go\s+to|load)\s+{p_name}\b', label_lower):
                    dest_url = p_url
                    break
            if not dest_url:
                raw_url_m = re.search(r'(https?://[^\s\'"<>]+|file://[^\s\'"<>]+)', label)
                if raw_url_m:
                    dest_url = raw_url_m.group(0).rstrip(".,;)")

            if dest_url:
                node_data["expected_url"] = dest_url
                node_data["destination_url"] = dest_url
                node_data["metadata"]["expected_url"] = dest_url
                node_data["metadata"]["destination_url"] = dest_url
                node_data["metadata"]["url"] = dest_url
                node_data["metadata"]["action_type"] = "navigate"
                node_data["metadata"]["postcondition"] = {"url": dest_url}

            # Search query extraction
            search_m = re.search(r'(?:search\s+for|search)\s+["\']([^"\']+)["\']', label, re.IGNORECASE)
            if not search_m:
                search_m = re.search(r'(?:search\s+for|search)\s+(.+)$', label, re.IGNORECASE)
            if search_m:
                s_query = search_m.group(1).strip().strip("'\"")
                node_data["metadata"]["action_type"] = "search"
                node_data["metadata"]["search_query"] = s_query

            if re.search(r'\b(?:identify|locate|select)\s+(?:the\s+)?(?:appropriate\s+)?result\b', label_lower):
                node_data["metadata"]["action_type"] = "identify_result"
            elif re.search(r'\b(?:play\s+the\s+selected\s+result|play\s+result|play\s+video|play\s+song)\b', label_lower):
                node_data["metadata"]["action_type"] = "play"
            elif re.search(r'\b(?:verify\s+(?:the\s+)?(?:observable\s+)?playback)\b', label_lower):
                node_data["metadata"]["action_type"] = "verify_playback"
                node_data["metadata"]["verification_type"] = "media_playback"
                node_data["metadata"]["success_criteria"] = "Observable media playback outcome active"
        if tool_call_dict:
            node_data["tool_call"] = tool_call_dict
            node_data["metadata"]["tool_call"] = tool_call_dict

        node = TaskNode(
            id=node_id,
            type=node_type,
            position={"x": 250.0, "y": 50.0 + i * 130.0},
            data=node_data,
        )
        nodes.append(node)

    # Wire edges: support parallel fan-out/fan-in when parallel intent is specified, otherwise sequential chain
    if is_parallel_intent and len(nodes) >= 2:
        if len(nodes) == 2:
            # Two independent parallel tasks: no serializing edge between them
            nodes[0].position = {"x": 120.0, "y": 50.0}
            nodes[1].position = {"x": 380.0, "y": 50.0}
        else:
            # Parallel branches followed by a final consolidation/verification step
            final_node = nodes[-1]
            for idx, branch_node in enumerate(nodes[:-1]):
                branch_node.position = {"x": 120.0 + idx * 220.0, "y": 50.0}
                edges.append(
                    TaskEdge(
                        id=f"e_{branch_node.id}_{final_node.id}",
                        source=branch_node.id,
                        target=final_node.id,
                    )
                )
            final_node.position = {"x": 250.0, "y": 220.0}
    else:
        prev_node: Optional[TaskNode] = None
        for node in nodes:
            if prev_node:
                edges.append(
                    TaskEdge(
                        id=f"e_{prev_node.id}_{node.id}",
                        source=prev_node.id,
                        target=node.id,
                    )
                )
            prev_node = node

    # Validate structural and tool capability integrity of the fallback plan
    from ai.plan_critic import PlanCritic
    structural_issues = PlanCritic.validate_dag_structure(nodes, edges)
    capability_issues = PlanCritic.validate_tool_capabilities(nodes)
    critical_issues = [
        iss for iss in (structural_issues + capability_issues)
        if iss.severity.value == "critical"
    ]
    if critical_issues:
        msgs = "; ".join(iss.message for iss in critical_issues)
        raise ValueError(f"Fallback plan rejected due to structural or capability validation failure: {msgs}")

    has_bound_tool = any(n.data.get("tool") is not None for n in nodes)
    limitations: List[str] = [
        "Fallback planning was used because the primary AI planner was unavailable or rejected; this plan passed structural validation but may have reduced contextual detail."
    ]
    if not has_bound_tool:
        limitations.append(
            "Goal does not map to a registered automated executor (browser or calculator); unbound tasks will fail closed at execution unless grounded."
        )

    resolved_reason = fallback_reason or "Generative provider unavailable; rule-based fallback engaged."
    metadata = PlanGraphMetadata(
        intent=f"Heuristic Fallback Plan for: {clean_goal}",
        confidence=0.60,
        reasoning_summary=(
            f"Deterministic fallback pipeline decomposed goal into {len(nodes)} actionable execution steps."
            if has_bound_tool
            else f"Deterministic fallback generated {len(nodes)} conceptual steps (no bound automated tool identified)."
        ),
        is_fallback=is_fallback,
        fallback_reason=resolved_reason,
        planning_mode="deterministic_fallback" if is_fallback else "primary_ai",
        provider_used="rule_based_fallback" if is_fallback else "primary_ai",
        primary_failure_code=primary_failure_code or ("provider_unavailable" if is_fallback else None),
        validation_passed=True,
        limitations=limitations if is_fallback else [],
        requires_clarification=not has_bound_tool,
        clarification_prompt=(
            "Specify a target website/URL, search query, or mathematical expression to enable automated tool execution."
            if not has_bound_tool
            else None
        ),
    )

    return PlanGraphResponse(
        nodes=nodes,
        edges=edges,
        metadata=metadata,
    )
