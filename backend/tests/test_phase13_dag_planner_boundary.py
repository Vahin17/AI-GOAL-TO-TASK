"""
test_phase13_dag_planner_boundary.py — Phase 13: DAGBuilder / Planner Boundary Tests
===================================================================================
Validates the clean boundary between Planning (WHAT needs to be done) and
Dynamic Execution (HOW browser actions are determined from live observations).

Requirements:
1. DAGBuilder creates high-level executable tasks rather than low-level browser actions.
2. DAGBuilder does not call ActionSynthesizer for browser tasks.
3. DAGBuilder does not call NavigationResolver during graph construction.
4. Planned browser tasks retain enough metadata / intent for RuntimeEngine to recognize them.
5. Browser-specific runtime details (selectors, coordinates, element IDs) are not required in the DAG.
6. RuntimeEngine passes high-level browser tasks to BrowserAgent rather than synthesizing browser actions.
7. Multi-step DAG dependencies and parallel groups function correctly without browser action pre-commit.
8. Non-browser tasks continue to function normally.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from orchestration.dag_builder import DAGBuilder, ARDOGraph, ARDOTaskNode
from orchestration.runtime_engine import RuntimeEngine
from orchestration.scheduler import ARDOSession
from orchestration.state_machine import TaskState
from execution.browser.agent_contracts import (
    BrowserTask,
    BrowserAgentOutcome,
    BrowserAgentResult,
    BrowserVerification,
    BrowserVerificationStatus,
)
from execution.synthesis.action_synthesizer import ActionSynthesizer
from execution.synthesis.navigation_resolver import NavigationResolver


class TestPhase13DAGPlannerBoundary:
    """Test suite for Phase 13 DAGBuilder and Planner boundary guarantees."""

    def test_1_dagbuilder_creates_high_level_tasks_without_browser_actions(self):
        """
        Req 1 & 5: DAGBuilder builds high-level ARDOTaskNodes without pre-synthesizing
        concrete low-level browser actions, selectors, or coordinates.
        """
        nodes_data = [
            {"id": "task_1", "data": {"label": "Open Wikipedia in a new tab"}},
            {"id": "task_2", "data": {"label": "Search for Quantum Computing"}},
            {"id": "task_3", "data": {"label": "Take verification screenshot"}},
        ]
        edges_data = [
            {"source": "task_1", "target": "task_2"},
            {"source": "task_2", "target": "task_3"},
        ]

        graph = DAGBuilder.build(
            nodes_data=nodes_data,
            edges_data=edges_data,
            goal_text="Search Quantum Computing on Wikipedia",
        )

        assert len(graph.nodes) == 3
        # Invariant: Nodes should NOT have pre-synthesized browser actions attached by DAGBuilder
        for task_id in ["task_1", "task_2", "task_3"]:
            node = graph.get_node(task_id)
            assert node is not None
            assert node.task_name in [
                "Open Wikipedia in a new tab",
                "Search for Quantum Computing",
                "Take verification screenshot",
            ]
            assert node.browser_action is None
            assert node.tool_call is None

    def test_2_dagbuilder_does_not_call_actionsynthesizer(self):
        """
        Req 2: DAGBuilder must NOT invoke ActionSynthesizer during active graph construction.
        """
        nodes_data = [
            {"id": "t1", "data": {"label": "Navigate to https://example.com"}},
            {"id": "t2", "data": {"label": "Click on the Login button"}},
        ]
        edges_data = [{"source": "t1", "target": "t2"}]

        with patch.object(ActionSynthesizer, "synthesize_for_task") as mock_synth:
            graph = DAGBuilder.build(nodes_data=nodes_data, edges_data=edges_data)
            assert len(graph.nodes) == 2
            mock_synth.assert_not_called()

    def test_3_dagbuilder_does_not_call_navigation_resolver(self):
        """
        Req 3: DAGBuilder must NOT invoke NavigationResolver during active graph construction.
        """
        nodes_data = [
            {"id": "t1", "data": {"label": "Open Wikipedia"}},
            {"id": "t2", "data": {"label": "Go to Gmail"}},
        ]

        with patch.object(NavigationResolver, "detect_navigation_intent") as mock_detect, \
             patch.object(NavigationResolver, "resolve_destination") as mock_resolve:
            graph = DAGBuilder.build(nodes_data=nodes_data, edges_data=[])
            assert len(graph.nodes) == 2
            mock_detect.assert_not_called()
            mock_resolve.assert_not_called()

    def test_4_planned_browser_tasks_recognized_by_runtime_engine(self):
        """
        Req 4: Planned browser tasks retain enough intent for RuntimeEngine to classify
        them as browser tasks without needing low-level selectors.
        """
        nodes_data = [
            {"id": "b1", "data": {"label": "Open Wikipedia in a new tab"}},
            {"id": "b2", "data": {"label": "Search for artificial intelligence"}},
            {"id": "b3", "data": {"label": "Take screenshot of results"}},
            {"id": "g1", "data": {"label": "Calculate Fibonacci sequence"}},
        ]
        graph = DAGBuilder.build(nodes_data=nodes_data, edges_data=[])
        engine = RuntimeEngine(graph=graph, session_id="test_boundary_session")

        # Browser tasks recognized via intent
        assert engine._is_browser_task(graph.get_node("b1")) is True
        assert engine._is_browser_task(graph.get_node("b2")) is True
        assert engine._is_browser_task(graph.get_node("b3")) is True

        # Non-browser generic task is NOT a browser task
        assert engine._is_browser_task(graph.get_node("g1")) is False

    def test_5_runtime_engine_dispatches_to_browser_agent_without_action_synthesis(self):
        """
        Req 6: When executing a planned browser task, RuntimeEngine passes the high-level
        task to BrowserAgent directly, rather than generating browser actions itself.
        """
        async def _run():
            nodes_data = [
                {"id": "b1", "data": {"label": "Open Wikipedia in a new tab"}},
            ]
            graph = DAGBuilder.build(nodes_data=nodes_data, edges_data=[])
            session = ARDOSession(session_id="session_p13_dispatch", graph=graph, mode="sequential")

            mock_agent = MagicMock()
            mock_res = BrowserAgentResult(
                task_id="b1",
                session_id="session_p13_dispatch",
                objective="Open Wikipedia in a new tab",
                outcome=BrowserAgentOutcome.COMPLETED,
                verification=BrowserVerification(
                    task_id="b1",
                    status=BrowserVerificationStatus.VERIFIED,
                    verified_by="Test Verifier",
                ),
            )
            mock_agent.run = AsyncMock(return_value=mock_res)
            session.engine._browser_agent = mock_agent

            events = []
            with patch.object(ActionSynthesizer, "synthesize_for_task") as mock_synth:
                async for ev in session.engine.run():
                    events.append(ev)

                mock_synth.assert_not_called()
                mock_agent.run.assert_called_once()
                called_btask: BrowserTask = mock_agent.run.call_args[0][0]
                assert called_btask.task_id == "b1"
                assert called_btask.objective == "Open Wikipedia in a new tab"
                assert graph.get_node("b1").status == TaskState.COMPLETED

        asyncio.run(_run())

    def test_6_multistep_dag_dependencies_and_parallel_groups(self):
        """
        Req 7: DAGBuilder preserves multi-step DAG structure, dependency ordering,
        and parallel group identification without browser action generation.
        """
        nodes_data = [
            {"id": "root", "data": {"label": "Open browser"}},
            {"id": "branch_a", "data": {"label": "Extract header text"}},
            {"id": "branch_b", "data": {"label": "Extract footer links"}},
            {"id": "merge", "data": {"label": "Summarize extracted content"}},
        ]
        edges_data = [
            {"source": "root", "target": "branch_a"},
            {"source": "root", "target": "branch_b"},
            {"source": "branch_a", "target": "merge"},
            {"source": "branch_b", "target": "merge"},
        ]

        graph = DAGBuilder.build(nodes_data=nodes_data, edges_data=edges_data)

        assert graph.get_node("root").dependency_ids == []
        assert graph.get_node("branch_a").dependency_ids == ["root"]
        assert graph.get_node("branch_b").dependency_ids == ["root"]
        assert set(graph.get_node("merge").dependency_ids) == {"branch_a", "branch_b"}

        assert graph.successors("root") == ["branch_a", "branch_b"]
        assert graph.predecessors("merge") == ["branch_a", "branch_b"]

        # branch_a and branch_b share exact same dependencies (['root']), so they form a parallel group
        p_groups = graph.parallel_groups
        assert any(set(g) == {"branch_a", "branch_b"} for g in p_groups)

    def test_7_cycle_detection_in_dagbuilder(self):
        """
        Req 7: Cycle detection properly rejects cyclic graphs during build.
        """
        nodes_data = [
            {"id": "t1", "data": {"label": "Step 1"}},
            {"id": "t2", "data": {"label": "Step 2"}},
        ]
        edges_data = [
            {"source": "t1", "target": "t2"},
            {"source": "t2", "target": "t1"},
        ]

        with pytest.raises(ValueError, match="Cycle detected"):
            DAGBuilder.build(nodes_data=nodes_data, edges_data=edges_data)

    def test_8_non_browser_tasks_continue_to_work_normally(self):
        """
        Req 8: Non-browser generic tasks continue executing properly in RuntimeEngine.
        """
        async def _run():
            nodes_data = [
                {"id": "calc_1", "data": {"label": "Compute statistical mean"}},
            ]
            graph = DAGBuilder.build(nodes_data=nodes_data, edges_data=[])
            session = ARDOSession(session_id="session_p13_non_browser", graph=graph, mode="sequential")

            with patch("random.random", return_value=0.99):  # Guarantee success in simulator
                events = []
                async for ev in session.engine.run():
                    events.append(ev)

                assert graph.get_node("calc_1").status == TaskState.COMPLETED

        asyncio.run(_run())
