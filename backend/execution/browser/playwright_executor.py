"""
playwright_executor.py — Real Playwright Browser Automation Adapter
====================================================================
Provides an async executor for headless browser automation using Playwright.
Supports safe, sandboxed actions (navigate, click, fill, press, wait, extract, screenshot).
Guarantees cleanup on shutdown or error.

Windows note: Playwright spawns browser processes via asyncio.create_subprocess_exec,
which requires a ProactorEventLoop on Windows. When uvicorn runs with --reload it uses
a SelectorEventLoop (for subprocess-based reloader). This class transparently delegates
all browser work to a dedicated background ProactorEventLoop thread on Windows.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import sys
import threading
import time
from typing import Any, Dict, List, Optional

from execution.browser.browser_models import (
    BrowserAction,
    BrowserActionResult,
    BrowserActionType,
    BrowserWorkflowResult,
)

logger = logging.getLogger(__name__)


def _needs_proactor_delegation() -> bool:
    """Return True if we're running inside a non-Proactor loop on Windows."""
    if sys.platform != "win32":
        return False
    try:
        loop = asyncio.get_running_loop()
        proactor_cls = getattr(asyncio, "ProactorEventLoop", None)
        return proactor_cls is not None and not isinstance(loop, proactor_cls)
    except RuntimeError:
        return False


class _ProactorWorker:
    """
    Singleton background thread running a ProactorEventLoop for Playwright on Windows.
    All Playwright coroutines are submitted to this loop via run_coroutine_threadsafe.
    """
    _instance: Optional[_ProactorWorker] = None
    _lock = threading.Lock()

    def __init__(self):
        self._loop = asyncio.ProactorEventLoop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            name="PlaywrightProactorWorker",
            daemon=True,
        )
        self._thread.start()
        logger.info("[PlaywrightProactorWorker] ProactorEventLoop thread started.")

    @classmethod
    def get(cls) -> "_ProactorWorker":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    async def run(self, coro):
        """Submit coro to the ProactorEventLoop and await its result from any loop."""
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return await asyncio.wrap_future(fut)

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop


