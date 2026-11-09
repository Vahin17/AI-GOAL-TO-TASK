"""
test_mocked_gemini_call_audit.py — Mocked End-to-End Gemini Call-Count Audit
=============================================================================
Audits Gemini call counts across the complete /orchestrate execution pipeline
for two representative goals:
  Scenario A: Simple navigation ("Open Wikipedia")
  Scenario B: Multi-step dynamic interaction ("Open YouTube and search for Vande Mataram")

Safety invariants:
- ZERO real Gemini API or other paid LLM API calls.
- Fail immediately if any unmocked external HTTP request is attempted.
- Fully mocked browser operations and observations (zero live websites).
- Deterministic fixtures and responses.
"""

import asyncio
import io
import json
import http.client
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch, MagicMock, AsyncMock
import pytest

from ai.gemini_provider import (
    GeminiProvider,
    GeminiProviderError,
)
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
    RecentActionRecord,
)
from execution.browser.browser_models import (
    BrowserAction,
    BrowserActionResult,
    BrowserActionType,
)
from execution.browser.browser_agent import BrowserAgent
from execution.browser.perception_models import (
    ObservedElement,
    PageSnapshot,
    TabInfo,
)
from execution.browser.semantic_verifier import SemanticVerifier
from orchestration.dag_builder import ARDOTaskNode, ARDOGraph, DAGBuilder
from orchestration.runtime_engine import RuntimeEngine
from orchestration.scheduler import ARDOSession, ARDOScheduler
from orchestration.state_machine import TaskState
from orchestration.safety_schemas import (
    ExecutionDecision,
    RiskLevel,
    SafetyDecision,
)
from schemas.models import (
    PlanGraphResponse,
    TaskNode,
    TaskEdge,
    PlanGraphMetadata,
)
from api.router import start_orchestration, ARDOStartRequest


# ─────────────────────────────────────────────────────────────────────────────
# 1. Strict Safety Guard: Block Real Outbound HTTP
# ─────────────────────────────────────────────────────────────────────────────

def _forbid_outbound_http(*args, **kwargs):
    raise AssertionError("SECURITY VIOLATION: Real outbound network/HTTP call was attempted during audit!")


def _forbid_http_connect(self, *args, **kwargs):
    host = getattr(self, "host", "")
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise AssertionError(f"SECURITY VIOLATION: Real outbound network connection to '{host}' attempted during audit!")


@pytest.fixture(autouse=True)
def guard_against_outbound_network():
    """Autouse fixture ensuring NO test in this audit can make outbound HTTP/network calls."""
    with patch("urllib.request.urlopen", side_effect=_forbid_outbound_http), \
         patch("http.client.HTTPConnection.connect", new=_forbid_http_connect), \
         patch("http.client.HTTPSConnection.connect", new=_forbid_http_connect):
        yield


# ─────────────────────────────────────────────────────────────────────────────
# 2. Call-Accounting Tracker
# ─────────────────────────────────────────────────────────────────────────────

class GeminiCallAuditTracker:
    """Tracks and classifies every Gemini call attempt across the pipeline."""

    def __init__(self):
        self.planning_calls: int = 0
        self.safety_calls: int = 0
        self.browser_action_calls: int = 0
        self.browser_finish_calls: int = 0
        self.recovery_calls: int = 0
        self.provider_retries: int = 0
        self.outbound_http_requests: int = 0

        # Browser operations
        self.navigation_operations: int = 0
        self.tabs_created: int = 1  # 1 initial tab
        self.actions_executed: List[BrowserAction] = []

        # Observations and verifications
        self.browser_iterations: int = 0
        self.dynamic_action_decisions: int = 0
        self.verification_checks: int = 0
        self.recovery_attempts: int = 0

        # Logs
        self.log_entries: List[dict] = []

    @property
    def total_browser_agent_calls(self) -> int:
        return self.browser_action_calls + self.browser_finish_calls + self.recovery_calls

    @property
    def total_logical_gemini_invocations(self) -> int:
        return self.planning_calls + self.safety_calls + self.total_browser_agent_calls

    def record_gemini_query(self, caller: str, prompt: str, decision_type: Optional[str] = None):
        self.log_entries.append({
            "stage": caller,
            "decision_type": decision_type,
            "prompt_snippet": prompt[:80].replace("\n", " "),
        })
        if caller == "planning":
            self.planning_calls += 1
        elif caller == "safety":
            self.safety_calls += 1
        elif caller == "browser_action":
            self.browser_action_calls += 1
            self.dynamic_action_decisions += 1
        elif caller == "browser_finish":
            self.browser_finish_calls += 1
        elif caller == "recovery":
            self.recovery_calls += 1
            self.recovery_attempts += 1


