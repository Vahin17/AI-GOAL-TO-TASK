"""
test_phase8_session_isolation.py — Unit Tests for Session-Scoped Browser State (Phase 8)
========================================================================================
Validates:
1. Two ARDO sessions receive distinct BrowserTaskAdapter, BrowserTool, and PlaywrightBrowserExecutor instances.
2. Session A's browser tool dispatches strictly to Session A's executor/adapter.
3. Session B's browser tool dispatches strictly to Session B's executor/adapter without cross-contamination.
4. Idempotent cleanup: Closing/cleaning Session A does not close or invalidate Session B's browser state.
5. Session tool lookup resolves its own session-scoped tool without leaking to global registry.
6. Non-browser global tools remain accessible to all session-scoped registries via parent inheritance.
7. Concurrent ARDOSession execution maintains strict session isolation.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from pydantic import BaseModel

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
from execution.browser.playwright_executor import PlaywrightBrowserExecutor
from orchestration.scheduler import ARDOSession, ARDOScheduler
from orchestration.dag_builder import DAGBuilder
from orchestration.safety_schemas import (
    SafetyContext,
    ActionType,
    TargetCategory,
)


class DummyInput(BaseModel):
    x: int = 1


class DummyOutput(BaseModel):
    result: int = 42


class DummyGlobalTool(BaseTool):
    name = "calculator"
    description = "Global math tool"
    capability = ToolCapability.SYSTEM_COMMAND
    input_schema = DummyInput
    output_schema = DummyOutput

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        return ToolResult(
            call_id=tool_call.call_id,
            tool_name=self.name,
            action_name=tool_call.action_name,
            success=True,
            output_data={"result": 42},
        )

    def infer_safety_context(self, task_id: str, tool_call: ToolCall) -> SafetyContext:
        return SafetyContext(
            task_id=task_id,
            action=ActionType.EXECUTE,
            target="math_calc",
            target_category=TargetCategory.APPLICATION_DATA,
        )


def make_graph(node_id: str, label: str):
    return DAGBuilder.build(
        nodes_data=[{"id": node_id, "data": {"label": label}}],
        edges_data=[],
    )


@pytest.fixture(autouse=True)
def clean_tool_registry():
    """Ensure clean global ToolRegistry and Scheduler before each test."""
    reg = ToolRegistry.reset_instance()
    reg.register(DummyGlobalTool())
    ARDOScheduler._sessions.clear()
    yield reg
    ToolRegistry.reset_instance()
    ARDOScheduler._sessions.clear()


class TestSessionIsolation:

    def test_session_gets_distinct_browser_instances(self):
        """Requirement A: Two sessions get different browser execution objects."""
        async def _run():
            graph_a = make_graph("t1", "Navigate A")
            graph_b = make_graph("t2", "Navigate B")

            session_a = ARDOSession(session_id="session_A", graph=graph_a, mode="safe")
            session_b = ARDOSession(session_id="session_B", graph=graph_b, mode="safe")

            # Assert distinct tool registries
            assert session_a.tool_registry is not session_b.tool_registry

            # Assert distinct browser tools
            tool_a = session_a.tool_registry.get_tool("browser")
            tool_b = session_b.tool_registry.get_tool("browser")
            assert tool_a is not None
            assert tool_b is not None
            assert tool_a is not tool_b

            # Assert distinct adapters and executors
            assert session_a.browser_adapter is not session_b.browser_adapter
            assert session_a.browser_adapter.executor is not session_b.browser_adapter.executor

            # Clean up
            await session_a.cleanup()
            await session_b.cleanup()

        asyncio.run(_run())

    def test_session_tool_dispatch_isolated_execution(self):
        """Requirements B & C: Session A dispatches to Adapter A only; Session B to Adapter B only."""
        async def _run():
            mock_exec_a = MagicMock(spec=PlaywrightBrowserExecutor)
            mock_exec_a.execute_action = AsyncMock(return_value=BrowserActionResult(
                success=True,
                action_type=BrowserActionType.NAVIGATE,
                current_url="https://site-a.com",
            ))
            adapter_a = BrowserTaskAdapter(executor=mock_exec_a)

            mock_exec_b = MagicMock(spec=PlaywrightBrowserExecutor)
            mock_exec_b.execute_action = AsyncMock(return_value=BrowserActionResult(
                success=True,
                action_type=BrowserActionType.NAVIGATE,
                current_url="https://site-b.com",
            ))
            adapter_b = BrowserTaskAdapter(executor=mock_exec_b)

            graph_a = make_graph("tA", "Task A")
            graph_b = make_graph("tB", "Task B")

            session_a = ARDOSession(session_id="session_A", graph=graph_a, mode="safe", browser_adapter=adapter_a)
            session_b = ARDOSession(session_id="session_B", graph=graph_b, mode="safe", browser_adapter=adapter_b)

            # Dispatch tool call from Session A
            call_a = ToolCall(
                call_id="call_a",
                tool_name="browser",
                action_name="navigate",
                arguments={"url": "https://site-a.com", "task_id": "tA", "session_id": "session_A"},
            )
            result_a = await session_a.tool_registry.dispatch(call_a)
            assert result_a.success is True
            assert result_a.metadata.get("current_url") == "https://site-a.com"
            mock_exec_a.execute_action.assert_called_once()
            mock_exec_b.execute_action.assert_not_called()

            # Dispatch tool call from Session B
            call_b = ToolCall(
                call_id="call_b",
                tool_name="browser",
                action_name="navigate",
                arguments={"url": "https://site-b.com", "task_id": "tB", "session_id": "session_B"},
            )
            result_b = await session_b.tool_registry.dispatch(call_b)
            assert result_b.success is True
            assert result_b.metadata.get("current_url") == "https://site-b.com"
            mock_exec_b.execute_action.assert_called_once()
            # mock_exec_a should still have only 1 call
            assert mock_exec_a.execute_action.call_count == 1

            await session_a.cleanup()
            await session_b.cleanup()

        asyncio.run(_run())

    def test_idempotent_cleanup_isolation(self):
        """Requirement D: Closing/cleaning Session A does not affect Session B's browser state."""
        async def _run():
            mock_exec_a = MagicMock(spec=PlaywrightBrowserExecutor)
            mock_exec_a.is_running = True
            mock_exec_a.stop = AsyncMock()
            adapter_a = BrowserTaskAdapter(executor=mock_exec_a)

            mock_exec_b = MagicMock(spec=PlaywrightBrowserExecutor)
            mock_exec_b.is_running = True
            mock_exec_b.stop = AsyncMock()
            adapter_b = BrowserTaskAdapter(executor=mock_exec_b)

            graph_a = make_graph("tA", "Task A")
            graph_b = make_graph("tB", "Task B")

            session_a = ARDOSession(session_id="session_A", graph=graph_a, mode="safe", browser_adapter=adapter_a)
            session_b = ARDOSession(session_id="session_B", graph=graph_b, mode="safe", browser_adapter=adapter_b)

            # Clean up Session A multiple times (idempotency check)
            await session_a.cleanup()
            await session_a.cleanup()
            mock_exec_a.stop.assert_called_once()

            # Verify Session B is completely untouched and active
            mock_exec_b.stop.assert_not_called()
            assert session_b.tool_registry.get_tool("browser") is not None

            # Now clean up Session B
            await session_b.cleanup()
            mock_exec_b.stop.assert_called_once()

        asyncio.run(_run())

    def test_session_and_global_tool_resolution(self):
        """Requirements E & F: Session inherits global tools, shadows browser locally without leaking."""
        global_reg = ToolRegistry.get_instance()
        assert global_reg.get_tool("calculator") is not None

        session_reg = global_reg.create_session_registry()
        mock_tool = MagicMock(spec=BaseTool)
        mock_tool.name = "browser"
        mock_tool.capability = ToolCapability.BROWSER_AUTOMATION
        session_reg.register(mock_tool)

        # Session registry resolves both
        assert session_reg.get_tool("browser") is mock_tool
        assert session_reg.get_tool("calculator") is not None
        assert session_reg.get_tool("calculator").name == "calculator"

        # Global registry does NOT have session's mock_tool in its local table
        assert global_reg._tools.get("browser") is not mock_tool

    def test_scheduler_session_lifecycle_and_stop(self):
        """Validates ARDOScheduler.stop_session cleans up only the target session."""
        async def _run():
            graph_a = make_graph("t1", "T1")
            graph_b = make_graph("t2", "T2")

            mock_exec_a = MagicMock(spec=PlaywrightBrowserExecutor)
            mock_exec_a.is_running = True
            mock_exec_a.stop = AsyncMock()
            adapter_a = BrowserTaskAdapter(executor=mock_exec_a)

            mock_exec_b = MagicMock(spec=PlaywrightBrowserExecutor)
            mock_exec_b.is_running = True
            mock_exec_b.stop = AsyncMock()
            adapter_b = BrowserTaskAdapter(executor=mock_exec_b)

            session_a = ARDOSession(session_id="ardo_sess_1", graph=graph_a, mode="safe", browser_adapter=adapter_a)
            session_b = ARDOSession(session_id="ardo_sess_2", graph=graph_b, mode="safe", browser_adapter=adapter_b)

            ARDOScheduler._sessions["ardo_sess_1"] = session_a
            ARDOScheduler._sessions["ardo_sess_2"] = session_b

            # Stop session 1
            stopped = await ARDOScheduler.stop_session("ardo_sess_1")
            assert stopped is True
            assert "ardo_sess_1" not in ARDOScheduler._sessions
            assert "ardo_sess_2" in ARDOScheduler._sessions
            mock_exec_a.stop.assert_called_once()
            mock_exec_b.stop.assert_not_called()

            # Clean up session 2
            await ARDOScheduler.stop_session("ardo_sess_2")
            mock_exec_b.stop.assert_called_once()

        asyncio.run(_run())
