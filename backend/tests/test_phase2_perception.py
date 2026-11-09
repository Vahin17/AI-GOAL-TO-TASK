"""
test_phase2_perception.py — Unit tests for Phase 2 browser perception improvements
====================================================================================
Tests that:
  1.  PageSnapshot / observation contains URL and title.
  2.  page_text is populated when page text is available.
  3.  Interactive elements receive semantic element references (elem_NNN).
  4.  Element references are unique within an observation.
  5.  The reference maps to the underlying observed element / CSS selector.
  6.  Accessibility information remains available (PageSnapshot.accessibility_tree).
  7.  Multiple tabs / pages can be represented via TabInfo.
  8.  Active page information is represented correctly (is_active flag).
  9.  Large page text is reasonably bounded (PAGE_TEXT_MAX_CHARS).
  10. Existing PageSnapshot / PageElement models do not regress.
  11. ObservedElement.from_page_element round-trips all fields.
  12. ObservedElement.to_agent_description is compact and human-readable.
  13. TabInfo.to_agent_description marks active tab.
  14. _assign_element_refs generates sequential unique refs.
  15. BrowserObservation.to_agent_summary produces the expected format.
  16. BrowserObservation.recent_action carries RecentActionRecord.
  17. BrowserObservation Phase 1 fields remain backward-compatible.
  18. observe() async method builds BrowserObservation from mock page.
  19. observe() handles missing page context gracefully.
  20. observe() with multi-page context produces multiple TabInfo entries.

All tests are synchronous or use asyncio.run() — does NOT require pytest-asyncio.
Uses mocked/fake browser data. No live Playwright or Gemini calls.
"""

