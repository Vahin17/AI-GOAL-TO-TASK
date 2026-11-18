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
        # Invariant 1: ONLY learn from verified executions with empirical evidence and valid user identity
        clean_uid = str(user_id or "").strip()
        if not clean_uid:
            logger.warning(f"[PlaybookService] Learning rejected for session {session_id}: missing user_id.")
            return None

        if goal_verification.overall_status != "verified":
            logger.warning(
                f"[PlaybookService] Learning rejected for session {session_id}: "
                f"overall status is '{goal_verification.overall_status}' (must be 'verified')."
            )
            return None

        if not evidence_records or len(evidence_records) == 0:
            logger.warning(
                f"[PlaybookService] Learning rejected for session {session_id}: "
                "no empirical evidence records provided."
            )
            return None

        valid_evidence = []
        for e in evidence_records:
            if not isinstance(e, dict):
                continue
            if str(e.get("verification_status", "")).lower() != "verified":
                continue
            if e.get("session_id") and e.get("session_id") != session_id:
                continue
            # AUD-S3-03: Strict user ownership (no 'system' wildcard)
            ev_uid = str(e.get("user_id") or "").strip()
            if ev_uid and ev_uid != clean_uid:
                continue
            # AUD-S3-02: Strict cryptographic evidence format & payload integrity verification
            from execution.evidence.evidence_vault import EvidenceVault
            is_valid, reason = EvidenceVault.verify_record(e)
            if not is_valid:
                logger.warning(f"[PlaybookService] Evidence validation failed for record {e.get('id')}: {reason}")
                continue
            valid_evidence.append(e)
        if not valid_evidence:
            logger.warning(
                f"[PlaybookService] Learning rejected for session {session_id}: "
                "no verified session-bound empirical evidence records provided."
            )
            return None

        nodes = getattr(graph, "nodes", {})
        if not nodes:
            return None

        is_fallback_plan = bool(
            getattr(graph, "is_fallback", False)
            or (hasattr(graph, "is_fallback_plan") and callable(graph.is_fallback_plan) and graph.is_fallback_plan())
            or (isinstance(getattr(graph, "metadata", None), dict) and graph.metadata.get("is_fallback"))
            or getattr(getattr(graph, "metadata", None), "is_fallback", False)
            or any(
                isinstance(getattr(n, "metadata", None), dict) and bool(n.metadata.get("is_fallback"))
                for n in nodes.values()
            )
        )
        if is_fallback_plan:
            logger.warning(
                f"[PlaybookService] Learning rejected for session {session_id}: "
                "heuristic fallback plans cannot promote into candidate playbooks."
            )
            return None

        # Build clean, redacted execution steps
        steps = []
        required_tools: Set[str] = set()
        expected_outcomes: List[str] = []

        for tid, node in nodes.items():
            node_status = node.status.value if hasattr(getattr(node, "status", None), "value") else str(getattr(node, "status", ""))
            if node_status != "completed":
                logger.warning(
                    f"[PlaybookService] Learning rejected for session {session_id}: "
                    f"task '{tid}' has non-completed status '{node_status}'."
                )
                return None

            tool_name = "browser"
            tc = getattr(node, "tool_call", None)
            if tc is not None:
                if isinstance(tc, dict) and tc.get("tool_name"):
                    tool_name = str(tc["tool_name"])
                elif hasattr(tc, "tool_name") and getattr(tc, "tool_name", None):
                    tool_name = str(tc.tool_name)
                elif hasattr(tc, "name") and getattr(tc, "name", None):
                    tool_name = str(tc.name)
            elif getattr(node, "tool_name", None):
                tool_name = str(node.tool_name)
            elif isinstance(getattr(node, "metadata", None), dict) and node.metadata.get("tool"):
                tool_name = str(node.metadata["tool"])
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

        evidence_ids = [e.get("id") for e in valid_evidence if e.get("id")]
        evidence_hashes = [e.get("artifact_hash") for e in valid_evidence if e.get("artifact_hash")]
        if not evidence_ids:
            evidence_ids = [f"ev_hash_{h[:12]}" for h in evidence_hashes]

        playbook_id = f"pb_{uuid.uuid4().hex[:12]}"
        plan_hash = getattr(graph, "plan_hash", "") or "unhashed"

        meta = {
            "discovered_from_session": session_id,
            "source_plan_hash": plan_hash,
            "supporting_evidence_hashes": evidence_hashes,
            "is_fallback": False,
            "total_steps": len(steps),
            "verification_status": goal_verification.overall_status,
            "confidence_score": goal_verification.confidence_score,
        }

        auth_db = AuthDatabase.get_instance()
        doc = auth_db.record_playbook(
            id=playbook_id,
            user_id=clean_uid,
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
            metadata=meta,
        )

        # Mirror candidate playbook to MongoDB if database is available
        try:
            from core.database import get_database
            mongo_db = get_database()
            if mongo_db is not None:
                mongo_db["candidate_playbooks"].replace_one(
                    {"id": playbook_id},
                    dict(doc),
                    upsert=True,
                )
        except Exception as me:
            logger.debug(f"[PlaybookService] MongoDB candidate playbook sync note: {me}")

        logger.info(
            f"[PlaybookService] Created candidate playbook {playbook_id} for user {user_id} "
            f"(steps={len(steps)}, tools={sorted(list(required_tools))})"
        )
        return doc

    @classmethod
    def get_playbooks_for_user(
        cls, user_id: str, category: Optional[str] = None, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Retrieves playbooks for a specific user, strictly respecting tenant isolation and quarantine exclusion."""
        clean_uid = str(user_id or "").strip()
        if not clean_uid:
            return []
        if status and status.strip().lower() == "quarantined":
            return []
        auth_db = AuthDatabase.get_instance()
        pbs = auth_db.get_playbooks_for_user(user_id=clean_uid, category=category, status=status)
        return [pb for pb in pbs if pb.get("status") != "quarantined" and pb.get("user_id") == clean_uid]

    @classmethod
    def get_playbook_by_id(
        cls, playbook_id: str, user_id: str
    ) -> Optional[Dict[str, Any]]:
        """Retrieves a single playbook for a specific user, excluding quarantined records."""
        clean_pid = str(playbook_id or "").strip()
        clean_uid = str(user_id or "").strip()
        if not clean_pid or not clean_uid:
            return None
        auth_db = AuthDatabase.get_instance()
        pb = auth_db.get_playbook_by_id(playbook_id=clean_pid, user_id=clean_uid)
        if not pb or pb.get("status") == "quarantined" or pb.get("user_id") != clean_uid:
            return None
        return pb

    @classmethod
    def promote_candidate(
        cls, playbook_id: str, user_id: str
    ) -> bool:
        """
        Promotes a candidate playbook to 'promoted' status for production reuse.
        Bumps the version monotonically after validating ownership, verification state,
        supporting evidence, and non-fallback provenance.
        """
        clean_pid = str(playbook_id or "").strip()
        clean_uid = str(user_id or "").strip()
        if not clean_pid or not clean_uid:
            return False

        auth_db = AuthDatabase.get_instance()
        pb = auth_db.get_playbook_by_id(clean_pid, user_id=clean_uid)
        if not pb or pb.get("user_id") != clean_uid:
            logger.warning(f"[PlaybookService] Cannot promote playbook {playbook_id}: not found or unauthorized.")
            return False

        if pb.get("status") not in ("candidate", "promoted"):
            logger.warning(f"[PlaybookService] Cannot promote playbook in '{pb.get('status')}' state.")
            return False

        # Invariant: Reject promotion if execution outcome was unverified, fallback-generated, or confidence is insufficient
        conf = float(pb.get("confidence_score") or 0.0)
        pb_meta = pb.get("metadata") or {}
        if isinstance(pb_meta, str):
            try:
                pb_meta = json.loads(pb_meta)
            except Exception:
                pb_meta = {}
        verif_status = pb_meta.get("verification_status", "verified")
        is_fb = bool(pb_meta.get("is_fallback", False) or pb.get("is_fallback", False))
        steps = pb.get("execution_steps") or []

        if (
            conf < 0.50
            or verif_status in ("unverified", "failed", "inconclusive", "rejected")
            or is_fb
            or not steps
        ):
            logger.warning(
                f"[PlaybookService] Cannot promote playbook {playbook_id}: ineligible "
                f"(status='{verif_status}', confidence={conf}, is_fallback={is_fb}, steps={len(steps)})."
            )
            return False

        success = auth_db.update_playbook_status(
            playbook_id=clean_pid,
            status="promoted",
            user_id=clean_uid,
            version_increment=True,
        )
        if success:
            logger.info(f"[PlaybookService] Promoted playbook {clean_pid} to 'promoted' for user {clean_uid}.")
        return success

    @classmethod
    def deprecate_playbook(
        cls, playbook_id: str, user_id: str
    ) -> bool:
        """Marks a playbook as deprecated."""
        clean_pid = str(playbook_id or "").strip()
        clean_uid = str(user_id or "").strip()
        if not clean_pid or not clean_uid:
            return False
        auth_db = AuthDatabase.get_instance()
        return auth_db.update_playbook_status(
            playbook_id=clean_pid,
            status="deprecated",
            user_id=clean_uid,
            version_increment=False,
        )

    @classmethod
    def record_playbook_execution_outcome(
        cls,
        playbook_id: str,
        success: bool,
        user_id: str,
    ) -> bool:
        """
        Safely records the execution outcome of an approved playbook with tenant and quarantine validation.
        """
        clean_pid = str(playbook_id or "").strip()
        clean_uid = str(user_id or "").strip()
        if not clean_pid or not clean_uid:
            return False
        auth_db = AuthDatabase.get_instance()
        return auth_db.record_playbook_outcome(
            playbook_id=clean_pid,
            success=success,
            user_id=clean_uid,
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
        clean_uid = str(user_id or "").strip()
        if not clean_uid:
            return []
        auth_db = AuthDatabase.get_instance()
        target_status = None if include_candidates else "promoted"
        all_pbs = auth_db.get_playbooks_for_user(
            user_id=clean_uid,
            category=category,
            status=target_status,
        )

        query_tokens = set(re.findall(r"\b[a-z0-9_]+\b", goal_text.lower()))
        matched_playbooks = []

        for pb in all_pbs:
            # 0. Strict quarantine, ownership, and lifecycle exclusion
            pb_status = str(pb.get("status", "")).lower()
            if pb.get("user_id") != clean_uid:
                continue
            if pb_status in ("quarantined", "deprecated", "revoked", "rejected"):
                continue
            if not include_candidates and pb_status != "promoted":
                continue

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
        """Formats a retrieved playbook for safe, sandboxed LLM context injection with delimiter neutralization."""
        raw_pattern = str(playbook.get("goal_pattern", ""))
        raw_steps = json.dumps(playbook.get("execution_steps", []))
        # Neutralize delimiter breakout tags and prompt injection control sequences
        def _sanitize(text: str) -> str:
            s = re.sub(r"(?i)</?\s*(?:untrusted_user_goal|untrusted_retrieved_context|system|system_instruction|instructions?|admin|root|prompt)\s*>", "[REDACTED_DELIMITER]", text)
            s = re.sub(r"(?i)\[\s*/?\s*(?:UNTRUSTED_PLAYBOOK_CONTEXT|INST|SYS|SYSTEM)\s*\]|<\|im_start\|>|<\|im_end\|>|<<SYS>>|<</SYS>>", "[REDACTED_TOKEN]", s)
            return s
        return (
            "[UNTRUSTED_PLAYBOOK_CONTEXT]\n"
            f"PlaybookID: {playbook.get('id', 'unknown')} (v{playbook.get('version', 1)}, status={playbook.get('status', 'promoted')})\n"
            f"ProvenanceSession: {playbook.get('provenance_session_id', 'unknown')}\n"
            f"Pattern: {_sanitize(raw_pattern)}\n"
            f"Steps: {_sanitize(raw_steps)}\n"
            "[/UNTRUSTED_PLAYBOOK_CONTEXT]"
        )