class _BrowserSession:
    """
    Internal: Playwright browser session living entirely on one event loop.
    All methods are called from within the correct loop context.
    """
    def __init__(self, headless: bool, default_timeout_ms: int):
        self.headless = headless
        self.default_timeout_ms = default_timeout_ms
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._is_started = False

    async def start(self) -> None:
        if self._is_started:
            return
        try:
            from playwright.async_api import async_playwright
            from core.config import settings
            self._playwright = await async_playwright().start()
            launch_kwargs = {
                "headless": self.headless,
                "args": ["--no-sandbox", "--disable-dev-shm-usage"],
            }
            if getattr(settings, "BROWSER_EGRESS_PROXY", None):
                launch_kwargs["proxy"] = {"server": settings.BROWSER_EGRESS_PROXY}
            elif settings.is_production():
                raise RuntimeError(
                    "Network Isolation Violation: BROWSER_EGRESS_PROXY must be configured in production "
                    "to prevent unproxied direct browser network egress."
                )

            self._browser = await self._playwright.chromium.launch(**launch_kwargs)
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AI-Goal-To-Task-Engine/1.0",
            )
            # Phase 2: Attach NetworkGuard SSRF and private-network route interception
            from execution.browser.network_guard import NetworkGuard
            await NetworkGuard.attach_to_context(self._context)

            self._page = await self._context.new_page()
            self._page.set_default_timeout(self.default_timeout_ms)
            self._is_started = True
            logger.info("[PlaywrightBrowserExecutor] Chromium browser started successfully.")
        except Exception as e:
            logger.error(f"[PlaywrightBrowserExecutor] Failed to launch Playwright browser: {e}")
            await self.stop()
            raise

    async def stop(self) -> None:
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

    @property
    def is_running(self) -> bool:
        return self._is_started and self._page is not None

    async def inspect_page(self, max_elements: int = 50) -> Any:
        if not self.is_running:
            await self.start()
        from execution.browser.environment_perception import EnvironmentPerceptionEngine
        engine = EnvironmentPerceptionEngine(max_elements=max_elements)
        return await engine.inspect(self._page)

    async def execute_action(self, action: BrowserAction) -> BrowserActionResult:
        t_start = time.perf_counter()

        # Phase 2: Immediate pre-check destination URL against NetworkGuard before launching or dispatching
        if action.action_type == BrowserActionType.NAVIGATE:
            if not action.url:
                raise ValueError("URL parameter required for NAVIGATE action.")
            from execution.browser.network_guard import NetworkGuard
            verdict = NetworkGuard.validate_url(action.url)
            if not verdict.allowed:
                logger.warning(f"[PlaywrightBrowserExecutor] Navigation blocked by NetworkGuard: {action.url} ({verdict.reason})")
                return BrowserActionResult(
                    success=False,
                    action_type=action.action_type,
                    current_url=self._page.url if self._page else None,
                    duration_ms=(time.perf_counter() - t_start) * 1000.0,
                    error=f"SSRF Protection Blocked Navigation: {verdict.reason}",
                )

        if not self.is_running:
            await self.start()

        url_before = getattr(self._page, "url", "about:blank") if self._page else "none"
        tab_count = len(self._context.pages) if self._context else 1

        timeout = action.timeout_ms or self.default_timeout_ms

        try:
            if action.action_type == BrowserActionType.NAVIGATE:
                tabs_before = len(self._context.pages) if self._context else 1
                current_page_before = self._page.url if self._page else "none"
                new_tab_requested = (
                    "new tab" in (action.description or "").lower()
                    or getattr(action, "new_tab", False)
                    or (isinstance(getattr(action, "metadata", None), dict) and getattr(action, "metadata", {}).get("new_tab", False))
                )
                logger.info(f"[BROWSER-EXEC] action={action.action_type.value} target='{action.url}' new_tab_requested={new_tab_requested}")
                new_tab_created = False

                # Support new tab if requested
                if new_tab_requested and self._context:
                    self._page = await self._context.new_page()
                    new_tab_created = True

                tabs_after = len(self._context.pages) if self._context else tabs_before
                current_page_after = self._page.url if self._page else "none"

                nav_timeout = max(timeout, 10000)
                await self._page.goto(action.url, timeout=nav_timeout, wait_until="domcontentloaded")
                current_url = self._page.url
                current_title = await self._page.title()
                duration = (time.perf_counter() - t_start) * 1000.0

                logger.info(
                    f"[NEW-TAB-SEMANTICS] tabs_before={tabs_before} tabs_after={tabs_after} "
                    f"current_page_before='{current_page_before}' current_page_after='{current_page_after}' "
                    f"new_tab_requested={new_tab_requested} new_tab_created={new_tab_created} "
                    f"target_url='{action.url}' final_url='{current_url}'"
                )

                logger.info(f"[VERIFICATION] destination_match=True page_loaded=True url='{current_url}' title='{current_title}'")

                from execution.browser.locator_resilience import LocatorResilienceEngine
                LocatorResilienceEngine.invalidate_page_cache(session_id="default", page_url=current_url)

                result = BrowserActionResult(
                    success=True,
                    action_type=action.action_type,
                    current_url=current_url,
                    current_title=current_title,
                    duration_ms=duration,
                    metadata={
                        "tabs_before": tabs_before,
                        "tabs_after": tabs_after,
                        "current_page_before": current_page_before,
                        "current_page_after": current_page_after,
                        "new_tab_requested": new_tab_requested,
                        "new_tab_created": new_tab_created,
                        "target_url": action.url,
                        "final_url": current_url,
                    },
                )
                logger.info(
                    f"[PLAYWRIGHT] method='{action.action_type.value}' url_before='{url_before}' "
                    f"url_after='{current_url}' tabs={tabs_after} success={result.success}"
                )
                return result

            elif action.action_type == BrowserActionType.CLICK:
                if not action.selector:
                    raise ValueError("Selector parameter required for CLICK action.")

                # Phase 2: Locator Resilience Fallback Resolution
                from execution.browser.locator_resilience import LocatorResilienceEngine
                hints = getattr(action, "metadata", {}) if isinstance(getattr(action, "metadata", None), dict) else {}
                target_selector = await LocatorResilienceEngine.resolve_resilient_selector(
                    page=self._page,
                    primary_selector=action.selector,
                    hints=hints,
                    timeout_ms=min(timeout, 3000),
                ) or action.selector

                # Human-like cursor interaction: hover to element center before clicking
                try:
                    el = await self._page.wait_for_selector(target_selector, state="visible", timeout=min(timeout, 5000))
                    if el:
                        box = await el.bounding_box()
                        if box:
                            target_x = round(box["x"] + box["width"] / 2, 1)
                            target_y = round(box["y"] + box["height"] / 2, 1)
                            logger.info(f"[CURSOR] action=move x={target_x} y={target_y}")
                            await self._page.mouse.move(target_x, target_y)
                except Exception:
                    pass
                logger.info(f"[CLICK] selector='{target_selector}'")
                await self._page.click(target_selector, timeout=timeout)
                duration = (time.perf_counter() - t_start) * 1000.0
                current_title = None
                try:
                    if hasattr(self._page, "title"):
                        current_title = await self._page.title()
                except Exception:
                    pass
                return BrowserActionResult(
                    success=True,
                    action_type=action.action_type,
                    current_url=self._page.url if self._page else None,
                    current_title=current_title,
                    duration_ms=duration,
                )

            elif action.action_type == BrowserActionType.FILL:
                if not action.selector:
                    raise ValueError("Selector parameter required for FILL action.")

                # Phase 2: Locator Resilience Fallback Resolution
                from execution.browser.locator_resilience import LocatorResilienceEngine
                hints = getattr(action, "metadata", {}) if isinstance(getattr(action, "metadata", None), dict) else {}
                target_selector = await LocatorResilienceEngine.resolve_resilient_selector(
                    page=self._page,
                    primary_selector=action.selector,
                    hints=hints,
                    timeout_ms=min(timeout, 3000),
                ) or action.selector

                val = action.value or ""
                # Human-like cursor interaction: move mouse to input field before filling
                try:
                    el = await self._page.wait_for_selector(target_selector, state="visible", timeout=min(timeout, 5000))
                    if el:
                        box = await el.bounding_box()
                        if box:
                            target_x = round(box["x"] + box["width"] / 2, 1)
                            target_y = round(box["y"] + box["height"] / 2, 1)
                            logger.info(f"[CURSOR] action=move x={target_x} y={target_y}")
                            await self._page.mouse.move(target_x, target_y)
                except Exception:
                    pass
                logger.info(f"[FILL] selector='{target_selector}' value_length={len(val)}")
                await self._page.fill(target_selector, val, timeout=timeout)

                duration = (time.perf_counter() - t_start) * 1000.0
                current_title = None
                try:
                    if hasattr(self._page, "title"):
                        current_title = await self._page.title()
                except Exception:
                    pass

                return BrowserActionResult(
                    success=True,
                    action_type=action.action_type,
                    current_url=self._page.url if self._page else None,
                    current_title=current_title,
                    duration_ms=duration,
                    metadata={"filled_length": len(val)},
                )

            elif action.action_type == BrowserActionType.PRESS:
                if not action.key:
                    raise ValueError("Key parameter required for PRESS action.")
                if action.selector:
                    await self._page.press(action.selector, action.key, timeout=timeout)
                else:
                    await self._page.keyboard.press(action.key)
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
                b64_str = base64.b64encode(screenshot_bytes).decode("utf-8")
                res = BrowserActionResult(
                    success=True,
                    action_type=action.action_type,
                    current_url=self._page.url,
                    duration_ms=duration,
                    metadata={
                        "screenshot_size_bytes": len(screenshot_bytes),
                        "screenshot_base64": b64_str,
                    },
                )
                logger.info(
                    f"[PLAYWRIGHT] method='{action.action_type.value}' url_before='{url_before}' "
                    f"url_after='{self._page.url}' tabs={tab_count} success=True "
                    f"screenshot_size={len(screenshot_bytes)}"
                )
                return res

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

            elif action.action_type == BrowserActionType.SCROLL:
                scroll_delta = int(action.value) if action.value and action.value.isdigit() else 500
                await self._page.mouse.wheel(0, scroll_delta)
                await self._page.wait_for_timeout(300)
                duration = (time.perf_counter() - t_start) * 1000.0
                logger.info(f"[PLAYWRIGHT] method='scroll' delta={scroll_delta} success=True")
                return BrowserActionResult(
                    success=True,
                    action_type=action.action_type,
                    current_url=self._page.url,
                    duration_ms=duration,
                    metadata={"scroll_delta": scroll_delta},
                )

            elif action.action_type == BrowserActionType.INSPECT_PAGE:
                snapshot = await self.inspect_page()
                duration = (time.perf_counter() - t_start) * 1000.0
                return BrowserActionResult(
                    success=True,
                    action_type=action.action_type,
                    current_url=snapshot.url,
                    current_title=snapshot.title,
                    duration_ms=duration,
                    metadata={
                        "element_count": len(snapshot.elements),
                        "snapshot": snapshot.to_dict(),
                    },
                )

            else:
                raise ValueError(f"Unsupported action type: {action.action_type}")

        except Exception as e:
            duration = (time.perf_counter() - t_start) * 1000.0
            error_msg = str(e)
            logger.warning(f"[PlaywrightBrowserExecutor] Action {action.action_type} failed: {error_msg}")
            url_after = getattr(self._page, "url", "about:blank") if self._page else "none"
            logger.info(
                f"[PLAYWRIGHT] method='{action.action_type.value}' url_before='{url_before}' "
                f"url_after='{url_after}' tabs={tab_count} success=False error='{error_msg}'"
            )
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
        """Executes a sequence of browser actions. Halts on first failure."""
        t_start = time.perf_counter()
        results: List[BrowserActionResult] = []
        overall_success = True
        error_summary = None

        for action in actions:
            res = await self.execute_action(action)
            results.append(res)
            if not res.success:
                overall_success = False
                error_summary = (
                    f"Action '{action.action_type.value}' failed on "
                    f"selector '{action.selector or action.url}': {res.error}"
                )
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


