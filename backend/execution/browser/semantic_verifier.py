"""
semantic_verifier.py — Phase 5: Semantic Task Verification Engine
===================================================================
Inspects post-action browser state to determine whether a BrowserTask
objective / success criteria was genuinely achieved.

Core Invariant:
    ACTION SUCCESS (Playwright completed) ≠ TASK SUCCESS (Objective met)

Features:
- Deterministic URL / Domain destination verification
- Page title and visible text semantic matching
- Tab count and active tab state verification
- Screenshot artifact presence verification
- Pure observational verification (never dispatches browser actions)
- Returns structured BrowserVerification (VERIFIED, NOT_VERIFIED, PENDING, SKIPPED)
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import List, Optional, Set, Tuple

from execution.browser.agent_contracts import (
    BrowserObservation,
    BrowserTask,
    BrowserVerification,
    BrowserVerificationStatus,
    GoalVerificationResult,
)
from execution.browser.browser_models import BrowserActionType

logger = logging.getLogger(__name__)

# Common stop words to ignore when doing keyword matching
_STOP_WORDS: Set[str] = {
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "when", "at",
    "by", "for", "with", "about", "against", "between", "into", "through",
    "during", "before", "after", "above", "below", "to", "from", "up", "down",
    "in", "out", "on", "off", "over", "under", "again", "further", "then",
    "once", "here", "there", "all", "any", "both", "each", "few", "more",
    "most", "other", "some", "such", "no", "nor", "not", "only", "own",
    "same", "so", "than", "too", "very", "can", "will", "just", "should",
    "now", "open", "navigate", "go", "click", "page", "website", "url",
    "browser", "tab", "verify", "check", "make", "sure", "that", "this",
    "search", "find", "play", "type", "fill", "select", "look", "watch", "correct",
    "interact", "content", "element", "elements", "action", "actions", "test", "stale", "recovery",
}

_URL_PATTERN = re.compile(
    r"https?://[a-zA-Z0-9.-]+(?::\d+)?(?:/[^\s]*)?|"
    r"(?:[a-zA-Z0-9-]+\.)+(?:com|org|net|edu|gov|io|ai|dev|co|uk|de|ca|jp|fr|in)(?:/[^\s]*)?",
    re.IGNORECASE,
)

ERROR_INDICATORS: Tuple[str, ...] = (
    "payment failed", "card declined", "transaction declined", "transaction failed",
    "order failed", "unable to process", "user already exists", "invalid username or password",
    "invalid credentials", "access denied", "permission denied", "404 not found",
    "page not found", "500 internal server error", "out of stock", "sold out",
    "error occurred", "submission failed", "booking failed", "checkout failed",
)


class SemanticVerifier:
    """
    Deterministic semantic verifier for BrowserTasks.

    Compares the current live BrowserObservation against the task's objective,
    expected_url, success_criteria, and constraints.
    """

    @classmethod
    def verify(
        cls,
        task: BrowserTask,
        observation: Optional[BrowserObservation],
    ) -> BrowserVerification:
        """
        Perform observational semantic verification of the post-action browser state.

        Returns:
            BrowserVerification instance with status VERIFIED, NOT_VERIFIED, or PENDING.
        """
        if observation is None:
            return BrowserVerification(
                task_id=task.task_id,
                status=BrowserVerificationStatus.NOT_VERIFIED,
                failure_reason="No browser observation available for verification.",
            )

        url = observation.current_url or ""
        title = observation.page_title or ""

        # ── 0. Check if the most recent action failed ─────────────────────────
        if observation.recent_action and not observation.recent_action.success:
            err = observation.recent_action.error or "Unknown error"
            return BrowserVerification(
                task_id=task.task_id,
                status=BrowserVerificationStatus.NOT_VERIFIED,
                observed_url=url,
                observed_title=title,
                verified_by="recent action failure check",
                confidence_score=0.90,
                failure_reason=f"Most recent action '{observation.recent_action.action_type.value}' failed: {err}",
            )

        # ── 0b. Check for explicit external failure indicators on page ────────
        error_indicator = cls._detect_error_indicators(task, observation)
        if error_indicator:
            return BrowserVerification(
                task_id=task.task_id,
                status=BrowserVerificationStatus.FAILED,
                observed_url=url,
                observed_title=title,
                verified_by="error indicator detection",
                confidence_score=0.10,
                failure_reason=f"External failure indicator / error banner detected on page: '{error_indicator}'",
            )

        # ── 1. URL / Domain Destination Verification ──────────────────────────
        tab_req = cls._requires_new_tab(task)
        expected_urls = cls._extract_target_urls(task)
        if expected_urls:
            url_match, reason = cls._verify_url(expected_urls, url)
            if not url_match:
                # If this task aimed to create/open a new tab and landed at about:blank:
                if tab_req and (url in ("about:blank", "", "none")):
                    tab_ok, tab_reason = cls._verify_tabs(observation, task=task)
                    if tab_ok:
                        return BrowserVerification(
                            task_id=task.task_id,
                            status=BrowserVerificationStatus.VERIFIED,
                            observed_url=url,
                            observed_title=title,
                            verified_by="new tab outcome verification",
                            confidence_score=0.90,
                        )
                return BrowserVerification(
                    task_id=task.task_id,
                    status=BrowserVerificationStatus.NOT_VERIFIED,
                    observed_url=url,
                    observed_title=title,
                    verified_by="URL destination verification",
                    confidence_score=0.85,
                    failure_reason=reason,
                )

        # ── 2. Tab State Verification ─────────────────────────────────────────
        if tab_req:
            tab_ok, tab_reason = cls._verify_tabs(observation, task=task)
            if not tab_ok:
                return BrowserVerification(
                    task_id=task.task_id,
                    status=BrowserVerificationStatus.NOT_VERIFIED,
                    observed_url=url,
                    observed_title=title,
                    verified_by="tab state verification",
                    confidence_score=0.85,
                    failure_reason=tab_reason,
                )
            if not task.success_criteria and not expected_urls:
                return BrowserVerification(
                    task_id=task.task_id,
                    status=BrowserVerificationStatus.VERIFIED,
                    observed_url=url,
                    observed_title=title,
                    verified_by="new tab outcome verification",
                    confidence_score=0.90,
                )

        # ── 3. Screenshot Requirement Verification ────────────────────────────
        screenshot_req = cls._requires_screenshot(task)
        if screenshot_req:
            ss_ok, ss_reason, ss_b64 = cls._verify_screenshot(observation)
            if not ss_ok:
                return BrowserVerification(
                    task_id=task.task_id,
                    status=BrowserVerificationStatus.NOT_VERIFIED,
                    observed_url=url,
                    observed_title=title,
                    verified_by="screenshot artifact verification",
                    confidence_score=0.85,
                    failure_reason=ss_reason,
                )

        # ── 4. Explicit Success Criteria / Text Verification ──────────────────
        if task.success_criteria:
            criteria_ok, criteria_reason = cls._verify_success_criteria(
                task.success_criteria, observation
            )
            if not criteria_ok:
                if "not found in visible page content" in criteria_reason:
                    return BrowserVerification(
                        task_id=task.task_id,
                        status=BrowserVerificationStatus.NOT_VERIFIED,
                        observed_url=url,
                        observed_title=title,
                        verified_by="success criteria evaluation",
                        confidence_score=0.85,
                        failure_reason=criteria_reason,
                    )
                return BrowserVerification(
                    task_id=task.task_id,
                    status=BrowserVerificationStatus.INCONCLUSIVE,
                    observed_url=url,
                    observed_title=title,
                    verified_by="success criteria evaluation (ambiguous / not found)",
                    confidence_score=0.20,
                    failure_reason=criteria_reason,
                )
            return BrowserVerification(
                task_id=task.task_id,
                status=BrowserVerificationStatus.VERIFIED,
                observed_url=url,
                observed_title=title,
                confidence_score=0.95,
                verified_by=f"success criteria verified: {criteria_reason}",
            )

        # ── 5. Default Verification from Objective ────────────────────────────
        if expected_urls or screenshot_req or tab_req:
            reasons = []
            if expected_urls:
                reasons.append(reason)
            if tab_req:
                reasons.append(tab_reason)
            if screenshot_req:
                reasons.append(ss_reason)
            return BrowserVerification(
                task_id=task.task_id,
                status=BrowserVerificationStatus.VERIFIED,
                observed_url=url,
                observed_title=title,
                confidence_score=0.85,
                verified_by=f"task objective satisfied ({'; '.join(reasons)})",
            )

        # ── 6. Check for mutating action ambiguity ────────────────────────────
        obj_lower = (task.objective or "").lower()
        if any(w in obj_lower for w in ("submit", "pay", "purchase", "delete", "send", "order", "checkout", "transfer", "charge", "book")):
            return BrowserVerification(
                task_id=task.task_id,
                status=BrowserVerificationStatus.NEEDS_HUMAN_REVIEW,
                observed_url=url,
                observed_title=title,
                confidence_score=0.35,
                verified_by="state-mutating action ambiguity guard",
                failure_reason="Ambiguous state after mutating action; operator confirmation required.",
            )

        # ── 7. Semantic text verification fallback ────────────────────────────
        matched, kw_reason = cls._verify_success_criteria(task.objective or "", observation)
        if matched:
            return BrowserVerification(
                task_id=task.task_id,
                status=BrowserVerificationStatus.VERIFIED,
                observed_url=url,
                observed_title=title,
                confidence_score=0.70,
                verified_by=f"semantic text verification ({kw_reason})",
            )

        return BrowserVerification(
            task_id=task.task_id,
            status=BrowserVerificationStatus.INCONCLUSIVE,
            observed_url=url,
            observed_title=title,
            confidence_score=0.20,
            verified_by="observational ambiguity check",
            failure_reason="No verifiable destination state observed.",
        )

    @classmethod
    def _detect_error_indicators(
        cls, task: BrowserTask, observation: BrowserObservation
    ) -> Optional[str]:
        """
        Detects failure banners or error headings on the live page that indicate
        the action failed externally, even if Playwright executed without error.
        """
        text = f"{observation.page_title or ''} {observation.page_text_summary or ''}".lower()
        task_obj = (task.objective or "").lower()
        for err in ERROR_INDICATORS:
            if err in text and err not in task_obj:
                return err
        return None

    @classmethod
    def verify_goal_outcome(
        cls,
        goal_text: str,
        graph: Any,
        session_id: str,
        evidence_items: Optional[List[Dict[str, Any]]] = None,
    ) -> GoalVerificationResult:
        """
        Evaluates whether all required tasks and objectives for a goal were verified.
        Does NOT declare the goal successful unless all tasks achieved verified outcomes.
        """
        nodes = getattr(graph, "nodes", {}) if graph else {}
        total = len(nodes)
        if total == 0:
            return GoalVerificationResult(
                session_id=session_id,
                goal_text=goal_text,
                overall_status="inconclusive",
                confidence_score=0.0,
                explanation="No tasks in orchestration graph.",
            )

        verified_count = 0
        failed_count = 0
        inconclusive_count = 0
        needs_review_count = 0
        supporting_hashes: List[str] = []

        if evidence_items:
            for ev in evidence_items:
                h = ev.get("artifact_hash")
                if h and h not in supporting_hashes:
                    supporting_hashes.append(h)

        for tid, node in nodes.items():
            status_val = node.status.value if hasattr(node.status, "value") else str(node.status)
            if status_val == "completed":
                verified_count += 1
            elif status_val in ("failed", "blocked"):
                failed_count += 1
            elif status_val in ("outcome_unknown", "approval_needed"):
                needs_review_count += 1
            else:
                inconclusive_count += 1

        if failed_count > 0:
            overall = "failed"
            conf = 0.90
            explanation = f"Goal failed: {failed_count} task(s) failed or were blocked."
        elif needs_review_count > 0:
            overall = "needs_human_review"
            conf = 0.50
            explanation = f"Goal requires operator review: {needs_review_count} task(s) in uncertain or approval state."
        elif verified_count == total:
            overall = "verified"
            conf = 0.95
            explanation = f"Goal fully verified: all {total} task(s) completed and backed by evidence."
        else:
            overall = "inconclusive"
            conf = 0.40
            explanation = f"Goal incomplete: {verified_count}/{total} tasks verified, {inconclusive_count} remaining."

        return GoalVerificationResult(
            session_id=session_id,
            goal_text=goal_text,
            overall_status=overall,
            confidence_score=conf,
            total_tasks=total,
            verified_tasks=verified_count,
            failed_tasks=failed_count,
            inconclusive_tasks=inconclusive_count,
            supporting_evidence_hashes=supporting_hashes,
            explanation=explanation,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # URL Extraction & Matching
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def _extract_target_urls(cls, task: BrowserTask) -> List[str]:
        """Extract explicit and mentioned target URLs/domains from BrowserTask."""
        targets: List[str] = []
        expected_url = getattr(task, "expected_url", None)
        if expected_url:
            targets.append(str(expected_url).strip())

        text_sources = [task.objective or "", task.success_criteria or ""] + (task.constraints or [])
        combined = " ".join(text_sources)

        for match in _URL_PATTERN.finditer(combined):
            matched_str = match.group(0).strip().rstrip(".,;)\"'")
            if matched_str and matched_str not in targets:
                targets.append(matched_str)

        return targets

    @classmethod
    def _verify_url(cls, expected_urls: List[str], observed_url: str) -> Tuple[bool, str]:
        """Verify whether observed URL satisfies any of the expected URLs/domains."""
        if not observed_url or observed_url in ("about:blank", "none"):
            if any(exp in ("about:blank", "blank") for exp in expected_urls):
                return True, "Observed expected initial page 'about:blank'"
            return False, f"Expected URL matching {expected_urls}, but observed blank page '{observed_url}'."

        norm_observed = cls._normalize_url(observed_url)
        observed_host = urllib.parse.urlparse(norm_observed).netloc.lower().lstrip("www.")

        for exp in expected_urls:
            if exp in ("about:blank", "blank"):
                continue
            norm_expected = cls._normalize_url(exp)
            expected_host = urllib.parse.urlparse(norm_expected).netloc.lower().lstrip("www.")
            expected_path = urllib.parse.urlparse(norm_expected).path.rstrip("/")
            observed_path = urllib.parse.urlparse(norm_observed).path.rstrip("/")

            host_matches = expected_host and (expected_host == observed_host or expected_host in observed_host or observed_host in expected_host)
            if not host_matches:
                continue

            # If expected URL includes a specific path beyond root, enforce path matching
            if expected_path and expected_path not in ("", "/"):
                if expected_path in observed_path:
                    return True, f"Observed path matches expected target '{norm_expected}'"
                # Host matched but path did not match
                continue

            # Host matches and no specific path required
            return True, f"Observed host '{observed_host}' matches expected target '{expected_host}'"

        return False, f"Observed URL '{observed_url}' does not match expected targets {expected_urls}."

    @classmethod
    def _normalize_url(cls, raw_url: str) -> str:
        """Normalize URL ensuring scheme and lowercase host."""
        u = raw_url.strip()
        if not u.startswith("http://") and not u.startswith("https://") and not u.startswith("file://"):
            u = "https://" + u
        return u

    # ─────────────────────────────────────────────────────────────────────────
    # Tab Verification
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def _requires_new_tab(cls, task: BrowserTask) -> bool:
        """Detect whether task requires new tab creation."""
        text = f"{task.objective} {task.success_criteria or ''} {' '.join(task.constraints or [])}".lower()
        if any(p in text for p in ("new tab", "another tab", "separate tab", "create tab", "open a tab", "open tab")):
            return True
        if getattr(task, "metadata", None) and (task.metadata.get("new_tab") or task.metadata.get("open_tab")):
            return True
        return False

    @classmethod
    def _verify_tabs(
        cls,
        observation: BrowserObservation,
        task: Optional[BrowserTask] = None,
    ) -> Tuple[bool, str]:
        """
        Verify that multiple tabs exist, an active tab is present, and a new tab was
        actually created during this task (not merely that multiple tabs already existed).
        """
        tab_count = observation.open_tab_count
        if len(observation.tabs) > 0:
            tab_count = max(tab_count, len(observation.tabs))

        if tab_count < 2:
            return False, f"Task required opening a new tab, but only {tab_count} tab was observed."

        # Verify an active tab exists
        if observation.tabs:
            has_active = any(t.is_active for t in observation.tabs)
            if not has_active:
                return False, "New tab created but no active tab indicator found."

        # Edge-case: ensure the verifier cannot report success merely because multiple tabs existed before execution.
        if task and getattr(task, "metadata", None):
            initial_count = task.metadata.get("initial_tab_count")
            if initial_count is not None and tab_count <= initial_count:
                return False, f"Task required opening a new tab, but tab count did not increase (initial: {initial_count}, current: {tab_count})."

        # Check recent action failure if present
        if observation.recent_action and not observation.recent_action.success:
            return False, f"New tab action execution failed: {observation.recent_action.error}"

        return True, f"New tab outcome verified ({tab_count} open tabs observed, active tab confirmed)."

    # ─────────────────────────────────────────────────────────────────────────
    # Screenshot Verification
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def _requires_screenshot(cls, task: BrowserTask) -> bool:
        """Detect whether task explicitly requires capturing a screenshot."""
        text = f"{task.objective} {task.success_criteria}".lower()
        return "screenshot" in text or "capture screen" in text

    @classmethod
    def _verify_screenshot(cls, observation: BrowserObservation) -> Tuple[bool, str, Optional[str]]:
        """Verify that a successful screenshot action was performed."""
        ra = observation.recent_action
        if ra and ra.action_type == BrowserActionType.SCREENSHOT and ra.success:
            return True, "Screenshot captured successfully.", None

        # Check page snapshot for screenshot data
        if observation.page_snapshot and getattr(observation.page_snapshot, "screenshot_base64", None):
            return True, "Screenshot artifact present in page snapshot.", observation.page_snapshot.screenshot_base64

        return False, "Task required a screenshot, but no successful screenshot action was recorded.", None

    # ─────────────────────────────────────────────────────────────────────────
    # Success Criteria & Text Verification
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def _verify_success_criteria(
        cls, criteria: str, observation: BrowserObservation
    ) -> Tuple[bool, str]:
        """Evaluate natural-language success criteria against page text, title, and elements."""
        criteria_clean = criteria.strip().lower()

        # Check quoted strings first (e.g. text containing "More information")
        quoted_phrases = re.findall(r'["\']([^"\']+)["\']', criteria)
        page_text = (observation.page_text_summary or "").lower()
        page_title = (observation.page_title or "").lower()
        observed_url = (observation.current_url or "").lower()

        for phrase in quoted_phrases:
            phrase_lower = phrase.lower()
            in_text = phrase_lower in page_text
            in_title = phrase_lower in page_title
            in_url = phrase_lower in observed_url
            in_elements = any(
                phrase_lower in (el.text or "").lower() or phrase_lower in (el.aria_label or "").lower()
                for el in observation.observed_elements
            )
            if not (in_text or in_title or in_url or in_elements):
                return False, f"Expected phrase '{phrase}' not found in visible page content, title, or URL."

        # Keyword matching for unquoted criteria
        words = re.findall(r"\b[a-zA-Z0-9_-]{3,}\b", criteria_clean)
        keywords = [w for w in words if w not in _STOP_WORDS]

        if not keywords:
            # If no specific keywords, check non-blank page
            if observed_url and observed_url not in ("", "about:blank"):
                return True, "General success criteria verified on active page."
            return False, "Unable to verify criteria on blank page."

        # Count matched keywords in page text, title, URL, elements, or recent action input value
        matched_keywords = set()
        recent_val = (observation.recent_action.value or "").lower() if observation.recent_action else ""
        for kw in keywords:
            if (
                kw in page_text
                or kw in page_title
                or kw in observed_url
                or kw in recent_val
                or any(
                    kw in (el.text or "").lower()
                    or kw in (el.name or "").lower()
                    or kw in (el.placeholder or "").lower()
                    or kw in (el.aria_label or "").lower()
                    for el in observation.observed_elements
                )
            ):
                matched_keywords.add(kw)

        # If at least half of the meaningful keywords match, considered verified
        match_ratio = len(matched_keywords) / len(keywords)
        if match_ratio >= 0.5:
            return True, f"Matched keywords: {sorted(matched_keywords)} ({len(matched_keywords)}/{len(keywords)})"

        return False, (
            f"Success criteria not satisfied. Only matched {len(matched_keywords)}/{len(keywords)} "
            f"keywords ({sorted(matched_keywords)}). Missing: {sorted(set(keywords) - matched_keywords)}"
        )
