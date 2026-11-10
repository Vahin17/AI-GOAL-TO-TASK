import pytest
from execution.browser.agent_contracts import (
    BrowserTask,
    BrowserObservation,
    BrowserVerificationStatus,
    BrowserAgentResult,
    BrowserAgentOutcome,
    BrowserVerification,
)
from execution.browser.semantic_verifier import SemanticVerifier
from orchestration.dag_builder import ARDOTaskNode, ARDOGraph
from orchestration.state_machine import TaskState


def test_media_playback_verified_when_video_playing():
    task = BrowserTask(
        task_id="t3",
        objective="Play song Yeh Fitoor Mera",
        session_id="s1",
    )
    obs = BrowserObservation(
        session_id="s1",
        task_id="t3",
        current_url="https://www.youtube.com/watch?v=fitoor123",
        page_title="Yeh Fitoor Mera - Official Video - YouTube",
        media_state=[{
            "tag": "video",
            "paused": False,
            "currentTime": 4.5,
            "duration": 280.0,
            "readyState": 4,
        }],
    )
    v = SemanticVerifier.verify(task, obs)
    assert v.status == BrowserVerificationStatus.VERIFIED
    assert v.confidence_score >= 0.90
    assert "HTML5 media playback verified" in v.verified_by


def test_media_playback_not_verified_when_paused():
    task = BrowserTask(
        task_id="t3",
        objective="Play song Yeh Fitoor Mera",
        session_id="s1",
    )
    obs = BrowserObservation(
        session_id="s1",
        task_id="t3",
        current_url="https://www.youtube.com/watch?v=fitoor123",
        page_title="Yeh Fitoor Mera - YouTube",
        media_state=[{
            "tag": "video",
            "paused": True,
            "currentTime": 0.0,
            "duration": 280.0,
            "readyState": 4,
        }],
    )
    v = SemanticVerifier.verify(task, obs)
    assert v.status == BrowserVerificationStatus.NOT_VERIFIED
    assert "not actively playing" in v.failure_reason


def test_media_playback_not_verified_when_no_media_element():
    task = BrowserTask(
        task_id="t3",
        objective="Play song Yeh Fitoor Mera",
        session_id="s1",
    )
    # Still on search results page without any video element playing
    obs = BrowserObservation(
        session_id="s1",
        task_id="t3",
        current_url="https://www.youtube.com/results?search_query=yeh+fitoor+mera",
        page_title="Yeh Fitoor Mera - YouTube",
        page_text_summary="Yeh Fitoor Mera official song video",
        media_state=[],
    )
    v = SemanticVerifier.verify(task, obs)
    assert v.status == BrowserVerificationStatus.NOT_VERIFIED
    assert "No media element found" in v.failure_reason


def test_goal_outcome_confidence_not_hardcoded_to_95():
    # If all tasks are completed with 0.70 confidence, goal confidence should reflect ~0.70, NOT 0.95!
    node1 = ARDOTaskNode(
        task_id="task_1",
        task_name="Open YouTube",
        status=TaskState.COMPLETED,
    )
    res1 = BrowserAgentResult(
        task_id="task_1",
        session_id="s1",
        outcome=BrowserAgentOutcome.COMPLETED,
        objective="Open YouTube",
        actions_taken=[],
        verification=BrowserVerification(
            task_id="task_1",
            status=BrowserVerificationStatus.VERIFIED,
            confidence_score=0.70,
        ),
    )
    node1.browser_result = res1

    node2 = ARDOTaskNode(
        task_id="task_2",
        task_name="Search for Yeh Fitoor Mera",
        status=TaskState.COMPLETED,
    )
    res2 = BrowserAgentResult(
        task_id="task_2",
        session_id="s1",
        outcome=BrowserAgentOutcome.COMPLETED,
        objective="Search for Yeh Fitoor Mera",
        actions_taken=[],
        verification=BrowserVerification(
            task_id="task_2",
            status=BrowserVerificationStatus.VERIFIED,
            confidence_score=0.70,
        ),
    )
    node2.browser_result = res2

    graph = ARDOGraph(nodes={"task_1": node1, "task_2": node2})

    goal_res = SemanticVerifier.verify_goal_outcome(
        goal_text="Play Yeh Fitoor Mera on YouTube",
        graph=graph,
        session_id="s1",
    )

    assert goal_res.overall_status == "verified"
    # Previously was hardcoded to 0.95! Now must accurately reflect aggregate ~0.70!
    assert goal_res.confidence_score == 0.70
    assert "0.95" not in str(goal_res.confidence_score)
