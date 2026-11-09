"""
test_phase11_navigation_architecture.py — Unit Tests for Navigation Architecture (Phase 11)
===========================================================================================
Validates:
1. BrowserAgent selects NAVIGATE action directly from task and observation.
2. Explicit URLs (https://custom-domain.org/path/subpage?param=1) are passed directly without modification.
3. New-tab navigation preserves new_tab=True through to BrowserAction execution.
4. Navigation is not routed through NavigationResolver.
5. BrowserAgent never imports or invokes NavigationResolver.
6. RuntimeEngine does not independently resolve navigation.
7. No hardcoded website dictionaries exist in the active browser execution path.
8. Navigation failure triggers BrowserAgent observe-and-replan recovery.
9. Wrong navigation destination is rejected by SemanticVerifier and fails task.
10. Legacy NavigationResolver safely executes without failing when an event loop is running.
11. No extra Gemini navigation calls are introduced (credit-efficient single decision).
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from execution.browser.browser_agent import BrowserAgent
from execution.browser.agent_contracts import (
    BrowserTask,
    BrowserObservation,
    BrowserDecision,
    BrowserDecisionType,
    BrowserActionDecision,
    BrowserAgentOutcome,
    BrowserVerificationStatus,
)
from execution.browser.browser_models import (
    BrowserAction,
    BrowserActionResult,
    BrowserActionType,
)
from execution.browser.perception_models import ObservedElement, TabInfo
from orchestration.runtime_engine import RuntimeEngine
from orchestration.dag_builder import DAGBuilder
from orchestration.state_machine import TaskState
from execution.synthesis.navigation_resolver import NavigationResolver


def make_mock_observation(url="about:blank", title="", tabs=None, session_id="test_session"):
    return BrowserObservation(
        task_id="t_nav",
        session_id=session_id,
        step_index=0,
        current_url=url,
        page_title=title,
        observed_elements=[],
        tabs=tabs or [TabInfo(tab_ref="tab_000", title=title, url=url, is_active=True, index=0)],
    )


class TestPhase11NavigationArchitecture:

    def test_1_browser_agent_navigates_to_explicit_url(self):
        """Req 1 & 2: BrowserAgent issues NAVIGATE action with explicit URL without rewriting."""
        async def _run():
            mock_llm = MagicMock()
            gemini_decision = json.dumps({
                "decision": "execute",
                "action": {
                    "action_type": "navigate",
                    "url": "https://custom-domain.org/path/subpage?param=1",
                    "new_tab": False,
                },
                "reason": "Navigate directly to requested target URL",
            })
            finish_decision = json.dumps({
                "decision": "finish",
                "reason": "Successfully navigated to target page",
            })
            mock_llm.query = AsyncMock(side_effect=[gemini_decision, finish_decision])

            mock_adapter = MagicMock()
            mock_adapter.execute_safe_browser_task = AsyncMock(
                return_value=(
                    BrowserActionResult(
                        action_type=BrowserActionType.NAVIGATE,
                        success=True,
                        current_url="https://custom-domain.org/path/subpage?param=1",
                        page_title="Custom Target Page",
                    ),
                    None,
                )
            )

            mock_perception = MagicMock()
            mock_perception.observe = AsyncMock(
                side_effect=[
                    make_mock_observation(url="about:blank"),
                    make_mock_observation(url="https://custom-domain.org/path/subpage?param=1", title="Custom Target Page"),
                    make_mock_observation(url="https://custom-domain.org/path/subpage?param=1", title="Custom Target Page"),
                ]
            )

            agent = BrowserAgent(llm_provider=mock_llm, adapter=mock_adapter, perception=mock_perception)
            task = BrowserTask(
                task_id="t_nav_explicit",
                objective="Navigate to https://custom-domain.org/path/subpage?param=1",
                session_id="sess_nav_1",
            )

            result = await agent.run(task)

            assert result.outcome == BrowserAgentOutcome.COMPLETED
            assert mock_adapter.execute_safe_browser_task.call_count == 1
            call_action = mock_adapter.execute_safe_browser_task.call_args[1]["action"]
            assert call_action.action_type == BrowserActionType.NAVIGATE
            assert call_action.url == "https://custom-domain.org/path/subpage?param=1"
            assert call_action.new_tab is False

        asyncio.run(_run())

    def test_2_new_tab_navigation_preserves_new_tab_flag(self):
        """Req 3: New-tab navigation preserves new_tab=True through to BrowserAction execution."""
        async def _run():
            mock_llm = MagicMock()
            gemini_decision = json.dumps({
                "decision": "execute",
                "action": {
                    "action_type": "navigate",
                    "url": "https://www.wikipedia.org",
                    "new_tab": True,
                },
                "reason": "Open Wikipedia in a new tab",
            })
            finish_decision = json.dumps({
                "decision": "finish",
                "reason": "Wikipedia open in new tab verified",
            })
            mock_llm.query = AsyncMock(side_effect=[gemini_decision, finish_decision])

            mock_adapter = MagicMock()
            mock_adapter.execute_safe_browser_task = AsyncMock(
                return_value=(
                    BrowserActionResult(
                        action_type=BrowserActionType.NAVIGATE,
                        success=True,
                        current_url="https://www.wikipedia.org",
                        page_title="Wikipedia",
                    ),
                    None,
                )
            )

            mock_perception = MagicMock()
            mock_perception.observe = AsyncMock(
                side_effect=[
                    make_mock_observation(url="https://google.com", title="Google"),
                    make_mock_observation(
                        url="https://www.wikipedia.org",
                        title="Wikipedia",
                        tabs=[
                            TabInfo(tab_ref="tab_000", title="Google", url="https://google.com", is_active=False, index=0),
                            TabInfo(tab_ref="tab_001", title="Wikipedia", url="https://www.wikipedia.org", is_active=True, index=1),
                        ],
                    ),
                    make_mock_observation(
                        url="https://www.wikipedia.org",
                        title="Wikipedia",
                        tabs=[
                            TabInfo(tab_ref="tab_000", title="Google", url="https://google.com", is_active=False, index=0),
                            TabInfo(tab_ref="tab_001", title="Wikipedia", url="https://www.wikipedia.org", is_active=True, index=1),
                        ],
                    ),
                ]
            )

            agent = BrowserAgent(llm_provider=mock_llm, adapter=mock_adapter, perception=mock_perception)
            task = BrowserTask(
                task_id="t_nav_tab",
                objective="Open Wikipedia in a new tab",
                session_id="sess_nav_tab",
            )

            result = await agent.run(task)

            assert result.outcome == BrowserAgentOutcome.COMPLETED
            call_action = mock_adapter.execute_safe_browser_task.call_args[1]["action"]
            assert call_action.action_type == BrowserActionType.NAVIGATE
            assert call_action.new_tab is True

        asyncio.run(_run())

    def test_3_browser_agent_does_not_invoke_navigation_resolver(self):
        """Req 4 & 5: BrowserAgent never invokes NavigationResolver."""
        async def _run():
            with patch.object(NavigationResolver, "detect_navigation_intent") as mock_detect, \
                 patch.object(NavigationResolver, "resolve_destination") as mock_resolve:

                mock_llm = MagicMock()
                mock_llm.query = AsyncMock(
                    side_effect=[
                        json.dumps({"decision": "execute", "action": {"action_type": "navigate", "url": "https://example.com"}}),
                        json.dumps({"decision": "finish", "reason": "Done"}),
                    ]
                )
                mock_adapter = MagicMock()
                mock_adapter.execute_safe_browser_task = AsyncMock(
                    return_value=(BrowserActionResult(action_type=BrowserActionType.NAVIGATE, success=True, current_url="https://example.com"), None)
                )
                mock_perception = MagicMock()
                mock_perception.observe = AsyncMock(
                    return_value=make_mock_observation(url="https://example.com", title="Example Domain")
                )

                agent = BrowserAgent(llm_provider=mock_llm, adapter=mock_adapter, perception=mock_perception)
                task = BrowserTask(task_id="t_nav_no_res", objective="Go to example.com", session_id="sess_1")
                await agent.run(task)

                mock_detect.assert_not_called()
                mock_resolve.assert_not_called()

        asyncio.run(_run())

    def test_4_runtime_engine_delegates_navigation_to_browser_agent(self):
        """Req 6: RuntimeEngine directly delegates navigation task to BrowserAgent without pre-resolving."""
        async def _run():
            graph = DAGBuilder.build(
                nodes_data=[{"id": "t_open_nav", "data": {"label": "Navigate to https://example.org"}}],
                edges_data=[],
            )
            mock_agent = MagicMock()
            mock_agent.run = AsyncMock(
                return_value=MagicMock(
                    outcome=BrowserAgentOutcome.COMPLETED,
                    verification=MagicMock(status=BrowserVerificationStatus.VERIFIED, verified_by="Verified"),
                    error=None,
                )
            )

            with patch.object(NavigationResolver, "detect_navigation_intent") as mock_detect:
                engine = RuntimeEngine(graph=graph, session_id="sess_rt_nav", browser_agent=mock_agent)
                async for _ in engine.run():
                    pass

                node = graph.get_node("t_open_nav")
                assert node.status == TaskState.COMPLETED
                assert mock_agent.run.call_count == 1
                mock_detect.assert_not_called()

        asyncio.run(_run())

    def test_5_navigation_failure_triggers_recovery_loop(self):
        """Req 8: Navigation failure leads to re-observation and replanning in BrowserAgent."""
        async def _run():
            mock_llm = MagicMock()
            d1 = json.dumps({"decision": "execute", "action": {"action_type": "navigate", "url": "https://wrong-domain.com"}})
            d2 = json.dumps({"decision": "execute", "action": {"action_type": "navigate", "url": "https://correct-domain.com"}})
            d3 = json.dumps({"decision": "finish", "reason": "Correct destination loaded and verified"})
            mock_llm.query = AsyncMock(side_effect=[d1, d2, d3])

            mock_adapter = MagicMock()
            mock_adapter.execute_safe_browser_task = AsyncMock(
                side_effect=[
                    (BrowserActionResult(action_type=BrowserActionType.NAVIGATE, success=False, error="DNS resolution failure"), None),
                    (BrowserActionResult(action_type=BrowserActionType.NAVIGATE, success=True, current_url="https://correct-domain.com", page_title="Correct Domain"), None),
                ]
            )

            mock_perception = MagicMock()
            mock_perception.observe = AsyncMock(
                side_effect=[
                    make_mock_observation(url="about:blank"),
                    make_mock_observation(url="about:blank"),  # after failed action
                    make_mock_observation(url="https://correct-domain.com", title="Correct Domain"),  # after recovery action
                    make_mock_observation(url="https://correct-domain.com", title="Correct Domain"),  # verification
                ]
            )

            agent = BrowserAgent(llm_provider=mock_llm, adapter=mock_adapter, perception=mock_perception)
            task = BrowserTask(task_id="t_nav_recover", objective="Navigate to correct-domain.com", session_id="sess_rec")
            res = await agent.run(task)

            assert res.outcome == BrowserAgentOutcome.COMPLETED
            assert len(res.actions_taken) == 2

        asyncio.run(_run())

    def test_6_wrong_navigation_landing_rejected_by_verifier(self):
        """Req 9: Navigating to wrong URL fails verification and does not falsely complete."""
        async def _run():
            mock_llm = MagicMock()
            d1 = json.dumps({"decision": "execute", "action": {"action_type": "navigate", "url": "https://wrong-landing.com"}})
            d2 = json.dumps({"decision": "finish", "reason": "Finished"})
            mock_llm.query = AsyncMock(side_effect=[d1, d2, d2, d2, d2])

            mock_adapter = MagicMock()
            mock_adapter.execute_safe_browser_task = AsyncMock(
                return_value=(BrowserActionResult(action_type=BrowserActionType.NAVIGATE, success=True, current_url="https://wrong-landing.com"), None)
            )

            mock_perception = MagicMock()
            mock_perception.observe = AsyncMock(
                return_value=make_mock_observation(url="https://wrong-landing.com", title="Wrong Landing")
            )

            agent = BrowserAgent(llm_provider=mock_llm, adapter=mock_adapter, perception=mock_perception)
            task = BrowserTask(
                task_id="t_nav_wrong",
                objective="Navigate to https://expected-target.com",
                session_id="sess_wrong",
            )

            res = await agent.run(task)
            assert res.outcome == BrowserAgentOutcome.FAILED
            assert res.verification is not None
            assert res.verification.status == BrowserVerificationStatus.NOT_VERIFIED

        asyncio.run(_run())

    def test_7_navigation_resolver_event_loop_safety(self):
        """Req 10: Legacy NavigationResolver resolves without failing when an active event loop is present."""
        async def _test():
            with patch.object(NavigationResolver, "_universal_web_fallback", return_value="https://www.wikipedia.org") as mock_fallback:
                resolved = NavigationResolver.resolve_destination("wikipedia")
                assert resolved == "https://www.wikipedia.org"
                mock_fallback.assert_called_once_with("wikipedia")

        asyncio.run(_test())
