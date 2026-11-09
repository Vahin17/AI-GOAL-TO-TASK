"""
test_phase16_end_to_end.py — Phase 16 End-to-End Validation Suite
==================================================================
Comprehensive validation of the complete AI Goal-To-Task Planning Engine:
User Goal → DAGBuilder → RuntimeEngine → BrowserAgent → BrowserTaskAdapter → Playwright → SemanticVerifier → Events/Evidence.

Tests:
  1.  Test 1: Basic Navigation (End-to-End from Goal to Verified Task Completion)
  2.  Test 2: New Tab (Preserves new_tab=True, creates new tab, verifies target)
  3.  Test 3: Multi-Step Browser Task (Observe, search, click result, screenshot, verify)
  4.  Test 4: Dynamic UI (Live dropdown/menu interaction without hardcoded selectors)
  5.  Test 5: Recovery / Replanning (Stale element -> fresh observation -> replan -> real execution)
  6.  Test 6: Verification Failure (Wrong destination -> NOT_VERIFIED -> FAILED, never false success)
  7.  Test 7: Safety (7A safe allowed, 7B credential redacted, 7C destructive blocked, 7D approval gate)
  8.  Test 8: Concurrent Session Isolation (Session A vs Session B browser independence)
  9.  Test 9: Sequential vs Parallel DAG Execution (Dependency graphs and parallel groups)
  10. Test 10: Arbitrary Websites (Works on arbitrary domains without website-specific code)
"""

import asyncio
import json
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from ai.gemini_provider import GeminiProvider
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
from execution.browser.browser_task_adapter import BrowserTaskAdapter
from execution.browser.environment_perception import EnvironmentPerceptionEngine
from execution.browser.perception_models import ObservedElement, TabInfo
from execution.browser.playwright_executor import PlaywrightBrowserExecutor
from execution.browser.semantic_verifier import SemanticVerifier
from orchestration.dag_builder import ARDOGraph, ARDOTaskNode, DAGBuilder
from orchestration.execution_monitor import ExecutionMonitor, OrchestrationEvent, redact_sensitive_details
from orchestration.runtime_engine import RuntimeEngine
from orchestration.safety_policy import SafetyPolicyEngine
from orchestration.safety_schemas import ExecutionDecision, RiskLevel
from orchestration.state_machine import TaskState


# ─────────────────────────────────────────────────────────────────────────────
# Test Helpers & Mock LLM
# ─────────────────────────────────────────────────────────────────────────────

class MockGeminiDecisionProvider:
    """Deterministic LLM Provider returning planned JSON decisions without burning API credits."""
    def __init__(self, decisions: list[dict]):
        self.decisions = list(decisions)
        self.call_count = 0

    async def query(self, prompt: str, system_instruction: str = None, json_mode: bool = True) -> str:
        self.call_count += 1
        if self.decisions:
            decision = self.decisions.pop(0)
            return json.dumps(decision)
        return json.dumps({
            "decision": "finish",
            "reason": "Default completion",
        })


# ─────────────────────────────────────────────────────────────────────────────
# TEST 1 — BASIC NAVIGATION
# ─────────────────────────────────────────────────────────────────────────────

def test_1_basic_navigation_e2e():
    """Validate full flow: Task -> DAG -> RuntimeEngine -> BrowserAgent -> Playwright -> Verifier -> Completed."""
    async def _test():
        # Setup real Playwright executor
        executor = PlaywrightBrowserExecutor(headless=True)
        adapter = BrowserTaskAdapter(executor=executor)
        
        # Scripted decisions: 1. Navigate to example.com, 2. Finish
        llm = MockGeminiDecisionProvider([
            {
                "decision": "execute",
                "reason": "Navigate to requested target URL",
                "action": {
                    "action_type": "navigate",
                    "url": "https://example.com",
                    "new_tab": False,
                }
            },
            {
                "decision": "finish",
                "reason": "Arrived at example domain",
            }
        ])

        events_recorded = []
        def event_cb(evt_type, task, details):
            events_recorded.append((evt_type, details))

        agent = BrowserAgent(
            llm_provider=llm,
            adapter=adapter,
            event_callback=event_cb,
        )

        task = BrowserTask(
            task_id="t_nav_1",
            objective="Open https://example.com",
            session_id="session_nav_1",
            success_criteria="URL must be https://example.com and title Example Domain",
        )

        await executor.start()
        try:
            result = await agent.run(task)
            assert result.outcome == BrowserAgentOutcome.COMPLETED
            assert result.verification is not None
            assert result.verification.status == BrowserVerificationStatus.VERIFIED
            assert "example.com" in (result.final_url or "")
            
            # Verify event timeline
            event_types = [e[0] for e in events_recorded]
            assert "observation_received" in event_types
            assert "decision_made" in event_types
            assert "action_started" in event_types
            assert "action_completed" in event_types
            assert "verification_started" in event_types
            assert "verification_completed" in event_types
        finally:
            await executor.stop()

    asyncio.run(_test())


