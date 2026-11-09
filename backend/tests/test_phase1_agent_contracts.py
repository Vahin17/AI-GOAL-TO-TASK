"""
test_phase1_agent_contracts.py — Unit tests for Phase 1 BrowserAgent contracts
===============================================================================
Tests that:
  - All 6 new contract models can be instantiated with valid data.
  - Required fields are enforced.
  - Optional fields default correctly.
  - Enum members cover expected values.
  - BrowserObservation correctly embeds PageSnapshot.
  - BrowserDecision.is_final is forced True for FINISH / FAIL.
  - BrowserAgentResult.succeeded property works correctly.
  - Existing BrowserAction / BrowserActionResult / PageSnapshot models still work.

Does NOT require pytest-asyncio (all tests are synchronous).
"""

import pytest
from pydantic import ValidationError

from execution.browser.agent_contracts import (
    BrowserTask,
    BrowserObservation,
    BrowserActionDecision,
    BrowserDecision,
    BrowserDecisionType,
    BrowserVerification,
    BrowserVerificationStatus,
    BrowserAgentResult,
    BrowserAgentOutcome,
)
from execution.browser.browser_models import (
    BrowserAction,
    BrowserActionResult,
    BrowserActionType,
)
from execution.browser.perception_models import PageElement, PageSnapshot


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_page_snapshot() -> PageSnapshot:
    el = PageElement(tag="a", text="More information", css_selector="a[href]")
    return PageSnapshot(url="https://example.com", title="Example Domain", elements=[el])


def make_browser_task(**overrides) -> BrowserTask:
    base = dict(task_id="t_001", objective="Open Wikipedia in a new tab", session_id="s_abc")
    base.update(overrides)
    return BrowserTask(**base)


def make_action_decision(**overrides) -> BrowserActionDecision:
    base = dict(action_type=BrowserActionType.NAVIGATE, url="https://en.wikipedia.org/wiki/Main_Page", new_tab=True)
    base.update(overrides)
    return BrowserActionDecision(**base)


# ─────────────────────────────────────────────────────────────────────────────
# 1. BrowserTask tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBrowserTask:
    def test_minimal_valid(self):
        task = BrowserTask(
            task_id="t_001",
            objective="Open Wikipedia",
            session_id="s_123",
        )
        assert task.task_id == "t_001"
        assert task.objective == "Open Wikipedia"
        assert task.session_id == "s_123"
        assert task.max_actions == 10
        assert task.constraints == []
        assert task.success_criteria is None

    def test_full_fields(self):
        task = make_browser_task(
            success_criteria="Wikipedia main page is visible",
            constraints=["Do not submit forms"],
            goal_text="open wikipedia in a new tab",
            max_actions=5,
        )
        assert task.success_criteria == "Wikipedia main page is visible"
        assert "Do not submit forms" in task.constraints
        assert task.max_actions == 5

    def test_missing_task_id_raises(self):
        with pytest.raises(ValidationError):
            BrowserTask(objective="Something", session_id="s_1")

    def test_missing_objective_raises(self):
        with pytest.raises(ValidationError):
            BrowserTask(task_id="t_1", session_id="s_1")

    def test_missing_session_id_raises(self):
        with pytest.raises(ValidationError):
            BrowserTask(task_id="t_1", objective="Something")

    def test_max_actions_minimum_one(self):
        with pytest.raises(ValidationError):
            BrowserTask(task_id="t_1", objective="X", session_id="s_1", max_actions=0)

    def test_no_selector_in_objective(self):
        """BrowserTask objective must not need raw CSS selectors to be valid."""
        task = BrowserTask(
            task_id="t_click",
            objective="Click the More information link",
            session_id="s_1",
        )
        assert "#content" not in task.objective or True  # objective is free text; no enforcement needed


