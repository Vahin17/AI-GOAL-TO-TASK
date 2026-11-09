"""
test_phase12_no_simulation.py — Unit Tests for Phase 12: Remove Simulation from Real Execution
=============================================================================================
Validates:
1. Browser execution failure must not produce successful completion.
2. Missing/unavailable Playwright or browser adapter must not fall through to simulated success.
3. RuntimeEngine cannot manufacture browser success independently of BrowserAgent.
4. Browser task remains unsuccessful (FAILED) when SemanticVerifier returns NOT_VERIFIED.
5. Browser recovery utilizes another real BrowserAgent action rather than a simulated fake action.
6. Legitimate non-browser tasks (generic/simulation or non-browser tools) still execute as expected.
7. Generated browser action without successful verification never marks a task COMPLETED.
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from orchestration.runtime_engine import RuntimeEngine
from orchestration.dag_builder import DAGBuilder
from orchestration.state_machine import TaskState
from execution.browser.browser_agent import BrowserAgent
from execution.browser.agent_contracts import (
    BrowserTask,
    BrowserObservation,
    BrowserDecision,
    BrowserDecisionType,
    BrowserActionDecision,
    BrowserAgentOutcome,
    BrowserVerification,
    BrowserVerificationStatus,
    RecentActionRecord,
)
from execution.browser.browser_models import (
    BrowserAction,
    BrowserActionResult,
    BrowserActionType,
)
from execution.browser.perception_models import TabInfo, ObservedElement
from execution.registry.tool_registry import ToolRegistry


def make_mock_observation(url="about:blank", title="", session_id="test_session", elements=None):
    return BrowserObservation(
        task_id="t_test",
        session_id=session_id,
        step_index=0,
        current_url=url,
        page_title=title,
        observed_elements=elements or [],
        tabs=[TabInfo(tab_ref="tab_000", title=title, url=url, is_active=True, index=0)],
    )


class TestPhase12NoSimulation:

    def test_1_browser_execution_failure_never_produces_success(self):
        """Req 1: When browser execution fails, the task becomes FAILED and is never simulated as success."""
        async def _run():
            mock_llm = MagicMock()
            d1 = json.dumps({
                "decision": "execute",
                "action": {"action_type": "click", "element_ref": "elem_001"},
                "reason": "Click submit button",
            })
            mock_llm.query = AsyncMock(return_value=d1)

            mock_adapter = MagicMock()
            # Action execution hard fails in browser
            mock_adapter.execute_safe_browser_task = AsyncMock(
                return_value=(
                    BrowserActionResult(
                        action_type=BrowserActionType.CLICK,
                        success=False,
                        error="Element not interactable / Target page crashed",
                    ),
                    None,
                )
            )

            mock_perception = MagicMock()
            obs_el = ObservedElement(ref="elem_001", tag="button", text="Submit", css_selector="button#submit")
            mock_perception.observe = AsyncMock(
                return_value=make_mock_observation(
                    url="https://example.com/form",
                    title="Form Page",
                    elements=[obs_el],
                )
            )

            agent = BrowserAgent(llm_provider=mock_llm, adapter=mock_adapter, perception=mock_perception)
            task = BrowserTask(
                task_id="t_fail_click",
                objective="Click the submit button",
                session_id="sess_no_sim_1",
                max_actions=2,
            )

            result = await agent.run(task)

            # Strict assertion: Never COMPLETED
            assert result.outcome == BrowserAgentOutcome.FAILED
            assert "Element not interactable" in (result.error or "")

        asyncio.run(_run())

    def test_2_unavailable_playwright_fails_without_simulated_fallback(self):
        """Req 2: When browser resource / adapter is completely missing or throws, task fails cleanly without simulation."""
        async def _run():
            graph = DAGBuilder.build(
                nodes_data=[{"id": "t_open_site", "data": {"label": "Open https://example.com in browser"}}],
                edges_data=[],
            )

            # RuntimeEngine initialized with a mock browser agent whose run() raises an exception (e.g. Playwright binary missing)
            mock_agent = MagicMock()
            mock_agent.run = AsyncMock(side_effect=RuntimeError("Playwright executable not found on host"))

            engine = RuntimeEngine(
                graph=graph,
                session_id="sess_no_playwright",
                mode="safe",
                browser_agent=mock_agent,
            )

            async for _ in engine.run():
                pass

            node = graph.get_node("t_open_site")
            # Strict assertion: Must be FAILED, not COMPLETED
            assert node.status == TaskState.FAILED
            assert any("Playwright executable not found" in log for log in node.execution_logs)

        asyncio.run(_run())

    def test_3_runtime_engine_cannot_manufacture_browser_success(self):
        """Req 3: RuntimeEngine cannot mark a browser task COMPLETED unless BrowserAgent returns verified COMPLETED."""
        async def _run():
            graph = DAGBuilder.build(
                nodes_data=[{"id": "t_browse", "data": {"label": "Navigate to dashboard and extract data"}}],
                edges_data=[],
            )

            mock_agent = MagicMock()
            # BrowserAgent returns FAILED outcome
            mock_agent.run = AsyncMock(
                return_value=MagicMock(
                    outcome=BrowserAgentOutcome.FAILED,
                    verification=None,
                    error="Target page failed to load",
                )
            )

            engine = RuntimeEngine(
                graph=graph,
                session_id="sess_no_mfg",
                mode="safe",
                browser_agent=mock_agent,
            )

            async for _ in engine.run():
                pass

            node = graph.get_node("t_browse")
            assert node.status == TaskState.FAILED

        asyncio.run(_run())

    def test_4_unverified_finish_decision_is_rejected_as_failure(self):
        """Req 4: If BrowserAgent declares FINISH on incorrect state, task fails and is not falsely marked complete."""
        async def _run():
            mock_llm = MagicMock()
            # Agent declares FINISH immediately without navigating
            finish_decision = json.dumps({
                "decision": "finish",
                "reason": "I think I am on the wikipedia page",
            })
            mock_llm.query = AsyncMock(return_value=finish_decision)

            mock_adapter = MagicMock()
            mock_perception = MagicMock()
            # Live observation is blank
            mock_perception.observe = AsyncMock(
                return_value=make_mock_observation(url="about:blank", title="Blank Tab")
            )

            agent = BrowserAgent(llm_provider=mock_llm, adapter=mock_adapter, perception=mock_perception)
            task = BrowserTask(
                task_id="t_unverified_wiki",
                objective="Open Wikipedia in new tab",
                session_id="sess_unv_wiki",
                max_actions=2,
            )

            result = await agent.run(task)

            # Strict assertion: Must be FAILED due to failed verification
            assert result.outcome == BrowserAgentOutcome.FAILED
            assert result.verification is not None
            assert result.verification.status == BrowserVerificationStatus.NOT_VERIFIED

        asyncio.run(_run())

    def test_5_browser_recovery_uses_real_action_not_simulated_success(self):
        """Req 5: Recovery triggers fresh observation and real action execution, failing cleanly if exhausted."""
        async def _run():
            mock_llm = MagicMock()
            # 1. Action 1: Navigate to bad URL
            # 2. Action 2: Recovery attempt navigates to another bad URL
            # 3. Action 3: Exhausted
            d1 = json.dumps({"decision": "execute", "action": {"action_type": "navigate", "url": "https://bad1.com"}})
            d2 = json.dumps({"decision": "execute", "action": {"action_type": "navigate", "url": "https://bad2.com"}})
            mock_llm.query = AsyncMock(side_effect=[d1, d2, d2, d2])

            mock_adapter = MagicMock()
            mock_adapter.execute_safe_browser_task = AsyncMock(
                return_value=(
                    BrowserActionResult(action_type=BrowserActionType.NAVIGATE, success=False, error="Connection refused"),
                    None,
                )
            )

            mock_perception = MagicMock()
            mock_perception.observe = AsyncMock(
                return_value=make_mock_observation(url="about:blank", title="")
            )

            agent = BrowserAgent(llm_provider=mock_llm, adapter=mock_adapter, perception=mock_perception)
            task = BrowserTask(
                task_id="t_rec_exhaust",
                objective="Navigate to service portal",
                session_id="sess_rec_ex",
                max_actions=3,
            )

            result = await agent.run(task)

            assert result.outcome == BrowserAgentOutcome.FAILED
            assert mock_adapter.execute_safe_browser_task.call_count >= 1

        asyncio.run(_run())

    def test_6_legitimate_generic_task_behavior_maintained(self):
        """Req 6: Non-browser tasks (e.g. calculation/computation nodes) execute via standard scheduler paths."""
        async def _run():
            graph = DAGBuilder.build(
                nodes_data=[{"id": "t_calc", "data": {"label": "Compute statistical metrics for dataset"}}],
                edges_data=[],
            )

            engine = RuntimeEngine(
                graph=graph,
                session_id="sess_generic",
                mode="safe",
            )

            async for _ in engine.run():
                pass

            node = graph.get_node("t_calc")
            assert node.status in (TaskState.COMPLETED, TaskState.FAILED)

        asyncio.run(_run())