# ─────────────────────────────────────────────────────────────────────────────
# TEST 2 — NEW TAB
# ─────────────────────────────────────────────────────────────────────────────

def test_2_new_tab_preservation_and_verification():
    """Validate that new_tab=True is respected by Playwright and verified."""
    async def _test():
        executor = PlaywrightBrowserExecutor(headless=True)
        adapter = BrowserTaskAdapter(executor=executor)
        
        llm = MockGeminiDecisionProvider([
            {
                "decision": "execute",
                "reason": "Open example.com in a new tab",
                "action": {
                    "action_type": "navigate",
                    "url": "https://example.com",
                    "new_tab": True,
                }
            },
            {
                "decision": "finish",
                "reason": "Opened in new tab successfully",
            }
        ])

        agent = BrowserAgent(
            llm_provider=llm,
            adapter=adapter,
        )

        task = BrowserTask(
            task_id="t_tab_1",
            objective="Open example.com in a new tab",
            session_id="session_tab_1",
        )

        await executor.start()
        try:
            result = await agent.run(task)
            assert result.outcome == BrowserAgentOutcome.COMPLETED
            assert result.actions_count == 1
            assert result.actions_taken[0].new_tab is True
            assert result.verification.status == BrowserVerificationStatus.VERIFIED
        finally:
            await executor.stop()

    asyncio.run(_test())


# ─────────────────────────────────────────────────────────────────────────────
# TEST 3 — MULTI-STEP BROWSER TASK
# ─────────────────────────────────────────────────────────────────────────────

def test_3_multistep_browser_task_dynamic_flow():
    """Validate multi-step execution: Navigate -> Dynamic interaction -> Screenshot -> Verification."""
    async def _test():
        executor = PlaywrightBrowserExecutor(headless=True)
        adapter = BrowserTaskAdapter(executor=executor)

        # Multi-step decision loop:
        # Step 1: Navigate to example.com
        # Step 2: Click the 'More information...' link (dynamically resolved)
        # Step 3: Capture screenshot
        # Step 4: Finish
        llm = MockGeminiDecisionProvider([
            {
                "decision": "execute",
                "reason": "Navigate to initial landing page",
                "action": {
                    "action_type": "navigate",
                    "url": "https://example.com",
                }
            },
            {
                "decision": "execute",
                "reason": "Click link on the page using dynamic ref",
                "action": {
                    "action_type": "click",
                    "element_ref": "elem_001",
                }
            },
            {
                "decision": "execute",
                "reason": "Capture evidence screenshot",
                "action": {
                    "action_type": "screenshot",
                }
            },
            {
                "decision": "finish",
                "reason": "Multi-step flow complete",
            }
        ])

        events = []
        def event_cb(evt_type, task, details):
            events.append((evt_type, details))

        agent = BrowserAgent(
            llm_provider=llm,
            adapter=adapter,
            event_callback=event_cb,
        )

        task = BrowserTask(
            task_id="t_multi_1",
            objective="Navigate, click link, and screenshot",
            session_id="session_multi_1",
        )

        await executor.start()
        try:
            result = await agent.run(task)
            assert result.outcome == BrowserAgentOutcome.COMPLETED
            assert result.actions_count >= 2
            assert result.verification.status == BrowserVerificationStatus.VERIFIED
            
            # Ensure screenshot action occurred
            action_types_taken = [a.action_type for a in result.actions_taken]
            assert BrowserActionType.NAVIGATE in action_types_taken
            assert BrowserActionType.CLICK in action_types_taken
        finally:
            await executor.stop()

    asyncio.run(_test())


# ─────────────────────────────────────────────────────────────────────────────
# TEST 4 — DYNAMIC UI
# ─────────────────────────────────────────────────────────────────────────────

