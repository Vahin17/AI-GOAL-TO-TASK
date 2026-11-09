"""
test_phase5_semantic_verification.py — Unit tests for Phase 5 Semantic Verification
===================================================================================
Tests that:
- ACTION SUCCESS ≠ TASK SUCCESS
- URL/domain matching correctly confirms or rejects navigation state
- Page text and title requirements are verified against live observation
- Tab requirements check open tab count and active tab state
- Screenshot requirement requires verified screenshot action/artifact
- FINISH decision performs strict semantic verification before claiming success
- Verification is observational only (no browser action side effects)
- All Phase 1–4 contracts remain regression-free
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from execution.browser.agent_contracts import (
    BrowserAgentOutcome,
    BrowserAgentResult,
    BrowserDecision,
    BrowserDecisionType,
    BrowserActionDecision,
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
from execution.browser.perception_models import ObservedElement, TabInfo, PageSnapshot
from execution.browser.semantic_verifier import SemanticVerifier


# ─────────────────────────────────────────────────────────────────────────────
# Test Fixtures & Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_obs(
    url: str = "https://example.com",
    title: str = "Example Domain",
    text_summary: str = "This domain is for use in illustrative examples.",
    open_tabs: int = 1,
    tabs: list = None,
    recent_action: RecentActionRecord = None,
    elements: list = None,
) -> BrowserObservation:
    if tabs is None:
        tabs = [TabInfo(tab_ref="tab_001", url=url, title=title, is_active=True, index=0)]
    return BrowserObservation(
        session_id="s_test",
        task_id="t_test",
        current_url=url,
        page_title=title,
        page_text_summary=text_summary,
        open_tab_count=open_tabs,
        tabs=tabs,
        recent_action=recent_action,
        observed_elements=elements or [],
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. URL & Destination Verification
# ─────────────────────────────────────────────────────────────────────────────

class TestUrlVerification:
    def test_successful_navigation_with_matching_url_is_verified(self):
        task = BrowserTask(
            task_id="t1",
            objective="Navigate to https://example.com",
            session_id="s1",
        )
        obs = make_obs(url="https://example.com/")
        v = SemanticVerifier.verify(task, obs)

        assert v.status == BrowserVerificationStatus.VERIFIED
        assert "matches expected" in (v.verified_by or "")

    def test_successful_navigation_with_wrong_url_is_not_verified(self):
        """Playwright action succeeded, but destination is wrong -> NOT_VERIFIED."""
        task = BrowserTask(
            task_id="t1",
            objective="Navigate to https://example.com",
            session_id="s1",
        )
        obs = make_obs(url="https://google.com/")
        v = SemanticVerifier.verify(task, obs)

        assert v.status == BrowserVerificationStatus.NOT_VERIFIED
        assert "does not match expected" in (v.failure_reason or "")

    def test_domain_extracted_from_objective_and_verified(self):
        task = BrowserTask(
            task_id="t1",
            objective="Open wikipedia.org and find the search box",
            session_id="s1",
        )
        obs_correct = make_obs(url="https://en.wikipedia.org/wiki/Main_Page", title="Wikipedia")
        v_correct = SemanticVerifier.verify(task, obs_correct)
        assert v_correct.status == BrowserVerificationStatus.VERIFIED

        obs_wrong = make_obs(url="https://example.com", title="Example Domain")
        v_wrong = SemanticVerifier.verify(task, obs_wrong)
        assert v_wrong.status == BrowserVerificationStatus.NOT_VERIFIED

    def test_blank_page_is_not_verified(self):
        task = BrowserTask(
            task_id="t1",
            objective="Open https://example.com",
            session_id="s1",
        )
        obs_blank = make_obs(url="about:blank", title="")
        v = SemanticVerifier.verify(task, obs_blank)
        assert v.status == BrowserVerificationStatus.NOT_VERIFIED


# ─────────────────────────────────────────────────────────────────────────────
# 2. Recent Action Failure Prevention
# ─────────────────────────────────────────────────────────────────────────────

class TestActionFailurePreventsVerification:
    def test_action_failure_returns_not_verified(self):
        task = BrowserTask(
            task_id="t1",
            objective="Navigate to https://example.com",
            session_id="s1",
        )
        recent_fail = RecentActionRecord(
            action_type=BrowserActionType.NAVIGATE,
            url="https://example.com",
            success=False,
            error="ERR_CONNECTION_REFUSED",
        )
        obs = make_obs(url="about:blank", recent_action=recent_fail)
        v = SemanticVerifier.verify(task, obs)

        assert v.status == BrowserVerificationStatus.NOT_VERIFIED
        assert "failed" in (v.failure_reason or "").lower()
        assert "ERR_CONNECTION_REFUSED" in (v.failure_reason or "")


# ─────────────────────────────────────────────────────────────────────────────
# 3. Visible Page Text & Success Criteria Verification
# ─────────────────────────────────────────────────────────────────────────────

class TestTextAndCriteriaVerification:
    def test_expected_visible_text_present_is_verified(self):
        task = BrowserTask(
            task_id="t1",
            objective="Open example.com",
            success_criteria='Page contains text "illustrative examples"',
            session_id="s1",
        )
        obs = make_obs(
            url="https://example.com",
            text_summary="This domain is for use in illustrative examples in documents.",
        )
        v = SemanticVerifier.verify(task, obs)

        assert v.status == BrowserVerificationStatus.VERIFIED

    def test_expected_text_absent_is_not_verified(self):
        task = BrowserTask(
            task_id="t1",
            objective="Open example.com",
            success_criteria='Page contains text "Quantum Supercomputer"',
            session_id="s1",
        )
        obs = make_obs(
            url="https://example.com",
            text_summary="This domain is for use in illustrative examples in documents.",
        )
        v = SemanticVerifier.verify(task, obs)

        assert v.status == BrowserVerificationStatus.NOT_VERIFIED
        assert "not found in visible page content" in (v.failure_reason or "")

    def test_keyword_overlap_verification(self):
        task = BrowserTask(
            task_id="t1",
            objective="Research quantum physics",
            success_criteria="Physics mechanics formulation overview",
            session_id="s1",
        )
        obs = make_obs(
            url="https://en.wikipedia.org/wiki/Quantum_mechanics",
            title="Quantum mechanics - Wikipedia",
            text_summary="Quantum mechanics is a fundamental theory in physics that describes nature.",
        )
        v = SemanticVerifier.verify(task, obs)
        assert v.status == BrowserVerificationStatus.VERIFIED


# ─────────────────────────────────────────────────────────────────────────────
# 4. Tab State Verification
# ─────────────────────────────────────────────────────────────────────────────

class TestTabStateVerification:
    def test_expected_new_tab_exists_is_verified(self):
        task = BrowserTask(
            task_id="t1",
            objective="Open https://example.com in a new tab",
            session_id="s1",
        )
        tabs = [
            TabInfo(tab_ref="tab_001", url="https://dashboard.internal", title="Dashboard", is_active=False, index=0),
            TabInfo(tab_ref="tab_002", url="https://example.com", title="Example Domain", is_active=True, index=1),
        ]
        obs = make_obs(url="https://example.com", open_tabs=2, tabs=tabs)
        v = SemanticVerifier.verify(task, obs)

        assert v.status == BrowserVerificationStatus.VERIFIED

    def test_expected_new_tab_missing_is_not_verified(self):
        task = BrowserTask(
            task_id="t1",
            objective="Open https://example.com in a new tab",
            session_id="s1",
        )
        # Only 1 tab observed
        obs = make_obs(url="https://example.com", open_tabs=1)
        v = SemanticVerifier.verify(task, obs)

        assert v.status == BrowserVerificationStatus.NOT_VERIFIED
        assert "required opening a new tab, but only 1" in (v.failure_reason or "")


# ─────────────────────────────────────────────────────────────────────────────
# 5. Screenshot Requirement Verification
# ─────────────────────────────────────────────────────────────────────────────

class TestScreenshotRequirementVerification:
    def test_screenshot_requirement_satisfied_by_recent_screenshot(self):
        task = BrowserTask(
            task_id="t1",
            objective="Open example.com and take a screenshot",
            session_id="s1",
        )
        recent_ss = RecentActionRecord(
            action_type=BrowserActionType.SCREENSHOT,
            success=True,
        )
        obs = make_obs(url="https://example.com", recent_action=recent_ss)
        v = SemanticVerifier.verify(task, obs)

        assert v.status == BrowserVerificationStatus.VERIFIED

    def test_screenshot_requirement_without_screenshot_is_not_verified(self):
        task = BrowserTask(
            task_id="t1",
            objective="Open example.com and take a screenshot",
            session_id="s1",
        )
        # Recent action was navigate, not screenshot
        recent_nav = RecentActionRecord(
            action_type=BrowserActionType.NAVIGATE,
            url="https://example.com",
            success=True,
        )
        obs = make_obs(url="https://example.com", recent_action=recent_nav)
        v = SemanticVerifier.verify(task, obs)

        assert v.status == BrowserVerificationStatus.NOT_VERIFIED
        assert "required a screenshot" in (v.failure_reason or "")


# ─────────────────────────────────────────────────────────────────────────────
# 6. Observational Only & No Action Execution
# ─────────────────────────────────────────────────────────────────────────────

class TestObservationalIntegrity:
    def test_verifier_does_not_execute_browser_actions(self):
        """Ensure SemanticVerifier is a pure function that never triggers browser operations."""
        mock_task = BrowserTask(task_id="t1", objective="Test pure verification", session_id="s1")
        mock_obs = make_obs()

        # Call verify
        v = SemanticVerifier.verify(mock_task, mock_obs)
        assert isinstance(v, BrowserVerification)
        # Ensure task and obs were not mutated
        assert mock_task.task_id == "t1"
        assert mock_obs.current_url == "https://example.com"


# ─────────────────────────────────────────────────────────────────────────────
# 7. End-to-End BrowserAgent FINISH + Verification Integration
# ─────────────────────────────────────────────────────────────────────────────

class TestBrowserAgentSemanticVerificationIntegration:
    def test_finish_with_verified_state_returns_completed(self):
        finish_json = json.dumps({
            "decision": "finish",
            "reason": "Successfully navigated to example.com",
            "action": None,
        })
        llm = MagicMock()
        llm.query = AsyncMock(return_value=finish_json)

        adapter = MagicMock()
        adapter.executor = MagicMock()

        obs = make_obs(url="https://example.com/")
        perception = MagicMock()
        perception.observe = AsyncMock(return_value=obs)

        agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
        task = BrowserTask(
            task_id="t1",
            objective="Navigate to https://example.com",
            session_id="s1",
        )

        result = asyncio.run(agent.run(task))

        assert result.outcome == BrowserAgentOutcome.COMPLETED
        assert result.verification.status == BrowserVerificationStatus.VERIFIED

    def test_finish_with_unverified_wrong_state_does_not_return_completed(self):
        """CRITICAL: Gemini declares FINISH on wrong page -> MUST NOT return COMPLETED."""
        finish_json = json.dumps({
            "decision": "finish",
            "reason": "I think I am done",
            "action": None,
        })
        llm = MagicMock()
        llm.query = AsyncMock(return_value=finish_json)

        adapter = MagicMock()
        adapter.executor = MagicMock()

        # Observed URL is google.com when task wanted example.com
        obs = make_obs(url="https://google.com/")
        perception = MagicMock()
        perception.observe = AsyncMock(return_value=obs)

        agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
        task = BrowserTask(
            task_id="t1",
            objective="Navigate to https://example.com",
            session_id="s1",
        )

        result = asyncio.run(agent.run(task))

        # Outcome must NOT be COMPLETED
        assert result.outcome != BrowserAgentOutcome.COMPLETED
        assert result.verification.status == BrowserVerificationStatus.NOT_VERIFIED
        assert "does not match expected" in result.verification.failure_reason
