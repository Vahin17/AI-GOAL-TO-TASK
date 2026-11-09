"""
test_phase7_safety_consolidation.py — Unit tests for Phase 7 Safety Consolidation
=================================================================================
Tests that:
- BrowserTaskAdapter is the single authoritative safety boundary for browser execution
- BrowserTool routes all actions through BrowserTaskAdapter (no direct bypass to executor)
- Blocked actions never reach PlaywrightBrowserExecutor
- Approval-required actions do not execute without explicit approval
- Permitted actions execute cleanly and preserve safety decision metadata
- BrowserAgent continues to use the authoritative safe execution path
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from execution.browser.browser_models import (
    BrowserAction,
    BrowserActionResult,
    BrowserActionType,
)
from execution.browser.browser_tool import BrowserTool
from execution.browser.browser_task_adapter import BrowserTaskAdapter
from execution.browser.browser_agent import BrowserAgent
from execution.browser.agent_contracts import (
    BrowserTask,
    BrowserObservation,
    BrowserAgentOutcome,
    BrowserVerificationStatus,
)
from execution.registry.tool_models import ToolCall, ToolResult
from orchestration.safety_schemas import (
    ExecutionDecision,
    RiskLevel,
    SafetyDecision,
)


# ─────────────────────────────────────────────────────────────────────────────
# Test Fixtures & Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_safety_dec(
    decision: ExecutionDecision = ExecutionDecision.ALLOW,
    risk: RiskLevel = RiskLevel.LOW,
    explanation: str = "Test decision",
) -> SafetyDecision:
    return SafetyDecision(
        task_id="t_test",
        task_name="test_task",
        risk_level=risk,
        execution_decision=decision,
        explanation=explanation,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. BrowserTool Routes Through BrowserTaskAdapter Safe Boundary
# ─────────────────────────────────────────────────────────────────────────────

class TestBrowserToolSafetyConsolidation:
    def test_browser_tool_calls_adapter_execute_safe_browser_task(self):
        """BrowserTool.execute() must route through adapter.execute_safe_browser_task."""
        mock_adapter = MagicMock()
        mock_adapter.execute_safe_browser_task = AsyncMock(return_value=(
            BrowserActionResult(
                success=True,
                action_type=BrowserActionType.NAVIGATE,
                current_url="https://example.com",
            ),
            make_safety_dec(ExecutionDecision.ALLOW),
        ))

        tool = BrowserTool(adapter=mock_adapter)
        call = ToolCall(
            call_id="c1",
            tool_name="browser",
            action_name="navigate",
            arguments={"url": "https://example.com"},
        )

        res = asyncio.run(tool.execute(call))

        assert res.success is True
        mock_adapter.execute_safe_browser_task.assert_awaited_once()

    def test_blocked_action_does_not_reach_playwright_executor(self):
        """When safety policy blocks an action in BrowserTool, executor is never called."""
        mock_executor = MagicMock()
        mock_executor.execute_action = AsyncMock()

        adapter = BrowserTaskAdapter(executor=mock_executor)
        # Mock policy engine to block
        adapter.safety_policy_engine = MagicMock()
        adapter.safety_policy_engine.evaluate = MagicMock(return_value=make_safety_dec(
            ExecutionDecision.BLOCK, RiskLevel.CRITICAL, "Blocked by test policy"
        ))

        tool = BrowserTool(adapter=adapter)
        call = ToolCall(
            call_id="c_blocked",
            tool_name="browser",
            action_name="click",
            arguments={"selector": "button#dangerous-action"},
        )

        res = asyncio.run(tool.execute(call))

        assert res.success is False
        assert "BLOCKED_BY_SAFETY_POLICY" in (res.error or "")
        assert res.metadata.get("is_blocked") is True
        assert res.metadata.get("execution_decision") == "block"
        # PlaywrightExecutor must NOT be reached
        mock_executor.execute_action.assert_not_called()

    def test_approval_required_action_halts_without_approval(self):
        """When action requires approval and is not approved, executor is not reached."""
        mock_executor = MagicMock()
        mock_executor.execute_action = AsyncMock()

        adapter = BrowserTaskAdapter(executor=mock_executor)
        adapter.safety_policy_engine = MagicMock()
        adapter.safety_policy_engine.evaluate = MagicMock(return_value=make_safety_dec(
            ExecutionDecision.REQUIRE_APPROVAL, RiskLevel.HIGH, "Approval required for high risk"
        ))
        adapter.safety_engine = MagicMock()
        adapter.safety_engine.is_approved = MagicMock(return_value=False)

        tool = BrowserTool(adapter=adapter)
        call = ToolCall(
            call_id="c_approval",
            tool_name="browser",
            action_name="fill",
            arguments={"selector": "input#api-key", "value": "secret"},
        )

        res = asyncio.run(tool.execute(call))

        assert res.success is False
        assert "APPROVAL_REQUIRED" in (res.error or "")
        assert res.metadata.get("approval_required") is True
        mock_executor.execute_action.assert_not_called()

    def test_permitted_action_executes_and_preserves_safety_metadata(self):
        """Permitted action executes via executor and returns safety metadata in ToolResult."""
        mock_executor = MagicMock()
        mock_executor.execute_action = AsyncMock(return_value=BrowserActionResult(
            success=True,
            action_type=BrowserActionType.NAVIGATE,
            current_url="https://example.com/allowed",
            current_title="Allowed Page",
        ))

        adapter = BrowserTaskAdapter(executor=mock_executor)
        adapter.safety_policy_engine = MagicMock()
        adapter.safety_policy_engine.evaluate = MagicMock(return_value=make_safety_dec(
            ExecutionDecision.ALLOW, RiskLevel.LOW, "Action permitted"
        ))

        tool = BrowserTool(adapter=adapter)
        call = ToolCall(
            call_id="c_allow",
            tool_name="browser",
            action_name="navigate",
            arguments={"url": "https://example.com/allowed"},
        )

        res = asyncio.run(tool.execute(call))

        assert res.success is True
        assert res.metadata.get("execution_decision") == "allow"
        assert res.metadata.get("risk_level") == "low"
        assert res.metadata.get("is_blocked") is False
        mock_executor.execute_action.assert_awaited_once()


# ─────────────────────────────────────────────────────────────────────────────
# 2. BrowserAgent Continues Using Authoritative Safe Boundary
# ─────────────────────────────────────────────────────────────────────────────

class TestBrowserAgentSafetyIntegration:
    def test_browser_agent_blocked_action_halts_without_executing(self):
        """BrowserAgent respects the single safety boundary and stops when blocked."""
        gemini_decisions = [
            json.dumps({
                "decision": "execute",
                "reason": "Perform destructive action",
                "action": {"action_type": "click", "element_ref": None, "url": None, "value": None, "key": None, "new_tab": False},
            }),
        ]
        llm = MagicMock()
        llm.query = AsyncMock(return_value=gemini_decisions[0])

        mock_executor = MagicMock()
        mock_executor.execute_action = AsyncMock()

        adapter = BrowserTaskAdapter(executor=mock_executor)
        adapter.safety_policy_engine = MagicMock()
        adapter.safety_policy_engine.evaluate = MagicMock(return_value=make_safety_dec(
            ExecutionDecision.BLOCK, RiskLevel.CRITICAL, "Critical destructive op blocked"
        ))

        obs = BrowserObservation(
            session_id="s1",
            task_id="t1",
            current_url="https://example.com",
            page_title="Example",
        )
        perception = MagicMock()
        perception.observe = AsyncMock(return_value=obs)

        agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
        task = BrowserTask(task_id="t1", objective="Delete all records", session_id="s1")

        result = asyncio.run(agent.run(task))

        assert result.outcome == BrowserAgentOutcome.BLOCKED
        assert "BLOCKED_BY_SAFETY_POLICY" in (result.error or "")
        mock_executor.execute_action.assert_not_called()
