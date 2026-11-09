"""
base_tool.py — Abstract Base Contract for All Executable Tools
=============================================================
Defines the standard interface that every tool in the engine must implement.
Future tools (HTTP, Filesystem, Search) must subclass BaseTool.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Type
from pydantic import BaseModel

from execution.registry.tool_models import ToolCapability, ToolCall, ToolResult
from orchestration.safety_schemas import SafetyContext


class BaseTool(ABC):
    """
    Abstract contract for all executable tools in the engine.
    """
    name: str
    description: str
    capability: ToolCapability
    input_schema: Type[BaseModel]
    output_schema: Type[BaseModel]

    @abstractmethod
    async def execute(self, tool_call: ToolCall) -> ToolResult:
        """
        Execute the tool call asynchronously and return a standardized ToolResult.
        Must never raise unhandled exceptions; return ToolResult(success=False, error=...) instead.
        """
        pass

    @abstractmethod
    def infer_safety_context(self, task_id: str, tool_call: ToolCall) -> SafetyContext:
        """
        Map the concrete tool call parameters into typed SafetyContext for policy checking.
        Must accurately reflect the real target, action type, sensitivity, etc.
        """
        pass

    def validate_call(self, tool_call: ToolCall) -> BaseModel:
        """
        Validate tool call arguments against the tool's input schema.
        Raises pydantic.ValidationError if invalid.
        """
        return self.input_schema(**tool_call.arguments)