import asyncio
import time
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from execution.browser.perception_models import (
    ObservedElement,
    PageElement,
    PageSnapshot,
    TabInfo,
)
from execution.browser.agent_contracts import (
    BrowserObservation,
    RecentActionRecord,
)
from execution.browser.browser_models import BrowserActionType
from execution.browser.environment_perception import (
    PAGE_TEXT_MAX_CHARS,
    EnvironmentPerceptionEngine,
    _assign_element_refs,
    _collect_tabs,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixture helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_page_element(
    tag: str = "a",
    text: str = "More information",
    css_selector: str = "a[href]",
    href: Optional[str] = "https://example.com/more",
    role: Optional[str] = "link",
    visible: bool = True,
    enabled: bool = True,
    **kwargs,
) -> PageElement:
    return PageElement(
        tag=tag,
        text=text,
        css_selector=css_selector,
        href=href,
        role=role,
        visible=visible,
        enabled=enabled,
        **kwargs,
    )


def make_snapshot(
    url: str = "https://example.com",
    title: str = "Example Domain",
    elements: Optional[List[PageElement]] = None,
    page_text: Optional[str] = None,
    accessibility_tree: Optional[Dict[str, Any]] = None,
) -> PageSnapshot:
    if elements is None:
        elements = [make_page_element()]
    return PageSnapshot(
        url=url,
        title=title,
        elements=elements,
        page_text=page_text,
        accessibility_tree=accessibility_tree,
    )


def make_mock_page(
    url: str = "https://example.com",
    title: str = "Example Domain",
    dom_elements: Optional[List[Dict]] = None,
    page_text: str = "Example Domain\nThis domain is for use in illustrative examples.",
    accessibility: bool = False,
) -> MagicMock:
    """
    Create a mock Playwright Page whose evaluate() returns static fixtures.
    All async methods return coroutines.
    """
    page = MagicMock()
    page.url = url

    async def mock_title():
        return title

    page.title = mock_title

    # Default DOM elements returned from _DOM_DISTILLATION_SCRIPT
    if dom_elements is None:
        dom_elements = [
            {
                "tag": "a",
                "role": "link",
                "text": "More information",
                "aria_label": None,
                "placeholder": None,
                "name": None,
                "element_id": None,
                "css_selector": "a[href]",
                "href": "https://www.iana.org/domains/example",
                "value": None,
                "visible": True,
                "enabled": True,
                "input_type": None,
            }
        ]

    async def mock_evaluate(script):
        # Return page text for page-text script, DOM elements for DOM script
        if "TEXT_TAGS" in script or "MAX_CHARS" in script:
            return page_text
        return dom_elements

    page.evaluate = mock_evaluate

    if accessibility:
        tree = {"role": "WebArea", "name": title, "children": []}
        async_snapshot = AsyncMock(return_value=tree)
        page.accessibility = MagicMock()
        page.accessibility.snapshot = async_snapshot
    else:
        # No accessibility attribute
        del page.accessibility

    return page


# ─────────────────────────────────────────────────────────────────────────────
# 1. PageSnapshot contains URL and title
# ─────────────────────────────────────────────────────────────────────────────

class TestPageSnapshotBasics:
    def test_url_and_title_present(self):
        snap = make_snapshot(url="https://example.com", title="Example Domain")
        assert snap.url == "https://example.com"
        assert snap.title == "Example Domain"

    def test_timestamp_auto_set(self):
        before = time.time()
        snap = make_snapshot()
        assert snap.timestamp >= before

    def test_elements_list(self):
        el = make_page_element(text="Sign in", css_selector="button#signin")
        snap = make_snapshot(elements=[el])
        assert len(snap.elements) == 1
        assert snap.elements[0].text == "Sign in"


# ─────────────────────────────────────────────────────────────────────────────
# 2. page_text is populated when available
# ─────────────────────────────────────────────────────────────────────────────

class TestPageText:
    def test_page_text_stored_in_snapshot(self):
        text = "Example Domain\nThis domain is for use in illustrative examples."
        snap = make_snapshot(page_text=text)
        assert snap.page_text == text

    def test_page_text_none_by_default(self):
        snap = make_snapshot()
        assert snap.page_text is None

    def test_inspect_populates_page_text(self):
        """EnvironmentPerceptionEngine.inspect() should populate page_text."""
        engine = EnvironmentPerceptionEngine()
        page = make_mock_page(page_text="Wikipedia\nThe free encyclopedia.")

        async def run():
            return await engine.inspect(page)

        snap = asyncio.run(run())
        assert snap.page_text == "Wikipedia\nThe free encyclopedia."

    def test_inspect_page_text_none_on_js_error(self):
        """If page text JS fails, inspect() still succeeds with page_text=None."""
        engine = EnvironmentPerceptionEngine()
        page = make_mock_page()

        async def bad_evaluate(script):
            if "TEXT_TAGS" in script or "MAX_CHARS" in script:
                raise RuntimeError("JS evaluation error")
            return []

        page.evaluate = bad_evaluate

        async def run():
            return await engine.inspect(page)

        snap = asyncio.run(run())
        assert snap.page_text is None  # Graceful degradation
        assert snap.url is not None


# ─────────────────────────────────────────────────────────────────────────────
# 3. Interactive elements receive semantic element references
# ─────────────────────────────────────────────────────────────────────────────

class TestElementRefs:
    def test_refs_assigned(self):
        els = [
            make_page_element(text="Search", css_selector="input#search"),
            make_page_element(text="Submit", css_selector="button#submit", tag="button"),
        ]
        observed = _assign_element_refs(els)
        assert observed[0].ref == "elem_001"
        assert observed[1].ref == "elem_002"

    def test_ref_format(self):
        """Refs must follow the elem_NNN zero-padded format."""
        els = [make_page_element() for _ in range(5)]
        observed = _assign_element_refs(els)
        for i, ob in enumerate(observed, start=1):
            assert ob.ref == f"elem_{i:03d}"

    def test_single_element(self):
        observed = _assign_element_refs([make_page_element()])
        assert observed[0].ref == "elem_001"

    def test_empty_list(self):
        assert _assign_element_refs([]) == []

    def test_more_than_999_elements_dont_crash(self):
        """Refs extend beyond elem_999 without crashing (elem_1000, etc.)."""
        els = [make_page_element(css_selector=f"a#{i}") for i in range(1, 1001)]
        observed = _assign_element_refs(els)
        assert observed[999].ref == "elem_1000"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Element references are unique within an observation
# ─────────────────────────────────────────────────────────────────────────────

class TestElementRefUniqueness:
    def test_refs_unique(self):
        els = [make_page_element(css_selector=f"a#{i}") for i in range(10)]
        observed = _assign_element_refs(els)
        refs = [ob.ref for ob in observed]
        assert len(refs) == len(set(refs)), "Refs must be unique within an observation"

    def test_different_observations_can_reuse_refs(self):
        """
        Refs are per-observation only. Two separate observations may assign
        elem_001 to different elements — this is by design.
        """
        snap_a = [make_page_element(text="Login", css_selector="#login")]
        snap_b = [make_page_element(text="Search", css_selector="#search")]
        obs_a = _assign_element_refs(snap_a)
        obs_b = _assign_element_refs(snap_b)
        # Both have elem_001 — different elements, same ref by design
        assert obs_a[0].ref == "elem_001"
        assert obs_b[0].ref == "elem_001"
        assert obs_a[0].css_selector != obs_b[0].css_selector


# ─────────────────────────────────────────────────────────────────────────────
# 5. Reference maps to underlying observed element / CSS selector
# ─────────────────────────────────────────────────────────────────────────────

class TestElementRefMapping:
    def test_ref_maps_to_css_selector(self):
        el = make_page_element(text="More information", css_selector="a[href]")
        observed = _assign_element_refs([el])
        ob = observed[0]
        assert ob.ref == "elem_001"
        assert ob.css_selector == "a[href]"  # execution layer still has selector

    def test_ref_lookup_by_ref_string(self):
        els = [
            make_page_element(text="Login", css_selector="#login"),
            make_page_element(text="Search", css_selector="#search"),
            make_page_element(text="Help", css_selector="#help"),
        ]
        observed = _assign_element_refs(els)
        ref_map = {ob.ref: ob for ob in observed}
        target = ref_map.get("elem_002")
        assert target is not None
        assert target.css_selector == "#search"

    def test_from_page_element_round_trip(self):
        el = PageElement(
            tag="input",
            role="searchbox",
            text=None,
            aria_label="Search Wikipedia",
            placeholder="Search…",
            name="q",
            element_id="searchInput",
            css_selector="#searchInput",
            href=None,
            value="",
            visible=True,
            enabled=True,
            input_type="search",
        )
        ob = ObservedElement.from_page_element("elem_007", el)
        assert ob.ref == "elem_007"
        assert ob.tag == "input"
        assert ob.role == "searchbox"
        assert ob.aria_label == "Search Wikipedia"
        assert ob.placeholder == "Search…"
        assert ob.name == "q"
        assert ob.element_id == "searchInput"
        assert ob.css_selector == "#searchInput"
        assert ob.input_type == "search"
        assert ob.visible is True
        assert ob.enabled is True


# ─────────────────────────────────────────────────────────────────────────────
# 6. Accessibility information remains available
# ─────────────────────────────────────────────────────────────────────────────

class TestAccessibility:
    def test_accessibility_tree_stored_in_snapshot(self):
        tree = {"role": "WebArea", "name": "Example Domain", "children": [
            {"role": "link", "name": "More information"}
        ]}
        snap = make_snapshot(accessibility_tree=tree)
        assert snap.accessibility_tree is not None
        assert snap.accessibility_tree["role"] == "WebArea"

    def test_accessibility_captured_by_inspect(self):
        """inspect() should populate accessibility_tree when page.accessibility exists."""
        engine = EnvironmentPerceptionEngine(capture_accessibility=True)
        page = make_mock_page(accessibility=True)

        async def run():
            return await engine.inspect(page)

        snap = asyncio.run(run())
        assert snap.accessibility_tree is not None

    def test_accessibility_none_when_not_available(self):
        engine = EnvironmentPerceptionEngine(capture_accessibility=True)
        page = make_mock_page(accessibility=False)

        async def run():
            return await engine.inspect(page)

        snap = asyncio.run(run())
        assert snap.accessibility_tree is None

    def test_accessibility_disabled_by_flag(self):
        engine = EnvironmentPerceptionEngine(capture_accessibility=False)
        page = make_mock_page(accessibility=True)

        async def run():
            return await engine.inspect(page)

        snap = asyncio.run(run())
        assert snap.accessibility_tree is None


# ─────────────────────────────────────────────────────────────────────────────
# 7. Multiple tabs / pages can be represented via TabInfo
# ─────────────────────────────────────────────────────────────────────────────

class TestTabInfo:
    def test_tab_info_minimal(self):
        tab = TabInfo(tab_ref="tab_001", url="https://example.com", is_active=True, index=0)
        assert tab.tab_ref == "tab_001"
        assert tab.is_active is True

    def test_tab_info_to_dict(self):
        tab = TabInfo(tab_ref="tab_002", url="https://github.com", title="GitHub", is_active=False, index=1)
        d = tab.to_dict()
        assert d["tab_ref"] == "tab_002"
        assert d["is_active"] is False

    def test_multiple_tabs_in_observation(self):
        tabs = [
            TabInfo(tab_ref="tab_001", url="https://example.com", title="Example", is_active=True, index=0),
            TabInfo(tab_ref="tab_002", url="https://github.com", title="GitHub", is_active=False, index=1),
        ]
        obs = BrowserObservation(
            session_id="s_1",
            task_id="t_1",
            tabs=tabs,
        )
        assert len(obs.tabs) == 2

    def test_collect_tabs_no_context(self):
        """With no context, _collect_tabs returns a single TabInfo for the active page."""
        page = make_mock_page()

        async def run():
            return await _collect_tabs(page, context=None)

        tabs = asyncio.run(run())
        assert len(tabs) == 1
        assert tabs[0].is_active is True
        assert tabs[0].tab_ref == "tab_001"

    def test_collect_tabs_with_context(self):
        """With a context that has multiple pages, _collect_tabs returns all."""
        page_a = make_mock_page(url="https://example.com", title="Example")
        page_b = make_mock_page(url="https://github.com", title="GitHub")
        context = MagicMock()
        context.pages = [page_a, page_b]

        async def run():
            return await _collect_tabs(page_a, context=context)

        tabs = asyncio.run(run())
        assert len(tabs) == 2
        assert tabs[0].is_active is True   # page_a is active
        assert tabs[1].is_active is False  # page_b is not
        assert tabs[0].tab_ref == "tab_001"
        assert tabs[1].tab_ref == "tab_002"


# ─────────────────────────────────────────────────────────────────────────────
# 8. Active page information is represented correctly
# ─────────────────────────────────────────────────────────────────────────────

class TestActiveTab:
    def test_active_tab_flag(self):
        tabs = [
            TabInfo(tab_ref="tab_001", url="https://a.com", is_active=False, index=0),
            TabInfo(tab_ref="tab_002", url="https://b.com", is_active=True, index=1),
        ]
        obs = BrowserObservation(session_id="s_1", task_id="t_1", tabs=tabs)
        active = [t for t in obs.tabs if t.is_active]
        assert len(active) == 1
        assert active[0].tab_ref == "tab_002"

    def test_active_tab_description_has_marker(self):
        tab = TabInfo(tab_ref="tab_001", url="https://example.com", title="Example", is_active=True, index=0)
        desc = tab.to_agent_description()
        assert "[ACTIVE]" in desc

    def test_inactive_tab_description_no_marker(self):
        tab = TabInfo(tab_ref="tab_002", url="https://github.com", title="GitHub", is_active=False, index=1)
        desc = tab.to_agent_description()
        assert "[ACTIVE]" not in desc


# ─────────────────────────────────────────────────────────────────────────────
# 9. Large page text is reasonably bounded
# ─────────────────────────────────────────────────────────────────────────────

class TestPageTextBounding:
    def test_page_text_bounded_in_snapshot(self):
        huge_text = "A" * 10_000
        snap = make_snapshot(page_text=huge_text)
        # PageSnapshot itself stores whatever is passed; bounding happens in observe()
        assert len(snap.page_text) == 10_000

    def test_page_text_bounded_in_observe(self):
        """observe() must bound page_text_summary to PAGE_TEXT_MAX_CHARS."""
        engine = EnvironmentPerceptionEngine()
        huge_text = "X" * 10_000
        page = make_mock_page(page_text=huge_text)

        async def run():
            return await engine.observe(page, session_id="s_1", task_id="t_1")

        obs = asyncio.run(run())
        assert obs.page_text_summary is not None
        assert len(obs.page_text_summary) <= PAGE_TEXT_MAX_CHARS

    def test_short_page_text_not_truncated(self):
        engine = EnvironmentPerceptionEngine()
        short_text = "Wikipedia\nThe free encyclopedia."
        page = make_mock_page(page_text=short_text)

        async def run():
            return await engine.observe(page, session_id="s_1", task_id="t_1")

        obs = asyncio.run(run())
        assert obs.page_text_summary == short_text

    def test_page_text_max_chars_constant(self):
        assert PAGE_TEXT_MAX_CHARS > 0
        assert PAGE_TEXT_MAX_CHARS <= 5_000  # Sanity: not excessively large


# ─────────────────────────────────────────────────────────────────────────────
# 10. Existing PageSnapshot / PageElement models do not regress
# ─────────────────────────────────────────────────────────────────────────────

class TestExistingModelsRegression:
    def test_page_element_semantic_description(self):
        el = PageElement(tag="button", text="Search", css_selector="button#search")
        desc = el.get_semantic_description()
        assert "button" in desc
        assert "Search" in desc

    def test_page_snapshot_filter_interactive(self):
        visible = make_page_element(visible=True, enabled=True, css_selector="#v")
        hidden = make_page_element(visible=False, enabled=True, css_selector="#h")
        snap = make_snapshot(elements=[visible, hidden])
        interactive = snap.filter_interactive()
        assert len(interactive) == 1
        assert interactive[0].css_selector == "#v"

    def test_page_snapshot_find_element_by_selector(self):
        el = make_page_element(css_selector="#unique")
        snap = make_snapshot(elements=[el])
        found = snap.find_element_by_selector("#unique")
        assert found is not None
        assert found.css_selector == "#unique"

    def test_page_snapshot_compact_summary(self):
        el = make_page_element(text="More information", css_selector="a[href]")
        snap = make_snapshot(elements=[el])
        summary = snap.to_compact_summary()
        assert "More information" in summary
        assert "example.com" in summary

    def test_inspect_still_returns_page_snapshot(self):
        engine = EnvironmentPerceptionEngine()
        page = make_mock_page()

        async def run():
            return await engine.inspect(page)

        snap = asyncio.run(run())
        assert isinstance(snap, PageSnapshot)
        assert snap.url == "https://example.com"


# ─────────────────────────────────────────────────────────────────────────────
# 11. ObservedElement.from_page_element round-trips all fields
# ─────────────────────────────────────────────────────────────────────────────

class TestObservedElementRoundTrip:
    def test_all_fields_copied(self):
        el = PageElement(
            tag="a",
            role="link",
            text="More information about this page",
            aria_label="Info link",
            placeholder=None,
            name="info",
            element_id="info-link",
            css_selector="#info-link",
            href="https://example.com/info",
            value=None,
            visible=True,
            enabled=True,
            input_type=None,
        )
        ob = ObservedElement.from_page_element("elem_042", el)
        assert ob.ref == "elem_042"
        assert ob.tag == "a"
        assert ob.role == "link"
        assert ob.text == "More information about this page"
        assert ob.aria_label == "Info link"
        assert ob.name == "info"
        assert ob.element_id == "info-link"
        assert ob.css_selector == "#info-link"
        assert ob.href == "https://example.com/info"
        assert ob.visible is True
        assert ob.enabled is True

    def test_long_text_truncated_at_80(self):
        long_text = "A" * 200
        el = make_page_element(text=long_text, css_selector="#t")
        ob = ObservedElement.from_page_element("elem_001", el)
        assert len(ob.text) == 80

    def test_to_dict_includes_ref(self):
        el = make_page_element()
        ob = ObservedElement.from_page_element("elem_005", el)
        d = ob.to_dict()
        assert d["ref"] == "elem_005"
        assert "css_selector" in d


# ─────────────────────────────────────────────────────────────────────────────
# 12. ObservedElement.to_agent_description compact and human-readable
# ─────────────────────────────────────────────────────────────────────────────

class TestObservedElementDescription:
    def test_link_description(self):
        el = make_page_element(tag="a", role="link", text="More information", css_selector="a[href]", href="https://example.com/more")
        ob = ObservedElement.from_page_element("elem_001", el)
        desc = ob.to_agent_description()
        assert "elem_001" in desc
        assert "link" in desc
        assert "More information" in desc
        assert "href=" in desc

    def test_disabled_element_marked(self):
        el = make_page_element(text="Submit", css_selector="button#sub", enabled=False)
        ob = ObservedElement.from_page_element("elem_002", el)
        desc = ob.to_agent_description()
        assert "(disabled)" in desc

    def test_input_type_shown(self):
        el = PageElement(tag="input", input_type="search", css_selector="#q", text=None, placeholder="Search…")
        ob = ObservedElement.from_page_element("elem_003", el)
        desc = ob.to_agent_description()
        assert "type=search" in desc

    def test_css_selector_not_in_description(self):
        """The agent description must NOT expose raw CSS selectors."""
        el = make_page_element(css_selector="div.some-complex-class > button:nth-child(3)")
        ob = ObservedElement.from_page_element("elem_004", el)
        desc = ob.to_agent_description()
        assert "nth-child" not in desc
        assert "some-complex-class" not in desc


# ─────────────────────────────────────────────────────────────────────────────
# 15. BrowserObservation.to_agent_summary format
# ─────────────────────────────────────────────────────────────────────────────

class TestBrowserObservationSummary:
    def make_full_observation(self) -> BrowserObservation:
        el = make_page_element(text="More information", css_selector="a[href]")
        ob = ObservedElement.from_page_element("elem_001", el)
        tab = TabInfo(tab_ref="tab_001", url="https://example.com", title="Example Domain", is_active=True, index=0)
        return BrowserObservation(
            session_id="s_1",
            task_id="t_1",
            current_url="https://example.com",
            page_title="Example Domain",
            page_text_summary="Example Domain\nThis domain is for use in illustrative examples.",
            observed_elements=[ob],
            tabs=[tab],
        )

    def test_summary_contains_url(self):
        obs = self.make_full_observation()
        summary = obs.to_agent_summary()
        assert "https://example.com" in summary

    def test_summary_contains_title(self):
        obs = self.make_full_observation()
        summary = obs.to_agent_summary()
        assert "Example Domain" in summary

    def test_summary_contains_page_text(self):
        obs = self.make_full_observation()
        summary = obs.to_agent_summary()
        assert "Page Text:" in summary
        assert "illustrative examples" in summary

    def test_summary_contains_elements(self):
        obs = self.make_full_observation()
        summary = obs.to_agent_summary()
        assert "elem_001" in summary
        assert "More information" in summary

    def test_summary_contains_tabs(self):
        obs = self.make_full_observation()
        summary = obs.to_agent_summary()
        assert "tab_001" in summary
        assert "[ACTIVE]" in summary

    def test_summary_recent_action_none(self):
        obs = self.make_full_observation()
        summary = obs.to_agent_summary()
        assert "Recent Action: none" in summary

    def test_summary_recent_action_present(self):
        obs = self.make_full_observation()
        obs = obs.model_copy(update={
            "recent_action": RecentActionRecord(
                action_type=BrowserActionType.CLICK,
                element_ref="elem_001",
                success=True,
                duration_ms=120.5,
            )
        })
        summary = obs.to_agent_summary()
        assert "click" in summary.lower()
        assert "elem_001" in summary
        assert "success" in summary

    def test_summary_recent_action_failure(self):
        obs = self.make_full_observation()
        obs = obs.model_copy(update={
            "recent_action": RecentActionRecord(
                action_type=BrowserActionType.FILL,
                element_ref="elem_002",
                success=False,
                error="Element not found",
                duration_ms=50.0,
            )
        })
        summary = obs.to_agent_summary()
        assert "FAILED" in summary
        assert "Element not found" in summary

    def test_summary_no_elements(self):
        obs = BrowserObservation(
            session_id="s_1",
            task_id="t_1",
            current_url="https://example.com",
            page_title="Example",
        )
        summary = obs.to_agent_summary()
        assert "none detected" in summary


# ─────────────────────────────────────────────────────────────────────────────
# 16. BrowserObservation.recent_action carries RecentActionRecord
# ─────────────────────────────────────────────────────────────────────────────

class TestRecentActionRecord:
    def test_recent_action_navigate(self):
        ra = RecentActionRecord(
            action_type=BrowserActionType.NAVIGATE,
            url="https://en.wikipedia.org/wiki/Main_Page",
            new_tab=True,
            success=True,
            duration_ms=850.0,
        )
        obs = BrowserObservation(session_id="s_1", task_id="t_1", recent_action=ra)
        assert obs.recent_action is not None
        assert obs.recent_action.action_type == BrowserActionType.NAVIGATE
        assert obs.recent_action.success is True

    def test_recent_action_click_failed(self):
        ra = RecentActionRecord(
            action_type=BrowserActionType.CLICK,
            element_ref="elem_003",
            success=False,
            error="Timeout waiting for element",
        )
        obs = BrowserObservation(session_id="s_1", task_id="t_1", recent_action=ra)
        assert obs.recent_action.success is False
        assert obs.recent_action.error == "Timeout waiting for element"
        assert obs.recent_action.element_ref == "elem_003"

    def test_recent_action_none_by_default(self):
        obs = BrowserObservation(session_id="s_1", task_id="t_1")
        assert obs.recent_action is None

    def test_recent_action_required_fields(self):
        with pytest.raises(ValidationError):
            RecentActionRecord()  # action_type is required


# ─────────────────────────────────────────────────────────────────────────────
# 17. BrowserObservation Phase 1 fields backward-compatible
# ─────────────────────────────────────────────────────────────────────────────

class TestBrowserObservationBackwardCompat:
    def test_phase1_fields_all_still_work(self):
        """Phase 1 test: constructing BrowserObservation without Phase 2 fields."""
        obs = BrowserObservation(
            session_id="s_abc",
            task_id="t_001",
            current_url="https://example.com",
            page_title="Example Domain",
            page_text_summary="Example content",
            open_tab_count=1,
            active_tab_index=0,
            previous_action="navigate to https://example.com",
            previous_action_success=True,
            actions_taken_count=1,
        )
        # Phase 2 fields default to empty/None
        assert obs.observed_elements == []
        assert obs.tabs == []
        assert obs.recent_action is None

    def test_page_snapshot_still_embeds(self):
        snap = make_snapshot()
        obs = BrowserObservation(
            session_id="s_1",
            task_id="t_1",
            page_snapshot=snap,
        )
        assert obs.page_snapshot is not None
        assert obs.page_snapshot.url == "https://example.com"


# ─────────────────────────────────────────────────────────────────────────────
# 18. observe() async method builds BrowserObservation from mock page
# ─────────────────────────────────────────────────────────────────────────────

class TestObserveMethod:
    def test_observe_returns_browser_observation(self):
        engine = EnvironmentPerceptionEngine()
        page = make_mock_page()

        async def run():
            return await engine.observe(page, session_id="s_1", task_id="t_1")

        obs = asyncio.run(run())
        assert isinstance(obs, BrowserObservation)
        assert obs.session_id == "s_1"
        assert obs.task_id == "t_1"

    def test_observe_populates_url_and_title(self):
        engine = EnvironmentPerceptionEngine()
        page = make_mock_page(url="https://en.wikipedia.org/wiki/Main_Page", title="Wikipedia")

        async def run():
            return await engine.observe(page, session_id="s_1", task_id="t_1")

        obs = asyncio.run(run())
        assert obs.current_url == "https://en.wikipedia.org/wiki/Main_Page"
        assert obs.page_title == "Wikipedia"

    def test_observe_populates_observed_elements(self):
        engine = EnvironmentPerceptionEngine()
        page = make_mock_page()

        async def run():
            return await engine.observe(page, session_id="s_1", task_id="t_1")

        obs = asyncio.run(run())
        assert len(obs.observed_elements) == 1
        assert obs.observed_elements[0].ref == "elem_001"
        assert obs.observed_elements[0].text == "More information"

    def test_observe_populates_tabs(self):
        engine = EnvironmentPerceptionEngine()
        page = make_mock_page()

        async def run():
            return await engine.observe(page, session_id="s_1", task_id="t_1")

        obs = asyncio.run(run())
        assert len(obs.tabs) >= 1
        assert obs.tabs[0].is_active is True

    def test_observe_populates_page_text_summary(self):
        engine = EnvironmentPerceptionEngine()
        page = make_mock_page(page_text="Example Domain\nThis is illustrative.")

        async def run():
            return await engine.observe(page, session_id="s_1", task_id="t_1")

        obs = asyncio.run(run())
        assert obs.page_text_summary == "Example Domain\nThis is illustrative."

    def test_observe_carries_recent_action(self):
        engine = EnvironmentPerceptionEngine()
        page = make_mock_page()
        ra = RecentActionRecord(
            action_type=BrowserActionType.NAVIGATE,
            url="https://example.com",
            success=True,
        )

        async def run():
            return await engine.observe(page, session_id="s_1", task_id="t_1", recent_action=ra)

        obs = asyncio.run(run())
        assert obs.recent_action is not None
        assert obs.recent_action.action_type == BrowserActionType.NAVIGATE

    def test_observe_carries_actions_count(self):
        engine = EnvironmentPerceptionEngine()
        page = make_mock_page()

        async def run():
            return await engine.observe(page, session_id="s_1", task_id="t_1", actions_taken_count=3)

        obs = asyncio.run(run())
        assert obs.actions_taken_count == 3

    def test_observe_embeds_page_snapshot(self):
        engine = EnvironmentPerceptionEngine()
        page = make_mock_page()

        async def run():
            return await engine.observe(page, session_id="s_1", task_id="t_1")

        obs = asyncio.run(run())
        assert obs.page_snapshot is not None
        assert isinstance(obs.page_snapshot, PageSnapshot)


# ─────────────────────────────────────────────────────────────────────────────
# 19. observe() handles missing page context gracefully
# ─────────────────────────────────────────────────────────────────────────────

class TestObserveErrorHandling:
    def test_observe_raises_on_none_page(self):
        engine = EnvironmentPerceptionEngine()

        async def run():
            return await engine.observe(None, session_id="s_1", task_id="t_1")  # type: ignore

        with pytest.raises(ValueError, match="Cannot observe None"):
            asyncio.run(run())

    def test_observe_handles_dom_evaluation_failure(self):
        """observe() should succeed even if DOM distillation JS fails."""
        engine = EnvironmentPerceptionEngine()
        page = make_mock_page()

        async def bad_evaluate(script):
            raise RuntimeError("Page crashed")

        page.evaluate = bad_evaluate

        async def run():
            return await engine.observe(page, session_id="s_1", task_id="t_1")

        obs = asyncio.run(run())
        # Should succeed with empty elements and no page_text
        assert isinstance(obs, BrowserObservation)
        assert obs.observed_elements == []
        assert obs.page_text_summary is None


# ─────────────────────────────────────────────────────────────────────────────
# 20. observe() with multi-page context produces multiple TabInfo entries
# ─────────────────────────────────────────────────────────────────────────────

class TestObserveMultiTab:
    def test_observe_multi_tab_context(self):
        engine = EnvironmentPerceptionEngine()
        page_a = make_mock_page(url="https://example.com", title="Example")
        page_b = make_mock_page(url="https://github.com", title="GitHub")
        context = MagicMock()
        context.pages = [page_a, page_b]

        async def run():
            return await engine.observe(
                page_a, session_id="s_1", task_id="t_1", context=context
            )

        obs = asyncio.run(run())
        assert obs.open_tab_count == 2
        assert len(obs.tabs) == 2
        active_tabs = [t for t in obs.tabs if t.is_active]
        assert len(active_tabs) == 1
        assert active_tabs[0].url == "https://example.com"
