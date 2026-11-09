"""
browser_tool.py — Playwright Browser Automation Tool for ToolRegistry
====================================================================
Adapts the verified PlaywrightBrowserExecutor and BrowserTaskAdapter into
the BaseTool contract.
Does NOT rewrite or duplicate browser execution logic.
Preserves existing safety classification and policies.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from execution.registry.base_tool import BaseTool
from execution.registry.tool_models import ToolCapability, ToolCall, ToolResult
from execution.browser.browser_models import (
    BrowserAction,
    BrowserActionResult,
    BrowserActionType,
)
from execution.browser.playwright_executor import PlaywrightBrowserExecutor
from execution.browser.browser_task_adapter import BrowserTaskAdapter
from orchestration.safety_schemas import SafetyContext

logger = logging.getLogger(__name__)


class BrowserTool(BaseTool):
    """
    Playwright Chromium browser automation tool conforming to BaseTool.
    Wraps PlaywrightBrowserExecutor and BrowserTaskAdapter.
    """
    name = "browser"
    description = (
        "Executes sandboxed browser automation actions (navigate, click, fill, press, "
        "wait_for_selector, extract_text, screenshot, get_title, get_url) via Playwright Chromium."
    )
    capability = ToolCapability.BROWSER_AUTOMATION
    input_schema = BrowserAction
    output_schema = BrowserActionResult

    def __init__(
        self,
        executor: Optional[PlaywrightBrowserExecutor] = None,
        adapter: Optional[BrowserTaskAdapter] = None,
    ):
        self.executor = executor or (adapter.executor if adapter is not None else None) or PlaywrightBrowserExecutor()
        self.adapter = adapter or BrowserTaskAdapter(executor=self.executor)
        self.adapter.executor = self.executor

    def validate_call(self, tool_call: ToolCall) -> BrowserAction:
        """
        Validate browser action parameters, automatically ensuring action_type is set.
        """
        args = dict(tool_call.arguments)
        if "action_type" not in args:
            try:
                args["action_type"] = BrowserActionType(tool_call.action_name.lower())
            except ValueError:
                args["action_type"] = tool_call.action_name

        action_type = args.get("action_type")
        if action_type in (BrowserActionType.CLICK, BrowserActionType.FILL, BrowserActionType.WAIT_FOR_SELECTOR, BrowserActionType.EXTRACT_TEXT) and not args.get("selector"):
            raise ValueError(f"Selector parameter required for {getattr(action_type, 'value', action_type)} action.")

        return self.input_schema(**args)

    def infer_safety_context(self, task_id: str, tool_call: ToolCall) -> SafetyContext:
        """
        Derive typed SafetyContext using existing BrowserTaskAdapter logic.
        Guarantees that concrete synthesized parameters (e.g. selectors, URLs)
        are accurately classified.
        """
        try:
            action_type = BrowserActionType(tool_call.action_name.lower())
        except ValueError:
            action_type = BrowserActionType.CLICK

        action = BrowserAction(
            action_type=action_type,
            selector=tool_call.arguments.get("selector"),
            value=tool_call.arguments.get("value"),
            url=tool_call.arguments.get("url"),
            key=tool_call.arguments.get("key"),
            timeout_ms=tool_call.timeout_ms,
            description=tool_call.description,
        )
        return self.adapter.infer_safety_context(task_id, action)

    async def inspect_page(self, max_elements: int = 50) -> Any:
        """
        Inspect active page and return structured PageSnapshot.
        Reuses existing page without launching a secondary session.
        """
        return await self.executor.inspect_page(max_elements=max_elements)

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        """
        Dispatches action to the underlying PlaywrightBrowserExecutor and formats standard ToolResult.
        """
        try:
            action_type = BrowserActionType(tool_call.action_name.lower())
        except ValueError as e:
            return ToolResult(
                call_id=tool_call.call_id,
                tool_name=self.name,
                action_name=tool_call.action_name,
                success=False,
                error=f"UNSUPPORTED_BROWSER_ACTION: '{tool_call.action_name}'. Supported: {[a.value for a in BrowserActionType]}",
            )

        metadata = dict(tool_call.arguments.get("metadata", {}))
        if "new_tab" in tool_call.arguments:
            metadata["new_tab"] = tool_call.arguments["new_tab"]

        action = BrowserAction(
            action_type=action_type,
            selector=tool_call.arguments.get("selector"),
            value=tool_call.arguments.get("value"),
            url=tool_call.arguments.get("url"),
            key=tool_call.arguments.get("key"),
            timeout_ms=tool_call.timeout_ms,
            description=tool_call.description,
            metadata=metadata,
        )

        task_id = (
            tool_call.arguments.get("task_id")
            or tool_call.arguments.get("metadata", {}).get("task_id")
            or getattr(tool_call, "task_id", None)
            or f"tool_{tool_call.call_id}"
        )
        session_id = (
            tool_call.arguments.get("session_id")
            or tool_call.arguments.get("metadata", {}).get("session_id")
            or getattr(tool_call, "session_id", None)
            or "default_session"
        )

        logger.info(f"[ACTION-EXEC] BrowserTool.execute called for tool_call='{tool_call.action_name}' task_id={task_id}")
        logger.info(
            f"[BROWSER-EXEC] BrowserTool action={action.action_type.value} "
            f"selector={action.selector} url={action.url} value={action.value}"
        )

        # Route through BrowserTaskAdapter as the single authoritative safety boundary
        action_res, safety_decision = await self.adapter.execute_safe_browser_task(
            task_id=task_id,
            session_id=session_id,
            action=action,
        )

        artifacts: Dict[str, Any] = {}
        if action.action_type == BrowserActionType.SCREENSHOT:
            if "screenshot_bytes" in action_res.metadata or "screenshot_size_bytes" in action_res.metadata:
                artifacts["screenshot"] = action_res.metadata

        output_data = {
            "current_url": action_res.current_url,
            "current_title": action_res.current_title,
            "action_type": action_res.action_type.value,
        }
        if "snapshot" in action_res.metadata:
            output_data["snapshot"] = action_res.metadata["snapshot"]

        safety_meta = {
            "execution_decision": safety_decision.execution_decision.value,
            "risk_level": safety_decision.risk_level.value,
            "is_blocked": safety_decision.is_blocked,
            "approval_required": safety_decision.approval_required,
            "safety_explanation": safety_decision.explanation,
        }

        return ToolResult(
            call_id=tool_call.call_id,
            tool_name=self.name,
            action_name=action.action_type.value,
            success=action_res.success,
            output_data=output_data,
            extracted_text=action_res.text_extracted,
            error=action_res.error,
            duration_ms=action_res.duration_ms,
            artifacts=artifacts,
            metadata={
                "current_url": action_res.current_url,
                "current_title": action_res.current_title,
                **safety_meta,
                **action_res.metadata,
            },
        )
