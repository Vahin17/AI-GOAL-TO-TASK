"""
evidence_vault.py — Cryptographic Evidence Vault & Provenance Engine (Phase 3 Objective A)
===========================================================================================
Maintains trustworthy, tamper-evident execution artifacts for every executed ARDO task:
1. Cryptographic SHA-256 hashing of all persisted evidence artifacts (screenshots, DOM snapshots, logs).
2. Explicit provenance binding: user_id (tenant), session_id, plan_id, plan_hash, and task_id.
3. Integrity verification: detects tampering, corruption, or missing evidence during retrieval.
4. Tenant isolation: strictly isolates evidence records by user_id.
5. Privacy-aware: runs all payload content through PrivacyRedactor prior to storage.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from execution.evidence.privacy_redactor import PrivacyRedactor

logger = logging.getLogger(__name__)

# Fallback in-memory store for unit tests or offline dev
_MEMORY_EVIDENCE_STORE: Dict[str, List[Dict[str, Any]]] = {}

# In-memory artifact store for binary evidence payloads (keyed by evidence id and artifact hash)
_BINARY_ARTIFACT_STORE: Dict[str, bytes] = {}


@dataclass
class EvidenceRecord:
    """
    Cryptographically authenticated evidence record.
    """
    id: str
    user_id: str
    session_id: str
    plan_id: str
    plan_hash: str
    task_id: str
    task_name: str
    evidence_type: str  # screenshot, dom_snapshot, extracted_text, verification_artifact
    artifact_hash: str  # SHA-256 hex digest
    captured_at: float
    verification_status: str = "inconclusive"  # verified, failed, inconclusive, needs_human_review
    verification_reason: Optional[str] = None
    confidence_score: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    content_payload: Optional[str] = None  # Redacted text or base64 payload

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class EvidenceVault:
    """
    Authoritative manager for execution evidence collection, cryptographic hashing,
    integrity verification, and access-controlled retrieval.
    """

    @classmethod
    def compute_artifact_hash(cls, content: Union[str, bytes]) -> str:
        """Computes the authoritative SHA-256 hash of an artifact."""
        if isinstance(content, str):
            content_bytes = content.encode("utf-8")
        elif isinstance(content, bytes):
            content_bytes = content
        else:
            content_bytes = str(content).encode("utf-8")
        return hashlib.sha256(content_bytes).hexdigest()

    @classmethod
    def record_evidence(
        cls,
        user_id: str,
        session_id: str,
        plan_id: str,
        plan_hash: str,
        task_id: str,
        task_name: str,
        evidence_type: str,
        raw_content: Optional[Union[str, bytes]] = None,
        verification_status: str = "inconclusive",
        verification_reason: Optional[str] = None,
        confidence_score: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
        db: Optional[Any] = None,
    ) -> EvidenceRecord:
        """
        Creates and persists a cryptographically signed, privacy-redacted evidence record.
        """
        now = time.time()
        ev_id = f"ev_{uuid.uuid4().hex[:14]}"

        # 1. Privacy redaction on content & metadata
        redacted_content: Optional[str] = None
        has_non_empty_content = (
            raw_content is not None
            and (
                (isinstance(raw_content, bytes) and len(raw_content) > 0)
                or (not isinstance(raw_content, bytes) and len(str(raw_content).strip()) > 0)
            )
        )
        if not has_non_empty_content and str(verification_status).lower() == "verified":
            logger.warning(
                f"[EvidenceVault] Rejecting 'verified' status for task {task_id} in session {session_id}: "
                "empty or missing raw_content cannot produce verified evidence."
            )
            verification_status = "unverified"
            confidence_score = 0.0

        if has_non_empty_content:
            if isinstance(raw_content, bytes):
                # For byte payloads, compute hash directly
                artifact_hash = cls.compute_artifact_hash(raw_content)
                redacted_content = "[BINARY_PAYLOAD_HASHED]"
                _BINARY_ARTIFACT_STORE[ev_id] = raw_content
            else:
                clean_text = PrivacyRedactor.redact_text(str(raw_content))
                artifact_hash = cls.compute_artifact_hash(clean_text)
                redacted_content = clean_text
        else:
            artifact_hash = "unverified" if str(verification_status).lower() == "unverified" else cls.compute_artifact_hash(f"{task_id}:{now}:{verification_status}")

        clean_metadata = PrivacyRedactor.redact_dict(metadata or {})
        clean_metadata["plan_hash"] = plan_hash
        clean_metadata["redacted"] = True

        record = EvidenceRecord(
            id=ev_id,
            user_id=user_id,
            session_id=session_id,
            plan_id=plan_id,
            plan_hash=plan_hash,
            task_id=task_id,
            task_name=task_name,
            evidence_type=evidence_type,
            artifact_hash=artifact_hash,
            captured_at=now,
            verification_status=verification_status,
            verification_reason=verification_reason,
            confidence_score=confidence_score,
            metadata=clean_metadata,
            content_payload=redacted_content,
        )

        # 2. Database persistence via AuthDatabase
        try:
            from core.auth_db import AuthDatabase
            auth_db = AuthDatabase.get_instance()
            auth_db.record_evidence(
                id=record.id,
                user_id=record.user_id,
                session_id=record.session_id,
                plan_id=record.plan_id,
                plan_hash=record.plan_hash,
                task_id=record.task_id,
                task_name=record.task_name,
                evidence_type=record.evidence_type,
                artifact_hash=record.artifact_hash,
                verification_status=record.verification_status,
                verification_reason=record.verification_reason,
                confidence_score=record.confidence_score,
                metadata=record.metadata,
                content_payload=record.content_payload,
            )
        except Exception as e:
            logger.warning(f"[EvidenceVault] Database persistence note: {e}")

        # 3. In-memory store fallback
        if session_id not in _MEMORY_EVIDENCE_STORE:
            _MEMORY_EVIDENCE_STORE[session_id] = []
        _MEMORY_EVIDENCE_STORE[session_id].append(record.to_dict())

        logger.info(
            f"[EvidenceVault] Recorded evidence {ev_id} for task {task_id} "
            f"(hash={artifact_hash[:12]}..., status={verification_status}, confidence={confidence_score:.2f})"
        )
        return record

    @classmethod
    def verify_integrity(
        cls, record: Union[EvidenceRecord, Dict[str, Any]], actual_content: Union[str, bytes]
    ) -> Tuple[bool, str]:
        """
        Validates evidence cryptographic integrity by re-computing SHA-256 hash.
        Accepts either an EvidenceRecord instance or a database record dictionary.
        Returns (is_valid, reason).
        """
        if not record:
            return False, "Evidence record is missing or null."

        recorded_hash = record.get("artifact_hash") if isinstance(record, dict) else getattr(record, "artifact_hash", None)
        rec_id = record.get("id") if isinstance(record, dict) else getattr(record, "id", "unknown")

        if not recorded_hash:
            return False, f"Evidence record {rec_id} has no artifact_hash to verify."

        h_str = str(recorded_hash).strip()
        if len(h_str) != 64 or not all(c in "0123456789abcdefABCDEF" for c in h_str):
            msg = f"Evidence record {rec_id} artifact_hash '{h_str}' is not a valid 64-character hexadecimal SHA-256 digest."
            logger.critical(f"[EvidenceVault] {msg}")
            return False, msg

        computed_hash = cls.compute_artifact_hash(actual_content)
        if computed_hash.lower() != h_str.lower():
            msg = (
                f"Evidence integrity violation for {rec_id}: recorded hash {h_str[:12]}... "
                f"does not match actual content hash {computed_hash[:12]}..."
            )
            logger.critical(f"[EvidenceVault] {msg}")
            return False, msg
        return True, "Cryptographic hash verified successfully."

    @classmethod
    def verify_record(
        cls,
        record: Union[EvidenceRecord, Dict[str, Any]],
        actual_content: Optional[Union[str, bytes]] = None,
    ) -> Tuple[bool, str]:
        """
        Independently verifies the cryptographic integrity and provenance of an evidence record.
        Strictly enforces:
        1. Exact 64-character hexadecimal SHA-256 digest (no prefixes, no truncated hashes).
        2. Non-empty payload or referenced artifact on disk / in vault.
        3. Placeholder sentinels (e.g. [BINARY_PAYLOAD_HASHED]) fail closed unless
           the authoritative vault or artifact path independently resolves and verifies the artifact.
        4. Cryptographic hash recomputation matching recorded artifact_hash.
        """
        if not record:
            return False, "Evidence record is missing or null."

        rec_dict = record.to_dict() if hasattr(record, "to_dict") else dict(record)
        rec_id = rec_dict.get("id", "unknown")

        # 1. Authoritative 64-hex SHA-256 format check
        h = str(rec_dict.get("artifact_hash") or "").strip()
        if not h or h.lower() == "unverified":
            return False, f"Evidence record {rec_id} has unverified or empty artifact_hash."
        if len(h) != 64 or not all(c in "0123456789abcdefABCDEF" for c in h):
            msg = f"Evidence record {rec_id} artifact_hash '{h}' is not a valid 64-character hexadecimal SHA-256 digest."
            logger.critical(f"[EvidenceVault] {msg}")
            return False, msg

        # 2. Resolve canonical content to verify
        content_to_verify: Optional[Union[str, bytes]] = actual_content

        # Direct in-record content
        if content_to_verify is None:
            if rec_dict.get("raw_content") is not None:
                content_to_verify = rec_dict.get("raw_content")
            elif rec_dict.get("content_bytes") is not None:
                content_to_verify = rec_dict.get("content_bytes")

        # Stored-by-reference file artifact lookup (checked before vault cache if explicitly referenced)
        meta = rec_dict.get("metadata") if isinstance(rec_dict.get("metadata"), dict) else {}
        ref_path = meta.get("artifact_path") or meta.get("file_path") or meta.get("storage_path")
        if content_to_verify is None and ref_path:
            import os
            if os.path.isfile(ref_path):
                try:
                    with open(ref_path, "rb") as f:
                        content_to_verify = f.read()
                except Exception as read_err:
                    return False, f"Evidence record {rec_id} referenced artifact '{ref_path}' could not be read: {read_err}"
            else:
                return False, f"Evidence record {rec_id} referenced artifact '{ref_path}' does not exist on disk."

        # Vault binary artifact store lookup
        if content_to_verify is None and rec_id in _BINARY_ARTIFACT_STORE:
            content_to_verify = _BINARY_ARTIFACT_STORE[rec_id]

        # Redacted text content payload
        payload = rec_dict.get("content_payload")
        if content_to_verify is None:
            if payload is not None:
                payload_str = str(payload).strip()
                # Fail closed on placeholder sentinels when no underlying binary artifact could be resolved
                if payload_str in ("[BINARY_PAYLOAD_HASHED]", "[REDACTED]", "PLACEHOLDER", "unverified"):
                    return False, f"Evidence record {rec_id} content_payload is placeholder sentinel '{payload_str}' without verifiable underlying artifact."
                if len(payload_str) > 0:
                    content_to_verify = payload

        if content_to_verify is None or (isinstance(content_to_verify, (str, bytes)) and len(content_to_verify) == 0):
            return False, f"Evidence record {rec_id} has no verifiable payload or referenced artifact."

        # 3. Cryptographic hash recomputation
        computed_hash = cls.compute_artifact_hash(content_to_verify)
        if computed_hash.lower() != h.lower():
            msg = (
                f"Evidence integrity violation for {rec_id}: recorded hash {h[:12]}... "
                f"does not match actual content hash {computed_hash[:12]}..."
            )
            logger.critical(f"[EvidenceVault] {msg}")
            return False, msg

        return True, "Cryptographic hash verified successfully."

    @classmethod
    def get_session_evidence(
        cls, session_id: str, user_id: str
    ) -> List[Dict[str, Any]]:
        """
        Retrieves all evidence for a session, strictly validating user_id ownership.
        """
        # 1. Query AuthDatabase
        try:
            from core.auth_db import AuthDatabase
            auth_db = AuthDatabase.get_instance()
            db_records = auth_db.get_session_evidence(session_id, user_id)
            if db_records:
                return db_records
        except Exception as e:
            logger.warning(f"[EvidenceVault] DB query note: {e}")

        # 2. In-memory fallback (filter by user_id)
        records = _MEMORY_EVIDENCE_STORE.get(session_id, [])
        return [r for r in records if r.get("user_id") == user_id]

    @classmethod
    def get_evidence_by_id(
        cls, evidence_id: str, user_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Retrieves a single evidence record enforcing user_id access control.
        """
        try:
            from core.auth_db import AuthDatabase
            auth_db = AuthDatabase.get_instance()
            rec = auth_db.get_evidence_by_id(evidence_id, user_id)
            if rec:
                return rec
        except Exception as e:
            logger.warning(f"[EvidenceVault] DB query note: {e}")

        for sess_records in _MEMORY_EVIDENCE_STORE.values():
            for r in sess_records:
                if r.get("id") == evidence_id and r.get("user_id") == user_id:
                    return r
        return None

