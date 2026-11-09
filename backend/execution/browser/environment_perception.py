"""
environment_perception.py — Live Playwright Page Perception Engine
===================================================================
Inspects an active Chromium Page instance without launching a secondary
browser lifecycle. Extracts visible, interactive controls and synthesizes
stable CSS selectors with bounded token representations.

Phase 2 additions:
    _PAGE_TEXT_SCRIPT   — Sandboxed JS that extracts bounded visible page text
                          (headings, paragraphs, list items, labels). Strips
                          scripts/styles; limits to PAGE_TEXT_MAX_CHARS chars.
    observe()           — New high-level method that produces a fully populated
                          BrowserObservation directly from a live Playwright page
                          (or browser context for multi-tab awareness). Uses the
                          existing inspect() method for the PageSnapshot and
                          wraps the results in the Phase 1/2 contract models.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from execution.browser.perception_models import (
    ObservedElement,
    PageElement,
    PageSnapshot,
    TabInfo,
)

logger = logging.getLogger(__name__)

# Maximum characters of page text to include in an observation.
# Keeps LLM context usage bounded. ~2 000 chars ≈ ~500 tokens.
PAGE_TEXT_MAX_CHARS = 2_000

# Sandboxed JavaScript snippet executed inside Playwright page context
# to distill interactive elements safely and determine visibility.
_DOM_DISTILLATION_SCRIPT = """
() => {
    const results = [];
    const selectorCounts = {};

    // Interactive element query
    const candidateNodes = document.querySelectorAll(
        'input, button, select, textarea, a[href], [role="button"], [role="link"], [role="searchbox"], [role="tab"]'
    );

    function isVisible(el) {
        if (!el) return false;
        if (el.checkVisibility) {
            return el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
        }
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
            return false;
        }
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    }

    function generateSelector(el) {
        const tag = el.tagName.toLowerCase();

        // 1. Unique ID (must be a valid identifier without spaces)
        if (el.id && typeof el.id === 'string' && /^[a-zA-Z0-9_\\-]+$/.test(el.id)) {
            try {
                if (document.querySelectorAll('#' + el.id).length === 1) {
                    return '#' + el.id;
                }
            } catch (e) {}
        }

        // 2. data-testid or data-id
        const testId = el.getAttribute('data-testid') || el.getAttribute('data-id') || el.getAttribute('data-cy');
        if (testId) {
            const sel = `${tag}[data-testid="${testId}"]`;
            try {
                if (document.querySelectorAll(sel).length === 1) return sel;
            } catch (e) {}
        }

        // 3. Name attribute
        if (el.name && typeof el.name === 'string') {
            const sel = `${tag}[name="${el.name}"]`;
            try {
                if (document.querySelectorAll(sel).length === 1) return sel;
            } catch (e) {}
        }

        // 4. aria-label attribute
        const ariaLabel = el.getAttribute('aria-label');
        if (ariaLabel && typeof ariaLabel === 'string') {
            const sel = `${tag}[aria-label="${ariaLabel.replace(/"/g, '\\\\"')}"]`;
            try {
                if (document.querySelectorAll(sel).length === 1) return sel;
            } catch (e) {}
        }

        // 5. Unique class combination fallback
        if (el.className && typeof el.className === 'string') {
            const classes = el.className.trim().split(/\\s+/).filter(c => /^[a-zA-Z0-9_\\-]+$/.test(c));
            if (classes.length > 0) {
                const sel = `${tag}.${classes[0]}`;
                try {
                    if (document.querySelectorAll(sel).length === 1) return sel;
                } catch (e) {}
            }
        }

        // 6. Indexed tag fallback
        return `${tag}`;
    }

    for (const node of candidateNodes) {
        if (!isVisible(node)) {
            continue;
        }

        const tag = node.tagName.toLowerCase();
        const role = node.getAttribute('role') || (tag === 'button' ? 'button' : tag === 'a' ? 'link' : null);
        const text = (node.innerText || node.textContent || '').trim();
        const ariaLabel = node.getAttribute('aria-label') || null;
        const placeholder = node.getAttribute('placeholder') || null;
        const name = node.getAttribute('name') || null;
        const elementId = node.id || null;
        const href = (tag === 'a') ? node.getAttribute('href') : null;
        const value = (node.value !== undefined && tag !== 'button') ? String(node.value) : null;
        const inputType = (tag === 'input') ? (node.type || 'text') : null;
        const enabled = !node.disabled && node.getAttribute('aria-disabled') !== 'true';

        let selector = generateSelector(node);
        if (selector in selectorCounts) {
            selectorCounts[selector]++;
            selector = `${selector}:nth-of-type(${selectorCounts[selector]})`;
        } else {
            selectorCounts[selector] = 1;
        }

        results.push({
            tag: tag,
            role: role,
            text: text ? text.substring(0, 100) : null,
            aria_label: ariaLabel,
            placeholder: placeholder,
            name: name,
            element_id: elementId,
            css_selector: selector,
            href: href,
            value: value,
            visible: true,
            enabled: enabled,
            input_type: inputType
        });

        if (results.length >= 60) {
            break; // Bounded perception
        }
    }

    return results;
}
"""

# Sandboxed JS that extracts human-readable visible page text.
# Targets headings, paragraphs, list items, labels.
# Strips scripts, styles, hidden content, and duplicated whitespace.
_PAGE_TEXT_SCRIPT = f"""
() => {{
    // Remove script/style/noscript nodes from a working copy
    const SKIP_TAGS = new Set(['SCRIPT','STYLE','NOSCRIPT','HEAD','META','LINK','SVG','IFRAME']);
    const MAX_CHARS = {PAGE_TEXT_MAX_CHARS};
    const TEXT_TAGS = new Set(['H1','H2','H3','H4','H5','H6','P','LI','LABEL','FIGCAPTION','TD','TH','CAPTION','BLOCKQUOTE','ARTICLE','SECTION','MAIN']);

    function walk(node, parts) {{
        if (parts.join('\\n').length >= MAX_CHARS) return;
        if (node.nodeType === Node.TEXT_NODE) {{
            const t = node.textContent.trim();
            if (t) parts.push(t);
            return;
        }}
        if (node.nodeType !== Node.ELEMENT_NODE) return;
        if (SKIP_TAGS.has(node.tagName)) return;
        const style = window.getComputedStyle(node);
        if (style.display === 'none' || style.visibility === 'hidden') return;

        const isBlock = TEXT_TAGS.has(node.tagName);
        const childParts = [];
        for (const child of node.childNodes) {{
            walk(child, childParts);
        }}
        if (childParts.length) {{
            if (isBlock) {{
                parts.push(childParts.join(' ').trim());
            }} else {{
                parts.push(...childParts);
            }}
        }}
    }}

    const parts = [];
    walk(document.body, parts);

    // Deduplicate adjacent identical lines and collapse whitespace
    const seen = new Set();
    const lines = [];
    for (const p of parts) {{
        const clean = p.replace(/\\s+/g, ' ').trim();
        if (clean && !seen.has(clean)) {{
            seen.add(clean);
            lines.push(clean);
            if (lines.join('\\n').length >= MAX_CHARS) break;
        }}
    }}
    return lines.join('\\n').substring(0, MAX_CHARS);
}}
"""


class EnvironmentPerceptionEngine:
    """
    Perception engine inspecting live Chromium Page contexts via Playwright.
    Reuses existing session pages to extract structured PageSnapshots.

    Phase 2 addition:
        observe() — Builds a fully populated BrowserObservation from a live
                    Playwright page (and optionally a browser context for
                    multi-tab awareness). Imports agent_contracts lazily to
                    avoid a circular import at module load time.
    """

    def __init__(self, max_elements: int = 50, capture_accessibility: bool = True):
        self.max_elements = max_elements
        self.capture_accessibility = capture_accessibility

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 1 API (unchanged): returns a PageSnapshot
    # ─────────────────────────────────────────────────────────────────────────

    async def inspect(self, page: Any) -> PageSnapshot:
        """
        Inspect the currently loaded page and return a token-efficient PageSnapshot.

        Args:
            page: Active Playwright Page instance (e.g. executor._page).

        Returns:
            PageSnapshot containing live interactive elements and metadata.
        """
        if page is None:
            raise ValueError("[EnvironmentPerceptionEngine] Cannot inspect None page context.")

        t0 = time.perf_counter()
        current_url = getattr(page, "url", "")
        current_title = ""

        try:
            current_title = await page.title()
        except Exception as te:
            logger.debug(f"[EnvironmentPerceptionEngine] Failed to get page title: {te}")

        # 1. Distill interactive elements via in-page evaluation
        raw_elements: List[Dict[str, Any]] = []
        try:
            raw_elements = await page.evaluate(_DOM_DISTILLATION_SCRIPT)
        except Exception as ee:
            logger.warning(f"[EnvironmentPerceptionEngine] In-page DOM evaluation error: {ee}")

        elements: List[PageElement] = []
        for r in raw_elements[: self.max_elements]:
            try:
                elements.append(PageElement(**r))
            except Exception as pe:
                logger.debug(f"[EnvironmentPerceptionEngine] Skipping malformed element: {pe}")

        # 2. Optional distilled accessibility tree
        accessibility_tree = None
        if self.capture_accessibility and hasattr(page, "accessibility"):
            try:
                raw_tree = await page.accessibility.snapshot()
                if raw_tree:
                    accessibility_tree = self._distill_accessibility_tree(raw_tree)
            except Exception as ae:
                logger.debug(f"[EnvironmentPerceptionEngine] Accessibility snapshot failed: {ae}")

        # 3. Page text (Phase 2 — best-effort, does not fail inspect())
        page_text: Optional[str] = None
        try:
            page_text = await page.evaluate(_PAGE_TEXT_SCRIPT)
            if page_text:
                page_text = page_text[:PAGE_TEXT_MAX_CHARS]
        except Exception as pte:
            logger.debug(f"[EnvironmentPerceptionEngine] Page text extraction failed: {pte}")

        duration_ms = (time.perf_counter() - t0) * 1000.0
        logger.info(
            f"[EnvironmentPerceptionEngine] Inspected page '{current_title}' ({current_url}): "
            f"{len(elements)} interactive elements found in {duration_ms:.1f}ms."
        )

        return PageSnapshot(
            url=current_url,
            title=current_title,
            elements=elements,
            accessibility_tree=accessibility_tree,
            page_text=page_text,
            timestamp=time.time(),
            metadata={
                "duration_ms": duration_ms,
                "element_count": len(elements),
            },
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 2 API: returns a BrowserObservation
    # ─────────────────────────────────────────────────────────────────────────

    async def observe(
        self,
        page: Any,
        session_id: str,
        task_id: str,
        *,
        context: Optional[Any] = None,
        actions_taken_count: int = 0,
        recent_action: Optional[Any] = None,
    ) -> Any:  # returns BrowserObservation (imported lazily)
        """
        Build a fully populated BrowserObservation from a live Playwright page.

        This is the Phase 2 high-level entry point.  It:
          1. Calls inspect() to get a PageSnapshot (existing Phase 1 path).
          2. Produces ObservedElement list with stable per-observation refs
             (elem_001, elem_002, …) from the PageSnapshot elements.
          3. Collects TabInfo for all open pages in the browser context (if
             the context is provided).
          4. Wraps everything in a BrowserObservation.

        Args:
            page:                Live Playwright Page instance.
            session_id:          ARDO session identifier.
            task_id:             Current ARDO DAG task identifier.
            context:             Playwright BrowserContext (optional). When
                                 supplied, tab information for all open pages
                                 is collected. When None, only the active page
                                 is represented as tab_001.
            actions_taken_count: How many browser actions have been taken so
                                 far for this task.
            recent_action:       Optional RecentActionRecord from the previous
                                 action execution.

        Returns:
            BrowserObservation populated with observed_elements, tabs,
            page_text_summary, and recent_action.
        """
        # Lazy import to avoid circular dependency
        from execution.browser.agent_contracts import BrowserObservation

        if page is None:
            raise ValueError("[EnvironmentPerceptionEngine] Cannot observe None page context.")

        # Step 1: obtain PageSnapshot (also populates page_text)
        snapshot = await self.inspect(page)

        # Step 2: convert PageElements → ObservedElements with stable refs
        observed_elements = _assign_element_refs(snapshot.elements)

        # Step 3: collect tab information
        tabs = await _collect_tabs(page, context)

        # Step 4: determine active tab index
        active_tab_index = next((t.index for t in tabs if t.is_active), 0)

        # Step 5: bounded page text summary
        page_text_summary: Optional[str] = None
        if snapshot.page_text:
            page_text_summary = snapshot.page_text[:PAGE_TEXT_MAX_CHARS]

        return BrowserObservation(
            session_id=session_id,
            task_id=task_id,
            current_url=snapshot.url,
            page_title=snapshot.title,
            page_snapshot=snapshot,
            page_text_summary=page_text_summary,
            open_tab_count=len(tabs) if tabs else 1,
            active_tab_index=active_tab_index,
            actions_taken_count=actions_taken_count,
            timestamp=snapshot.timestamp,
            observed_elements=observed_elements,
            tabs=tabs,
            recent_action=recent_action,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _distill_accessibility_tree(self, node: Dict[str, Any], depth: int = 0) -> Optional[Dict[str, Any]]:
        """
        Recursively distill accessibility tree to only retain essential landmarks/controls.
        """
        if depth > 4:
            return None

        role = node.get("role", "")
        name = node.get("name", "")
        children = node.get("children", [])

        # Filter uninformative generic containers
        distilled_children = []
        for c in children:
            dc = self._distill_accessibility_tree(c, depth + 1)
            if dc:
                distilled_children.append(dc)

        # Retain if node has a name, is an interactive role, or has meaningful children
        is_interactive = role in (
            "button", "textbox", "searchbox", "link", "combobox",
            "checkbox", "radio", "heading", "alert", "dialog"
        )
        if name or is_interactive or distilled_children:
            res: Dict[str, Any] = {"role": role}
            if name:
                res["name"] = name[:50]
            if distilled_children:
                res["children"] = distilled_children[:10]
            return res

        return None


# ─────────────────────────────────────────────────────────────────────────────
# Module-level helpers (not part of EnvironmentPerceptionEngine class)
# ─────────────────────────────────────────────────────────────────────────────

def _assign_element_refs(elements: List[PageElement]) -> List[ObservedElement]:
    """
    Convert a list of PageElement objects into ObservedElement objects by
    assigning a stable per-observation semantic reference.

    The format is "elem_NNN" (zero-padded to three digits, 1-indexed).

    Refs are only stable within this single observation. After the next
    browser action the agent must re-observe to get fresh refs.

    Args:
        elements: PageElement list from a PageSnapshot.

    Returns:
        List[ObservedElement] with unique refs elem_001 … elem_NNN.
    """
    result: List[ObservedElement] = []
    for i, el in enumerate(elements, start=1):
        ref = f"elem_{i:03d}"
        result.append(ObservedElement.from_page_element(ref, el))
    return result


async def _collect_tabs(page: Any, context: Optional[Any]) -> List[TabInfo]:
    """
    Collect TabInfo for all open pages in the browser context.

    If context is None, returns a single TabInfo for the active page only.

    Args:
        page:    Active Playwright Page instance.
        context: Playwright BrowserContext, or None.

    Returns:
        Ordered List[TabInfo] with is_active=True on the current page.
    """
    tabs: List[TabInfo] = []

    if context is not None:
        try:
            pages = context.pages  # synchronous attribute on BrowserContext
        except Exception:
            pages = [page]
    else:
        pages = [page]

    for idx, pg in enumerate(pages):
        is_active = pg is page
        url = ""
        title = ""
        try:
            url = getattr(pg, "url", "") or ""
        except Exception:
            pass
        try:
            title = await pg.title() if is_active else ""
        except Exception:
            pass
        tabs.append(TabInfo(
            tab_ref=f"tab_{idx + 1:03d}",
            url=url,
            title=title,
            is_active=is_active,
            index=idx,
        ))

    # Guarantee at least one tab entry
    if not tabs:
        url = getattr(page, "url", "") or ""
        tabs.append(TabInfo(tab_ref="tab_001", url=url, title="", is_active=True, index=0))

    return tabs
