"""
test_phase15_event_evidence_observability.py — Phase 15 Test Suite
===================================================================
Validates the clean, structured, and truthful event and evidence trail for execution:
1. Task start produces a structured TASK_STARTED event.
2. Browser observation produces useful observation evidence.
3. Successful browser action produces ACTION_STARTED and ACTION_COMPLETED.
4. Failed browser action produces ACTION_FAILED and does not falsely report success.
5. BrowserAgent decision produces structured DECISION_MADE information.
6. Decision evidence does not expose hidden chain-of-thought.
7. SemanticVerifier result is observable.
8. NOT_VERIFIED does not become TASK_COMPLETED.
9. Recovery produces observable recovery information.
10. Task/session/run identifiers remain correctly correlated.
11. Sensitive action input is redacted from events.
12. Existing WebSocket/frontend event consumers remain compatible.
13. Non-browser task events continue to work.
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from execution.browser.agent_contracts import (
    BrowserActionDecision,
    BrowserAgentOutcome,
    BrowserAgentResult,
    BrowserDecision,
    BrowserDecisionType,
    BrowserObservation,
    BrowserTask,
    BrowserVerification,
    BrowserVerificationStatus,
)
from execution.browser.browser_agent import BrowserAgent
from execution.browser.browser_models import (
    BrowserAction,
    BrowserActionResult,
    BrowserActionType,
)
from execution.browser.perception_models import ObservedElement, TabInfo
from orchestration.dag_builder import ARDOGraph, ARDOTaskNode
from orchestration.execution_monitor import ExecutionMonitor, OrchestrationEvent, redact_sensitive_details
from orchestration.runtime_engine import RuntimeEngine
from orchestration.state_machine import TaskState


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures & Mocks
# ─────────────────────────────────────────────────────────────────────────────

class MockLLM:
    """Mock LLM returning predefined JSON responses."""
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.query_count = 0

    async def query(self, prompt: str, system_instruction: str = None, json_mode: bool = False) -> str:
        self.query_count += 1
        if self.responses:
            return self.responses.pop(0)
        return json.dumps({
            "decision": "finish",
            "reason": "Task goal achieved",
        })


class MockPerception:
    """Mock perception engine returning controlled BrowserObservations."""
    def __init__(self, url="https://en.wikipedia.org", title="Wikipedia", elements=None):
        self.url = url
        self.title = title
        self.elements = elements or [
            ObservedElement(
                ref="elem_001",
                tag="input",
                role="searchbox",
                text="",
                css_selector="#searchInput",
                is_interactive=True,
            )
        ]

    async def observe(self, page=None, session_id="", task_id="", context=None, actions_taken_count=0, recent_action=None):
        return BrowserObservation(
            session_id=session_id,
            task_id=task_id,
            current_url=self.url,
            page_title=self.title,
            open_tab_count=1,
            active_tab_index=0,
            observed_elements=self.elements,
            page_text_summary=f"Welcome to {self.title}",
            actions_taken_count=actions_taken_count,
            recent_action=recent_action,
        )


class MockAdapter:
    """Mock browser task adapter."""
    def __init__(self, success=True, error=None, current_url="https://en.wikipedia.org"):
        self.success = success
        self.error = error
        self.current_url = current_url
        self.executed_actions = []

    async def execute_safe_browser_task(self, task_id: str, session_id: str, action: BrowserAction):
        self.executed_actions.append(action)
        res = BrowserActionResult(
            action_type=action.action_type,
            success=self.success,
            error=self.error,
            current_url=self.current_url,
        )
        return res, MagicMock(is_blocked=False)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Task start produces a structured TASK_STARTED event
# ─────────────────────────────────────────────────────────────────────────────

def test_task_started_event_structure():
    monitor = ExecutionMonitor(session_id="session_test_1")
    event = monitor.on_task_started("task_101", "Open Wikipedia")

    assert isinstance(event, OrchestrationEvent)
    assert event.event_type == "task_started"
    assert event.session_id == "session_test_1"
    assert event.task_id == "task_101"
    assert event.task_name == "Open Wikipedia"
    assert event.status == "active"
    assert event.timestamp > 0


# ─────────────────────────────────────────────────────────────────────────────
# 2. Browser observation produces useful observation evidence
# ─────────────────────────────────────────────────────────────────────────────

def test_observation_received_evidence():
    async def _test():
        events = []
        def callback(evt_type, task, details):
            events.append((evt_type, details))

        llm = MockLLM([
            json.dumps({"decision": "finish", "reason": "Verified on page"}),
        ])
        perception = MockPerception(url="https://en.wikipedia.org/wiki/AI", title="Artificial Intelligence")
        agent = BrowserAgent(
            llm_provider=llm,
            adapter=MockAdapter(),
            perception=perception,
            event_callback=callback,
        )

        task = BrowserTask(
            task_id="task_obs_1",
            objective="Verify Wikipedia article",
            session_id="session_obs_1",
        )

        with patch("execution.browser.semantic_verifier.SemanticVerifier.verify") as mock_verify:
            mock_verify.return_value = BrowserVerification(
                task_id=task.task_id,
                status=BrowserVerificationStatus.VERIFIED,
                verified_by="SemanticVerifier",
                checks_performed=["url_check", "content_check"],
            )
            res = await agent.run(task)

        obs_events = [e for e in events if e[0] == "observation_received"]
        assert len(obs_events) >= 1
        details = obs_events[0][1]
        assert details["url"] == "https://en.wikipedia.org/wiki/AI"
        assert details["title"] == "Artificial Intelligence"
        assert details["elements_count"] >= 1
        assert "Artificial Intelligence" in details["visible_text_snippet"] or "Welcome" in details["visible_text_snippet"]

    asyncio.run(_test())


# ─────────────────────────────────────────────────────────────────────────────
# 3. Successful browser action produces ACTION_STARTED and ACTION_COMPLETED
# ─────────────────────────────────────────────────────────────────────────────

def test_successful_action_event_sequence():
    async def _test():
        events = []
        def callback(evt_type, task, details):
            events.append((evt_type, details))

        llm = MockLLM([
            json.dumps({
                "decision": "execute",
                "reason": "Click search box",
                "action": {
                    "action_type": "click",
                    "element_ref": "elem_001",
                }
            }),
            json.dumps({"decision": "finish", "reason": "Clicked and done"}),
        ])
        adapter = MockAdapter(success=True)
        agent = BrowserAgent(
            llm_provider=llm,
            adapter=adapter,
            perception=MockPerception(),
            event_callback=callback,
        )

        task = BrowserTask(task_id="t_act_1", objective="Click search", session_id="s_act_1")

        with patch("execution.browser.semantic_verifier.SemanticVerifier.verify") as mock_verify:
            mock_verify.return_value = BrowserVerification(
                task_id=task.task_id,
                status=BrowserVerificationStatus.VERIFIED,
            )
            res = await agent.run(task)

        event_types = [e[0] for e in events]
        assert "action_started" in event_types
        assert "action_completed" in event_types

        # Find action_started details
        started = [e[1] for e in events if e[0] == "action_started"][0]
        assert started["action_type"] == "click"
        assert started["element_ref"] == "elem_001"

        # Find action_completed details
        completed = [e[1] for e in events if e[0] == "action_completed"][0]
        assert completed["action_type"] == "click"
        assert "duration_ms" in completed
        assert completed["duration_ms"] >= 0

    asyncio.run(_test())


# ─────────────────────────────────────────────────────────────────────────────
# 4. Failed browser action produces ACTION_FAILED and does not falsely report success
# ─────────────────────────────────────────────────────────────────────────────

def test_failed_action_event_sequence():
    async def _test():
        events = []
        def callback(evt_type, task, details):
            events.append((evt_type, details))

        llm = MockLLM([
            json.dumps({
                "decision": "execute",
                "reason": "Click search box",
                "action": {
                    "action_type": "click",
                    "element_ref": "elem_001",
                }
            }),
        ])
        adapter = MockAdapter(success=False, error="Element detached from DOM")
        agent = BrowserAgent(
            llm_provider=llm,
            adapter=adapter,
            perception=MockPerception(),
            event_callback=callback,
        )

        task = BrowserTask(task_id="t_fail_1", objective="Click search", session_id="s_fail_1", max_actions=1)
        res = await agent.run(task)

        event_types = [e[0] for e in events]
        assert "action_started" in event_types
        assert "action_failed" in event_types
        assert "action_completed" not in event_types

        failed_event = [e[1] for e in events if e[0] == "action_failed"][0]
        assert "Element detached" in failed_event["error"]

    asyncio.run(_test())


# ─────────────────────────────────────────────────────────────────────────────
# 5. BrowserAgent decision produces structured DECISION_MADE information
# ─────────────────────────────────────────────────────────────────────────────

def test_decision_made_event_structure():
    async def _test():
        events = []
        def callback(evt_type, task, details):
            events.append((evt_type, details))

        llm = MockLLM([
            json.dumps({
                "decision": "execute",
                "reason": "Navigate to Wikipedia URL",
                "action": {
                    "action_type": "navigate",
                    "url": "https://en.wikipedia.org",
                    "new_tab": True,
                }
            }),
            json.dumps({"decision": "finish", "reason": "Loaded page"}),
        ])
        agent = BrowserAgent(
            llm_provider=llm,
            adapter=MockAdapter(),
            perception=MockPerception(),
            event_callback=callback,
        )

        task = BrowserTask(task_id="t_dec_1", objective="Navigate", session_id="s_dec_1")
        with patch("execution.browser.semantic_verifier.SemanticVerifier.verify") as mock_verify:
            mock_verify.return_value = BrowserVerification(
                task_id=task.task_id,
                status=BrowserVerificationStatus.VERIFIED,
            )
            await agent.run(task)

        decision_events = [e[1] for e in events if e[0] == "decision_made"]
        assert len(decision_events) >= 1
        d = decision_events[0]
        assert d["decision"] == "execute"
        assert d["reason"] == "Navigate to Wikipedia URL"
        assert d["action"]["action_type"] == "navigate"
        assert d["action"]["url"] == "https://en.wikipedia.org"
        assert d["action"]["new_tab"] is True

    asyncio.run(_test())


# ─────────────────────────────────────────────────────────────────────────────
# 6. Decision evidence does not expose hidden chain-of-thought
# ─────────────────────────────────────────────────────────────────────────────

def test_decision_evidence_no_chain_of_thought():
    async def _test():
        events = []
        def callback(evt_type, task, details):
            events.append((evt_type, details))

        llm = MockLLM([
            json.dumps({
                "thought": "Internal hidden reasoning that should not be leaked",
                "decision": "finish",
                "reason": "Page loaded completely",
            })
        ])
        agent = BrowserAgent(
            llm_provider=llm,
            adapter=MockAdapter(),
            perception=MockPerception(),
            event_callback=callback,
        )

        task = BrowserTask(task_id="t_cot_1", objective="Check page", session_id="s_cot_1")
        with patch("execution.browser.semantic_verifier.SemanticVerifier.verify") as mock_verify:
            mock_verify.return_value = BrowserVerification(
                task_id=task.task_id,
                status=BrowserVerificationStatus.VERIFIED,
            )
            await agent.run(task)

        decision_events = [e[1] for e in events if e[0] == "decision_made"]
        assert len(decision_events) >= 1
        d = decision_events[0]
        assert "thought" not in d
        assert "chain_of_thought" not in d
        assert d["decision"] == "finish"
        assert d["reason"] == "Page loaded completely"

    asyncio.run(_test())


# ─────────────────────────────────────────────────────────────────────────────
# 7. SemanticVerifier result is observable
# ─────────────────────────────────────────────────────────────────────────────

def test_verification_result_is_observable():
    async def _test():
        events = []
        def callback(evt_type, task, details):
            events.append((evt_type, details))

        llm = MockLLM([
            json.dumps({"decision": "finish", "reason": "Task completed"}),
        ])
        agent = BrowserAgent(
            llm_provider=llm,
            adapter=MockAdapter(),
            perception=MockPerception(url="https://en.wikipedia.org/wiki/AI", title="AI"),
            event_callback=callback,
        )

        task = BrowserTask(task_id="t_ver_1", objective="Search AI", session_id="s_ver_1")
        with patch("execution.browser.semantic_verifier.SemanticVerifier.verify") as mock_verify:
            mock_verify.return_value = BrowserVerification(
                task_id=task.task_id,
                status=BrowserVerificationStatus.VERIFIED,
                verified_by="SemanticVerifier",
                checks_performed=["url_check", "content_check"],
            )
            await agent.run(task)

        ver_events = [e[1] for e in events if e[0] == "verification_completed"]
        assert len(ver_events) == 1
        v = ver_events[0]
        assert v["status"] == "verified"
        assert v["current_url"] == "https://en.wikipedia.org/wiki/AI"
        assert v["page_title"] == "AI"

    asyncio.run(_test())


# ─────────────────────────────────────────────────────────────────────────────
# 8. NOT_VERIFIED does not become TASK_COMPLETED
# ─────────────────────────────────────────────────────────────────────────────

def test_not_verified_does_not_become_completed():
    async def _test():
        events = []
        def callback(evt_type, task, details):
            events.append((evt_type, details))

        llm = MockLLM([
            json.dumps({"decision": "finish", "reason": "I think I am done"}),
        ])
        agent = BrowserAgent(
            llm_provider=llm,
            adapter=MockAdapter(),
            perception=MockPerception(),
            event_callback=callback,
        )

        task = BrowserTask(task_id="t_unver_1", objective="Must reach google.com", session_id="s_unver_1", max_actions=1)
        with patch("execution.browser.semantic_verifier.SemanticVerifier.verify") as mock_verify:
            mock_verify.return_value = BrowserVerification(
                task_id=task.task_id,
                status=BrowserVerificationStatus.NOT_VERIFIED,
                failure_reason="Destination URL mismatch",
                verified_by="SemanticVerifier",
            )
            res = await agent.run(task)

        assert res.outcome == BrowserAgentOutcome.FAILED
        assert res.verification.status == BrowserVerificationStatus.NOT_VERIFIED

        ver_completed = [e[1] for e in events if e[0] == "verification_completed"]
        assert len(ver_completed) >= 1
        assert ver_completed[0]["status"] == "not_verified"
        assert "Destination URL mismatch" in ver_completed[0]["failure_reason"]

    asyncio.run(_test())


# ─────────────────────────────────────────────────────────────────────────────
# 9. Recovery produces observable recovery information
# ─────────────────────────────────────────────────────────────────────────────

def test_recovery_observability():
    async def _test():
        events = []
        def callback(evt_type, task, details):
            events.append((evt_type, details))

        # Step 1: LLM says RECOVER, Step 2: FINISH
        llm = MockLLM([
            json.dumps({"decision": "recover", "reason": "Page was slow, waiting for re-render"}),
            json.dumps({"decision": "finish", "reason": "Now ready"}),
        ])
        agent = BrowserAgent(
            llm_provider=llm,
            adapter=MockAdapter(),
            perception=MockPerception(),
            event_callback=callback,
        )

        task = BrowserTask(task_id="t_rec_1", objective="Wait and finish", session_id="s_rec_1")
        with patch("execution.browser.semantic_verifier.SemanticVerifier.verify") as mock_verify:
            mock_verify.return_value = BrowserVerification(
                task_id=task.task_id,
                status=BrowserVerificationStatus.VERIFIED,
            )
            res = await agent.run(task)

        rec_events = [e[1] for e in events if e[0] == "recovery_started"]
        assert len(rec_events) >= 1
        assert "Page was slow" in rec_events[0]["reason"]
        assert rec_events[0]["attempt"] == 1

    asyncio.run(_test())


# ─────────────────────────────────────────────────────────────────────────────
# 10. Task/session/run identifiers remain correctly correlated
# ─────────────────────────────────────────────────────────────────────────────

def test_session_task_correlation():
    monitor = ExecutionMonitor(session_id="session_corr_A")
    e1 = monitor.on_task_started("task_A_1", "Task A1")
    e2 = monitor.on_observation_received("task_A_1", "Task A1", {"url": "https://a.com"})
    e3 = monitor.on_task_completed("task_A_1", "Task A1")

    assert e1.session_id == "session_corr_A"
    assert e2.session_id == "session_corr_A"
    assert e3.session_id == "session_corr_A"
    assert e1.task_id == "task_A_1"
    assert e2.task_id == "task_A_1"
    assert e3.task_id == "task_A_1"


# ─────────────────────────────────────────────────────────────────────────────
# 11. Sensitive action input is redacted from events
# ─────────────────────────────────────────────────────────────────────────────

def test_sensitive_data_redaction():
    raw_details = {
        "action": "fill",
        "password": "SuperSecretPassword123!",
        "api_key": "AIzaSySecretToken",
        "auth_token": "Bearer abc.def.ghi",
        "nested": {
            "cookie": "session=secret_cookie_val",
            "safe_field": "public_username",
        },
        "safe_number": 42,
    }

    redacted = redact_sensitive_details(raw_details)
    assert redacted["password"] == "[REDACTED]"
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["auth_token"] == "[REDACTED]"
    assert redacted["nested"]["cookie"] == "[REDACTED]"
    assert redacted["nested"]["safe_field"] == "public_username"
    assert redacted["safe_number"] == 42


# ─────────────────────────────────────────────────────────────────────────────
# 12. Existing WebSocket/frontend event consumers remain compatible
# ─────────────────────────────────────────────────────────────────────────────

def test_websocket_frontend_contract_compatibility():
    monitor = ExecutionMonitor(session_id="session_ws_1")
    evt = monitor.on_task_started("task_ws_1", "Open browser")
    d = evt.to_dict()

    # Core required fields for frontend consumers
    assert "event_type" in d
    assert "session_id" in d
    assert "task_id" in d
    assert "task_name" in d
    assert "status" in d
    assert "details" in d
    assert "timestamp" in d
    assert isinstance(d["timestamp"], float)


# ─────────────────────────────────────────────────────────────────────────────
# 13. Non-browser task events continue to work in RuntimeEngine
# ─────────────────────────────────────────────────────────────────────────────

def test_non_browser_task_events_in_runtime():
    async def _test():
        graph = ARDOGraph()
        node = ARDOTaskNode(
            task_id="task_generic_1",
            task_name="Calculate mathematical hash",
            dependency_ids=[],
        )
        graph.nodes[node.task_id] = node
        graph.adjacency[node.task_id] = []
        graph.reverse_adj[node.task_id] = []

        engine = RuntimeEngine(session_id="s_gen_1", graph=graph)
        events = []
        async for event in engine.run():
            events.append(event)

        types = [e.event_type for e in events]
        assert "task_started" in types
        assert "task_completed" in types
        assert "session_complete" in types
        assert node.status == TaskState.COMPLETED

    asyncio.run(_test())
