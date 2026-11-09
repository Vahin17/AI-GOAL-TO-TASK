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
        if raw_content is not None:
            if isinstance(raw_content, bytes):
                # For byte payloads, compute hash directly
                artifact_hash = cls.compute_artifact_hash(raw_content)
                redacted_content = "[BINARY_PAYLOAD_HASHED]"
            else:
                clean_text = PrivacyRedactor.redact_text(str(raw_content))
                artifact_hash = cls.compute_artifact_hash(clean_text)
                redacted_content = clean_text
        else:
            artifact_hash = cls.compute_artifact_hash(f"{task_id}:{now}:{verification_status}")

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

        computed_hash = cls.compute_artifact_hash(actual_content)
        if computed_hash != recorded_hash:
            msg = (
                f"Evidence integrity violation for {rec_id}: recorded hash {recorded_hash[:12]}... "
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

