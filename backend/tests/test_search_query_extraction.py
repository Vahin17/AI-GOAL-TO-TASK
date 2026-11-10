import pytest
from execution.browser.agent_contracts import BrowserTask, BrowserObservation, ObservedElement, TabInfo
from execution.browser.browser_agent import BrowserAgent


def test_search_query_extraction_from_metadata():
    task = BrowserTask(
        task_id="t1",
        objective="Search something else",
        session_id="s1",
        metadata={"search_query": "Yeh Fitoor Mera"},
    )
    assert BrowserAgent._extract_search_intent(task) == "Yeh Fitoor Mera"

    task2 = BrowserTask(
        task_id="t2",
        objective="Search something else",
        session_id="s1",
        metadata={"query": "Yeh Fitoor Mera"},
    )
    assert BrowserAgent._extract_search_intent(task2) == "Yeh Fitoor Mera"


def test_search_query_extraction_with_incidental_ui_words():
    # Observed defect in live trial: "Search for Yeh Fitoor Mera on YouTube search bar" -> was returning "bar"
    task = BrowserTask(
        task_id="t1",
        objective="Search for Yeh Fitoor Mera on YouTube search bar",
        session_id="s1",
    )
    assert BrowserAgent._extract_search_intent(task) == "Yeh Fitoor Mera"


def test_search_query_extraction_quoted():
    task = BrowserTask(
        task_id="t1",
        objective='Search for "Yeh Fitoor Mera" on YouTube',
        session_id="s1",
    )
    assert BrowserAgent._extract_search_intent(task) == "Yeh Fitoor Mera"

    task2 = BrowserTask(
        task_id="t2",
        objective="Search 'Yeh Fitoor Mera' inside YouTube search bar",
        session_id="s1",
    )
    assert BrowserAgent._extract_search_intent(task2) == "Yeh Fitoor Mera"


def test_search_query_extraction_variations():
    cases = [
        ("Search YouTube for Yeh Fitoor Mera", "Yeh Fitoor Mera"),
        ("In YouTube, search for Yeh Fitoor Mera", "Yeh Fitoor Mera"),
        ("Search Yeh Fitoor Mera on YouTube", "Yeh Fitoor Mera"),
        ("Find Yeh Fitoor Mera on YouTube", "Yeh Fitoor Mera"),
        ("Search for song Yeh Fitoor Mera", "Yeh Fitoor Mera"),
        ("Search for Yeh Fitoor Mera video", "Yeh Fitoor Mera"),
    ]
    for obj, expected in cases:
        task = BrowserTask(task_id="t", objective=obj, session_id="s")
        assert BrowserAgent._extract_search_intent(task) == expected, f"Failed on: {obj}"


def test_incidental_and_ui_element_names_rejected():
    # Never extract bare UI element names as search queries
    bad_objectives = [
        "Search bar",
        "searchbar",
        "searchbox",
        "search box",
        "Search input",
        "input field",
        "bar",
        "Search",
    ]
    for obj in bad_objectives:
        task = BrowserTask(task_id="t", objective=obj, session_id="s")
        assert BrowserAgent._extract_search_intent(task) is None, f"Should reject: {obj}"


def test_downstream_media_and_compound_tasks_preserve_full_reasoning():
    # If the task is video playback or multi-step compound, do not fast-path as a simple search
    complex_tasks = [
        "Search for Yeh Fitoor Mera and play the song",
        "Open YouTube, search for Yeh Fitoor Mera and play",
        "Play song Yeh Fitoor Mera",
        "Watch video Yeh Fitoor Mera on YouTube",
    ]
    for obj in complex_tasks:
        task = BrowserTask(task_id="t", objective=obj, session_id="s")
        assert BrowserAgent._extract_search_intent(task) is None, f"Should not fast-path: {obj}"


def test_is_search_results_state_rejects_stale_bar_results():
    agent = BrowserAgent(adapter=None, llm_provider=None)

    obs_bar = BrowserObservation(
        session_id="s1",
        task_id="t1",
        current_url="https://www.youtube.com/results?search_query=bar",
        page_title="bar - YouTube",
        page_text_summary="Results for bar",
    )
    # Stale 'bar' results must NOT satisfy 'Yeh Fitoor Mera' query
    assert not agent._is_search_results_state("Yeh Fitoor Mera", obs_bar)

    obs_valid = BrowserObservation(
        session_id="s1",
        task_id="t1",
        current_url="https://www.youtube.com/results?search_query=yeh+fitoor+mera",
        page_title="Yeh Fitoor Mera - YouTube",
        page_text_summary="Yeh Fitoor Mera official song video",
    )
    assert agent._is_search_results_state("Yeh Fitoor Mera", obs_valid)
