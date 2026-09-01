"""
playwright_executor.py — Real Playwright Browser Automation Adapter
====================================================================
Provides an async executor for headless browser automation using Playwright.
Supports safe, sandboxed actions (navigate, click, fill, press, wait, extract, screenshot).
Guarantees cleanup on shutdown or error.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from execution.browser.browser_models import (
    BrowserAction,
    BrowserActionResult,
    BrowserActionType,
    BrowserWorkflowResult,
)

logger = logging.getLogger(__name__)


class PlaywrightBrowserExecutor:
    """
    Asynchronous executor managing Playwright Chromium browser lifecycle.
    """

    def __init__(self, headless: bool = True, default_timeout_ms: int = 5000):
        self.headless = headless
        self.default_timeout_ms = default_timeout_ms
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._is_started = False
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Launch the Playwright browser and open a fresh page context."""
        async with self._lock:
            if self._is_started:
                return

            try:
                from playwright.async_api import async_playwright
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=self.headless,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                self._context = await self._browser.new_context(
                    viewport={"width": 1280, "height": 720},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AI-Goal-To-Task-Engine/1.0",
                )
                self._page = await self._context.new_page()
                self._page.set_default_timeout(self.default_timeout_ms)
                self._is_started = True
                logger.info("[PlaywrightBrowserExecutor] Chromium browser started successfully.")
            except Exception as e:
                logger.error(f"[PlaywrightBrowserExecutor] Failed to launch Playwright browser: {e}")
                await self.stop()
                raise

    async def stop(self) -> None:
        """Close browser context, browser instance, and stop Playwright."""
        async with self._lock:
            if self._page:
                try:
                    await self._page.close()
                except Exception:
                    pass
                self._page = None

            if self._context:
                try:
                    await self._context.close()
                except Exception:
                    pass
                self._context = None

            if self._browser:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None

            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None

            self._is_started = False
            logger.info("[PlaywrightBrowserExecutor] Browser stopped.")

    async def __aenter__(self) -> "PlaywrightBrowserExecutor":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.stop()

    @property
    def is_running(self) -> bool:
        return self._is_started and self._page is not None

    async def execute_action(self, action: BrowserAction) -> BrowserActionResult:
        """
        Executes a single declarative browser action and returns structured result.
        """
        if not self.is_running:
            await self.start()

        t_start = time.perf_counter()
        timeout = action.timeout_ms or self.default_timeout_ms

        try:
            if action.action_type == BrowserActionType.NAVIGATE:
                if not action.url:
                    raise ValueError("URL parameter required for NAVIGATE action.")
                await self._page.goto(action.url, timeout=timeout, wait_until="load")
                current_url = self._page.url
                current_title = await self._page.title()
                duration = (time.perf_counter() - t_start) * 1000.0
                return BrowserActionResult(
                    success=True,
                    action_type=action.action_type,
                    current_url=current_url,
                    current_title=current_title,
                    duration_ms=duration,
                )

            elif action.action_type == BrowserActionType.CLICK:
                if not action.selector:
                    raise ValueError("Selector parameter required for CLICK action.")
                await self._page.click(action.selector, timeout=timeout)
                duration = (time.perf_counter() - t_start) * 1000.0
                return BrowserActionResult(
                    success=True,
                    action_type=action.action_type,
                    current_url=self._page.url,
                    duration_ms=duration,
                )

            elif action.action_type == BrowserActionType.FILL:
                if not action.selector:
                    raise ValueError("Selector parameter required for FILL action.")
                val = action.value or ""
                await self._page.fill(action.selector, val, timeout=timeout)
                duration = (time.perf_counter() - t_start) * 1000.0
                return BrowserActionResult(
                    success=True,
                    action_type=action.action_type,
                    current_url=self._page.url,
                    duration_ms=duration,
                    metadata={"filled_length": len(val)},
                )

            elif action.action_type == BrowserActionType.PRESS:
                if not action.selector or not action.key:
                    raise ValueError("Selector and Key parameters required for PRESS action.")
                await self._page.press(action.selector, action.key, timeout=timeout)
                duration = (time.perf_counter() - t_start) * 1000.0
                return BrowserActionResult(
                    success=True,
                    action_type=action.action_type,
                    current_url=self._page.url,
                    duration_ms=duration,
                )

            elif action.action_type == BrowserActionType.WAIT_FOR_SELECTOR:
                if not action.selector:
                    raise ValueError("Selector parameter required for WAIT_FOR_SELECTOR action.")
                await self._page.wait_for_selector(action.selector, timeout=timeout, state="visible")
                duration = (time.perf_counter() - t_start) * 1000.0
                return BrowserActionResult(
                    success=True,
                    action_type=action.action_type,
                    current_url=self._page.url,
                    duration_ms=duration,
                )

            elif action.action_type == BrowserActionType.EXTRACT_TEXT:
                if not action.selector:
                    raise ValueError("Selector parameter required for EXTRACT_TEXT action.")
                element = await self._page.wait_for_selector(action.selector, timeout=timeout)
                text = (await element.inner_text()) if element else ""
                duration = (time.perf_counter() - t_start) * 1000.0
                return BrowserActionResult(
                    success=True,
                    action_type=action.action_type,
                    text_extracted=text.strip(),
                    current_url=self._page.url,
                    duration_ms=duration,
                )

            elif action.action_type == BrowserActionType.SCREENSHOT:
                screenshot_bytes = await self._page.screenshot(timeout=timeout)
                duration = (time.perf_counter() - t_start) * 1000.0
                return BrowserActionResult(
                    success=True,
                    action_type=action.action_type,
                    current_url=self._page.url,
                    duration_ms=duration,
                    metadata={"screenshot_size_bytes": len(screenshot_bytes)},
                )

            elif action.action_type == BrowserActionType.GET_TITLE:
                title = await self._page.title()
                duration = (time.perf_counter() - t_start) * 1000.0
                return BrowserActionResult(
                    success=True,
                    action_type=action.action_type,
                    current_title=title,
                    current_url=self._page.url,
                    duration_ms=duration,
                )

            elif action.action_type == BrowserActionType.GET_URL:
                duration = (time.perf_counter() - t_start) * 1000.0
                return BrowserActionResult(
                    success=True,
                    action_type=action.action_type,
                    current_url=self._page.url,
                    duration_ms=duration,
                )

            else:
                raise ValueError(f"Unsupported action type: {action.action_type}")

        except Exception as e:
            duration = (time.perf_counter() - t_start) * 1000.0
            error_msg = str(e)
            logger.warning(f"[PlaywrightBrowserExecutor] Action {action.action_type} failed: {error_msg}")
            return BrowserActionResult(
                success=False,
                action_type=action.action_type,
                error=error_msg,
                duration_ms=duration,
                current_url=self._page.url if self._page else None,
            )

    async def execute_workflow(
        self,
        workflow_id: str,
        session_id: str,
        goal_text: str,
        actions: List[BrowserAction],
    ) -> BrowserWorkflowResult:
        """
        Executes a sequence of browser actions. Halts on first failure.
        """
        t_start = time.perf_counter()
        results: List[BrowserActionResult] = []
        overall_success = True
        error_summary = None

        for action in actions:
            res = await self.execute_action(action)
            results.append(res)
            if not res.success:
                overall_success = False
                error_summary = f"Action '{action.action_type.value}' failed on selector '{action.selector or action.url}': {res.error}"
                break

        total_duration = (time.perf_counter() - t_start) * 1000.0
        final_url = self._page.url if self._page else None

        return BrowserWorkflowResult(
            workflow_id=workflow_id,
            session_id=session_id,
            goal_text=goal_text,
            success=overall_success,
            step_results=results,
            total_duration_ms=total_duration,
            final_url=final_url,
            error_summary=error_summary,
        )
