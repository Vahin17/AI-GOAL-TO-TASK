"""
plan_hasher.py — Canonical Deterministic SHA-256 Plan Hashing (Phase 1)
=======================================================================
Computes a cryptographic, canonical SHA-256 hash of an ARDO plan DAG.

Invariants:
1. Determinism: Serializing the identical plan structure always yields the identical hash.
2. Order-independence: Jitter in node or edge list order does not alter the hash (canonical sorting).
3. Field sensitivity: Altering any execution-relevant field (label, tool, parameter, dependency)
   strictly changes the resulting hash.
4. Layout neutrality: Visual UI coordinates (x, y positions on React Flow) do NOT affect the hash.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, Union

from schemas.models import TaskNode, TaskEdge, PlanGraphResponse


def _extract_node_canonical(node: Union[TaskNode, Dict[str, Any], Any]) -> Dict[str, Any]:
    """Extracts execution-relevant fields from a node into a canonical dict."""
    if hasattr(node, "id"):
        nid = str(node.id)
        data = getattr(node, "data", {}) or {}
        label = str(data.get("label", ""))
        ntype = str(getattr(node, "type", "default"))
        tool_call = data.get("tool_call") or {}
    elif isinstance(node, dict):
        nid = str(node.get("id") or node.get("task_id", ""))
        data = node.get("data", {}) if isinstance(node.get("data"), dict) else {}
        label = str(data.get("label") or node.get("title") or node.get("task_name", ""))
        ntype = str(node.get("type", "default"))
        tool_call = data.get("tool_call") or node.get("tool_call") or {}
    else:
        # ARDOTaskNode or similar object
        nid = str(getattr(node, "task_id", getattr(node, "id", "")))
        label = str(getattr(node, "task_name", getattr(node, "title", "")))
        ntype = str(getattr(node, "raw_type", None) or getattr(node, "type", None) or getattr(node, "execution_type", "default"))
        tool_call = getattr(node, "tool_call", None) or (getattr(node, "metadata", {}) or {}).get("tool_call") or {}

    # Normalize tool call
    canon_tool: Optional[Dict[str, Any]] = None
    if tool_call:
        if hasattr(tool_call, "model_dump"):
            tool_dict = tool_call.model_dump(mode="json")
        elif hasattr(tool_call, "to_dict"):
            tool_dict = tool_call.to_dict()
        elif isinstance(tool_call, dict):
            tool_dict = tool_call
        else:
            tool_dict = {"repr": str(tool_call)}

        canon_tool = {str(k): str(v) for k, v in sorted(tool_dict.items()) if v is not None}

    return {
        "id": nid,
        "label": label.strip(),
        "type": ntype.strip(),
        "tool_call": canon_tool,
    }


def _extract_edge_canonical(edge: Union[TaskEdge, Dict[str, Any], Any]) -> Dict[str, str]:
    """Extracts source and target from an edge."""
    if hasattr(edge, "source") and hasattr(edge, "target"):
        return {"source": str(edge.source).strip(), "target": str(edge.target).strip()}
    elif isinstance(edge, dict):
        return {
            "source": str(edge.get("source", "")).strip(),
            "target": str(edge.get("target", "")).strip(),
        }
    return {"source": "", "target": ""}


def compute_plan_hash(
    nodes: Union[List[TaskNode], List[Dict[str, Any]], List[Any]],
    edges: Union[List[TaskEdge], List[Dict[str, Any]], List[Any]],
    intent: Optional[str] = None,
) -> str:
    """
    Computes a deterministic canonical SHA-256 hash for a given plan DAG.
    """
    # 1. Canonicalize and sort nodes by ID
    canon_nodes = [_extract_node_canonical(n) for n in nodes]
    canon_nodes.sort(key=lambda n: n["id"])

    # 2. Canonicalize and sort edges by (source, target)
    canon_edges = [_extract_edge_canonical(e) for e in edges]
    canon_edges.sort(key=lambda e: (e["source"], e["target"]))

    # 3. Canonical representation dictionary
    canonical_repr = {
        "intent": (intent or "").strip(),
        "nodes": canon_nodes,
        "edges": canon_edges,
    }

    # 4. Serialize with strict key sorting, no whitespace padding, utf-8
    canonical_json = json.dumps(
        canonical_repr,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )

    # 5. Compute SHA-256 hex digest
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

