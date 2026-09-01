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

__all__ = [
    "BrowserAction",
    "BrowserActionResult",
    "BrowserActionType",
    "BrowserWorkflowResult",
    "PlaywrightBrowserExecutor",
    "BrowserTaskAdapter",
]