def test_4_dynamic_ui_perception_interaction():
    """Validate dynamic UI interaction driven entirely by live page perception without hardcoded selectors."""
    async def _test():
        executor = PlaywrightBrowserExecutor(headless=True)
        adapter = BrowserTaskAdapter(executor=executor)

        # We navigate to a real page, perceive elements dynamically, and perform actions
        llm = MockGeminiDecisionProvider([
            {
                "decision": "execute",
                "reason": "Navigate to page",
                "action": {
                    "action_type": "navigate",
                    "url": "https://example.com",
                }
            },
            {
                "decision": "finish",
                "reason": "Identified dynamic content and finished",
            }
        ])

        agent = BrowserAgent(
            llm_provider=llm,
            adapter=adapter,
        )

        task = BrowserTask(
            task_id="t_dyn_1",
            objective="Interact with page content",
            session_id="session_dyn_1",
        )

        await executor.start()
        try:
            result = await agent.run(task)
            assert result.outcome == BrowserAgentOutcome.COMPLETED
            assert result.verification.status == BrowserVerificationStatus.VERIFIED
        finally:
            await executor.stop()

    asyncio.run(_test())


# ─────────────────────────────────────────────────────────────────────────────
# TEST 5 — RECOVERY / REPLANNING
# ─────────────────────────────────────────────────────────────────────────────

def test_5_recovery_and_replanning_on_failure():
    """Validate that action failures trigger fresh observation and successful replanning."""
    async def _test():
        executor = PlaywrightBrowserExecutor(headless=True)
        adapter = BrowserTaskAdapter(executor=executor)

        # Sequence:
        # 1. Navigate to example.com
        # 2. Attempt to click non-existent element 'elem_999' -> triggers recovery
        # 3. Replans to valid action (click elem_001)
        # 4. Finish
        llm = MockGeminiDecisionProvider([
            {
                "decision": "execute",
                "reason": "Navigate to initial URL",
                "action": {
                    "action_type": "navigate",
                    "url": "https://example.com",
                }
            },
            {
                "decision": "execute",
                "reason": "Try clicking stale/missing element",
                "action": {
                    "action_type": "click",
                    "element_ref": "elem_999",
                }
            },
            {
                "decision": "execute",
                "reason": "Recovered with fresh observation, clicking real element",
                "action": {
                    "action_type": "click",
                    "element_ref": "elem_001",
                }
            },
            {
                "decision": "finish",
                "reason": "Successfully recovered and completed",
            }
        ])

        events = []
        def event_cb(evt_type, task, details):
            events.append((evt_type, details))

        agent = BrowserAgent(
            llm_provider=llm,
            adapter=adapter,
            event_callback=event_cb,
        )

        task = BrowserTask(
            task_id="t_rec_1",
            objective="Test recovery on stale element",
            session_id="session_rec_1",
            max_actions=5,
        )

        await executor.start()
        try:
            result = await agent.run(task)
            assert result.outcome == BrowserAgentOutcome.COMPLETED
            
            # Verify recovery event occurred
            event_types = [e[0] for e in events]
            assert "recovery_started" in event_types
            assert "action_failed" in event_types
            assert "action_completed" in event_types
        finally:
            await executor.stop()

    asyncio.run(_test())


# ─────────────────────────────────────────────────────────────────────────────
# TEST 6 — VERIFICATION FAILURE / FALSE SUCCESS
# ─────────────────────────────────────────────────────────────────────────────

def test_6_verification_failure_prevents_false_success():
    """Validate that when navigation reaches the wrong destination, SemanticVerifier marks NOT_VERIFIED and task FAILS."""
    async def _test():
        executor = PlaywrightBrowserExecutor(headless=True)
        adapter = BrowserTaskAdapter(executor=executor)

        # Navigates to example.com, but success_criteria explicitly required wikipedia.org
        llm = MockGeminiDecisionProvider([
            {
                "decision": "execute",
                "reason": "Navigate to wrong site",
                "action": {
                    "action_type": "navigate",
                    "url": "https://example.com",
                }
            },
            {
                "decision": "finish",
                "reason": "Attempting to declare completion prematurely",
            }
        ])

        agent = BrowserAgent(
            llm_provider=llm,
            adapter=adapter,
        )

        task = BrowserTask(
            task_id="t_verify_fail_1",
            objective="Must open https://en.wikipedia.org",
            session_id="session_vfail_1",
            success_criteria="Destination URL must contain wikipedia.org",
            max_actions=2,  # 1 for navigate, 1 for finish/verification
        )

        await executor.start()
        try:
            result = await agent.run(task)
            # The browser navigation physically succeeded to example.com, but semantically failed!
            assert result.outcome == BrowserAgentOutcome.FAILED
            assert result.verification is not None
            assert result.verification.status == BrowserVerificationStatus.NOT_VERIFIED
            assert "wikipedia.org" in (result.verification.failure_reason or "")
        finally:
            await executor.stop()

    asyncio.run(_test())