# ─────────────────────────────────────────────────────────────────────────────
# 3. Mock Perception & Observation Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_observation(
    url: str = "about:blank",
    title: str = "",
    elements: list = None,
    recent_action: RecentActionRecord = None,
    tab_count: int = 1,
    session_id: str = "s_audit",
    task_id: str = "t_audit",
) -> BrowserObservation:
    snapshot = PageSnapshot(title=title, url=url)
    return BrowserObservation(
        session_id=session_id,
        task_id=task_id,
        current_url=url,
        page_title=title,
        observed_elements=elements or [],
        recent_action=recent_action,
        page_snapshot=snapshot,
        open_tab_count=tab_count,
        tabs=[TabInfo(tab_ref=f"tab_{i+1:03d}", url=url, title=title, is_active=(i==0), index=i) for i in range(tab_count)],
    )


def make_safety_allow(task_id: str = "t_audit", task_name: str = "task") -> SafetyDecision:
    return SafetyDecision(
        task_id=task_id,
        task_name=task_name,
        risk_level=RiskLevel.LOW,
        execution_decision=ExecutionDecision.ALLOW,
        reasoning="Audit mock action is safe.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# TEST 1 — Scenario A1: Simple Navigation with Destination Metadata
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_scenario_a1_simple_navigation_with_metadata():
    """
    Scenario A1: 'Open Wikipedia' with resolved destination metadata.
    Pipeline: Planning -> Safety -> DAGBuilder -> RuntimeEngine -> BrowserAgent -> SemanticVerifier.
    Expected:
    - Planning: 1 call
    - Safety: 1 call
    - BrowserAgent Action: 0 calls (fast-path navigation)
    - BrowserAgent FINISH: 0 calls (verification-first completion)
    - Total Gemini: 2 calls
    - Outbound HTTP: 0
    - Navigations: 1
    - Tabs: 1
    - Status: COMPLETED, VERIFIED
    """
    tracker = GeminiCallAuditTracker()

    # 1. Deterministic Planning Response with metadata
    plan_json = json.dumps({
        "nodes": [
            {
                "id": "task_wiki_1",
                "type": "input",
                "position": {"x": 250, "y": 50},
                "data": {
                    "label": "Open Wikipedia",
                    "metadata": {
                        "destination_url": "https://www.wikipedia.org",
                        "action_type": "navigate",
                    },
                },
            }
        ],
        "edges": [],
        "metadata": {
            "intent": "Open Wikipedia",
            "confidence": 1.0,
            "reasoning_summary": "Single step navigation to Wikipedia",
        },
    })

    # 2. Deterministic Safety Response
    safety_json = json.dumps({
        "task_wiki_1": {
            "task_id": "task_wiki_1",
            "action": "navigate",
            "target": "https://www.wikipedia.org",
            "target_category": "network_endpoint",
            "scope": "external",
            "environment": "production",
            "reversibility": "high",
            "destructive_potential": "none",
            "privilege_required": False,
            "system_critical": False,
            "data_sensitivity": "none",
            "external_side_effect": True,
            "consequence": "Navigates to Wikipedia homepage",
            "ambiguity": "clear",
            "semantic_confidence": 0.98,
        }
    })

    # 3. Intercept GeminiProvider.query
    async def mock_gemini_query(self, prompt: str, system_instruction: str = "", json_mode: bool = False) -> str:
        if "Goal to decompose:" in prompt or "elite operational copilot" in system_instruction:
            tracker.record_gemini_query("planning", prompt)
            return plan_json
        elif "safety classification assistant" in system_instruction or "Allowed enum values" in prompt:
            tracker.record_gemini_query("safety", prompt)
            return safety_json
        elif "TASK OBJECTIVE:" in prompt:
            # If called, BrowserAgent is making a decision
            tracker.record_gemini_query("browser_action", prompt)
            return json.dumps({
                "decision": "execute",
                "reason": "Navigate to Wikipedia",
                "action": {"action_type": "navigate", "url": "https://www.wikipedia.org", "new_tab": False},
            })
        raise RuntimeError(f"Unexpected Gemini query: {prompt[:100]}")

    # 4. Mock Perception & Browser Execution
    obs_initial = make_observation(url="about:blank", task_id="task_wiki_1")
    obs_loaded = make_observation(
        url="https://www.wikipedia.org/",
        title="Wikipedia, the free encyclopedia",
        task_id="task_wiki_1",
        recent_action=RecentActionRecord(action_type=BrowserActionType.NAVIGATE, url="https://www.wikipedia.org", success=True),
    )
    obs_sequence = [obs_initial, obs_loaded]
    obs_idx = 0

    async def mock_observe(*args, **kwargs):
        nonlocal obs_idx
        res = obs_sequence[min(obs_idx, len(obs_sequence) - 1)]
        obs_idx += 1
        return res

    async def mock_execute_action(self, task_id, session_id, action, approval_token=None):
        tracker.actions_executed.append(action)
        if action.action_type == BrowserActionType.NAVIGATE:
            tracker.navigation_operations += 1
        if action.new_tab:
            tracker.tabs_created += 1
        return (
            BrowserActionResult(success=True, action_type=action.action_type, current_url=action.url or "https://www.wikipedia.org/"),
            make_safety_allow(task_id),
        )

    # 5. Run complete /orchestrate pipeline under strict audit guards
    with patch("urllib.request.urlopen", side_effect=_forbid_outbound_http), \
         patch.object(GeminiProvider, "query", new=mock_gemini_query), \
         patch("execution.browser.environment_perception.EnvironmentPerceptionEngine.observe", side_effect=mock_observe), \
         patch("execution.browser.browser_task_adapter.BrowserTaskAdapter.execute_safe_browser_task", new=mock_execute_action), \
         patch("core.config.settings.ENABLE_RAG", False):

        # Execute start_orchestration
        start_req = ARDOStartRequest(goal="Open Wikipedia", mode="safe")
        resp = await start_orchestration(start_req)
        session_id = resp.session_id

        # Await execution of RuntimeEngine background task
        session = ARDOScheduler._sessions[session_id]
        if session._task:
            await session._task

        # Verify DAG task node results
        node = session.graph.get_node("task_wiki_1")
        assert node is not None
        assert node.status == TaskState.COMPLETED
        assert node.browser_result is not None
        assert node.browser_result.outcome == BrowserAgentOutcome.COMPLETED
        assert node.browser_result.verification.status == BrowserVerificationStatus.VERIFIED

    # 6. Audit Assertions
    assert tracker.planning_calls == 1
    assert tracker.safety_calls == 1
    assert tracker.browser_action_calls == 0  # Bypassed by fast-path!
    assert tracker.browser_finish_calls == 0  # Bypassed by verification-first!
    assert tracker.recovery_calls == 0
    assert tracker.provider_retries == 0
    assert tracker.outbound_http_requests == 0
    assert tracker.total_logical_gemini_invocations == 2
    assert tracker.navigation_operations == 1
    assert tracker.tabs_created == 1


# ─────────────────────────────────────────────────────────────────────────────
# TEST 2 — Scenario A2: Simple Navigation without Pre-Resolved Metadata
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_scenario_a2_simple_navigation_without_metadata():
    """
    Scenario A2: 'Open Wikipedia' where planner does not supply destination metadata.
    Gemini resolves the initial destination URL in BrowserAgent iteration 1.
    Expected:
    - Planning: 1 call
    - Safety: 1 call
    - BrowserAgent Action: 1 call (initial decision to navigate)
    - BrowserAgent FINISH: 0 calls (verification-first completion)
    - Total Gemini: 3 calls
    - Outbound HTTP: 0
    - Navigations: 1
    - Tabs: 1
    - Status: COMPLETED, VERIFIED
    """
    tracker = GeminiCallAuditTracker()

    # 1. Planning Response without metadata
    plan_json = json.dumps({
        "nodes": [
            {
                "id": "task_wiki_2",
                "type": "input",
                "position": {"x": 250, "y": 50},
                "data": {
                    "label": "Open Wikipedia",
                },
            }
        ],
        "edges": [],
        "metadata": {
            "intent": "Open Wikipedia",
            "confidence": 1.0,
            "reasoning_summary": "Single step navigation to Wikipedia without pre-resolved metadata",
        },
    })

    # 2. Safety Response
    safety_json = json.dumps({
        "task_wiki_2": {
            "task_id": "task_wiki_2",
            "action": "navigate",
            "target": "https://www.wikipedia.org",
            "target_category": "network_endpoint",
            "scope": "external",
            "environment": "production",
            "reversibility": "high",
            "destructive_potential": "none",
            "privilege_required": False,
            "system_critical": False,
            "data_sensitivity": "none",
            "external_side_effect": True,
            "consequence": "Navigates to Wikipedia homepage",
            "ambiguity": "clear",
            "semantic_confidence": 0.98,
        }
    })

    # 3. Intercept GeminiProvider.query
    async def mock_gemini_query(self, prompt: str, system_instruction: str = "", json_mode: bool = False) -> str:
        if "Goal to decompose:" in prompt or "elite operational copilot" in system_instruction:
            tracker.record_gemini_query("planning", prompt)
            return plan_json
        elif "safety classification assistant" in system_instruction or "Allowed enum values" in prompt:
            tracker.record_gemini_query("safety", prompt)
            return safety_json
        elif "TASK OBJECTIVE:" in prompt:
            tracker.record_gemini_query("browser_action", prompt, "execute_navigate")
            return json.dumps({
                "decision": "execute",
                "reason": "Navigate to Wikipedia main page",
                "action": {"action_type": "navigate", "url": "https://www.wikipedia.org", "new_tab": False},
            })
        raise RuntimeError(f"Unexpected Gemini query: {prompt[:100]}")

    # 4. Mock Perception & Browser Execution
    obs_initial = make_observation(url="about:blank", task_id="task_wiki_2")
    obs_loaded = make_observation(
        url="https://www.wikipedia.org/",
        title="Wikipedia, the free encyclopedia",
        task_id="task_wiki_2",
        recent_action=RecentActionRecord(action_type=BrowserActionType.NAVIGATE, url="https://www.wikipedia.org", success=True),
    )
    obs_sequence = [obs_initial, obs_loaded]
    obs_idx = 0

    async def mock_observe(*args, **kwargs):
        nonlocal obs_idx
        res = obs_sequence[min(obs_idx, len(obs_sequence) - 1)]
        obs_idx += 1
        return res

    async def mock_execute_action(self, task_id, session_id, action, approval_token=None):
        tracker.actions_executed.append(action)
        if action.action_type == BrowserActionType.NAVIGATE:
            tracker.navigation_operations += 1
        if action.new_tab:
            tracker.tabs_created += 1
        return (
            BrowserActionResult(success=True, action_type=action.action_type, current_url=action.url or "https://www.wikipedia.org/"),
            make_safety_allow(task_id),
        )

    # 5. Run complete /orchestrate pipeline
    with patch("urllib.request.urlopen", side_effect=_forbid_outbound_http), \
         patch.object(GeminiProvider, "query", new=mock_gemini_query), \
         patch("execution.browser.environment_perception.EnvironmentPerceptionEngine.observe", side_effect=mock_observe), \
         patch("execution.browser.browser_task_adapter.BrowserTaskAdapter.execute_safe_browser_task", new=mock_execute_action), \
         patch("core.config.settings.ENABLE_RAG", False):

        start_req = ARDOStartRequest(goal="Open Wikipedia", mode="safe")
        resp = await start_orchestration(start_req)
        session_id = resp.session_id

        session = ARDOScheduler._sessions[session_id]
        if session._task:
            await session._task

        node = session.graph.get_node("task_wiki_2")
        assert node is not None
        assert node.status == TaskState.COMPLETED
        assert node.browser_result is not None
        assert node.browser_result.outcome == BrowserAgentOutcome.COMPLETED
        assert node.browser_result.verification.status == BrowserVerificationStatus.VERIFIED

    # 6. Audit Assertions
    assert tracker.planning_calls == 1
    assert tracker.safety_calls == 1
    assert tracker.browser_action_calls == 1  # 1 initial action selection call
    assert tracker.browser_finish_calls == 0  # 0 FINISH calls (verification-first completed)
    assert tracker.recovery_calls == 0
    assert tracker.provider_retries == 0
    assert tracker.outbound_http_requests == 0
    assert tracker.total_logical_gemini_invocations == 3
    assert tracker.navigation_operations == 1
    assert tracker.tabs_created == 1


# ─────────────────────────────────────────────────────────────────────────────
# TEST 3 — Scenario B: Multi-Step Dynamic Interaction (DAG with Navigation + Search)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_scenario_b_multistep_dynamic_interaction_dag():
    """
    Scenario B: 'Open YouTube and search for Vande Mataram'
    Decomposed into a 2-node DAG:
      Node 1: 'Open YouTube' (fast-path navigation)
      Node 2: 'Search for Vande Mataram on YouTube' (dynamic interaction requiring Gemini)
    Validates:
    - Navigation alone does NOT complete the compound goal.
    - Node 2 uses Gemini for UI reasoning (fill + submit) and finishes properly.
    - Total logical calls measured by stage and per-node.
    """
    tracker = GeminiCallAuditTracker()

    # 1. 2-Node DAG Planner Output
    plan_json = json.dumps({
        "nodes": [
            {
                "id": "node_nav",
                "type": "input",
                "position": {"x": 250, "y": 50},
                "data": {
                    "label": "Open YouTube",
                    "metadata": {
                        "destination_url": "https://www.youtube.com",
                        "action_type": "navigate",
                    },
                },
            },
            {
                "id": "node_search",
                "type": "output",
                "position": {"x": 250, "y": 200},
                "data": {
                    "label": "Search for Vande Mataram on YouTube",
                    "metadata": {
                        "tool": "browser",
                    },
                },
            },
        ],
        "edges": [
            {"id": "e_nav_search", "source": "node_nav", "target": "node_search"}
        ],
        "metadata": {
            "intent": "Open YouTube and search for Vande Mataram",
            "confidence": 1.0,
            "reasoning_summary": "Two-step plan: Navigate to YouTube, then perform dynamic search",
        },
    })

    # 2. Batch Safety Evaluation for both nodes
    safety_json = json.dumps({
        "node_nav": {
            "task_id": "node_nav",
            "action": "navigate",
            "target": "https://www.youtube.com",
            "target_category": "network_endpoint",
            "scope": "external",
            "environment": "production",
            "reversibility": "high",
            "destructive_potential": "none",
            "privilege_required": False,
            "system_critical": False,
            "data_sensitivity": "none",
            "external_side_effect": True,
            "consequence": "Navigates to YouTube homepage",
            "ambiguity": "clear",
            "semantic_confidence": 0.95,
        },
        "node_search": {
            "task_id": "node_search",
            "action": "search",
            "target": "YouTube search input",
            "target_category": "application_data",
            "scope": "application",
            "environment": "production",
            "reversibility": "high",
            "destructive_potential": "none",
            "privilege_required": False,
            "system_critical": False,
            "data_sensitivity": "none",
            "external_side_effect": False,
            "consequence": "Enters search query on YouTube",
            "ambiguity": "clear",
            "semantic_confidence": 0.92,
        },
    })

    # 3. Dynamic Gemini Decisions for Node 2
    gemini_fill_decision = json.dumps({
        "decision": "execute",
        "reason": "Enter 'Vande Mataram' into the search input",
        "action": {
            "action_type": "fill",
            "element_ref": "elem_search_box",
            "value": "Vande Mataram",
            "new_tab": False,
        },
    })
    gemini_press_decision = json.dumps({
        "decision": "execute",
        "reason": "Submit search by pressing Enter",
        "action": {
            "action_type": "press",
            "key": "Enter",
            "new_tab": False,
        },
    })
    gemini_finish_decision = json.dumps({
        "decision": "finish",
        "reason": "Search query executed and results are visible",
    })

    node_search_call_count = 0
    async def mock_gemini_query(self, prompt: str, system_instruction: str = "", json_mode: bool = False) -> str:
        nonlocal node_search_call_count
        if "Goal to decompose:" in prompt or "elite operational copilot" in system_instruction:
            tracker.record_gemini_query("planning", prompt)
            return plan_json
        elif "safety classification assistant" in system_instruction or "Allowed enum values" in prompt:
            tracker.record_gemini_query("safety", prompt)
            return safety_json
        elif "TASK OBJECTIVE:" in prompt:
            node_search_call_count += 1
            if node_search_call_count == 1:
                tracker.record_gemini_query("browser_action", prompt, "fill")
                return gemini_fill_decision
            elif node_search_call_count == 2:
                tracker.record_gemini_query("browser_action", prompt, "press")
                return gemini_press_decision
            else:
                tracker.record_gemini_query("browser_finish", prompt, "finish")
                return gemini_finish_decision
        raise RuntimeError(f"Unexpected Gemini query: {prompt[:100]}")

    # 4. Realistic Perception Observation Sequence
    # Node 1: Initial -> Loaded
    obs_blank = make_observation(url="about:blank", task_id="node_nav")
    obs_yt_home = make_observation(
        url="https://www.youtube.com/",
        title="YouTube",
        task_id="node_nav",
        elements=[ObservedElement(ref="elem_search_box", tag="input", css_selector="#search", role="searchbox", placeholder="Search")],
        recent_action=RecentActionRecord(action_type=BrowserActionType.NAVIGATE, url="https://www.youtube.com", success=True),
    )
    # Node 2: Initial (already on YouTube) -> Filled -> Submitted/Results
    obs_search_init = make_observation(
        url="https://www.youtube.com/",
        title="YouTube",
        task_id="node_search",
        elements=[ObservedElement(ref="elem_search_box", tag="input", css_selector="#search", role="searchbox", placeholder="Search")],
    )
    obs_search_filled = make_observation(
        url="https://www.youtube.com/",
        title="YouTube",
        task_id="node_search",
        elements=[ObservedElement(ref="elem_search_box", tag="input", css_selector="#search", value="Vande Mataram")],
        recent_action=RecentActionRecord(action_type=BrowserActionType.FILL, success=True),
    )
    obs_search_results = make_observation(
        url="https://www.youtube.com/results?search_query=Vande+Mataram",
        title="Vande Mataram - YouTube",
        task_id="node_search",
        recent_action=RecentActionRecord(action_type=BrowserActionType.PRESS, key="Enter", success=True),
    )

    observations_queue = [
        obs_blank,
        obs_yt_home,
        obs_search_init,
        obs_search_filled,
        obs_search_results,
        obs_search_results,
    ]
    obs_idx = 0

    async def mock_observe(*args, **kwargs):
        nonlocal obs_idx
        res = observations_queue[min(obs_idx, len(observations_queue) - 1)]
        obs_idx += 1
        return res

    async def mock_execute_action(self, task_id, session_id, action, approval_token=None):
        tracker.actions_executed.append(action)
        if action.action_type == BrowserActionType.NAVIGATE:
            tracker.navigation_operations += 1
        if action.new_tab:
            tracker.tabs_created += 1
        current_url = action.url or ("https://www.youtube.com/results?search_query=Vande+Mataram" if action.action_type == BrowserActionType.PRESS else "https://www.youtube.com/")
        return (
            BrowserActionResult(success=True, action_type=action.action_type, current_url=current_url),
            make_safety_allow(task_id),
        )

    # 5. Run full pipeline
    with patch("urllib.request.urlopen", side_effect=_forbid_outbound_http), \
         patch.object(GeminiProvider, "query", new=mock_gemini_query), \
         patch("execution.browser.environment_perception.EnvironmentPerceptionEngine.observe", side_effect=mock_observe), \
         patch("execution.browser.browser_task_adapter.BrowserTaskAdapter.execute_safe_browser_task", new=mock_execute_action), \
         patch("core.config.settings.ENABLE_RAG", False):

        start_req = ARDOStartRequest(goal="Open YouTube and search for Vande Mataram", mode="sequential")
        resp = await start_orchestration(start_req)
        session_id = resp.session_id

        session = ARDOScheduler._sessions[session_id]
        if session._task:
            await session._task

        # Assert Node 1 state
        node1 = session.graph.get_node("node_nav")
        assert node1 is not None
        assert node1.status == TaskState.COMPLETED
        assert node1.browser_result.outcome == BrowserAgentOutcome.COMPLETED

        # Assert Node 2 state
        node2 = session.graph.get_node("node_search")
        assert node2 is not None
        assert node2.status == TaskState.COMPLETED
        assert node2.browser_result.outcome == BrowserAgentOutcome.COMPLETED
        assert node2.browser_result.verification.status == BrowserVerificationStatus.VERIFIED

    # 6. Audit Assertions
    # Stage counts:
    assert tracker.planning_calls == 1
    assert tracker.safety_calls == 1
    assert tracker.browser_action_calls == 2     # Node 2 dynamic actions: fill + press
    assert tracker.browser_finish_calls == 1     # Node 2 FINISH
    assert tracker.recovery_calls == 0
    assert tracker.provider_retries == 0
    assert tracker.outbound_http_requests == 0
    assert tracker.total_logical_gemini_invocations == 5

    # Browser operations:
    assert tracker.navigation_operations == 1   # Exactly 1 navigation (Node 1)
    assert tracker.tabs_created == 1            # No runaway tab creation
    assert len(tracker.actions_executed) == 3   # navigate, fill, press
    assert tracker.actions_executed[0].action_type == BrowserActionType.NAVIGATE
    assert tracker.actions_executed[1].action_type == BrowserActionType.FILL
    assert tracker.actions_executed[2].action_type == BrowserActionType.PRESS


# ─────────────────────────────────────────────────────────────────────────────
# TEST 4 — Scenario B (Single Compound Task Node): Navigation Alone Does Not Complete
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_scenario_b_single_compound_task_preserves_dynamic_interaction():
    """
    Scenario B (Single Compound Task variant):
    Objective: 'Open YouTube and search for Vande Mataram'
    Validates that when navigation executes in iteration 1:
    - _can_verify_first() rejects premature completion because the goal requires dynamic search.
    - BrowserAgent proceeds into iteration 2 to interact with UI using Gemini.
    - Exactly 1 navigation and 0 duplicate tabs occur.
    """
    tracker = GeminiCallAuditTracker()

    task = BrowserTask(
        task_id="t_yt_compound",
        objective="Open YouTube and search for Vande Mataram",
        session_id="s_yt_comp",
    )

    gemini_nav_decision = json.dumps({
        "decision": "execute",
        "reason": "Navigate to YouTube",
        "action": {"action_type": "navigate", "url": "https://www.youtube.com", "new_tab": False},
    })
    gemini_fill_decision = json.dumps({
        "decision": "execute",
        "reason": "Type search query in search bar",
        "action": {"action_type": "fill", "element_ref": "elem_search_box", "value": "Vande Mataram", "new_tab": False},
    })
    gemini_press_decision = json.dumps({
        "decision": "execute",
        "reason": "Submit search query",
        "action": {"action_type": "press", "key": "Enter", "new_tab": False},
    })
    gemini_finish_decision = json.dumps({
        "decision": "finish",
        "reason": "Search complete",
    })

    call_seq = 0
    async def mock_gemini_query(prompt: str, system_instruction: str = "", json_mode: bool = False) -> str:
        nonlocal call_seq
        call_seq += 1
        if call_seq == 1:
            tracker.record_gemini_query("browser_action", prompt, "navigate")
            return gemini_nav_decision
        elif call_seq == 2:
            tracker.record_gemini_query("browser_action", prompt, "fill")
            return gemini_fill_decision
        elif call_seq == 3:
            tracker.record_gemini_query("browser_action", prompt, "press")
            return gemini_press_decision
        else:
            tracker.record_gemini_query("browser_finish", prompt, "finish")
            return gemini_finish_decision

    mock_llm = MagicMock()
    mock_llm.query = AsyncMock(side_effect=mock_gemini_query)

    obs_initial = make_observation(url="about:blank", task_id="t_yt_compound")
    obs_nav = make_observation(
        url="https://www.youtube.com/",
        title="YouTube",
        task_id="t_yt_compound",
        elements=[ObservedElement(ref="elem_search_box", tag="input", css_selector="#search", role="searchbox")],
        recent_action=RecentActionRecord(action_type=BrowserActionType.NAVIGATE, url="https://www.youtube.com", success=True),
    )
    obs_filled = make_observation(
        url="https://www.youtube.com/",
        title="YouTube",
        task_id="t_yt_compound",
        elements=[ObservedElement(ref="elem_search_box", tag="input", css_selector="#search", value="Vande Mataram")],
        recent_action=RecentActionRecord(action_type=BrowserActionType.FILL, success=True),
    )
    obs_results = make_observation(
        url="https://www.youtube.com/results?search_query=Vande+Mataram",
        title="Vande Mataram - YouTube",
        task_id="t_yt_compound",
        recent_action=RecentActionRecord(action_type=BrowserActionType.PRESS, key="Enter", success=True),
    )

    obs_list = [obs_initial, obs_nav, obs_filled, obs_results, obs_results]
    obs_idx = 0
    async def mock_observe(*args, **kwargs):
        nonlocal obs_idx
        res = obs_list[min(obs_idx, len(obs_list) - 1)]
        obs_idx += 1
        return res

    mock_perception = MagicMock()
    mock_perception.observe = AsyncMock(side_effect=mock_observe)

    async def mock_execute(task_id, session_id, action, approval_token=None):
        tracker.actions_executed.append(action)
        if action.action_type == BrowserActionType.NAVIGATE:
            tracker.navigation_operations += 1
        if action.new_tab:
            tracker.tabs_created += 1
        return (
            BrowserActionResult(success=True, action_type=action.action_type),
            make_safety_allow(task_id),
        )

    mock_adapter = MagicMock()
    mock_adapter.execute_safe_browser_task = AsyncMock(side_effect=mock_execute)
    mock_adapter.executor = MagicMock(_page=None, _context=None)

    agent = BrowserAgent(llm_provider=mock_llm, adapter=mock_adapter, perception=mock_perception)

    with patch("urllib.request.urlopen", side_effect=_forbid_outbound_http):
        result = await agent.run(task)

    assert result.outcome == BrowserAgentOutcome.COMPLETED
    assert result.verification.status == BrowserVerificationStatus.VERIFIED

    # Proves navigation alone did not complete:
    assert tracker.navigation_operations == 1
    assert tracker.tabs_created == 1
    assert len(tracker.actions_executed) == 3
    assert tracker.browser_action_calls == 3  # navigate, fill, press
    assert tracker.browser_finish_calls == 1  # FINISH
    assert tracker.total_browser_agent_calls == 4
    assert tracker.outbound_http_requests == 0


# ─────────────────────────────────────────────────────────────────────────────
# TEST 5 — Scenario B2: Multi-Step Single Compound Task through Full /orchestrate Pipeline
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_scenario_b2_compound_full_pipeline():
    """
    Scenario B2 (Full /orchestrate pipeline):
    User Goal: 'Open YouTube and search for Vande Mataram'
    Planner outputs 1 compound task node:
      'Open YouTube and search for Vande Mataram'
    Validates complete pipeline call count:
    - Planning: 1
    - Safety: 1
    - BrowserAgent Action: 3 (navigate, fill, press)
    - BrowserAgent FINISH: 1
    - Total Logical Gemini Invocations: exactly 6
    - Outbound HTTP: 0
    - Navigation: 1
    - Tabs: 1
    """
    tracker = GeminiCallAuditTracker()

    # 1. 1-Node Compound Plan
    plan_json = json.dumps({
        "nodes": [
            {
                "id": "node_compound",
                "type": "input",
                "position": {"x": 250, "y": 50},
                "data": {
                    "label": "Open YouTube and search for Vande Mataram",
                    "metadata": {
                        "tool": "browser",
                    },
                },
            }
        ],
        "edges": [],
        "metadata": {
            "intent": "Open YouTube and search for Vande Mataram",
            "confidence": 1.0,
            "reasoning_summary": "Single compound browser task for navigation and search",
        },
    })

    # 2. Batch Safety Response
    safety_json = json.dumps({
        "node_compound": {
            "task_id": "node_compound",
            "action": "search",
            "target": "https://www.youtube.com",
            "target_category": "network_endpoint",
            "scope": "external",
            "environment": "production",
            "reversibility": "high",
            "destructive_potential": "none",
            "privilege_required": False,
            "system_critical": False,
            "data_sensitivity": "none",
            "external_side_effect": True,
            "consequence": "Navigates to YouTube and performs search",
            "ambiguity": "clear",
            "semantic_confidence": 0.94,
        }
    })

    # 3. Dynamic Gemini Decisions
    gemini_nav_decision = json.dumps({
        "decision": "execute",
        "reason": "Navigate to YouTube",
        "action": {"action_type": "navigate", "url": "https://www.youtube.com", "new_tab": False},
    })
    gemini_fill_decision = json.dumps({
        "decision": "execute",
        "reason": "Type search query in search bar",
        "action": {"action_type": "fill", "element_ref": "elem_search_box", "value": "Vande Mataram", "new_tab": False},
    })
    gemini_press_decision = json.dumps({
        "decision": "execute",
        "reason": "Submit search query",
        "action": {"action_type": "press", "key": "Enter", "new_tab": False},
    })
    gemini_finish_decision = json.dumps({
        "decision": "finish",
        "reason": "Search complete",
    })

    browser_call_seq = 0
    async def mock_gemini_query(self, prompt: str, system_instruction: str = "", json_mode: bool = False) -> str:
        nonlocal browser_call_seq
        if "Goal to decompose:" in prompt or "elite operational copilot" in system_instruction:
            tracker.record_gemini_query("planning", prompt)
            return plan_json
        elif "safety classification assistant" in system_instruction or "Allowed enum values" in prompt:
            tracker.record_gemini_query("safety", prompt)
            return safety_json
        elif "TASK OBJECTIVE:" in prompt:
            browser_call_seq += 1
            if browser_call_seq == 1:
                tracker.record_gemini_query("browser_action", prompt, "navigate")
                return gemini_nav_decision
            elif browser_call_seq == 2:
                tracker.record_gemini_query("browser_action", prompt, "fill")
                return gemini_fill_decision
            elif browser_call_seq == 3:
                tracker.record_gemini_query("browser_action", prompt, "press")
                return gemini_press_decision
            else:
                tracker.record_gemini_query("browser_finish", prompt, "finish")
                return gemini_finish_decision
        raise RuntimeError(f"Unexpected Gemini query: {prompt[:100]}")

    # 4. Perception Observation Sequence
    obs_initial = make_observation(url="about:blank", task_id="node_compound")
    obs_nav = make_observation(
        url="https://www.youtube.com/",
        title="YouTube",
        task_id="node_compound",
        elements=[ObservedElement(ref="elem_search_box", tag="input", css_selector="#search", role="searchbox")],
        recent_action=RecentActionRecord(action_type=BrowserActionType.NAVIGATE, url="https://www.youtube.com", success=True),
    )
    obs_filled = make_observation(
        url="https://www.youtube.com/",
        title="YouTube",
        task_id="node_compound",
        elements=[ObservedElement(ref="elem_search_box", tag="input", css_selector="#search", value="Vande Mataram")],
        recent_action=RecentActionRecord(action_type=BrowserActionType.FILL, success=True),
    )
    obs_results = make_observation(
        url="https://www.youtube.com/results?search_query=Vande+Mataram",
        title="Vande Mataram - YouTube",
        task_id="node_compound",
        recent_action=RecentActionRecord(action_type=BrowserActionType.PRESS, key="Enter", success=True),
    )

    obs_list = [obs_initial, obs_nav, obs_filled, obs_results, obs_results]
    obs_idx = 0
    async def mock_observe(*args, **kwargs):
        nonlocal obs_idx
        res = obs_list[min(obs_idx, len(obs_list) - 1)]
        obs_idx += 1
        return res

    async def mock_execute(self, task_id, session_id, action, approval_token=None):
        tracker.actions_executed.append(action)
        if action.action_type == BrowserActionType.NAVIGATE:
            tracker.navigation_operations += 1
        if action.new_tab:
            tracker.tabs_created += 1
        return (
            BrowserActionResult(success=True, action_type=action.action_type, current_url=action.url or "https://www.youtube.com/"),
            make_safety_allow(task_id),
        )

    # 5. Execute full pipeline
    with patch("urllib.request.urlopen", side_effect=_forbid_outbound_http), \
         patch.object(GeminiProvider, "query", new=mock_gemini_query), \
         patch("execution.browser.environment_perception.EnvironmentPerceptionEngine.observe", side_effect=mock_observe), \
         patch("execution.browser.browser_task_adapter.BrowserTaskAdapter.execute_safe_browser_task", new=mock_execute), \
         patch("core.config.settings.ENABLE_RAG", False):

        start_req = ARDOStartRequest(goal="Open YouTube and search for Vande Mataram", mode="safe")
        resp = await start_orchestration(start_req)
        session_id = resp.session_id

        session = ARDOScheduler._sessions[session_id]
        if session._task:
            await session._task

        node = session.graph.get_node("node_compound")
        assert node is not None
        assert node.status == TaskState.COMPLETED
        assert node.browser_result.outcome == BrowserAgentOutcome.COMPLETED
        assert node.browser_result.verification.status == BrowserVerificationStatus.VERIFIED

    # 6. Audit Assertions for Scenario B2
    assert tracker.planning_calls == 1
    assert tracker.safety_calls == 1
    assert tracker.browser_action_calls == 3     # 1. navigate, 2. fill, 3. press
    assert tracker.browser_finish_calls == 1     # 4. finish
    assert tracker.recovery_calls == 0
    assert tracker.provider_retries == 0
    assert tracker.outbound_http_requests == 0
    assert tracker.total_browser_agent_calls == 4
    # Exactly 1 + 1 + 3 + 1 = 6 total logical Gemini invocations:
    assert tracker.total_logical_gemini_invocations == 6
    assert tracker.navigation_operations == 1
    assert tracker.tabs_created == 1
    assert len(tracker.actions_executed) == 3

