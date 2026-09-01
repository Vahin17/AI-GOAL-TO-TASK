"""
safety_schemas.py — Phase 5: Hybrid Safety Engine Core Types
==============================================================
Defines all safety-related schemas, enums, and data models for the
Hybrid Context-Aware Safety & Guardrail Engine.

Architecture:
    RiskLevel         — Classification of assessed risk (LOW..CRITICAL, UNKNOWN)
    ExecutionDecision — Separate authorization decision (ALLOW..BLOCK)
    SafetyContext     — Structured typed output from semantic analysis
    SafetyDecision    — Final combined assessment with policy trace
    SafetyRecord      — MongoDB-persisted audit record (schema_version: 2)

Critical Design Rule:
    RiskLevel and ExecutionDecision are SEPARATE concepts.
    CRITICAL → always BLOCK. HIGH → always REQUIRE_APPROVAL.
    The LLM may inform SafetyContext, but NEVER authorizes execution.
    Deterministic policy rules are the sole authoritative enforcement layer.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from enum import Enum
from typing import Dict, List, Optional
from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────────────────────
# Top-Level Enums
# ─────────────────────────────────────────────────────────────────────────────

class RiskLevel(str, Enum):
    """Assessed risk classification for a task."""
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"
    UNKNOWN  = "unknown"   # Semantic analysis unavailable or malformed


class ExecutionDecision(str, Enum):
    """
    Deterministic authorization decision for task execution.
    Separate from RiskLevel — one MUST NOT imply the other directly.

    Mapping enforced by policy engine:
        LOW     → ALLOW
        MEDIUM  → ALLOW_WITH_LOGGING
        HIGH    → REQUIRE_APPROVAL
        CRITICAL→ BLOCK
        UNKNOWN → REQUIRE_APPROVAL (conservative default)
    """
    ALLOW              = "allow"
    ALLOW_WITH_LOGGING = "allow_with_logging"
    REQUIRE_APPROVAL   = "require_approval"
    BLOCK              = "block"


class ApprovalStatus(str, Enum):
    """Status of operator approval for a safety-gated task."""
    PENDING  = "pending"
    APPROVED = "approved"
    DENIED   = "denied"
    INVALID  = "invalid"   # Approval invalidated due to task mutation


# ─────────────────────────────────────────────────────────────────────────────
# SafetyContext Field Enums (strongly-typed, used by deterministic policy)
# ─────────────────────────────────────────────────────────────────────────────

class ActionType(str, Enum):
    READ            = "read"
    WRITE           = "write"
    CREATE          = "create"
    DELETE          = "delete"
    MODIFY          = "modify"
    EXECUTE         = "execute"
    DEPLOY          = "deploy"
    TRANSMIT        = "transmit"
    CONFIGURE       = "configure"
    RESET           = "reset"
    INSTALL         = "install"
    TERMINATE       = "terminate"
    NAVIGATE        = "navigate"
    SEARCH          = "search"
    CLEAN           = "clean"
    AUTHENTICATE    = "authenticate"
    UNKNOWN         = "unknown"


class TargetCategory(str, Enum):
    OS_SYSTEM_FILE          = "os_system_file"
    OS_BOOT_FILE            = "os_boot_file"
    APPLICATION_DATA        = "application_data"
    USER_PROJECT_FILE       = "user_project_file"
    USER_PERSONAL_DATA      = "user_personal_data"
    TEMP_ARTIFACT           = "temp_artifact"
    CACHE                   = "cache"
    CREDENTIAL              = "credential"
    NETWORK_ENDPOINT        = "network_endpoint"
    DATABASE                = "database"
    CONFIGURATION_FILE      = "configuration_file"
    BUILD_ARTIFACT          = "build_artifact"
    BROWSER_DATA            = "browser_data"
    RECYCLE_BIN             = "recycle_bin"
    DOWNLOAD                = "download"
    EXTERNAL_SERVICE        = "external_service"
    UNKNOWN                 = "unknown"


class Scope(str, Enum):
    SYSTEM          = "system"
    USER            = "user"
    APPLICATION     = "application"
    PROCESS         = "process"
    NETWORK         = "network"
    EXTERNAL        = "external"
    CLEANUP         = "cleanup"
    UNKNOWN         = "unknown"


class Environment(str, Enum):
    PRODUCTION  = "production"
    STAGING     = "staging"
    DEVELOPMENT = "development"
    LOCAL       = "local"
    UNKNOWN     = "unknown"


class Reversibility(str, Enum):
    HIGH        = "high"     # Easily undoable (e.g., navigation)
    MEDIUM      = "medium"   # Partially reversible (e.g., modify config)
    LOW         = "low"      # Difficult to reverse (e.g., empty recycle bin)
    NONE        = "none"     # Irreversible (e.g., delete OS files, wipe disk)
    UNKNOWN     = "unknown"


class DestructivePotential(str, Enum):
    NONE        = "none"
    LOW         = "low"
    MEDIUM      = "medium"
    HIGH        = "high"
    CRITICAL    = "critical"


class DataSensitivity(str, Enum):
    NONE            = "none"
    LOW             = "low"
    MEDIUM          = "medium"
    HIGH            = "high"
    CREDENTIAL      = "credential"
    PII             = "pii"


class AmbiguityLevel(str, Enum):
    CLEAR           = "clear"
    SOMEWHAT        = "somewhat"
    HIGHLY          = "highly"
    CONTRADICTORY   = "contradictory"


# ─────────────────────────────────────────────────────────────────────────────
# SafetyContext — Structured Semantic Parsing Output
# ─────────────────────────────────────────────────────────────────────────────

class SafetyContext(BaseModel):
    """
    Structured typed output from the Semantic Risk Analyzer.
    Produced by LLM analysis but consumed only by the Deterministic Policy Engine.
    The LLM NEVER determines final authorization — only provides this context.
    """
    task_id:              str
    action:               ActionType       = ActionType.UNKNOWN
    target:               str              = "unknown"
    target_category:      TargetCategory   = TargetCategory.UNKNOWN
    scope:                Scope            = Scope.UNKNOWN
    environment:          Environment      = Environment.UNKNOWN
    reversibility:        Reversibility    = Reversibility.UNKNOWN
    destructive_potential: DestructivePotential = DestructivePotential.NONE
    privilege_required:   bool             = False
    system_critical:      bool             = False
    data_sensitivity:     DataSensitivity  = DataSensitivity.NONE
    external_side_effect: bool             = False
    consequence:          str              = ""
    ambiguity:            AmbiguityLevel   = AmbiguityLevel.CLEAR
    semantic_confidence:  float            = 1.0    # [0.0, 1.0]

    @classmethod
    def unknown(cls, task_id: str) -> "SafetyContext":
        """Create a maximally uncertain context for fallback scenarios."""
        return cls(
            task_id=task_id,
            action=ActionType.UNKNOWN,
            target_category=TargetCategory.UNKNOWN,
            scope=Scope.UNKNOWN,
            reversibility=Reversibility.UNKNOWN,
            ambiguity=AmbiguityLevel.HIGHLY,
            semantic_confidence=0.0,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Policy Identifier Registry
# ─────────────────────────────────────────────────────────────────────────────

class PolicyID(str, Enum):
    """Stable identifier for each deterministic safety policy rule."""
    PROTECTED_SYSTEM_RESOURCE    = "POLICY_PROTECTED_SYSTEM_RESOURCE"
    IRREVERSIBLE_USER_DATA       = "POLICY_IRREVERSIBLE_USER_DATA"
    PRIVILEGED_OPERATION         = "POLICY_PRIVILEGED_OPERATION"
    EXTERNAL_SIDE_EFFECT         = "POLICY_EXTERNAL_SIDE_EFFECT"
    DESTRUCTIVE_AMBIGUOUS_TARGET = "POLICY_DESTRUCTIVE_AMBIGUOUS_TARGET"
    CREDENTIAL_MODIFICATION      = "POLICY_CREDENTIAL_MODIFICATION"
    HIGH_IMPACT_CONFIGURATION    = "POLICY_HIGH_IMPACT_CONFIGURATION"
    PRODUCTION_DESTRUCTIVE_OP    = "POLICY_PRODUCTION_DESTRUCTIVE_OP"
    UNKNOWN_SEMANTIC_CONTEXT     = "POLICY_UNKNOWN_SEMANTIC_CONTEXT"
    LOW_CONFIDENCE_HIGH_IMPACT   = "POLICY_LOW_CONFIDENCE_HIGH_IMPACT"


# ─────────────────────────────────────────────────────────────────────────────
# SafetyDecision — Complete Policy Output
# ─────────────────────────────────────────────────────────────────────────────

class SafetyDecision(BaseModel):
    """
    Final output of the Deterministic Policy Engine.
    Contains risk classification, execution decision, triggered policies, and explanation.
    
    This record is the authoritative source of truth for task execution eligibility.
    """
    task_id:              str
    task_name:            str
    risk_level:           RiskLevel
    execution_decision:   ExecutionDecision
    triggered_policy_ids: List[PolicyID]          = Field(default_factory=list)
    semantic_context:     Optional[SafetyContext]  = None
    semantic_confidence:  float                    = 1.0
    explanation:          str                      = ""
    evaluated_at:         float                    = Field(default_factory=time.time)

    @property
    def approval_required(self) -> bool:
        return self.execution_decision == ExecutionDecision.REQUIRE_APPROVAL

    @property
    def is_blocked(self) -> bool:
        return self.execution_decision == ExecutionDecision.BLOCK


# ─────────────────────────────────────────────────────────────────────────────
# SafetyRecord — MongoDB Persistence (Schema Version 2)
# ─────────────────────────────────────────────────────────────────────────────

class SafetyRecord(BaseModel):
    """
    Audit record stored in the 'safety_flags' MongoDB collection.
    Evolves existing collection via schema_version=2.
    """
    safety_record_id:     str  = Field(default_factory=lambda: f"safe_{uuid.uuid4().hex[:10]}")
    schema_version:       int  = 2
    session_id:           str
    goal_id:              str
    plan_id:              str
    task_id:              str
    original_task:        str
    task_content_hash:    str  = ""           # Deterministic hash of task content for approval binding
    semantic_context:     Optional[Dict]      # SafetyContext as dict
    semantic_confidence:  float               = 1.0
    risk_level:           RiskLevel
    execution_decision:   ExecutionDecision
    triggered_policy_ids: List[str]           = Field(default_factory=list)
    explanation:          str                 = ""
    approval_required:    bool                = False
    approval_status:      ApprovalStatus      = ApprovalStatus.PENDING
    evaluated_at:         float               = Field(default_factory=time.time)
    approved_at:          Optional[float]     = None
    denied_at:            Optional[float]     = None
    approved_by:          Optional[str]       = "operator"

    @staticmethod
    def compute_content_hash(task_name: str) -> str:
        """Deterministic hash of task content to bind approvals to a specific task version."""
        return hashlib.sha256(task_name.strip().lower().encode()).hexdigest()[:16]

    def to_mongo_dict(self) -> dict:
        data = self.model_dump()
        data["risk_level"] = self.risk_level.value
        data["execution_decision"] = self.execution_decision.value
        data["approval_status"] = self.approval_status.value
        return data


# ─────────────────────────────────────────────────────────────────────────────
# Approval Binding — Task-Specific Approval Gate
# ─────────────────────────────────────────────────────────────────────────────

class ApprovalBinding(BaseModel):
    """
    Binds a human approval to a specific safety evaluation instance.
    Approval is invalid if task content hash changes after approval.
    """
    session_id:          str
    task_id:             str
    safety_record_id:    str
    task_content_hash:   str
    status:              ApprovalStatus = ApprovalStatus.PENDING
    approved_at:         Optional[float] = None
    denied_at:           Optional[float] = None

    def approve(self, current_hash: str) -> bool:
        """
        Approve the task. Validates that content hash matches.
        Returns False if task has mutated since evaluation.
        """
        if current_hash != self.task_content_hash:
            self.status = ApprovalStatus.INVALID
            return False
        self.status = ApprovalStatus.APPROVED
        self.approved_at = time.time()
        return True

    def deny(self) -> None:
        self.status = ApprovalStatus.DENIED
        self.denied_at = time.time()
