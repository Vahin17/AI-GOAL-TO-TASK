"""
agent_contracts.py — Browser Agent Contract Models (Phase 1 & 2)
================================================================
Defines the typed data contracts that a future BrowserAgent will use to
observe, decide, act, verify, and report on browser tasks.

Phase 1 (complete): BrowserTask, BrowserObservation, BrowserDecision,
    BrowserActionDecision, BrowserVerification, BrowserAgentResult.

Phase 2 additions:
    RecentActionRecord  — Structured record of the most recent browser
                          action and its outcome.
    BrowserObservation  — Extended with observed_elements (ObservedElement
                          list with stable per-observation refs), tabs
                          (TabInfo list), and recent_action.

These are pure data structures.

They do NOT:
- implement the BrowserAgent loop.
- contain Gemini / LLM calls.
- contain Playwright / executor calls.
- contain safety enforcement logic.

They DO:
- Reuse existing BrowserActionType from browser_models.py.
- Reuse PageSnapshot from perception_models.py.
- Provide the typed boundary between planning, observation, reasoning,
  execution, verification, and result reporting.

Intended flow (Phase 2 will wire this up):

    BrowserTask
        │
        ▼
    BrowserObservation
        │
        ▼
    BrowserDecision
        │
        ├─► BrowserActionDecision  (if action is needed)
        │
        ▼
    BrowserVerification
        │
        ▼
    BrowserAgentResult
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from execution.browser.browser_models import BrowserActionType
from execution.browser.perception_models import ObservedElement, PageSnapshot, TabInfo


# ─────────────────────────────────────────────────────────────────────────────
# 1. BrowserTask
# ─────────────────────────────────────────────────────────────────────────────

class BrowserTask(BaseModel):
    """
    The browser objective that the BrowserAgent must accomplish.

    Captures INTENT, not implementation details.
    The agent must decide HOW to achieve the objective by observing the
    live browser environment — it is NOT given pre-selected CSS selectors.

    Fields:
        task_id:          Stable ID linking back to the ARDO DAG node.
        objective:        Human-readable description of what must be achieved.
                          Example: "Open Wikipedia in a new tab".
        success_criteria: Optional natural-language description of what a
                          successful outcome looks like. The agent uses this
                          to drive verification. Leave None to defer to
                          generic task-completion checks.
        constraints:      Optional list of operational constraints the agent
                          must respect. Example: "Do not submit any forms".
        session_id:       ARDO session identifier this task belongs to.
        goal_text:        The original user goal that triggered this task,
                          for contextual grounding during multi-step execution.
        max_actions:      Upper bound on the number of browser actions the
                          agent may take to accomplish this task. Prevents
                          runaway loops. Default 10.
        metadata:         Optional free-form key/value for Phase 2/3 extensions.
    """
    task_id: str = Field(..., description="Stable ID from the ARDO DAG node")
    objective: str = Field(..., description="What the browser agent must accomplish")
    success_criteria: Optional[str] = Field(
        default=None,
        description="Natural-language description of a successful outcome",
    )
    constraints: List[str] = Field(
        default_factory=list,
        description="Operational constraints the agent must respect",
    )
    session_id: str = Field(..., description="ARDO session identifier")
    goal_text: str = Field(
        default="",
        description="Original user goal providing broader context",
    )
    max_actions: int = Field(
        default=10,
        ge=1,
        description="Maximum browser actions the agent may execute for this task",
    )
    metadata: dict = Field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 support: RecentActionRecord
# ─────────────────────────────────────────────────────────────────────────────

class RecentActionRecord(BaseModel):
    """
    Compact record of the most recently executed browser action and its result.

    Carried inside BrowserObservation so the future BrowserAgent can see
    what just happened before deciding its next move. This enables recovery
    reasoning ("the previous click failed, I should try a different element").

    Fields:
        action_type:     The BrowserActionType that was executed.
        element_ref:     Semantic ref used (e.g. "elem_003"), or None for
                         navigate/screenshot actions.
        url:             URL argument of the action, if applicable.
        value:           Input value of the action, if applicable.
        new_tab:         Whether the action targeted a new tab.
        success:         Whether the action completed without error.
        error:           Error message if success=False.
        duration_ms:     How long the action took.
    """
    action_type: BrowserActionType
    element_ref: Optional[str] = Field(
        default=None,
        description="Semantic element ref that was targeted (from ObservedElement.ref)",
    )
    url: Optional[str] = None
    value: Optional[str] = None
    new_tab: bool = False
    success: bool = False
    error: Optional[str] = None
    duration_ms: float = Field(default=0.0, ge=0.0)


# ─────────────────────────────────────────────────────────────────────────────
# 2. BrowserObservation  (extended in Phase 2)
# ─────────────────────────────────────────────────────────────────────────────

class BrowserObservation(BaseModel):
    """
    What the BrowserAgent currently knows about the live browser environment.

    Produced by EnvironmentPerceptionEngine.observe() and passed to the
    future BrowserAgent so it can decide its next action.

    Phase 1 fields (unchanged):
        session_id, task_id, current_url, page_title, page_snapshot,
        page_text_summary, open_tab_count, active_tab_index,
        previous_action, previous_action_success, actions_taken_count,
        timestamp.

    Phase 2 additions:
        observed_elements  — Agent-facing list of ObservedElement instances
                             each with a stable per-observation ref
                             (e.g. "elem_001"). The agent uses these refs
                             to identify elements without raw CSS selectors.
        tabs               — Ordered list of TabInfo representing open
                             browser tabs. is_active marks the current tab.
        recent_action      — Structured record of the most recent browser
                             action + result. None before any action.
    """
    session_id: str
    task_id: str
    current_url: str = Field(default="", description="Live URL of the active page")
    page_title: str = Field(default="", description="Live title of the active page")
    page_snapshot: Optional[PageSnapshot] = Field(
        default=None,
        description="Structured page state from environment perception",
    )
    page_text_summary: Optional[str] = Field(
        default=None,
        description="Bounded textual excerpt of visible page content",
    )
    open_tab_count: int = Field(default=1, ge=1)
    active_tab_index: int = Field(default=0, ge=0)
    previous_action: Optional[str] = Field(
        default=None,
        description="Description of the action that produced this observation",
    )
    previous_action_success: Optional[bool] = Field(
        default=None,
        description="Whether the previous action succeeded (None if no prior action)",
    )
    actions_taken_count: int = Field(
        default=0,
        ge=0,
        description="Number of actions taken so far for this task",
    )
    timestamp: float = Field(default_factory=time.time)
    # ── Phase 2 additions ────────────────────────────────────────────────────
    observed_elements: List[ObservedElement] = Field(
        default_factory=list,
        description=(
            "Agent-facing interactive elements, each with a stable per-observation ref. "
            "Use these refs (e.g. 'elem_003') to identify elements — not css_selector."
        ),
    )
    tabs: List[TabInfo] = Field(
        default_factory=list,
        description="Open browser tabs. The active tab has is_active=True.",
    )
    recent_action: Optional[RecentActionRecord] = Field(
        default=None,
        description="Structured record of the most recent browser action and its outcome.",
    )

    def to_agent_summary(self) -> str:
        """
        Produce a compact, token-bounded textual summary of the observation
        suitable for direct inclusion in an LLM prompt.

        Format example:
            URL: https://example.com
            Title: Example Domain
            Page Text:
              Example Domain\nThis domain is for use…
            Interactive Elements (3):
              elem_001 [a/link] "More information" href=https://…
            Tabs (1):
              tab_001 [ACTIVE] https://example.com — Example Domain
            Recent Action: none
        """
        lines: List[str] = [
            f"URL: {self.current_url}",
            f"Title: {self.page_title}",
        ]
        if self.page_text_summary:
            lines.append("Page Text:")
            for ln in self.page_text_summary.splitlines()[:12]:
                lines.append(f"  {ln}")
        if self.observed_elements:
            lines.append(f"Interactive Elements ({len(self.observed_elements)}):")
            for el in self.observed_elements:
                lines.append(f"  {el.to_agent_description()}")
        else:
            lines.append("Interactive Elements: none detected")
        if self.tabs:
            lines.append(f"Tabs ({len(self.tabs)}):")
            for t in self.tabs:
                lines.append(f"  {t.to_agent_description()}")
        if self.recent_action:
            ra = self.recent_action
            outcome = "success" if ra.success else f"FAILED ({ra.error or 'unknown error'})"
            ref_str = f" on {ra.element_ref}" if ra.element_ref else ""
            lines.append(f"Recent Action: {ra.action_type.value}{ref_str} → {outcome}")
        else:
            lines.append("Recent Action: none")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 3. BrowserActionDecision
# ─────────────────────────────────────────────────────────────────────────────

class BrowserActionDecision(BaseModel):
    """
    The concrete next browser action selected by the agent.

    Reuses BrowserActionType (navigate, click, fill, screenshot, etc.) from
    the existing browser_models.py.

    IMPORTANT: The agent selects actions in terms of intent and element
    references from the observation. Raw CSS selectors are NOT required here
    — the element_reference field carries a semantic label (e.g. link text,
    aria-label, element_id) that the execution layer can resolve to a
    concrete selector.

    Fields:
        action_type:       What kind of browser action to perform.
        element_reference: Semantic reference to the target element. This is
                           NOT a raw CSS selector. Examples:
                           "More information", "Search button", "#search-form".
                           Phase 3 will resolve this to a concrete selector.
        url:               URL for navigate actions.
        value:             Input value for fill actions.
        key:               Key name for press actions.
        new_tab:           Whether the action should open/use a new tab.
        reasoning:         One-sentence rationale for selecting this action.
                           Used for telemetry and explainability; not
                           required for execution.
        timeout_ms:        Execution timeout for this specific action.
    """
    action_type: BrowserActionType = Field(
        ..., description="Browser action type from BrowserActionType enum"
    )
    element_reference: Optional[str] = Field(
        default=None,
        description=(
            "Semantic element reference (text, aria-label, id) — NOT a raw CSS selector. "
            "The execution layer resolves this to a concrete selector."
        ),
    )
    element_ref: Optional[str] = Field(
        default=None,
        description="Alias/shorthand for element_reference (e.g. 'elem_001').",
    )
    url: Optional[str] = Field(default=None, description="Target URL for navigate actions")
    value: Optional[str] = Field(default=None, description="Input value for fill actions")
    key: Optional[str] = Field(default=None, description="Key name for press actions")
    new_tab: bool = Field(default=False, description="Open or target a new browser tab")
    reasoning: Optional[str] = Field(
        default=None,
        description="One-sentence rationale for this action choice (telemetry only)",
    )
    timeout_ms: int = Field(default=5000, ge=100)

    def model_post_init(self, __context: Any) -> None:
        """Synchronize element_ref and element_reference."""
        if self.element_ref is not None and self.element_reference is None:
            object.__setattr__(self, "element_reference", self.element_ref)
        elif self.element_reference is not None and self.element_ref is None:
            object.__setattr__(self, "element_ref", self.element_reference)

    def to_dict(self) -> dict:
        return {
            "action_type": self.action_type.value if hasattr(self.action_type, "value") else str(self.action_type),
            "element_reference": self.element_reference,
            "element_ref": self.element_ref,
            "url": self.url,
            "value": self.value,
            "key": self.key,
            "new_tab": self.new_tab,
            "reasoning": self.reasoning,
            "timeout_ms": self.timeout_ms,
        }


# ─────────────────────────────────────────────────────────────────────────────
# 4. BrowserDecision
# ─────────────────────────────────────────────────────────────────────────────

class BrowserDecisionType(str, Enum):
    """
    The class of decision the agent has reached after observing the browser.

    EXECUTE:  A concrete browser action should be performed.
    FINISH:   The task objective is achieved — no further actions needed.
    RECOVER:  The current page/state is unexpected; the agent will attempt
              a corrective action before continuing.
    FAIL:     The task cannot safely or meaningfully continue.
              Examples: page unrecoverable, max_actions exceeded, blocked by
              safety policy.
    """
    EXECUTE  = "execute"
    FINISH   = "finish"
    RECOVER  = "recover"
    FAIL     = "fail"


class BrowserDecision(BaseModel):
    """
    The agent's decision after observing the browser state.

    Answers: "What should the BrowserAgent do next?"

    Fields:
        decision:       The class of decision (execute / finish / recover / fail).
        action:         The concrete action to perform. Required when
                        decision == EXECUTE or RECOVER.
        reason:         Short explanation of why this decision was made.
                        Required for all decisions (used for explainability).
        is_final:       True when the decision terminates the agent loop
                        (FINISH or FAIL). The agent will not iterate further.
    """
    decision: BrowserDecisionType = Field(
        ..., description="Class of decision reached by the agent"
    )
    action: Optional[BrowserActionDecision] = Field(
        default=None,
        description="Concrete action to execute (required when decision is EXECUTE or RECOVER)",
    )
    reason: str = Field(
        ..., description="Short explanation of the decision for telemetry/explainability"
    )
    is_final: bool = Field(
        default=False,
        description="True when this decision terminates the agent loop",
    )

    def model_post_init(self, __context) -> None:
        """Validate that FINISH and FAIL decisions are marked final."""
        if self.decision in (BrowserDecisionType.FINISH, BrowserDecisionType.FAIL):
            # Force is_final True for terminal states
            object.__setattr__(self, "is_final", True)


# ─────────────────────────────────────────────────────────────────────────────
# 5. BrowserVerification
# ─────────────────────────────────────────────────────────────────────────────

class BrowserVerificationStatus(str, Enum):
    """
    Outcome of a post-action verification check (Phase 3 Objective C).

    VERIFIED:           The expected outcome was confirmed with supporting evidence.
    NOT_VERIFIED:       Verification failed — expected outcome not observed.
    FAILED:             Evidence indicates the intended outcome did not occur (e.g. error banner).
    PENDING:            Verification is waiting on observations.
    INCONCLUSIVE:       Evidence is insufficient or ambiguous; outcome cannot be proven.
    NEEDS_HUMAN_REVIEW: Human judgment or confirmation required (e.g. uncertain high-risk side effect).
    SKIPPED:            No verification was defined or appropriate for this task.
    """
    VERIFIED           = "verified"
    NOT_VERIFIED       = "not_verified"
    FAILED             = "failed"
    PENDING            = "pending"
    INCONCLUSIVE       = "inconclusive"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    SKIPPED            = "skipped"


class BrowserVerification(BaseModel):
    """
    Whether the browser task objective was actually achieved.

    IMPORTANT INVARIANT:
        action_success (Playwright completed) ≠ task_verified (objective met).

    For example: page.goto() can return success even if the wrong page loaded.
    Verification independently checks that the expected state exists.
    """
    task_id: str
    status: BrowserVerificationStatus = Field(
        ..., description="Verification outcome"
    )
    observed_url: Optional[str] = Field(default=None)
    observed_title: Optional[str] = Field(default=None)
    verified_by: Optional[str] = Field(
        default=None,
        description="Description of the verification check performed",
    )
    failure_reason: Optional[str] = Field(
        default=None,
        description="Explanation for NOT_VERIFIED, FAILED, or INCONCLUSIVE status",
    )
    screenshot_b64: Optional[str] = Field(
        default=None,
        description="Base64-encoded screenshot captured at verification time",
    )
    timestamp: float = Field(default_factory=time.time)

    # Phase 3: Cryptographic Evidence & Explainable Confidence
    confidence_score: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Explainable confidence score [0.0 - 1.0]"
    )
    evidence_id: Optional[str] = Field(default=None, description="Vault evidence identifier")
    evidence_hash: Optional[str] = Field(default=None, description="SHA-256 hash of supporting evidence")
    supporting_evidence: List[Dict[str, Any]] = Field(
        default_factory=list, description="Structured supporting evidence artifacts"
    )

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status.value if hasattr(self.status, "value") else str(self.status),
            "observed_url": self.observed_url,
            "observed_title": self.observed_title,
            "verified_by": self.verified_by,
            "failure_reason": self.failure_reason,
            "screenshot_b64": self.screenshot_b64,
            "timestamp": self.timestamp,
            "confidence_score": self.confidence_score,
            "evidence_id": self.evidence_id,
            "evidence_hash": self.evidence_hash,
            "supporting_evidence": self.supporting_evidence,
        }


class GoalVerificationResult(BaseModel):
    """
    Goal-level verification across all executed tasks in an orchestration graph.
    """
    session_id: str
    goal_text: str
    overall_status: str  # verified, failed, inconclusive, needs_human_review
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    total_tasks: int = 0
    verified_tasks: int = 0
    failed_tasks: int = 0
    inconclusive_tasks: int = 0
    supporting_evidence_hashes: List[str] = Field(default_factory=list)
    explanation: str = ""

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "goal_text": self.goal_text,
            "overall_status": self.overall_status,
            "confidence_score": self.confidence_score,
            "total_tasks": self.total_tasks,
            "verified_tasks": self.verified_tasks,
            "failed_tasks": self.failed_tasks,
            "inconclusive_tasks": self.inconclusive_tasks,
            "supporting_evidence_hashes": self.supporting_evidence_hashes,
            "explanation": self.explanation,
        }


# ─────────────────────────────────────────────────────────────────────────────
# 6. BrowserAgentResult
# ─────────────────────────────────────────────────────────────────────────────

class BrowserAgentOutcome(str, Enum):
    """
    High-level outcome of a complete BrowserAgent task run.

    COMPLETED:        Task was executed and verified successfully.
    COMPLETED_UNVERIFIED: Task was executed but verification was PENDING/SKIPPED.
    FAILED:           Task could not be completed (error, safety block, timeout).
    BLOCKED:          Task was blocked by safety policy before any action.
    """
    COMPLETED             = "completed"
    COMPLETED_UNVERIFIED  = "completed_unverified"
    FAILED                = "failed"
    BLOCKED               = "blocked"


class BrowserAgentResult(BaseModel):
    """
    The final result returned by the BrowserAgent after completing or
    abandoning a BrowserTask.

    This is the higher-level browser task outcome, distinct from ToolResult
    which represents a single tool execution. BrowserAgentResult spans the
    full observe → reason → act → verify lifecycle.

    Fields:
        task_id:         The completed BrowserTask ID.
        session_id:      ARDO session ID.
        outcome:         High-level outcome (COMPLETED / FAILED / BLOCKED).
        objective:       Original task objective for traceability.
        actions_taken:   Ordered list of all actions the agent decided and
                         executed during this task.
        verification:    The final verification result.
        error:           Error message if the task failed. None on success.
        actions_count:   Total number of browser actions executed.
        duration_ms:     Wall-clock duration of the entire agent run in ms.
        final_url:       URL the browser was at when the task concluded.
        final_title:     Page title when the task concluded.
        screenshot_b64:  Optional final-state screenshot (may come from
                         BrowserVerification.screenshot_b64).
    """
    task_id: str
    session_id: str
    outcome: BrowserAgentOutcome = Field(
        ..., description="High-level outcome of the browser task"
    )
    objective: str = Field(
        ..., description="Original task objective (copied from BrowserTask)"
    )
    actions_taken: List[BrowserActionDecision] = Field(
        default_factory=list,
        description="Ordered list of actions the agent decided and executed",
    )
    verification: Optional[BrowserVerification] = Field(
        default=None,
        description="Final verification result for this task",
    )
    error: Optional[str] = Field(
        default=None,
        description="Error message if the task failed or was blocked",
    )
    actions_count: int = Field(
        default=0,
        ge=0,
        description="Total number of browser actions executed",
    )
    duration_ms: float = Field(
        default=0.0,
        ge=0.0,
        description="Total duration of the agent run in milliseconds",
    )
    final_url: Optional[str] = Field(default=None)
    final_title: Optional[str] = Field(default=None)
    screenshot_b64: Optional[str] = Field(
        default=None,
        description="Base64-encoded screenshot of the final browser state",
    )

    @property
    def succeeded(self) -> bool:
        """True when the task completed (verified or unverified)."""
        return self.outcome in (
            BrowserAgentOutcome.COMPLETED,
            BrowserAgentOutcome.COMPLETED_UNVERIFIED,
        )

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "outcome": self.outcome.value if hasattr(self.outcome, "value") else str(self.outcome),
            "objective": self.objective,
            "actions_taken": [
                a.to_dict() if hasattr(a, "to_dict") else (a.dict() if hasattr(a, "dict") else str(a))
                for a in self.actions_taken
            ],
            "verification": (
                self.verification.to_dict()
                if (self.verification is not None and hasattr(self.verification, "to_dict"))
                else (self.verification.dict() if self.verification is not None and hasattr(self.verification, "dict") else None)
            ),
            "error": self.error,
            "actions_count": self.actions_count,
            "duration_ms": self.duration_ms,
            "final_url": self.final_url,
            "final_title": self.final_title,
            "screenshot_b64": self.screenshot_b64,
        }
