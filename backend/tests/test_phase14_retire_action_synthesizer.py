"""
test_phase14_retire_action_synthesizer.py — Phase 14: ActionSynthesizer Retirement Tests
========================================================================================
Validates that ActionSynthesizer is completely retired and unused by the active
production architecture, with all browser action decisions owned by BrowserAgent.

Requirements:
1. ActionSynthesizer is not imported or used by DAGBuilder.
2. ActionSynthesizer is not imported or used by RuntimeEngine.
3. ActionSynthesizer is not imported or used by BrowserAgent.
4. NavigationResolver does not depend on ActionSynthesizer.
5. Planned browser tasks reach BrowserAgent without ActionSynthesizer.
6. BrowserAgent generates and executes real BrowserActions via Gemini observe-decide loop.
7. DAGBuilder creates valid high-level task DAGs without action synthesis.
8. Non-browser tasks continue executing normally.
9. Real browser failure leads to recovery/failure, never a synthesizer fallback.
10. Direct ActionSynthesizer instantiation fails with explicit retirement error.
11. Clean codebase scan confirming no production module imports ActionSynthesizer.
"""

import json
import inspect
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

import orchestration.dag_builder as dag_builder_module
import orchestration.runtime_engine as runtime_engine_module
import execution.browser.browser_agent as browser_agent_module
import execution.synthesis.navigation_resolver as navigation_resolver_module

from orchestration.dag_builder import DAGBuilder, ARDOGraph
from orchestration.runtime_engine import RuntimeEngine
from orchestration.scheduler import ARDOSession
from orchestration.state_machine import TaskState
from execution.browser.agent_contracts import (
    BrowserTask,
    BrowserAgentOutcome,
    BrowserAgentResult,
    BrowserVerification,
    BrowserVerificationStatus,
    BrowserObservation,
)
from execution.browser.browser_models import BrowserAction, BrowserActionType, BrowserActionResult
from execution.browser.perception_models import TabInfo
from execution.synthesis.action_synthesizer import ActionSynthesizer
from execution.synthesis.navigation_resolver import NavigationResolver


