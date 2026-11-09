"""
test_phase16_integration_fixes.py — Regression Tests for Live Integration Fixes
================================================================================
Validates:
1. BrowserAgent receives valid session page on cold start without requiring manual pre-launch.
2. ARDOSession-scoped browser task executes without page=None errors.
3. Approval: approve_task unblocks/resumes task from APPROVAL_NEEDED to execution and completion.
4. Approval: deny_task transitions task to BLOCKED and cascades dependency blocks.
5. Approval: duplicate approve/deny calls are handled safely and idempotently.
6. Serialization: BrowserAgentResult, BrowserVerification, and ARDOTaskNode serialize cleanly to dict.
7. Persistence: persistence exception does not fail the execution outcome.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock
import pytest

from execution.browser.agent_contracts import (
    BrowserActionDecision,
    BrowserAgentOutcome,
    BrowserAgentResult,
    BrowserObservation,
    BrowserTask,
    BrowserVerification,
    BrowserVerificationStatus,
)
from execution.browser.browser_agent import BrowserAgent
from execution.browser.browser_models import BrowserActionType
from execution.browser.browser_task_adapter import BrowserTaskAdapter
from execution.browser.playwright_executor import PlaywrightBrowserExecutor
from orchestration.dag_builder import ARDOGraph, ARDOTaskNode
from orchestration.runtime_engine import RuntimeEngine
from orchestration.scheduler import ARDOSession, ARDOScheduler
from orchestration.state_machine import TaskState


class MockDecisionProvider:
    def __init__(self, decisions):
        self._decisions = list(decisions)
        self._idx = 0

    async def query(self, prompt: str, system_instruction: str = "", json_mode: bool = False) -> str:
        import json
        if self._idx < len(self._decisions):
            d = self._decisions[self._idx]
            self._idx += 1
            return json.dumps(d)
        return json.dumps({"decision": "finish", "reason": "Default finish"})


def test_browser_agent_cold_start_initializes_page():
    """Bug 1 Fix: BrowserAgent.run() starts the browser adapter/executor if cold, avoiding page=None."""
    async def _test():
        executor = PlaywrightBrowserExecutor(headless=True)
        adapter = BrowserTaskAdapter(executor=executor)
        # Verify executor is not started initially
        assert not executor.is_running

        llm = MockDecisionProvider([
            {"decision": "execute", "reason": "Nav", "action": {"action_type": "navigate", "url": "https://example.com"}},
            {"decision": "finish", "reason": "Done"},
        ])
        agent = BrowserAgent(llm_provider=llm, adapter=adapter)
        task = BrowserTask(task_id="cold_start_task", objective="Open example.com", session_id="sess_cold")

        try:
            res = await agent.run(task)
            assert res.outcome == BrowserAgentOutcome.COMPLETED
            assert executor.is_running
            assert adapter.get_page() is not None
        finally:
            await executor.stop()

    asyncio.run(_test())


def test_browser_task_via_runtime_engine_cold_start():
    """Bug 1 Fix: RuntimeEngine executing a browser task from cold ARDOSession initializes browser without page=None."""
    async def _test():
        graph = ARDOGraph()
        node = ARDOTaskNode(
            task_id="b_task_1",
            task_name="Navigate to https://example.com",
            dependency_ids=[],
        )
        graph.nodes[node.task_id] = node

        executor = PlaywrightBrowserExecutor(headless=True)
        adapter = BrowserTaskAdapter(executor=executor)

        engine = RuntimeEngine(
            graph=graph,
            session_id="sess_rt_cold",
            mode="sequential",
            browser_adapter=adapter,
        )

        llm = MockDecisionProvider([
            {"decision": "execute", "reason": "Nav", "action": {"action_type": "navigate", "url": "https://example.com"}},
            {"decision": "finish", "reason": "Done"},
        ])
        agent = BrowserAgent(llm_provider=llm, adapter=adapter)
        engine._browser_agent = agent

        try:
            events = []
            async for evt in engine.run():
                events.append(evt)

            assert node.status == TaskState.COMPLETED
            assert node.browser_result is not None
            assert node.browser_result.outcome == BrowserAgentOutcome.COMPLETED
        finally:
            await engine.cleanup()

    asyncio.run(_test())


def test_approval_approve_resumes_and_completes_task(monkeypatch):
    """Bug 2 Fix: approve_task unblocks an APPROVAL_NEEDED task, transitions it to WAITING->ACTIVE, and completes it."""
    monkeypatch.setenv("AUTH_DB_BACKEND", "sqlite")
    from core.auth_db import AuthDatabase
    auth_db = AuthDatabase.reset_instance(force_sqlite=True)
    user = auth_db.create_user(
        full_name="Operator",
        email="operator_phase16@ardo.ai",
        password_hash="test_hash",
        is_active=True,
        is_verified=True,
    )

    async def _test():
        graph = ARDOGraph()
        node_gated = ARDOTaskNode(
            task_id="gated_task",
            task_name="Approve-required task",
            dependency_ids=[],
            approval_needed=True,
            risk_score=0.9,
        )
        node_dep = ARDOTaskNode(
            task_id="dep_task",
            task_name="Dependent task",
            dependency_ids=["gated_task"],
        )
        for n in [node_gated, node_dep]:
            graph.nodes[n.task_id] = n
            graph.adjacency[n.task_id] = []
            graph.reverse_adj[n.task_id] = list(n.dependency_ids)
        graph.adjacency["gated_task"] = ["dep_task"]

        engine = RuntimeEngine(graph=graph, session_id="sess_approve_resume", mode="sequential")

        events = []
        approved = False

        async def _approve_after_delay():
            nonlocal approved
            await asyncio.sleep(0.5)
            auth_db.record_plan_approval(
                session_id="sess_approve_resume",
                user_id=user["id"],
                plan_hash=graph.recompute_plan_hash(engine.goal_id),
                task_id="gated_task",
                status="approved",
            )
            engine.approve_task("gated_task")
            approved = True

        asyncio.create_task(_approve_after_delay())

        async for evt in engine.run():
            events.append(evt)

        assert approved
        assert node_gated.status == TaskState.COMPLETED
        assert node_dep.status == TaskState.COMPLETED
        event_types = [e.event_type for e in events]
        assert "task_approval_needed" in event_types
        assert "task_approved" in event_types

    asyncio.run(_test())


def test_approval_deny_blocks_task_and_cascades():
    """Bug 2 Fix: deny_task marks task as BLOCKED and cascades BLOCKED to dependent tasks."""
    async def _test():
        graph = ARDOGraph()
        node_gated = ARDOTaskNode(
            task_id="deny_gated",
            task_name="Dangerous operation",
            dependency_ids=[],
            approval_needed=True,
            risk_score=0.95,
        )
        node_dep = ARDOTaskNode(
            task_id="deny_dep",
            task_name="Downstream operation",
            dependency_ids=["deny_gated"],
        )
        for n in [node_gated, node_dep]:
            graph.nodes[n.task_id] = n
            graph.adjacency[n.task_id] = []
            graph.reverse_adj[n.task_id] = list(n.dependency_ids)
        graph.adjacency["deny_gated"] = ["deny_dep"]

        engine = RuntimeEngine(graph=graph, session_id="sess_deny_cascade", mode="sequential")

        events = []
        denied = False

        async def _deny_after_delay():
            nonlocal denied
            await asyncio.sleep(1.0)
            engine.deny_task("deny_gated")
            denied = True

        asyncio.create_task(_deny_after_delay())

        async for evt in engine.run():
            events.append(evt)

        assert denied
        assert node_gated.status == TaskState.BLOCKED
        assert node_dep.status == TaskState.BLOCKED

    asyncio.run(_test())


def test_duplicate_approval_handling():
    """Bug 2 Fix: Repeated approve_task / deny_task calls are idempotent and do not crash."""
    graph = ARDOGraph()
    node = ARDOTaskNode(
        task_id="dup_test",
        task_name="Gated task",
        dependency_ids=[],
        approval_needed=True,
    )
    graph.nodes[node.task_id] = node
    engine = RuntimeEngine(graph=graph, session_id="sess_dup_approve", mode="sequential")

    # Calling approve multiple times
    engine.approve_task("dup_test")
    engine.approve_task("dup_test")
    assert node.status == TaskState.WAITING
    assert not node.approval_needed

    # Calling deny multiple times
    node2 = ARDOTaskNode(task_id="dup_test_2", task_name="Gated task 2", dependency_ids=[], approval_needed=True)
    graph.nodes[node2.task_id] = node2
    engine.deny_task("dup_test_2")
    engine.deny_task("dup_test_2")
    assert node2.status == TaskState.BLOCKED


def test_browser_agent_result_and_task_node_serialization():
    """Bug 3 Fix: BrowserAgentResult, BrowserVerification, and ARDOTaskNode.to_dict() produce clean JSON/BSON serializable dicts."""
    verification = BrowserVerification(
        task_id="t_ser_1",
        status=BrowserVerificationStatus.VERIFIED,
        observed_url="https://example.com",
        observed_title="Example Domain",
        verified_by="URL match",
    )
    action_decision = BrowserActionDecision(
        action_type=BrowserActionType.NAVIGATE,
        url="https://example.com",
        new_tab=True,
        reasoning="Navigating to destination",
    )
    agent_res = BrowserAgentResult(
        task_id="t_ser_1",
        session_id="sess_ser_1",
        outcome=BrowserAgentOutcome.COMPLETED,
        objective="Open example.com",
        actions_taken=[action_decision],
        verification=verification,
        actions_count=1,
        duration_ms=250.0,
        final_url="https://example.com",
        final_title="Example Domain",
    )

    # 1. BrowserAgentResult.to_dict()
    res_dict = agent_res.to_dict()
    assert isinstance(res_dict, dict)
    assert res_dict["outcome"] == "completed"
    assert res_dict["verification"]["status"] == "verified"
    assert res_dict["actions_taken"][0]["new_tab"] is True

    # 2. ARDOTaskNode.to_dict()
    node = ARDOTaskNode(
        task_id="t_ser_1",
        task_name="Open example.com",
        browser_result=agent_res,
    )
    node_dict = node.to_dict()
    assert isinstance(node_dict, dict)
    assert isinstance(node_dict["browser_result"], dict)
    assert node_dict["browser_result"]["outcome"] == "completed"
    assert node_dict["browser_result"]["final_url"] == "https://example.com"


def test_persistence_failure_does_not_alter_task_outcome():
    """Bug 3 Fix: A database persistence failure logs an error but does not fail or alter task execution."""
    async def _test():
        graph = ARDOGraph()
        node = ARDOTaskNode(
            task_id="task_persist_fail",
            task_name="Standard task",
            dependency_ids=[],
        )
        graph.nodes[node.task_id] = node

        # Mock DB that throws on update_one
        mock_db = MagicMock()
        mock_db.task_runtime_state.update_one = AsyncMock(side_effect=Exception("MongoDB Connection Dropped"))
        mock_db.execution_metrics.update_one = AsyncMock(side_effect=Exception("MongoDB Connection Dropped"))
        mock_db.replay_sessions.update_one = AsyncMock(side_effect=Exception("MongoDB Connection Dropped"))

        engine = RuntimeEngine(
            graph=graph,
            session_id="sess_persist_fail",
            mode="sequential",
            db=mock_db,
        )

        events = []
        async for evt in engine.run():
            events.append(evt)

        # Execution completes successfully despite DB errors
        assert node.status == TaskState.COMPLETED
        event_types = [e.event_type for e in events]
        assert "session_complete" in event_types

    asyncio.run(_test())