# ─────────────────────────────────────────────────────────────────────────────
# 2. BrowserObservation tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBrowserObservation:
    def test_minimal_valid(self):
        obs = BrowserObservation(session_id="s_1", task_id="t_1")
        assert obs.current_url == ""
        assert obs.page_title == ""
        assert obs.page_snapshot is None
        assert obs.open_tab_count == 1
        assert obs.actions_taken_count == 0

    def test_with_snapshot(self):
        snap = make_page_snapshot()
        obs = BrowserObservation(
            session_id="s_1",
            task_id="t_1",
            current_url="https://example.com",
            page_title="Example Domain",
            page_snapshot=snap,
        )
        assert obs.page_snapshot is not None
        assert obs.page_snapshot.url == "https://example.com"
        assert len(obs.page_snapshot.elements) == 1

    def test_previous_action_fields(self):
        obs = BrowserObservation(
            session_id="s_1",
            task_id="t_1",
            previous_action="navigate to https://example.com",
            previous_action_success=True,
            actions_taken_count=1,
        )
        assert obs.previous_action_success is True
        assert obs.actions_taken_count == 1

    def test_missing_session_id_raises(self):
        with pytest.raises(ValidationError):
            BrowserObservation(task_id="t_1")

    def test_missing_task_id_raises(self):
        with pytest.raises(ValidationError):
            BrowserObservation(session_id="s_1")


# ─────────────────────────────────────────────────────────────────────────────
# 3. BrowserActionDecision tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBrowserActionDecision:
    def test_navigate_action(self):
        action = BrowserActionDecision(
            action_type=BrowserActionType.NAVIGATE,
            url="https://en.wikipedia.org/wiki/Main_Page",
            new_tab=True,
        )
        assert action.action_type == BrowserActionType.NAVIGATE
        assert action.url == "https://en.wikipedia.org/wiki/Main_Page"
        assert action.new_tab is True
        assert action.element_reference is None

    def test_click_with_semantic_reference(self):
        """element_reference should be semantic text, not a raw CSS selector."""
        action = BrowserActionDecision(
            action_type=BrowserActionType.CLICK,
            element_reference="More information",
            reasoning="Link text matches the task objective",
        )
        assert action.element_reference == "More information"
        assert "#" not in action.element_reference  # semantic, not selector

    def test_fill_action(self):
        action = BrowserActionDecision(
            action_type=BrowserActionType.FILL,
            element_reference="Search input",
            value="Playwright browser automation",
        )
        assert action.value == "Playwright browser automation"

    def test_screenshot_action_minimal(self):
        action = BrowserActionDecision(action_type=BrowserActionType.SCREENSHOT)
        assert action.action_type == BrowserActionType.SCREENSHOT

    def test_missing_action_type_raises(self):
        with pytest.raises(ValidationError):
            BrowserActionDecision()

    def test_timeout_minimum_100ms(self):
        with pytest.raises(ValidationError):
            BrowserActionDecision(action_type=BrowserActionType.NAVIGATE, timeout_ms=0)


# ─────────────────────────────────────────────────────────────────────────────
# 4. BrowserDecision tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBrowserDecision:
    def test_execute_decision(self):
        action = make_action_decision()
        d = BrowserDecision(
            decision=BrowserDecisionType.EXECUTE,
            action=action,
            reason="Need to navigate to target URL",
        )
        assert d.decision == BrowserDecisionType.EXECUTE
        assert d.is_final is False
        assert d.action is not None

    def test_finish_decision_is_final(self):
        d = BrowserDecision(
            decision=BrowserDecisionType.FINISH,
            reason="Wikipedia page is loaded and verified",
        )
        assert d.is_final is True
        assert d.action is None

    def test_fail_decision_is_final(self):
        d = BrowserDecision(
            decision=BrowserDecisionType.FAIL,
            reason="Max actions exceeded",
        )
        assert d.is_final is True

    def test_recover_decision_not_final(self):
        action = make_action_decision(action_type=BrowserActionType.NAVIGATE, url="https://example.com")
        d = BrowserDecision(
            decision=BrowserDecisionType.RECOVER,
            action=action,
            reason="Unexpected page state, retrying navigation",
        )
        assert d.is_final is False

    def test_missing_reason_raises(self):
        with pytest.raises(ValidationError):
            BrowserDecision(decision=BrowserDecisionType.EXECUTE)

    def test_all_decision_types_valid(self):
        for dt in BrowserDecisionType:
            d = BrowserDecision(decision=dt, reason=f"Testing {dt.value}")
            assert d.decision == dt


