"""
tool_registry.py — Universal Central Tool Registry
==================================================
Manages discovery, schema validation, and safe dispatch for all registered tools.
Maintains singleton instance lifecycle with reset support for isolated test fixtures.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from execution.registry.base_tool import BaseTool
from execution.registry.tool_models import ToolCapability, ToolCall, ToolResult

logger = logging.getLogger(__name__)


class ToolRegistry:
    """
    Central registry managing tool registration, capability lookup, schema validation, and dispatch.
    Supports hierarchical session scoping so each ARDO session gets its own isolated browser tools
    while inheriting global shared tools.
    """
    _instance: Optional[ToolRegistry] = None

    def __init__(self, parent: Optional[ToolRegistry] = None):
        self._parent: Optional[ToolRegistry] = parent
        self._tools: Dict[str, BaseTool] = {}

    @classmethod
    def get_instance(cls) -> ToolRegistry:
        """Get the singleton registry instance."""
        if cls._instance is None:
            cls._instance = ToolRegistry()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> ToolRegistry:
        """Reset the singleton instance (useful for clean test isolation)."""
        cls._instance = ToolRegistry()
        return cls._instance

    def create_session_registry(self) -> ToolRegistry:
        """Create an isolated session-scoped registry inheriting global tools."""
        return ToolRegistry(parent=self)

    def register(self, tool: BaseTool) -> None:
        """Register a tool. Overwrites if already registered under same name in this scope."""
        if tool.name in self._tools:
            logger.warning(f"[ToolRegistry] Overwriting existing tool registration: '{tool.name}'")
        self._tools[tool.name] = tool
        logger.info(f"[ToolRegistry] Registered tool: '{tool.name}' [capability={tool.capability.value}]")

    def unregister(self, name: str) -> Optional[BaseTool]:
        """Unregister a tool from this registry scope."""
        return self._tools.pop(name, None)

    def get_tool(self, name: str) -> Optional[BaseTool]:
        """Look up tool by exact name in this scope, or parent scope if not found."""
        if name in self._tools:
            return self._tools[name]
        if self._parent is not None:
            return self._parent.get_tool(name)
        return None

    def find_by_capability(self, capability: ToolCapability) -> List[BaseTool]:
        """Find all registered tools offering the given capability across this and parent scope."""
        seen_names = set(self._tools.keys())
        tools = [t for t in self._tools.values() if t.capability == capability]
        if self._parent is not None:
            for t in self._parent.find_by_capability(capability):
                if t.name not in seen_names:
                    tools.append(t)
        return tools

    def list_tools(self) -> List[Dict[str, Any]]:
        """Return serialized metadata and JSON schemas for all registered tools."""
        merged: Dict[str, BaseTool] = {}
        if self._parent is not None:
            for t in self._parent._tools.values():
                merged[t.name] = t
        for t in self._tools.values():
            merged[t.name] = t

        result = []
        for t in merged.values():
            schema = {}
            if hasattr(t.input_schema, "model_json_schema"):
                schema = t.input_schema.model_json_schema()
            elif hasattr(t.input_schema, "schema"):
                schema = t.input_schema.schema()

            result.append({
                "name": t.name,
                "description": t.description,
                "capability": t.capability.value,
                "input_schema": schema,
            })
        return result

    async def dispatch(self, tool_call: ToolCall) -> ToolResult:
        """
        Safely dispatches a ToolCall to the target tool.
        Enforces schema validation and catches all execution errors without crashing.
        """
        t0 = time.perf_counter()
        tool = self.get_tool(tool_call.tool_name)
        if not tool:
            duration_ms = (time.perf_counter() - t0) * 1000.0
            return ToolResult(
                call_id=tool_call.call_id,
                tool_name=tool_call.tool_name,
                action_name=tool_call.action_name,
                success=False,
                error=f"TOOL_NOT_FOUND: No tool registered with name '{tool_call.tool_name}'",
                duration_ms=duration_ms,
            )

        # 1. Validate call arguments against tool schema
        try:
            tool.validate_call(tool_call)
        except Exception as ve:
            duration_ms = (time.perf_counter() - t0) * 1000.0
            logger.error(f"[ToolRegistry] Schema validation error for tool '{tool_call.tool_name}': {ve}")
            return ToolResult(
                call_id=tool_call.call_id,
                tool_name=tool_call.tool_name,
                action_name=tool_call.action_name,
                success=False,
                error=f"VALIDATION_ERROR: Invalid arguments for tool '{tool_call.tool_name}': {str(ve)}",
                duration_ms=duration_ms,
            )

        # 2. Execute asynchronously
        try:
            result = await tool.execute(tool_call)
            return result
        except Exception as ee:
            duration_ms = (time.perf_counter() - t0) * 1000.0
            logger.error(f"[ToolRegistry] Unexpected execution error in tool '{tool_call.tool_name}': {ee}")
            return ToolResult(
                call_id=tool_call.call_id,
                tool_name=tool_call.tool_name,
                action_name=tool_call.action_name,
                success=False,
                error=f"EXECUTION_ERROR: {str(ee)}",
                duration_ms=duration_ms,
            )
