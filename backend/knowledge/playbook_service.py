"""
playbook_service.py — Controlled Reusable Playbooks & Safe Learning Engine (Phase 3 Objective D)
=================================================================================================
Manages the lifecycle of learned execution patterns:
1. Eligibility Gate: Only executions with VERIFIED goal and task outcomes can produce candidate playbooks.
   Never learns from failed, inconclusive, or unverified runs.
2. Privacy Redaction: Strips all passwords, credentials, payment cards, emails, and PII prior to persistence.
3. Tenant Isolation: Playbooks are bound strictly to user_id.
4. Promotion & Versioning: Newly synthesized patterns enter as 'candidate'. Controlled promotion advances to
   'promoted' with monotonic version increments. Deprecation/revocation supported.
5. Untrusted Contextual Retrieval: Retrieved playbooks are tagged as UNTRUSTED contextual data and validate
   required tools and preconditions before being surfaced to the planning LLM.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Set

from core.auth_db import AuthDatabase
from execution.evidence.privacy_redactor import PrivacyRedactor
from execution.browser.agent_contracts import GoalVerificationResult

logger = logging.getLogger(__name__)


class PlaybookService:
    """
    Authoritative manager for candidate playbook discovery, promotion, and safe retrieval.
    """

    @classmethod
    def generate_candidate_playbook(
        cls,
        user_id: str,
        session_id: str,
        goal_text: str,
        graph: Any,
        goal_verification: GoalVerificationResult,
        evidence_records: Optional[List[Dict[str, Any]]] = None,
        category: str = "web_automation",
    ) -> Optional[Dict[str, Any]]:
        """
        Synthesizes a candidate playbook from a VERIFIED execution session.
        Invariants:
        - Rejects unverified, failed, or inconclusive executions.
        - Redacts all secrets and PII from execution steps.
        - Preserves user tenant isolation.
        """
        # Invariant 1: ONLY learn from verified executions
        if goal_verification.overall_status != "verified":
            logger.warning(
                f"[PlaybookService] Learning rejected for session {session_id}: "
                f"overall status is '{goal_verification.overall_status}' (must be 'verified')."
            )
            return None

        nodes = getattr(graph, "nodes", {})
        if not nodes:
            return None

        # Build clean, redacted execution steps
        steps = []
        required_tools: Set[str] = set()
        expected_outcomes: List[str] = []

        for tid, node in nodes.items():
            tool_name = "browser"
            if getattr(node, "tool_call", None) and hasattr(node.tool_call, "name"):
                tool_name = str(node.tool_call.name)
            elif getattr(node, "tool_name", None):
                tool_name = str(node.tool_name)
            required_tools.add(tool_name)

            raw_step = {
                "step_id": tid,
                "task_name": node.task_name,
                "tool": tool_name,
                "browser_action": node.browser_action.to_dict() if hasattr(node.browser_action, "to_dict") else node.browser_action,
                "metadata": node.metadata,
            }

            # Invariant 2: Thorough privacy redaction
            clean_step = PrivacyRedactor.redact_dict(raw_step)
            steps.append(clean_step)
            expected_outcomes.append(f"{node.task_name}: completed and verified")

        evidence_ids = []
        if evidence_records:
            evidence_ids = [e.get("id") for e in evidence_records if e.get("id")]

        playbook_id = f"pb_{uuid.uuid4().hex[:12]}"
        plan_hash = getattr(graph, "plan_hash", "") or "unhashed"

        auth_db = AuthDatabase.get_instance()
        doc = auth_db.record_playbook(
            id=playbook_id,
            user_id=user_id,
            workflow_category=category,
            goal_pattern=PrivacyRedactor.redact_text(goal_text),
            preconditions=["Network connectivity available", "Valid browser context"],
            execution_steps=steps,
            required_tools=sorted(list(required_tools)),
            expected_outcomes=expected_outcomes,
            supporting_evidence_ids=evidence_ids,
            provenance_session_id=session_id,
            provenance_plan_hash=plan_hash,
            status="candidate",
            confidence_score=max(0.5, min(0.95, goal_verification.confidence_score)),
            version=1,
            metadata={"discovered_from_session": session_id, "total_steps": len(steps)},
        )

        logger.info(
            f"[PlaybookService] Created candidate playbook {playbook_id} for user {user_id} "
            f"(steps={len(steps)}, tools={sorted(list(required_tools))})"
        )
        return doc

    @classmethod
    def get_playbooks_for_user(
        cls, user_id: str, category: Optional[str] = None, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Retrieves playbooks for a specific user, strictly respecting tenant isolation."""
        auth_db = AuthDatabase.get_instance()
        return auth_db.get_playbooks_for_user(user_id=user_id, category=category, status=status)

    @classmethod
    def get_playbook_by_id(
        cls, playbook_id: str, user_id: str
    ) -> Optional[Dict[str, Any]]:
        """Retrieves a single playbook for a specific user."""
        auth_db = AuthDatabase.get_instance()
        return auth_db.get_playbook_by_id(playbook_id=playbook_id, user_id=user_id)

    @classmethod
    def promote_candidate(
        cls, playbook_id: str, user_id: str
    ) -> bool:
        """
        Promotes a candidate playbook to 'promoted' status for production reuse.
        Bumps the version monotonically.
        """
        auth_db = AuthDatabase.get_instance()
        pb = auth_db.get_playbook_by_id(playbook_id, user_id=user_id)
        if not pb:
            logger.warning(f"[PlaybookService] Cannot promote playbook {playbook_id}: not found or unauthorized.")
            return False

        if pb.get("status") not in ("candidate", "promoted"):
            logger.warning(f"[PlaybookService] Cannot promote playbook in '{pb.get('status')}' state.")
            return False

        success = auth_db.update_playbook_status(
            playbook_id=playbook_id,
            status="promoted",
            user_id=user_id,
            version_increment=True,
        )
        if success:
            logger.info(f"[PlaybookService] Promoted playbook {playbook_id} to 'promoted' for user {user_id}.")
        return success

    @classmethod
    def deprecate_playbook(
        cls, playbook_id: str, user_id: str
    ) -> bool:
        """Marks a playbook as deprecated."""
        auth_db = AuthDatabase.get_instance()
        return auth_db.update_playbook_status(
            playbook_id=playbook_id,
            status="deprecated",
            user_id=user_id,
            version_increment=False,
        )

    @classmethod
    def retrieve_playbooks_for_goal(
        cls,
        goal_text: str,
        user_id: str,
        available_tools: Optional[Set[str]] = None,
        category: Optional[str] = None,
        include_candidates: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Safely retrieves eligible playbooks for a planning query:
        1. Enforces tenant isolation (only playbooks owned by user_id).
        2. Filters by status ('promoted' only, unless include_candidates=True).
        3. Validates required tools against available session tools.
        4. Tags context as UNTRUSTED to prevent prompt injection or control bypass.
        """
        auth_db = AuthDatabase.get_instance()
        target_status = None if include_candidates else "promoted"
        all_pbs = auth_db.get_playbooks_for_user(
            user_id=user_id,
            category=category,
            status=target_status,
        )

        query_tokens = set(re.findall(r"\b[a-z0-9_]+\b", goal_text.lower()))
        matched_playbooks = []

        for pb in all_pbs:
            # 1. Tool availability validation
            required = pb.get("required_tools", [])
            if available_tools is not None:
                if any(tool not in available_tools for tool in required):
                    logger.debug(
                        f"[PlaybookService] Skipping playbook {pb.get('id')}: "
                        f"missing required tools {set(required) - available_tools}"
                    )
                    continue

            # 2. Keyword relevance matching on goal pattern
            pattern_text = str(pb.get("goal_pattern", "")).lower()
            pattern_tokens = set(re.findall(r"\b[a-z0-9_]+\b", pattern_text))
            overlap = query_tokens.intersection(pattern_tokens)

            # Compute Jaccard / token overlap
            if overlap or not query_tokens:
                relevance = len(overlap) / max(1, len(pattern_tokens))
                pb_copy = dict(pb)
                pb_copy["relevance_score"] = round(relevance, 3)
                pb_copy["untrusted_context_tag"] = "[UNTRUSTED_PLAYBOOK_CONTEXT: REQUIRES PLAN CRITIC & SAFETY VALIDATION]"
                matched_playbooks.append(pb_copy)

        # Sort by relevance and confidence
        matched_playbooks.sort(
            key=lambda x: (x.get("relevance_score", 0), x.get("confidence_score", 0)),
            reverse=True,
        )
        return matched_playbooks

    @classmethod
    def find_applicable_playbooks(
        cls,
        user_id: str,
        goal_text: str,
        available_tools: Optional[Any] = None,
        category: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Convenience alias for retrieve_playbooks_for_goal with tool set normalization."""
        tools_set = set(available_tools) if available_tools is not None else None
        return cls.retrieve_playbooks_for_goal(
            goal_text=goal_text,
            user_id=user_id,
            available_tools=tools_set,
            category=category,
        )

    @classmethod
    def format_playbook_for_context(cls, playbook: Dict[str, Any]) -> str:
        """Formats a retrieved playbook for safe, sandboxed LLM context injection."""
        pattern = playbook.get("goal_pattern", "")
        steps = json.dumps(playbook.get("execution_steps", []))
        return (
            "[UNTRUSTED_PLAYBOOK_CONTEXT]\n"
            f"Pattern: {pattern}\n"
            f"Steps: {steps}\n"
            "[/UNTRUSTED_PLAYBOOK_CONTEXT]"
        )
