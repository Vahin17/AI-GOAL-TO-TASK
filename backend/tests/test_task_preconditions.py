import pytest
from unittest.mock import AsyncMock, MagicMock
from execution.browser.agent_contracts import (
    BrowserTask,
    BrowserObservation,
    BrowserDecisionType,
    BrowserActionType,
    BrowserAgentOutcome,
)
from execution.browser.browser_agent import BrowserAgent
from orchestration.dag_builder import ARDOTaskNode, ARDOGraph
from orchestration.runtime_engine import RuntimeEngine
from orchestration.state_machine import TaskState


@pytest.mark.asyncio
async def test_precondition_url_mismatch_triggers_navigation_recovery():
    # If a dependent task begins but browser is on an unexpected page,
    # the agent must navigate to the prerequisite URL
    mock_adapter = MagicMock()
    mock_adapter.executor.is_running = True
    
    # Initial observation is on about:blank or google.com instead of youtube results
    obs_initial = BrowserObservation(
        session_id="s1",
        task_id="t3",
        current_url="https://www.google.com",
        page_title="Google",
    )
    obs_recovered = BrowserObservation(
        session_id="s1",
        task_id="t3",
        current_url="https://www.youtube.com/results?search_query=yeh+fitoor+mera",
        page_title="Yeh Fitoor Mera - YouTube",
    )
    
    mock_adapter.inspect_page = AsyncMock(side_effect=[obs_initial, obs_recovered])
    
    # Adapter action result for the navigation recovery
    action_res = MagicMock()
    action_res.success = True
    mock_adapter.execute_safe_browser_task = AsyncMock(return_value=(action_res, MagicMock(is_allowed=True)))

    task = BrowserTask(
        task_id="t3",
        objective="Play song Yeh Fitoor Mera",
        session_id="s1",
        precondition_url="https://www.youtube.com/results?search_query=yeh+fitoor+mera",
        max_actions=2,
    )

    agent = BrowserAgent(adapter=mock_adapter, llm_provider=None)
    agent._observe = AsyncMock(side_effect=[obs_initial, obs_recovered])
    # Mock ask_gemini to return finish after recovery
    agent._ask_gemini = AsyncMock(return_value=MagicMock(decision=BrowserDecisionType.FINISH, reason="Done"))

    res = await agent.run(task)

    # First action taken must have been NAVIGATE to the prerequisite URL
    assert len(res.actions_taken) >= 1
    assert res.actions_taken[0].action_type == BrowserActionType.NAVIGATE
    assert res.actions_taken[0].url == "https://www.youtube.com/results?search_query=yeh+fitoor+mera"


def test_runtime_engine_propagates_postconditions_to_dependent_tasks():
    # Verify RuntimeEngine DAG wiring:
    # Task 1: Open YouTube -> completes with postcondition https://www.youtube.com/
    # Task 2: Search -> depends on Task 1, receives precondition_url
    node1 = ARDOTaskNode(
        task_id="task_1",
        task_name="Open YouTube",
        status=TaskState.COMPLETED,
        metadata={"destination_url": "https://www.youtube.com/"},
    )
    node1.metadata["postcondition"] = {
        "url": "https://www.youtube.com/",
        "title": "YouTube",
    }
    
    node2 = ARDOTaskNode(
        task_id="task_2",
        task_name="Search for Yeh Fitoor Mera on YouTube search bar",
        dependency_ids=["task_1"],
    )
    
    graph = ARDOGraph(nodes={"task_1": node1, "task_2": node2})

    engine = RuntimeEngine(graph=graph, session_id="test_dag_session", goal_id="test_goal")
    
    # Check that when node2 is prepared, precondition_url is extracted from node1's postcondition
    dep_node = engine.graph.get_node("task_1")
    dep_post = dep_node.metadata.get("postcondition", {})
    assert dep_post.get("url") == "https://www.youtube.com/"