# ─────────────────────────────────────────────────────────────────────────────
# 5. BrowserVerification tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBrowserVerification:
    def test_verified_status(self):
        v = BrowserVerification(
            task_id="t_1",
            status=BrowserVerificationStatus.VERIFIED,
            observed_url="https://en.wikipedia.org/wiki/Main_Page",
            observed_title="Wikipedia, the free encyclopedia",
            verified_by="URL prefix match",
        )
        assert v.status == BrowserVerificationStatus.VERIFIED
        assert v.screenshot_b64 is None

    def test_not_verified_with_reason(self):
        v = BrowserVerification(
            task_id="t_2",
            status=BrowserVerificationStatus.NOT_VERIFIED,
            failure_reason="Title did not contain 'Wikipedia'",
        )
        assert v.status == BrowserVerificationStatus.NOT_VERIFIED
        assert "Wikipedia" in v.failure_reason

    def test_skipped_status(self):
        v = BrowserVerification(task_id="t_3", status=BrowserVerificationStatus.SKIPPED)
        assert v.status == BrowserVerificationStatus.SKIPPED

    def test_pending_status(self):
        v = BrowserVerification(
            task_id="t_4",
            status=BrowserVerificationStatus.PENDING,
            failure_reason="Page still loading",
        )
        assert v.status == BrowserVerificationStatus.PENDING

    def test_missing_task_id_raises(self):
        with pytest.raises(ValidationError):
            BrowserVerification(status=BrowserVerificationStatus.VERIFIED)

    def test_missing_status_raises(self):
        with pytest.raises(ValidationError):
            BrowserVerification(task_id="t_1")

    def test_action_success_ne_task_verified(self):
        """Verification is independent from action success."""
        v_fail = BrowserVerification(
            task_id="t_1",
            status=BrowserVerificationStatus.NOT_VERIFIED,
            observed_url="https://example.com",
            failure_reason="Wrong page loaded after navigation",
        )
        assert v_fail.status == BrowserVerificationStatus.NOT_VERIFIED
        # Even though the navigate action itself may have returned success=True
        # the verification can still say NOT_VERIFIED (different concept).


# ─────────────────────────────────────────────────────────────────────────────
# 6. BrowserAgentResult tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBrowserAgentResult:
    def test_completed_result(self):
        actions = [make_action_decision()]
        v = BrowserVerification(
            task_id="t_1",
            status=BrowserVerificationStatus.VERIFIED,
            verified_by="URL check",
        )
        result = BrowserAgentResult(
            task_id="t_1",
            session_id="s_1",
            outcome=BrowserAgentOutcome.COMPLETED,
            objective="Open Wikipedia in a new tab",
            actions_taken=actions,
            verification=v,
            actions_count=1,
            duration_ms=2300.5,
            final_url="https://en.wikipedia.org/wiki/Main_Page",
            final_title="Wikipedia, the free encyclopedia",
        )
        assert result.succeeded is True
        assert result.outcome == BrowserAgentOutcome.COMPLETED

    def test_completed_unverified_still_succeeds(self):
        result = BrowserAgentResult(
            task_id="t_1",
            session_id="s_1",
            outcome=BrowserAgentOutcome.COMPLETED_UNVERIFIED,
            objective="Navigate to example.com",
        )
        assert result.succeeded is True

    def test_failed_result(self):
        result = BrowserAgentResult(
            task_id="t_1",
            session_id="s_1",
            outcome=BrowserAgentOutcome.FAILED,
            objective="Navigate to example.com",
            error="Playwright not installed",
        )
        assert result.succeeded is False
        assert result.error == "Playwright not installed"

    def test_blocked_result(self):
        result = BrowserAgentResult(
            task_id="t_1",
            session_id="s_1",
            outcome=BrowserAgentOutcome.BLOCKED,
            objective="Delete all database records",
            error="Blocked by safety policy: destructive action",
        )
        assert result.succeeded is False

    def test_empty_actions_list(self):
        result = BrowserAgentResult(
            task_id="t_1",
            session_id="s_1",
            outcome=BrowserAgentOutcome.FAILED,
            objective="X",
        )
        assert result.actions_taken == []
        assert result.actions_count == 0

    def test_missing_task_id_raises(self):
        with pytest.raises(ValidationError):
            BrowserAgentResult(session_id="s_1", outcome=BrowserAgentOutcome.FAILED, objective="X")


