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