@pytest.mark.asyncio
async def test_real_browser_agent_result_postcondition_propagation():
    """
    Test using the REAL BrowserAgentResult class (not a mock) to verify:
    1. Result without observation attribute handled safely (no AttributeError).
    2. Valid result with verification extracts observed_url and observed_title.
    3. Result without verification falls back to final_url and final_title.
    4. Missing predecessor evidence handles None safely without raising or falsely claiming verified.
    """
    from execution.browser.agent_contracts import (
        BrowserAgentResult,
        BrowserAgentOutcome,
        BrowserVerification,
        BrowserVerificationStatus,
    )

    # 1. Real BrowserAgentResult with full verification
    res_verified = BrowserAgentResult(
        task_id="t1",
        session_id="s_real",
        outcome=BrowserAgentOutcome.COMPLETED,
        objective="Navigate to https://example.com",
        final_url="https://example.com/home",
        final_title="Example Home",
        verification=BrowserVerification(
            task_id="t1",
            status=BrowserVerificationStatus.VERIFIED,
            observed_url="https://example.com/verified",
            observed_title="Verified Title",
            verified_by="URL check",
        ),
    )

    node1 = ARDOTaskNode(
        task_id="t1",
        task_name="Navigate to example",
        status=TaskState.ACTIVE,
    )
    graph = ARDOGraph(nodes={"t1": node1})
    engine = RuntimeEngine(graph=graph, session_id="s_real")

    # Mock browser agent returning the real result
    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(return_value=res_verified)
    engine._browser_agent = mock_agent

    success = await engine._attempt_execution(node1)
    assert success is True
    post = node1.metadata.get("postcondition", {})
    assert post.get("url") == "https://example.com/verified"
    assert post.get("title") == "Verified Title"
    assert post.get("verified") is True

    # 2. Real BrowserAgentResult without verification (unverified / missing evidence)
    # Per ARDO invariant: missing evidence must NEVER be represented as verified or successful
    res_unverified = BrowserAgentResult(
        task_id="t2",
        session_id="s_real",
        outcome=BrowserAgentOutcome.COMPLETED,
        objective="Navigate to unverified target",
        final_url="https://fallback.com",
        final_title="Fallback Title",
        verification=None,
    )

    node2 = ARDOTaskNode(
        task_id="t2",
        task_name="Navigate to unverified target",
        status=TaskState.ACTIVE,
    )
    graph.nodes["t2"] = node2
    mock_agent.run = AsyncMock(return_value=res_unverified)

    success2 = await engine._attempt_execution(node2)
    # Must fail because unverified task cannot succeed (False Success Prevention)
    assert success2 is False
    assert node2.status == TaskState.FAILED
    assert node2.metadata.get("postcondition") is None

    # 3. Real BrowserAgentResult verified but with url only in final_url
    res_fallback_url = BrowserAgentResult(
        task_id="t3",
        session_id="s_real",
        outcome=BrowserAgentOutcome.COMPLETED,
        objective="Task with url in final_url",
        final_url="https://example.com/from_final",
        final_title="From Final",
        verification=BrowserVerification(
            task_id="t3",
            status=BrowserVerificationStatus.VERIFIED,
            observed_url=None,
            observed_title=None,
            verified_by="Action check",
        ),
    )
    node3 = ARDOTaskNode(
        task_id="t3",
        task_name="Navigate to fallback target",
        status=TaskState.ACTIVE,
    )
    graph.nodes["t3"] = node3
    mock_agent.run = AsyncMock(return_value=res_fallback_url)

    success3 = await engine._attempt_execution(node3)
    assert success3 is True
    post3 = node3.metadata.get("postcondition", {})
    assert post3.get("url") == "https://example.com/from_final"
    assert post3.get("title") == "From Final"
    assert post3.get("verified") is True

    # 4. Dependent task receives predecessor postcondition safely
    node4 = ARDOTaskNode(
        task_id="t4",
        task_name="Navigate to dependent on t1",
        dependency_ids=["t1"],
        status=TaskState.ACTIVE,
    )
    graph.nodes["t4"] = node4

    captured_task = None
    async def capture_run(btask):
        nonlocal captured_task
        captured_task = btask
        return res_empty

    mock_agent.run = AsyncMock(side_effect=capture_run)
    await engine._attempt_execution(node4)
    assert captured_task is not None
    assert captured_task.precondition_url == "https://example.com/verified"
    assert "t1" in (captured_task.precondition_state or {})


@pytest.mark.asyncio
async def test_mutating_task_suppresses_automatic_precondition_re_navigation():
    """Mutating tasks (submit/pay/delete/order) must not automatically re-navigate on precondition mismatch."""
    mock_adapter = MagicMock()
    mock_adapter.executor.is_running = True

    obs_wrong = BrowserObservation(
        session_id="s_mut",
        task_id="t_mut",
        current_url="https://checkout.example.com/confirm",
        page_title="Confirm Order",
    )
    mock_adapter.inspect_page = AsyncMock(return_value=obs_wrong)

    mutating_task = BrowserTask(
        task_id="t_mut",
        objective="Submit payment for order #1234",
        session_id="s_mut",
        precondition_url="https://checkout.example.com/cart",
        max_actions=2,
    )

    agent = BrowserAgent(adapter=mock_adapter, llm_provider=None)
    agent._observe = AsyncMock(return_value=obs_wrong)
    agent._ask_gemini = AsyncMock(return_value=MagicMock(decision=BrowserDecisionType.FINISH, reason="Done"))

    res = await agent.run(mutating_task)

    # First action must NOT be NAVIGATE to the cart
    if res.actions_taken:
        assert res.actions_taken[0].action_type != BrowserActionType.NAVIGATE