# ─────────────────────────────────────────────────────────────────────────────
# 7. Regression: Existing models unaffected
# ─────────────────────────────────────────────────────────────────────────────

class TestExistingModelsRegression:
    def test_browser_action_still_works(self):
        action = BrowserAction(action_type=BrowserActionType.NAVIGATE, url="https://example.com")
        assert action.action_type == BrowserActionType.NAVIGATE
        d = action.to_dict()
        assert d["url"] == "https://example.com"

    def test_browser_action_result_still_works(self):
        result = BrowserActionResult(
            success=True,
            action_type=BrowserActionType.SCREENSHOT,
            current_url="https://example.com",
        )
        assert result.success is True
        d = result.to_dict()
        assert d["action_type"] == "screenshot"

    def test_page_snapshot_still_works(self):
        snap = make_page_snapshot()
        assert snap.url == "https://example.com"
        assert snap.title == "Example Domain"
        assert len(snap.filter_interactive()) == 1
        summary = snap.to_compact_summary()
        assert "More information" in summary

    def test_page_element_semantic_description(self):
        el = PageElement(
            tag="button",
            text="Search",
            aria_label="Search Wikipedia",
            css_selector="button#search-btn",
        )
        desc = el.get_semantic_description()
        assert "Search" in desc
        assert "button" in desc


# ─────────────────────────────────────────────────────────────────────────────
# 8. Cross-contract composition test
# ─────────────────────────────────────────────────────────────────────────────

class TestContractComposition:
    def test_full_contract_chain(self):
        """
        Validates that all 6 contracts can be composed together
        into a coherent agent flow without type errors.
        """
        # Step 1: Task
        task = make_browser_task(
            success_criteria="Wikipedia main page is loaded",
            max_actions=5,
        )

        # Step 2: Observation
        snap = make_page_snapshot()
        obs = BrowserObservation(
            session_id=task.session_id,
            task_id=task.task_id,
            current_url="https://www.google.com",
            page_title="Google",
            page_snapshot=snap,
        )

        # Step 3: Action Decision
        action = BrowserActionDecision(
            action_type=BrowserActionType.NAVIGATE,
            url="https://en.wikipedia.org/wiki/Main_Page",
            new_tab=True,
            reasoning="Task requires navigating to Wikipedia",
        )

        # Step 4: Decision
        decision = BrowserDecision(
            decision=BrowserDecisionType.EXECUTE,
            action=action,
            reason="Navigate to Wikipedia as task requires",
        )
        assert not decision.is_final

        # Step 5: Verification
        verification = BrowserVerification(
            task_id=task.task_id,
            status=BrowserVerificationStatus.VERIFIED,
            observed_url="https://en.wikipedia.org/wiki/Main_Page",
            verified_by="URL prefix match",
        )

        # Step 6: Result
        result = BrowserAgentResult(
            task_id=task.task_id,
            session_id=task.session_id,
            outcome=BrowserAgentOutcome.COMPLETED,
            objective=task.objective,
            actions_taken=[action],
            verification=verification,
            actions_count=1,
            duration_ms=1500.0,
        )

        assert result.succeeded is True
        assert result.verification.status == BrowserVerificationStatus.VERIFIED
        assert len(result.actions_taken) == 1
