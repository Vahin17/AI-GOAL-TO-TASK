"""
dag_builder.py — ARDO DAG Graph Construction
=============================================
Converts a flat list of TaskNodes + TaskEdges (from the LLM planner) into
a validated Directed Acyclic Graph (DAG) with full ARDO metadata attached
to every node. Performs cycle detection and parallel group identification.

Design Decisions:
    - Each task is enriched with ARDO fields at graph-build time.
    - Parallel groups are identified by finding nodes with identical
      dependency sets (zero or shared deps) — these can execute concurrently.
    - Cycle detection uses iterative DFS with a visited/recursion stack.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from orchestration.safety_schemas import SafetyDecision

import uuid
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from orchestration.state_machine import TaskState

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# ARDO Task Node
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ARDOTaskNode:
    """
    A fully enriched task node within the ARDO graph.

    All ARDO-specific metadata (priority, risk, retries, etc.) lives here
    alongside the original label from the LLM plan.
    """
    task_id: str
    task_name: str
    dependency_ids: List[str] = field(default_factory=list)

    # Scheduler state
    status: TaskState = TaskState.PENDING
    priority_score: float = 0.5          # [0.0 – 1.0], computed by PriorityEngine
    risk_score: float = 0.1              # [0.0 – 1.0], higher = more dangerous
    retry_count: int = 0
    max_retries: int = 3
    retry_confidence: float = 1.0        # degrades with repeated failures

    # Execution metadata
    estimated_execution_time: float = 15.0  # seconds (estimated)
    actual_execution_time: Optional[float] = None
    execution_type: str = "sequential"       # sequential | parallel | conditional
    execution_mode: str = "safe"             # safe | aggressive | sequential | parallel
    fallback_available: bool = False

    # Runtime tracking
    parallel_group: Optional[int] = None    # int group id for concurrent nodes
    approval_needed: bool = False
    started_at: Optional[float] = None
    completed_at: Optional[float] = None

    # Logging
    execution_logs: List[str] = field(default_factory=list)

    # Starvation guard
    starvation_counter: int = 0
    queue_wait_start: Optional[float] = None

    # Phase 5: Safety metadata (from SafetyEngine)
    safety_metadata: Dict = field(default_factory=dict)

    # Phase 10 & 11: Real Browser Execution metadata
    browser_action: Optional[Any] = None
    browser_result: Optional[Any] = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "task_name": self.task_name,
            "dependency_ids": self.dependency_ids,
            "status": self.status.value,
            "priority_score": self.priority_score,
            "risk_score": self.risk_score,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "retry_confidence": self.retry_confidence,
            "estimated_execution_time": self.estimated_execution_time,
            "actual_execution_time": self.actual_execution_time,
            "execution_type": self.execution_type,
            "execution_mode": self.execution_mode,
            "fallback_available": self.fallback_available,
            "parallel_group": self.parallel_group,
            "approval_needed": self.approval_needed,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "execution_logs": self.execution_logs,
            "starvation_counter": self.starvation_counter,
            "safety_metadata": self.safety_metadata,
            "browser_action": self.browser_action.to_dict() if hasattr(self.browser_action, "to_dict") else self.browser_action,
            "browser_result": self.browser_result.to_dict() if hasattr(self.browser_result, "to_dict") else self.browser_result,
        }


# ─────────────────────────────────────────────────────────────────────────────
# ARDO Graph Container
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ARDOGraph:
    """
    Container for the complete ARDO execution graph.

    Attributes:
        nodes:           task_id → ARDOTaskNode
        adjacency:       task_id → list of downstream task_ids (successors)
        reverse_adj:     task_id → list of upstream task_ids (predecessors)
        parallel_groups: list of task_id groups that can run concurrently
        session_id:      unique session identifier
    """
    nodes: Dict[str, ARDOTaskNode] = field(default_factory=dict)
    adjacency: Dict[str, List[str]] = field(default_factory=dict)
    reverse_adj: Dict[str, List[str]] = field(default_factory=dict)
    parallel_groups: List[List[str]] = field(default_factory=list)
    session_id: str = field(default_factory=lambda: f"ardo_{uuid.uuid4().hex[:10]}")

    def get_node(self, task_id: str) -> Optional[ARDOTaskNode]:
        return self.nodes.get(task_id)

    def all_task_ids(self) -> List[str]:
        return list(self.nodes.keys())

    def successors(self, task_id: str) -> List[str]:
        return self.adjacency.get(task_id, [])

    def predecessors(self, task_id: str) -> List[str]:
        return self.reverse_adj.get(task_id, [])

    def snapshot(self) -> List[dict]:
        """Serialise all nodes for MongoDB persistence / API response."""
        return [n.to_dict() for n in self.nodes.values()]


# ─────────────────────────────────────────────────────────────────────────────
# DAG Builder
# ─────────────────────────────────────────────────────────────────────────────

class DAGBuilder:
    """
    Constructs a validated ARDOGraph from the LLM planner output.

    Steps:
        1. Create ARDOTaskNode for each node in the plan response.
        2. Wire edges → build adjacency + reverse-adjacency maps.
        3. Detect dependency_ids from inbound edges.
        4. Run cycle detection — raise ValueError on cyclic graphs.
        5. Identify parallel groups (nodes with identical predecessor sets).
        6. Mark high-risk nodes (risk_score > 0.7) as approval_needed.
    """

    @staticmethod
    def build(
        nodes_data: List[dict],
        edges_data: List[dict],
        execution_mode: str = "safe",
        risk_threshold: float = 0.7,
        safety_decisions: Optional[Dict[str, "SafetyDecision"]] = None,
    ) -> ARDOGraph:
        """
        Build and validate an ARDOGraph.

        Args:
            nodes_data:       List of dicts with 'id', 'data' (from TaskNode.dict()).
            edges_data:       List of dicts with 'source', 'target' (from TaskEdge.dict()).
            execution_mode:   Global execution mode for all nodes.
            risk_threshold:   Fallback risk_score threshold when no SafetyDecision provided.
            safety_decisions: Optional {task_id: SafetyDecision} from SafetyEngine.
                              When provided, replaces keyword-based heuristic risk scoring.
                              BLOCKED tasks (CRITICAL) start in TaskState.BLOCKED state.
                              REQUIRE_APPROVAL tasks have approval_needed=True.

        Returns:
            Validated ARDOGraph.

        Raises:
            ValueError: If the graph contains cycles.
        """
        graph = ARDOGraph()

        # ── 1. Create nodes ────────────────────────────────────────────────
        from orchestration.safety_schemas import ExecutionDecision, RiskLevel
        for raw in nodes_data:
            node_id = raw["id"]
            label = raw.get("data", {}).get("label", raw.get("task_name", raw.get("label", f"Task {node_id}")))

            # Use SafetyDecision if provided, otherwise fall back to keyword heuristic
            decision = safety_decisions.get(node_id) if safety_decisions else None

            if decision:
                # Phase 5: Hybrid Safety Engine path
                # Map risk_level to a numeric score for PriorityEngine compatibility
                risk_score_map = {
                    RiskLevel.LOW: 0.05,
                    RiskLevel.MEDIUM: 0.35,
                    RiskLevel.HIGH: 0.70,
                    RiskLevel.CRITICAL: 0.95,
                    RiskLevel.UNKNOWN: 0.50,
                }
                risk = risk_score_map.get(decision.risk_level, 0.10)
                approval_needed = decision.execution_decision == ExecutionDecision.REQUIRE_APPROVAL
                # CRITICAL tasks are BLOCKED at graph build time — they never enter the scheduler
                start_blocked = decision.execution_decision == ExecutionDecision.BLOCK
                # Safety metadata for explainability
                safety_meta = {
                    "risk_level": decision.risk_level.value,
                    "execution_decision": decision.execution_decision.value,
                    "triggered_policies": [p.value for p in decision.triggered_policy_ids],
                    "explanation": decision.explanation,
                    "semantic_confidence": decision.semantic_confidence,
                    "safety_record_id": getattr(decision, "safety_record_id", ""),
                }
            else:
                # Fallback: keyword-based heuristic (demoted, used only if safety_decisions=None)
                risk = DAGBuilder._estimate_risk(label)
                approval_needed = risk >= risk_threshold
                start_blocked = False
                safety_meta = {}

            ardo_node = ARDOTaskNode(
                task_id=node_id,
                task_name=label,
                risk_score=risk,
                execution_mode=execution_mode,
                approval_needed=approval_needed,
                fallback_available=True,
                safety_metadata=safety_meta,
            )
            if start_blocked:
                from orchestration.state_machine import TaskState as _TS
                ardo_node.status = _TS.BLOCKED
                ardo_node.execution_logs.append(
                    f"[SafetyEngine] Task BLOCKED by policy: {safety_meta.get('explanation', 'Critical risk')}"
                )
            graph.nodes[node_id] = ardo_node
            graph.adjacency[node_id] = []
            graph.reverse_adj[node_id] = []

        # ── 2. Wire edges ──────────────────────────────────────────────────
        for edge in edges_data:
            src = edge["source"]
            tgt = edge["target"]
            if src in graph.adjacency and tgt in graph.nodes:
                graph.adjacency[src].append(tgt)
                graph.reverse_adj[tgt].append(src)
                # dependency_ids on the target node
                if src not in graph.nodes[tgt].dependency_ids:
                    graph.nodes[tgt].dependency_ids.append(src)

        # ── 3. Cycle detection ─────────────────────────────────────────────
        if DAGBuilder._has_cycle(graph):
            raise ValueError(
                "ARDO DAGBuilder: Cycle detected in task graph. "
                "ARDO requires a strictly acyclic dependency structure."
            )

        # ── 4. Parallel groups ─────────────────────────────────────────────
        graph.parallel_groups = DAGBuilder._identify_parallel_groups(graph)

        # Annotate nodes with their group index
        for group_idx, group in enumerate(graph.parallel_groups):
            for task_id in group:
                if task_id in graph.nodes:
                    graph.nodes[task_id].parallel_group = group_idx
                    if len(group) > 1:
                        graph.nodes[task_id].execution_type = "parallel"

        logger.info(
            f"[DAGBuilder] Graph built: {len(graph.nodes)} nodes, "
            f"{sum(len(v) for v in graph.adjacency.values())} edges, "
            f"{len(graph.parallel_groups)} parallel groups."
        )
        return graph

    # ── Private helpers ────────────────────────────────────────────────────

    @staticmethod
    def _has_cycle(graph: ARDOGraph) -> bool:
        """Iterative DFS cycle detection on directed graph."""
        visited: Set[str] = set()
        rec_stack: Set[str] = set()

        def dfs(node_id: str) -> bool:
            visited.add(node_id)
            rec_stack.add(node_id)
            for neighbour in graph.adjacency.get(node_id, []):
                if neighbour not in visited:
                    if dfs(neighbour):
                        return True
                elif neighbour in rec_stack:
                    return True
            rec_stack.discard(node_id)
            return False

        for nid in graph.nodes:
            if nid not in visited:
                if dfs(nid):
                    return True
        return False

    @staticmethod
    def _identify_parallel_groups(graph: ARDOGraph) -> List[List[str]]:
        """
        Group nodes that share the exact same predecessor set — they can
        legally execute concurrently once all shared predecessors complete.
        Nodes with no predecessors form a single root group.
        """
        from collections import defaultdict
        bucket: Dict[frozenset, List[str]] = defaultdict(list)

        for task_id, node in graph.nodes.items():
            key = frozenset(node.dependency_ids)
            bucket[key].append(task_id)

        return list(bucket.values())

    @staticmethod
    def _estimate_risk(label: str) -> float:
        """
        DEMOTED: Weak keyword fallback used ONLY when SafetyEngine is unavailable.
        This is NOT the primary safety mechanism. It should never be called
        in normal operation when safety_decisions are provided to build().

        Keyword matching cannot determine contextual risk:
        - 'delete temporary files' != 'delete OS boot files'
        - 'clear cache' != 'clear production database'
        """
        label_lower = label.lower()
        # Minimal lexical signals — these are weak signals, not authoritative
        HIGH_RISK_KEYWORDS = [
            "delete", "remove", "drop", "destroy", "wipe",
            "deploy", "migrate", "rollback", "overwrite", "format",
            "shutdown", "terminate", "kill", "reset", "purge",
        ]
        MEDIUM_RISK_KEYWORDS = [
            "update", "modify", "patch", "configure", "install",
            "execute", "run", "write", "send", "publish",
        ]
        for kw in HIGH_RISK_KEYWORDS:
            if kw in label_lower:
                return 0.80
        for kw in MEDIUM_RISK_KEYWORDS:
            if kw in label_lower:
                return 0.40
        return 0.10
