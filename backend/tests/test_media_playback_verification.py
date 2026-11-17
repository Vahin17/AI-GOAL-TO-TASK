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


def test_playback_intent_detection_negative_cases():
    """
    Terms containing 'play' as substrings or distinct nouns like 'display',
    'playlist', 'google play', 'play store', 'playwright', 'coldplay',
    must NOT trigger media playback verification.
    """
    cases = [
        "Display the search results for query",
        "Open my favorite playlist",
        "Navigate to Google Play store to view apps",
        "Download app from play store",
        "Run playwright automated test",
        "Look at the gameplay video screenshots",
        "Roleplay forum discussion",
        "Search for Coldplay concert dates",
    ]
    for text in cases:
        task = BrowserTask(task_id="t_test", objective=text, session_id="s1")
        assert SemanticVerifier._has_playback_intent(task) is False, f"False positive playback intent for: {text}"


def test_playback_intent_detection_positive_cases():
    """
    Explicit playback objectives must trigger media playback verification.
    """
    cases = [
        "Play song Yeh Fitoor Mera",
        "Play the video on YouTube",
        "Search YouTube for Vande Mataram and play the song",
        "Start playing audio track",
        "Ensure media playback is active",
        "Listen to music album",
        "Play the music video",
    ]
    for text in cases:
        task = BrowserTask(task_id="t_test", objective=text, session_id="s1")
        assert SemanticVerifier._has_playback_intent(task) is True, f"Failed to detect playback intent for: {text}"


def test_media_playback_insufficient_media_state():
    """
    If readyState == 0 or currentTime == 0.0 even if paused is False,
    media playback must NOT be verified.
    """
    task = BrowserTask(
        task_id="t_media",
        objective="Play song Yeh Fitoor Mera",
        session_id="s1",
    )
    # Case 1: currentTime == 0.0
    obs_zero = BrowserObservation(
        session_id="s1",
        task_id="t_media",
        current_url="https://www.youtube.com/watch?v=123",
        page_title="Yeh Fitoor Mera",
        media_state=[{
            "tag": "video",
            "paused": False,
            "currentTime": 0.0,
            "duration": 200.0,
            "readyState": 4,
        }],
    )
    v1 = SemanticVerifier.verify(task, obs_zero)
    assert v1.status == BrowserVerificationStatus.NOT_VERIFIED
    assert "currentTime == 0" in v1.failure_reason or "not actively playing" in v1.failure_reason

    # Case 2: readyState == 0 (HAVE_NOTHING)
    obs_not_ready = BrowserObservation(
        session_id="s1",
        task_id="t_media",
        current_url="https://www.youtube.com/watch?v=123",
        page_title="Yeh Fitoor Mera",
        media_state=[{
            "tag": "video",
            "paused": False,
            "currentTime": 2.5,
            "duration": 200.0,
            "readyState": 0,
        }],
    )
    v2 = SemanticVerifier.verify(task, obs_not_ready)
    assert v2.status == BrowserVerificationStatus.NOT_VERIFIED
    assert "readyState" in v2.failure_reason or "not actively playing" in v2.failure_reason