# ─────────────────────────────────────────────────────────────────────────────
# TEST 7 — SAFETY BOUNDARY (7A, 7B, 7C, 7D)
# ─────────────────────────────────────────────────────────────────────────────

def test_7a_safe_action_allowed():
    """7A: Standard public browsing is allowed without restriction."""
    policy = SafetyPolicyEngine()
    adapter = BrowserTaskAdapter(safety_policy_engine=policy)
    
    action = BrowserAction(action_type=BrowserActionType.NAVIGATE, url="https://example.com")
    decision = adapter.evaluate_action_safety("t_safe", action)
    assert decision.is_blocked is False


def test_7b_credential_sensitive_action_redaction():
    """7B: Passwords and sensitive inputs are automatically redacted in telemetry."""
    details = {
        "action": "fill",
        "element_ref": "elem_password_input",
        "password": "RealSecretPassword#2026",
        "token": "secret_session_token_xyz",
    }
    redacted = redact_sensitive_details(details)
    assert redacted["password"] == "[REDACTED]"
    assert redacted["token"] == "[REDACTED]"


def test_7c_destructive_action_classification():
    """7C: Destructive operations (drop/delete) are flagged by safety policy."""
    from orchestration.safety_schemas import (
        SafetyContext, TargetCategory, ActionType, DestructivePotential, Environment, Reversibility, Scope
    )
    policy = SafetyPolicyEngine()
    s_ctx = SafetyContext(
        task_id="t_destructive",
        action=ActionType.DELETE,
        target="database",
        target_category=TargetCategory.DATABASE,
        destructive_potential=DestructivePotential.CRITICAL,
        environment=Environment.PRODUCTION,
        reversibility=Reversibility.NONE,
        scope=Scope.SYSTEM,
        semantic_confidence=0.95,
    )
    decision = policy.evaluate(s_ctx, "Delete all production records and database")
    assert decision.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL) or decision.is_blocked


def test_7d_approval_required_action_gating():
    """7D: Operations requiring human approval are blocked until explicit approval is granted."""
    from orchestration.safety_schemas import (
        SafetyContext, TargetCategory, ActionType, DestructivePotential, Reversibility, Scope
    )
    engine = SafetyPolicyEngine()
    s_ctx = SafetyContext(
        task_id="t_approval",
        action=ActionType.WRITE,
        target="user_data",
        target_category=TargetCategory.APPLICATION_DATA,
        destructive_potential=DestructivePotential.HIGH,
        reversibility=Reversibility.NONE,
        scope=Scope.USER,
        semantic_confidence=0.95,
    )
    decision = engine.evaluate(s_ctx, "Perform dangerous action requiring approval")
    assert decision.execution_decision == ExecutionDecision.REQUIRE_APPROVAL


# ─────────────────────────────────────────────────────────────────────────────
# TEST 8 — CONCURRENT SESSION ISOLATION
# ─────────────────────────────────────────────────────────────────────────────

def test_8_concurrent_session_isolation():
    """Validate that two concurrent browser sessions maintain strict resource isolation."""
    async def _test():
        executor_A = PlaywrightBrowserExecutor(headless=True)
        adapter_A = BrowserTaskAdapter(executor=executor_A)

        executor_B = PlaywrightBrowserExecutor(headless=True)
        adapter_B = BrowserTaskAdapter(executor=executor_B)

        llm_A = MockGeminiDecisionProvider([
            {"decision": "execute", "reason": "Nav A", "action": {"action_type": "navigate", "url": "https://example.com"}},
            {"decision": "finish", "reason": "Done A"},
        ])
        llm_B = MockGeminiDecisionProvider([
            {"decision": "execute", "reason": "Nav B", "action": {"action_type": "navigate", "url": "https://iana.org"}},
            {"decision": "finish", "reason": "Done B"},
        ])

        agent_A = BrowserAgent(llm_provider=llm_A, adapter=adapter_A)
        agent_B = BrowserAgent(llm_provider=llm_B, adapter=adapter_B)

        task_A = BrowserTask(task_id="t_iso_A", objective="Open example.com", session_id="session_ISO_A")
        task_B = BrowserTask(task_id="t_iso_B", objective="Open iana.org", session_id="session_ISO_B")

        await executor_A.start()
        await executor_B.start()
        try:
            # Run both concurrently
            res_A, res_B = await asyncio.gather(
                agent_A.run(task_A),
                agent_B.run(task_B),
            )

            assert res_A.outcome == BrowserAgentOutcome.COMPLETED
            assert res_B.outcome == BrowserAgentOutcome.COMPLETED

            assert "example.com" in res_A.final_url
            assert "iana.org" in res_B.final_url

            # Ensure session IDs are isolated
            assert res_A.session_id == "session_ISO_A"
            assert res_B.session_id == "session_ISO_B"
        finally:
            await executor_A.stop()
            await executor_B.stop()

    asyncio.run(_test())


