"""
browser_models.py — Data models for Playwright Browser Execution
================================================================
Defines strongly-typed browser actions, parameters, and result schemas.
Arbitrary JavaScript execution and unrestricted shell access are strictly disallowed.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class BrowserActionType(str, Enum):
    """Supported safe browser action types."""
    NAVIGATE          = "navigate"
    CLICK             = "click"
    FILL              = "fill"
    PRESS             = "press"
    WAIT_FOR_SELECTOR = "wait_for_selector"
    EXTRACT_TEXT      = "extract_text"
    SCREENSHOT        = "screenshot"
    GET_TITLE         = "get_title"
    GET_URL           = "get_url"
    INSPECT_PAGE      = "inspect_page"
    SCROLL            = "scroll"


class BrowserAction(BaseModel):
    """
    A single declarative browser automation action.
    """
    action_type: BrowserActionType
    selector:    Optional[str] = None
    value:       Optional[str] = None
    url:         Optional[str] = None
    key:         Optional[str] = None
    timeout_ms:  int = 5000
    description: Optional[str] = None
    new_tab:     bool = False
    metadata:    Dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_type": self.action_type.value,
            "selector": self.selector,
            "value": self.value,
            "url": self.url,
            "key": self.key,
            "timeout_ms": self.timeout_ms,
            "description": self.description,
            "new_tab": self.new_tab,
            "metadata": self.metadata,
        }


class BrowserActionResult(BaseModel):
    """
    Structured outcome of an executed browser action.
    """
    success:        bool
    action_type:    BrowserActionType
    text_extracted: Optional[str] = None
    current_url:    Optional[str] = None
    current_title:  Optional[str] = None
    error:          Optional[str] = None
    duration_ms:    float = 0.0
    metadata:       Dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "action_type": self.action_type.value,
            "text_extracted": self.text_extracted,
            "current_url": self.current_url,
            "current_title": self.current_title,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "metadata": self.metadata,
        }


class BrowserWorkflowResult(BaseModel):
    """
    Aggregate result of a multi-step browser execution workflow.
    """
    workflow_id:     str
    session_id:      str
    goal_text:       str
    success:         bool
    step_results:    List[BrowserActionResult] = Field(default_factory=list)
    total_duration_ms: float = 0.0
    final_url:       Optional[str] = None
    error_summary:   Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "session_id": self.session_id,
            "goal_text": self.goal_text,
            "success": self.success,
            "step_results": [r.to_dict() for r in self.step_results],
            "total_duration_ms": self.total_duration_ms,
            "final_url": self.final_url,
            "error_summary": self.error_summary,
        }