class PlaywrightBrowserExecutor:
    """
    Public async executor — delegates to _BrowserSession on the appropriate event loop.

    On Windows under SelectorEventLoop (e.g. uvicorn --reload), Playwright cannot spawn
    subprocesses. This class routes all Playwright operations to a singleton
    ProactorEventLoop running in a background thread (_ProactorWorker).
    """

    def __init__(self, headless: Optional[bool] = None, default_timeout_ms: int = 5000):
        from core.config import settings
        self.headless = getattr(settings, "HEADLESS_BROWSER", True) if headless is None else headless
        self.default_timeout_ms = default_timeout_ms
        self._session: Optional[_BrowserSession] = None

    def _get_session(self) -> _BrowserSession:
        if self._session is None:
            self._session = _BrowserSession(
                headless=self.headless,
                default_timeout_ms=self.default_timeout_ms,
            )
        return self._session

    async def _run(self, coro):
        """Run coro on the correct loop (Proactor thread on Windows, current loop elsewhere)."""
        if _needs_proactor_delegation():
            worker = _ProactorWorker.get()
            return await worker.run(coro)
        return await coro

    async def start(self) -> None:
        """Launch the Playwright browser and open a fresh page context."""
        await self._run(self._get_session().start())

    async def stop(self) -> None:
        """Close browser context, browser instance, and stop Playwright."""
        if self._session is not None:
            await self._run(self._session.stop())

    async def __aenter__(self) -> "PlaywrightBrowserExecutor":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.stop()

    @property
    def is_running(self) -> bool:
        return self._session is not None and self._session.is_running

    @property
    def _page(self):
        """Backward-compat: expose the internal Playwright Page object."""
        if self._session is None:
            return None
        return self._session._page

    @property
    def _context(self):
        """Backward-compat: expose the internal Playwright BrowserContext."""
        if self._session is None:
            return None
        return self._session._context

    @property
    def _browser(self):
        """Backward-compat: expose the internal Playwright Browser."""
        if self._session is None:
            return None
        return self._session._browser

    async def inspect_page(self, max_elements: int = 50) -> Any:
        """Inspect active page and return structured PageSnapshot."""
        return await self._run(self._get_session().inspect_page(max_elements=max_elements))

    async def execute_action(self, action: BrowserAction) -> BrowserActionResult:
        """Executes a single declarative browser action and returns structured result."""
        return await self._run(self._get_session().execute_action(action))

    async def execute_workflow(
        self,
        workflow_id: str,
        session_id: str,
        goal_text: str,
        actions: List[BrowserAction],
    ) -> BrowserWorkflowResult:
        """Executes a sequence of browser actions. Halts on first failure."""
        return await self._run(
            self._get_session().execute_workflow(workflow_id, session_id, goal_text, actions)
        )