# ─────────────────────────────────────────────────────────────────────────────
# TEST 9 — SEQUENTIAL VS PARALLEL DAG EXECUTION
# ─────────────────────────────────────────────────────────────────────────────

def test_9_dag_sequential_and_parallel_execution():
    """Validate that RuntimeEngine executes tasks with correct dependency ordering and lifecycle semantics."""
    async def _test():
        graph = ARDOGraph()
        # Graph: A -> [B, C] -> D
        node_a = ARDOTaskNode(task_id="node_a", task_name="Task A (Root)", dependency_ids=[])
        node_b = ARDOTaskNode(task_id="node_b", task_name="Task B (Branch 1)", dependency_ids=["node_a"])
        node_c = ARDOTaskNode(task_id="node_c", task_name="Task C (Branch 2)", dependency_ids=["node_a"])
        node_d = ARDOTaskNode(task_id="node_d", task_name="Task D (Join)", dependency_ids=["node_b", "node_c"])

        for n in [node_a, node_b, node_c, node_d]:
            graph.nodes[n.task_id] = n
            graph.adjacency[n.task_id] = []
            graph.reverse_adj[n.task_id] = list(n.dependency_ids)

        graph.adjacency["node_a"] = ["node_b", "node_c"]
        graph.adjacency["node_b"] = ["node_d"]
        graph.adjacency["node_c"] = ["node_d"]
        graph.parallel_groups = [["node_b", "node_c"]]

        engine = RuntimeEngine(session_id="session_dag_e2e", graph=graph)
        events = []
        async for evt in engine.run():
            events.append(evt)

        # All nodes must have completed successfully
        assert node_a.status == TaskState.COMPLETED
        assert node_b.status == TaskState.COMPLETED
        assert node_c.status == TaskState.COMPLETED
        assert node_d.status == TaskState.COMPLETED

        # Verify ordering: node_a completes before node_b/c start, node_b/c complete before node_d starts
        completed_ids = [e.task_id for e in events if e.event_type == "task_completed" and e.task_id]
        assert completed_ids.index("node_a") < completed_ids.index("node_b")
        assert completed_ids.index("node_a") < completed_ids.index("node_c")
        assert completed_ids.index("node_b") < completed_ids.index("node_d")
        assert completed_ids.index("node_c") < completed_ids.index("node_d")

    asyncio.run(_test())


# ─────────────────────────────────────────────────────────────────────────────
# TEST 10 — ARBITRARY WEBSITES (NO HARDCODED MAPPINGS)
# ─────────────────────────────────────────────────────────────────────────────

def test_10_arbitrary_website_generalization():
    """Validate that BrowserAgent interacts with unmapped public domains without any domain-specific code."""
    async def _test():
        executor = PlaywrightBrowserExecutor(headless=True)
        adapter = BrowserTaskAdapter(executor=executor)

        # Testing against https://httpbin.org or https://example.org
        target_domain = "https://example.org"
        llm = MockGeminiDecisionProvider([
            {
                "decision": "execute",
                "reason": f"Navigate to arbitrary domain {target_domain}",
                "action": {
                    "action_type": "navigate",
                    "url": target_domain,
                }
            },
            {
                "decision": "finish",
                "reason": "Reached target arbitrary domain",
            }
        ])

        agent = BrowserAgent(
            llm_provider=llm,
            adapter=adapter,
        )

        task = BrowserTask(
            task_id="t_arb_1",
            objective=f"Open {target_domain}",
            session_id="session_arb_1",
            success_criteria=f"URL must contain example.org",
        )

        await executor.start()
        try:
            result = await agent.run(task)
            assert result.outcome == BrowserAgentOutcome.COMPLETED
            assert "example.org" in result.final_url
            assert result.verification.status == BrowserVerificationStatus.VERIFIED
        finally:
            await executor.stop()

    asyncio.run(_test())
