"""
tool_models.py — Data models for the Universal Tool Registry
============================================================
Defines strongly-typed tool calls, execution results, and capability domains.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


class ToolCapability(str, Enum):
    """Broad capability domains for tool categorization."""
    BROWSER_AUTOMATION = "browser_automation"
    HTTP_REQUEST       = "http_request"
    FILE_SYSTEM        = "file_system"
    DATA_SEARCH        = "data_search"
    SYSTEM_COMMAND     = "system_command"
    CUSTOM             = "custom"


class ToolCall(BaseModel):
    """
    Concrete tool invocation request bound to an ARDOTaskNode.
    """
    call_id:     str = Field(..., description="Unique ID for this tool call")
    tool_name:   str = Field(..., description="Target tool registered in ToolRegistry")
    action_name: str = Field(..., description="Specific sub-action within the tool")
    arguments:   Dict[str, Any] = Field(default_factory=dict, description="Tool arguments")
    timeout_ms:  int = Field(default=5000, description="Execution timeout in milliseconds")
    description: Optional[str] = Field(None, description="Human-readable execution intent")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "action_name": self.action_name,
            "arguments": self.arguments,
            "timeout_ms": self.timeout_ms,
            "description": self.description,
        }


class ToolResult(BaseModel):
    """
    Unified result returned by any Tool executor.
    """
    call_id:        str = Field(..., description="Matching ToolCall ID")
    tool_name:      str = Field(..., description="Name of executed tool")
    action_name:    str = Field(..., description="Name of executed action")
    success:        bool = Field(..., description="Whether action completed without error")
    output_data:    Optional[Any] = Field(default=None, description="Structured output payload")
    extracted_text: Optional[str] = Field(default=None, description="Any primary extracted textual content")
    error:          Optional[str] = Field(default=None, description="Error message if failed")
    duration_ms:    float = Field(default=0.0, description="Execution duration in milliseconds")
    artifacts:      Dict[str, Any] = Field(default_factory=dict, description="Artifacts (screenshots, logs, blobs)")
    metadata:       Dict[str, Any] = Field(default_factory=dict, description="Additional contextual metadata")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "action_name": self.action_name,
            "success": self.success,
            "output_data": self.output_data,
            "extracted_text": self.extracted_text,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "artifacts": self.artifacts,
            "metadata": self.metadata,
        }
