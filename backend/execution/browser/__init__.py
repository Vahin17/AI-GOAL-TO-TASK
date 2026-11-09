"""
Browser automation package for AI Goal-to-Task Execution Engine.
"""

from execution.browser.browser_models import (
    BrowserAction,
    BrowserActionResult,
    BrowserActionType,
    BrowserWorkflowResult,
)
from execution.browser.playwright_executor import PlaywrightBrowserExecutor
from execution.browser.browser_task_adapter import BrowserTaskAdapter
from execution.browser.perception_models import (
    ObservedElement,
    PageElement,
    PageSnapshot,
    TabInfo,
)
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
    RecentActionRecord,
)
from execution.browser.browser_agent import BrowserAgent
from execution.browser.semantic_verifier import SemanticVerifier

__all__ = [
    # Existing browser execution models
    "BrowserAction",
    "BrowserActionResult",
    "BrowserActionType",
    "BrowserWorkflowResult",
    "PlaywrightBrowserExecutor",
    "BrowserTaskAdapter",
    # Perception models (Phase 2: ObservedElement, TabInfo)
    "ObservedElement",
    "PageElement",
    "PageSnapshot",
    "TabInfo",
    # BrowserAgent contracts (Phase 1 & 2)
    "BrowserTask",
    "BrowserObservation",
    "BrowserActionDecision",
    "BrowserDecision",
    "BrowserDecisionType",
    "BrowserVerification",
    "BrowserVerificationStatus",
    "BrowserAgentResult",
    "BrowserAgentOutcome",
    "RecentActionRecord",
    # Phase 3: BrowserAgent
    "BrowserAgent",
    # Phase 5: SemanticVerifier
    "SemanticVerifier",
]
