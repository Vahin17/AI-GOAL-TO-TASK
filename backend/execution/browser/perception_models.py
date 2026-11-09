"""
perception_models.py — Strongly-typed models for environment perception
========================================================================
Defines token-efficient PageElement, PageSnapshot, ObservedElement, and
TabInfo models representing the interactive and semantic state of a live
browser page.

Phase 2 additions:
    ObservedElement  — Wraps PageElement with a stable per-observation
                       semantic reference (e.g. "elem_001") that the
                       future BrowserAgent uses to identify elements
                       without reasoning over raw CSS selectors.
    TabInfo          — Represents a single open browser tab/page.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class PageElement(BaseModel):
    """
    A single interactive or semantic element detected on an active page.
    """
    tag: str
    role: Optional[str] = None
    text: Optional[str] = None
    aria_label: Optional[str] = None
    placeholder: Optional[str] = None
    name: Optional[str] = None
    element_id: Optional[str] = None
    css_selector: str
    href: Optional[str] = None
    value: Optional[str] = None
    visible: bool = True
    enabled: bool = True
    input_type: Optional[str] = None

    def get_semantic_description(self) -> str:
        """
        Compact description of the element for LLM / heuristic grounding.
        """
        parts = [f"<{self.tag}"]
        if self.element_id:
            parts.append(f"id='{self.element_id}'")
        if self.name:
            parts.append(f"name='{self.name}'")
        if self.input_type:
            parts.append(f"type='{self.input_type}'")
        if self.role:
            parts.append(f"role='{self.role}'")
        if self.placeholder:
            parts.append(f"placeholder='{self.placeholder}'")
        if self.aria_label:
            parts.append(f"aria-label='{self.aria_label}'")
        parts.append(f"selector='{self.css_selector}'>")
        if self.text:
            cleaned_text = self.text.strip().replace("\n", " ")
            if len(cleaned_text) > 40:
                cleaned_text = cleaned_text[:37] + "..."
            parts.append(f" text=\"{cleaned_text}\"")
        return "".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tag": self.tag,
            "role": self.role,
            "text": self.text,
            "aria_label": self.aria_label,
            "placeholder": self.placeholder,
            "name": self.name,
            "element_id": self.element_id,
            "css_selector": self.css_selector,
            "href": self.href,
            "value": self.value,
            "visible": self.visible,
            "enabled": self.enabled,
            "input_type": self.input_type,
        }


class PageSnapshot(BaseModel):
    """
    Structured, token-efficient representation of the current live page state.
    """
    url: str
    title: str
    elements: List[PageElement] = Field(default_factory=list)
    accessibility_tree: Optional[Dict[str, Any]] = None
    page_text: Optional[str] = None
    timestamp: float = Field(default_factory=time.time)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def find_element_by_selector(self, selector: str) -> Optional[PageElement]:
        """Find element matching exact selector."""
        for el in self.elements:
            if el.css_selector == selector:
                return el
        return None

    def filter_interactive(self) -> List[PageElement]:
        """Return only visible and enabled interactive controls."""
        return [el for el in self.elements if el.visible and el.enabled]

    def to_compact_summary(self, max_elements: int = 40) -> str:
        """
        Produces a token-bounded textual representation of the page
        suitable for LLM grounding (< 1,000 tokens).
        """
        lines = [
            f"Page Title: {self.title}",
            f"Page URL: {self.url}",
            f"Detected Interactive Elements ({min(len(self.elements), max_elements)} shown):",
        ]
        for i, el in enumerate(self.elements[:max_elements], 1):
            lines.append(f"  [{i}] {el.get_semantic_description()}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "elements": [el.to_dict() for el in self.elements],
            "accessibility_tree": self.accessibility_tree,
            "page_text": self.page_text,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: ObservedElement — agent-facing element reference
# ─────────────────────────────────────────────────────────────────────────────

class ObservedElement(BaseModel):
    """
    An interactive page element as presented to the future BrowserAgent.

    Wraps PageElement with a stable per-observation semantic reference
    (e.g. "elem_001") so the agent reasons using short stable IDs
    rather than raw CSS selectors.

    IMPORTANT LIFETIME GUARANTEE:
        The ref is stable within ONE observation snapshot only.
        After any browser action the agent must re-observe; refs from a
        prior observation MUST NOT be assumed still valid.

    Selector resolution flow:
        Agent uses ref ("elem_004")
            ↓
        ObservedElement.css_selector (execution detail, kept internally)
            ↓
        PlaywrightBrowserExecutor executes against page

    Fields:
        ref:            Short stable reference within this observation.
                        Format: "elem_NNN" (zero-padded three digits).
        tag:            HTML tag name (a, button, input, …).
        role:           ARIA role or inferred role.
        text:           Visible text (truncated at 80 chars).
        aria_label:     ARIA label attribute.
        placeholder:    Input placeholder text.
        name:           HTML name attribute.
        element_id:     HTML id attribute.
        css_selector:   CSS selector for the execution layer.
                        The agent should reference elements by `ref` only.
        href:           href for link elements.
        value:          Current input value.
        input_type:     Input type (text, password, email, …).
        visible:        Whether the element is visible.
        enabled:        Whether the element is interactive.
    """
    ref: str = Field(..., description="Stable per-observation semantic reference, e.g. 'elem_001'")
    tag: str
    role: Optional[str] = None
    text: Optional[str] = None
    aria_label: Optional[str] = None
    placeholder: Optional[str] = None
    name: Optional[str] = None
    element_id: Optional[str] = None
    css_selector: str = Field(..., description="CSS selector for execution layer use only")
    href: Optional[str] = None
    value: Optional[str] = None
    input_type: Optional[str] = None
    visible: bool = True
    enabled: bool = True

    @classmethod
    def from_page_element(cls, ref: str, el: "PageElement") -> "ObservedElement":
        """Construct from an existing PageElement plus a generated ref."""
        return cls(
            ref=ref,
            tag=el.tag,
            role=el.role,
            text=el.text[:80] if el.text else None,
            aria_label=el.aria_label,
            placeholder=el.placeholder,
            name=el.name,
            element_id=el.element_id,
            css_selector=el.css_selector,
            href=el.href,
            value=el.value,
            input_type=el.input_type,
            visible=el.visible,
            enabled=el.enabled,
        )

    def to_agent_description(self) -> str:
        """
        Compact one-liner suitable for LLM context.
        Example:  'elem_001 [a/link] "More information" href=https://...'
        """
        parts = [f"{self.ref} [{self.tag}/{self.role or 'element'}]"]
        label = self.text or self.aria_label or self.placeholder or self.name or ""
        if label:
            parts.append(f'"{label[:60]}"')
        if self.href:
            parts.append(f"href={self.href[:80]}")
        if self.input_type:
            parts.append(f"type={self.input_type}")
        if not self.enabled:
            parts.append("(disabled)")
        return " ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ref": self.ref,
            "tag": self.tag,
            "role": self.role,
            "text": self.text,
            "aria_label": self.aria_label,
            "placeholder": self.placeholder,
            "name": self.name,
            "element_id": self.element_id,
            "css_selector": self.css_selector,
            "href": self.href,
            "value": self.value,
            "input_type": self.input_type,
            "visible": self.visible,
            "enabled": self.enabled,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: TabInfo — represents an open browser tab/page
# ─────────────────────────────────────────────────────────────────────────────

class TabInfo(BaseModel):
    """
    Represents a single open browser tab/page in the current browser context.

    Fields:
        tab_ref:    Stable per-observation tab reference, e.g. "tab_001".
        url:        Current URL of the tab.
        title:      Page title of the tab.
        is_active:  True if this is the currently focused/active tab.
        index:      Zero-based tab index in the browser context.
    """
    tab_ref: str = Field(..., description="Stable per-observation tab reference, e.g. 'tab_001'")
    url: str = Field(default="")
    title: str = Field(default="")
    is_active: bool = Field(default=False)
    index: int = Field(default=0, ge=0)

    def to_agent_description(self) -> str:
        """Compact one-liner: 'tab_001 [ACTIVE] https://example.com — Example Domain'"""
        active_marker = " [ACTIVE]" if self.is_active else ""
        return f"{self.tab_ref}{active_marker} {self.url} — {self.title}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tab_ref": self.tab_ref,
            "url": self.url,
            "title": self.title,
            "is_active": self.is_active,
            "index": self.index,
        }