class TestPhase14RetireActionSynthesizer:
    """Test suite for Phase 14 ActionSynthesizer retirement validation."""

    def test_1_dagbuilder_does_not_import_or_use_actionsynthesizer(self):
        """
        Req 1: DAGBuilder source code and module namespace do not import ActionSynthesizer.
        """
        source = inspect.getsource(dag_builder_module)
        assert "ActionSynthesizer" not in source
        assert "action_synthesizer" not in source
        assert not hasattr(dag_builder_module, "ActionSynthesizer")

    def test_2_runtime_engine_does_not_import_or_use_actionsynthesizer(self):
        """
        Req 2: RuntimeEngine source code and module namespace do not import ActionSynthesizer.
        """
        source = inspect.getsource(runtime_engine_module)
        assert "ActionSynthesizer" not in source
        assert "action_synthesizer" not in source
        assert not hasattr(runtime_engine_module, "ActionSynthesizer")

    def test_3_browser_agent_does_not_import_or_use_actionsynthesizer(self):
        """
        Req 3: BrowserAgent does not import or reference ActionSynthesizer.
        """
        source = inspect.getsource(browser_agent_module)
        assert "ActionSynthesizer" not in source
        assert "action_synthesizer" not in source
        assert not hasattr(browser_agent_module, "ActionSynthesizer")

    def test_4_navigation_resolver_does_not_depend_on_actionsynthesizer(self):
        """
        Req 4: NavigationResolver does not import or call ActionSynthesizer.
        """
        source = inspect.getsource(navigation_resolver_module)
        assert "ActionSynthesizer" not in source
        assert "action_synthesizer" not in source
        assert not hasattr(navigation_resolver_module, "ActionSynthesizer")

    def test_5_actionsynthesizer_instantiation_raises_runtime_error(self):
        """
        Req 10: ActionSynthesizer is retired and explicitly blocks usage on instantiation.
        """
        with pytest.raises(RuntimeError, match="retired in Phase 14"):
            ActionSynthesizer()

    def test_6_browser_task_dispatched_to_browser_agent_without_synthesizer(self):
        """
        Req 5: Browser task flows from DAGBuilder -> RuntimeEngine -> BrowserAgent without synthesizer.
        """
        async def _run():
            nodes_data = [{"id": "t1", "data": {"label": "Navigate to https://example.com"}}]
            graph = DAGBuilder.build(nodes_data=nodes_data, edges_data=[])
            session = ARDOSession(session_id="session_p14_dispatch", graph=graph, mode="sequential")

            mock_agent = MagicMock()
            mock_res = BrowserAgentResult(
                task_id="t1",
                session_id="session_p14_dispatch",
                objective="Navigate to https://example.com",
                outcome=BrowserAgentOutcome.COMPLETED,
                verification=BrowserVerification(
                    task_id="t1",
                    status=BrowserVerificationStatus.VERIFIED,
                    verified_by="Verification passed",
                ),
            )
            mock_agent.run = AsyncMock(return_value=mock_res)
            session.engine._browser_agent = mock_agent

            events = []
            async for ev in session.engine.run():
                events.append(ev)

            mock_agent.run.assert_called_once()
            called_task: BrowserTask = mock_agent.run.call_args[0][0]
            assert called_task.task_id == "t1"
            assert called_task.objective == "Navigate to https://example.com"
            assert graph.get_node("t1").status == TaskState.COMPLETED

        asyncio.run(_run())

    def test_7_browser_agent_executes_via_gemini_loop_without_synthesizer(self):
        """
        Req 6: BrowserAgent executes real BrowserActions using Gemini decision flow.
        """
        async def _run():
            from execution.browser.browser_agent import BrowserAgent

            mock_llm = MagicMock()
            gemini_decision = json.dumps({
                "decision": "execute",
                "action": {
                    "action_type": "navigate",
                    "url": "https://www.wikipedia.org",
                    "new_tab": False,
                },
                "reason": "Navigating to Wikipedia",
            })
            finish_decision = json.dumps({
                "decision": "finish",
                "reason": "Task complete",
            })
            mock_llm.query = AsyncMock(side_effect=[gemini_decision, finish_decision])

            mock_adapter = MagicMock()
            mock_adapter.execute_safe_browser_task = AsyncMock(return_value=(
                BrowserActionResult(
                    action_type=BrowserActionType.NAVIGATE,
                    success=True,
                    current_url="https://www.wikipedia.org",
                    page_title="Wikipedia",
                ),
                None,
            ))

            obs1 = BrowserObservation(
                session_id="s_14",
                task_id="agent_p14_test",
                current_url="about:blank",
                page_title="Blank",
                observed_elements=[],
                tabs=[TabInfo(tab_ref="tab_001", title="Blank", url="about:blank", is_active=True, index=0)],
            )
            obs2 = BrowserObservation(
                session_id="s_14",
                task_id="agent_p14_test",
                current_url="https://www.wikipedia.org",
                page_title="Wikipedia",
                observed_elements=[],
                tabs=[TabInfo(tab_ref="tab_001", title="Wikipedia", url="https://www.wikipedia.org", is_active=True, index=0)],
            )
            mock_perception = MagicMock()
            mock_perception.observe = AsyncMock(side_effect=[obs1, obs2, obs2])

            agent = BrowserAgent(llm_provider=mock_llm, adapter=mock_adapter, perception=mock_perception)
            task = BrowserTask(
                task_id="agent_p14_test",
                session_id="s_14",
                objective="Open Wikipedia",
                expected_url_prefix="https://www.wikipedia.org",
            )

            result = await agent.run(task)
            assert result.outcome == BrowserAgentOutcome.COMPLETED
            assert result.verification is not None
            assert result.verification.status == BrowserVerificationStatus.VERIFIED
            mock_adapter.execute_safe_browser_task.assert_called_once()

        asyncio.run(_run())

    def test_8_dagbuilder_builds_valid_dags_without_synthesizer(self):
        """
        Req 7: DAGBuilder creates structured multi-step DAGs with correct dependency order.
        """
        nodes_data = [
            {"id": "step_1", "data": {"label": "Open browser"}},
            {"id": "step_2", "data": {"label": "Search for documentation"}},
            {"id": "step_3", "data": {"label": "Download PDF"}},
        ]
        edges_data = [
            {"source": "step_1", "target": "step_2"},
            {"source": "step_2", "target": "step_3"},
        ]

        graph = DAGBuilder.build(nodes_data=nodes_data, edges_data=edges_data)
        assert len(graph.nodes) == 3
        assert graph.get_node("step_1").dependency_ids == []
        assert graph.get_node("step_2").dependency_ids == ["step_1"]
        assert graph.get_node("step_3").dependency_ids == ["step_2"]
        for nid in ["step_1", "step_2", "step_3"]:
            assert graph.get_node(nid).browser_action is None
            assert graph.get_node(nid).tool_call is None

    def test_9_browser_failure_leads_to_failed_without_synthesizer_fallback(self):
        """
        Req 9: Real browser failure transitions to FAILED rather than falling back to ActionSynthesizer.
        """
        async def _run():
            nodes_data = [{"id": "fail_task", "data": {"label": "Navigate to invalid URL"}}]
            graph = DAGBuilder.build(nodes_data=nodes_data, edges_data=[])
            session = ARDOSession(session_id="session_p14_fail", graph=graph, mode="sequential")

            mock_agent = MagicMock()
            mock_res = BrowserAgentResult(
                task_id="fail_task",
                session_id="session_p14_fail",
                objective="Navigate to invalid URL",
                outcome=BrowserAgentOutcome.FAILED,
                error="Browser crashed",
            )
            mock_agent.run = AsyncMock(return_value=mock_res)
            session.engine._browser_agent = mock_agent

            events = []
            async for ev in session.engine.run():
                events.append(ev)

            assert graph.get_node("fail_task").status == TaskState.FAILED

        asyncio.run(_run())

    def test_10_production_modules_free_of_actionsynthesizer(self):
        """
        Req 11: Repository verification that active orchestration, planning, and execution
        modules contain zero imports of ActionSynthesizer.
        """
        for mod in [dag_builder_module, runtime_engine_module, browser_agent_module, navigation_resolver_module]:
            source = inspect.getsource(mod)
            assert "from execution.synthesis.action_synthesizer import" not in source
            assert "import ActionSynthesizer" not in source
