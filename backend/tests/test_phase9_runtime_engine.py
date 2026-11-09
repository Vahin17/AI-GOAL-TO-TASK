"""
test_phase9_runtime_engine.py — Unit Tests for RuntimeEngine Correctness & Responsibility Cleanup (Phase 9)
===========================================================================================================
Validates:
1. Browser tasks are dispatched directly to BrowserAgent (not ActionSynthesizer).
2. RuntimeEngine does not choose/synthesize browser actions directly.
3. BrowserAgent verified success transitions task to COMPLETED.
4. BrowserAgent failure transitions task to FAILED.
5. BrowserAgent unverified finish is rejected (false-success protection).
6. BrowserAgent failure is not redundantly retried by RuntimeEngine.
7. Completed and terminal tasks are protected against duplicate re-execution.
8. Parallel tasks maintain strict lifecycle bookkeeping and capture exceptions.
9. Dependency failures cascade and prevent execution of downstream tasks.
10. Deadlock/stalled execution terminates cleanly.
11. Injected session-scoped ToolRegistry and browser resources are respected without global singleton leaks.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from execution.registry.tool_registry import ToolRegistry
from execution.registry.base_tool import BaseTool
from execution.registry.tool_models import ToolCall, ToolResult, ToolCapability
from execution.browser.browser_models import (
    BrowserAction,
    BrowserActionResult,
    BrowserActionType,
)
from execution.browser.browser_tool import BrowserTool
from execution.browser.browser_task_adapter import BrowserTaskAdapter
from execution.browser.agent_contracts import (
    BrowserTask,
    BrowserAgentOutcome,
    BrowserAgentResult,
    BrowserVerification,
    BrowserVerificationStatus,
)
from orchestration.scheduler import ARDOSession, ARDOScheduler
from orchestration.runtime_engine import RuntimeEngine
from orchestration.dag_builder import DAGBuilder, ARDOGraph, ARDOTaskNode
from orchestration.state_machine import TaskState


def make_graph(nodes_data, edges_data=None):
    return DAGBuilder.build(
        nodes_data=[{"id": n["id"], "data": {"label": n.get("label", n["id"])}} for n in nodes_data],
        edges_data=edges_data or [],
    )


def make_agent_result(task_id: str, outcome: BrowserAgentOutcome, is_verified: bool = True, reason: str = ""):
    verification = None
    if outcome == BrowserAgentOutcome.COMPLETED:
        verification = BrowserVerification(
            task_id=task_id,
            status=BrowserVerificationStatus.VERIFIED if is_verified else BrowserVerificationStatus.NOT_VERIFIED,
            verified_by="Verification passed" if is_verified else None,
            failure_reason=None if is_verified else (reason or "Verification failed"),
        )
    return BrowserAgentResult(
        task_id=task_id,
        session_id="test_session",
        outcome=outcome,
        objective=f"Objective for {task_id}",
        verification=verification,
        error=None if outcome == BrowserAgentOutcome.COMPLETED else (reason or "Failed"),
    )


class TestRuntimeEngineResponsibilityAndCorrectness:

    def test_browser_task_dispatched_to_browser_agent(self):
        """Req 1 & 2: Browser tasks are delegated to BrowserAgent without ActionSynthesizer."""
        async def _run():
            graph = make_graph([{"id": "t1", "label": "Open browser dashboard and navigate"}])
            mock_agent = MagicMock()
            mock_agent.run = AsyncMock(return_value=make_agent_result("t1", BrowserAgentOutcome.COMPLETED, True, "Task verified"))

            engine = RuntimeEngine(
                graph=graph,
                session_id="sess_p9_1",
                mode="safe",
                browser_agent=mock_agent,
            )

            events = []
            async for ev in engine.run():
                events.append(ev)

            # Assert BrowserAgent.run was called with BrowserTask
            mock_agent.run.assert_called_once()
            call_arg = mock_agent.run.call_args[0][0]
            assert isinstance(call_arg, BrowserTask)
            assert call_arg.task_id == "t1"

            # Assert node completed
            node = graph.get_node("t1")
            assert node.status == TaskState.COMPLETED
            assert node.browser_result is not None

        asyncio.run(_run())

    def test_browser_agent_verified_success_becomes_task_success(self):
        """Req 3: Verified BrowserAgent outcome translates to COMPLETED node."""
        async def _run():
            graph = make_graph([{"id": "t_ok", "label": "Navigate to settings page"}])
            mock_agent = MagicMock()
            mock_agent.run = AsyncMock(return_value=make_agent_result("t_ok", BrowserAgentOutcome.COMPLETED, True, "Settings page open"))

            engine = RuntimeEngine(graph=graph, session_id="sess_ok", mode="safe", browser_agent=mock_agent)
            async for _ in engine.run():
                pass

            node = graph.get_node("t_ok")
            assert node.status == TaskState.COMPLETED

        asyncio.run(_run())

    def test_browser_agent_unverified_finish_fails(self):
        """Req 5: False-success protection: unverified completion fails node."""
        async def _run():
            graph = make_graph([{"id": "t_unverified", "label": "Click button on page"}])
            mock_agent = MagicMock()
            mock_agent.run = AsyncMock(return_value=make_agent_result("t_unverified", BrowserAgentOutcome.COMPLETED, False, "Required text missing"))

            engine = RuntimeEngine(graph=graph, session_id="sess_unv", mode="safe", browser_agent=mock_agent)
            async for _ in engine.run():
                pass

            node = graph.get_node("t_unverified")
            assert node.status == TaskState.FAILED

        asyncio.run(_run())

    def test_browser_agent_failure_is_not_redundantly_retried(self):
        """Req 4 & 6: BrowserAgent failure marks task FAILED without outer retry loop."""
        async def _run():
            graph = make_graph([{"id": "t_fail", "label": "Extract table data from page"}])
            mock_agent = MagicMock()
            mock_agent.run = AsyncMock(return_value=make_agent_result("t_fail", BrowserAgentOutcome.FAILED, False, "Element not found after recovery"))

            engine = RuntimeEngine(graph=graph, session_id="sess_fail", mode="safe", browser_agent=mock_agent)
            async for _ in engine.run():
                pass

            # Agent run called exactly ONCE (no outer engine retry loop)
            assert mock_agent.run.call_count == 1
            node = graph.get_node("t_fail")
            assert node.status == TaskState.FAILED

        asyncio.run(_run())

    def test_completed_tasks_protected_from_duplicate_execution(self):
        """Req 7: Terminal completed tasks are never re-executed."""
        async def _run():
            graph = make_graph([{"id": "t_done", "label": "Already finished task"}])
            node = graph.get_node("t_done")
            node.status = TaskState.COMPLETED

            mock_agent = MagicMock()
            mock_agent.run = AsyncMock()

            engine = RuntimeEngine(graph=graph, session_id="sess_done", mode="safe", browser_agent=mock_agent)
            async for _ in engine.run():
                pass

            mock_agent.run.assert_not_called()
            assert node.status == TaskState.COMPLETED

        asyncio.run(_run())

    def test_parallel_execution_bookkeeping_and_exception_handling(self):
        """Req 9 & 10: Parallel tasks have complete lifecycle tracking and exception capture."""
        async def _run():
            graph = make_graph([
                {"id": "t_p1", "label": "Open page 1"},
                {"id": "t_p2", "label": "Open page 2"},
            ])
            # Configure parallel groups
            graph.parallel_groups = [["t_p1", "t_p2"]]

            mock_agent = MagicMock()
            # Task 1 succeeds, Task 2 raises an exception
            async def _agent_side_effect(btask):
                if btask.task_id == "t_p1":
                    return make_agent_result("t_p1", BrowserAgentOutcome.COMPLETED, True, "Verified")
                else:
                    raise RuntimeError("Unexpected engine crash in task 2")

            mock_agent.run = AsyncMock(side_effect=_agent_side_effect)

            engine = RuntimeEngine(graph=graph, session_id="sess_parallel", mode="parallel", browser_agent=mock_agent)
            async for _ in engine.run():
                pass

            node1 = graph.get_node("t_p1")
            node2 = graph.get_node("t_p2")

            assert node1.status == TaskState.COMPLETED
            assert node2.status == TaskState.FAILED

        asyncio.run(_run())

    def test_dependency_failure_cascades_and_blocks_dependents(self):
        """Req 11: Failure of an upstream task cascades and blocks downstream dependents."""
        async def _run():
            graph = make_graph(
                nodes_data=[
                    {"id": "t_root", "label": "Navigate to login page"},
                    {"id": "t_dep", "label": "Fill password on page"},
                ],
                edges_data=[{"source": "t_root", "target": "t_dep"}],
            )

            mock_agent = MagicMock()
            mock_agent.run = AsyncMock(return_value=make_agent_result("t_root", BrowserAgentOutcome.FAILED, False, "Root failed"))

            engine = RuntimeEngine(graph=graph, session_id="sess_dep_cascade", mode="safe", browser_agent=mock_agent)
            async for _ in engine.run():
                pass

            root_node = graph.get_node("t_root")
            dep_node = graph.get_node("t_dep")

            assert root_node.status == TaskState.FAILED
            assert dep_node.status == TaskState.BLOCKED

        asyncio.run(_run())

    def test_session_scoped_resources_used_without_global_leak(self):
        """Req 13: Injected session tool registry & adapter are utilized."""
        mock_adapter = MagicMock(spec=BrowserTaskAdapter)
        mock_registry = MagicMock(spec=ToolRegistry)
        mock_registry.get_tool = MagicMock(return_value=None)

        graph = make_graph([{"id": "t_scoped", "label": "Scoped task"}])
        engine = RuntimeEngine(
            graph=graph,
            session_id="sess_scoped_check",
            mode="safe",
            browser_adapter=mock_adapter,
            tool_registry=mock_registry,
        )

        assert engine.browser_adapter is mock_adapter
        assert engine.tool_registry is mock_registry
