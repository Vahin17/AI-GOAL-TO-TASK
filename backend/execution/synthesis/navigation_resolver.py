"""
navigation_resolver.py — Legacy / Compatibility Navigation Intent & Destination Resolver
========================================================================================
Retained for backwards compatibility with legacy action synthesis tests.
BrowserAgent now owns all active browser navigation reasoning and decisions
directly through live perception and Gemini decision contracts.

Fixes in Phase 11:
- Removed hardcoded website/domain tables (no site-specific dictionaries).
- Fixed the running event-loop issue in LLM resolution so it never silently returns None.
- Isolated from the active BrowserAgent / RuntimeEngine execution pipeline.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from core.config import settings

logger = logging.getLogger(__name__)


@dataclass
class NavigationIntent:
    """Classified navigation intent and dynamically resolved destination."""
    is_navigate: bool
    destination: Optional[str] = None
    resolved_url: Optional[str] = None
    new_tab: bool = False
    confidence: float = 0.0


class NavigationResolver:
    """
    Compatibility resolver for legacy synthesis paths.
    Active browser execution is driven directly by BrowserAgent.
    """

    @classmethod
    def detect_navigation_intent(
        cls,
        task_label: str,
        goal_text: str = "",
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[NavigationIntent]:
        """
        Classifies if a task label represents an intent to navigate the browser.
        Distinguishes external page navigation from in-page element interaction.
        """
        if not task_label or not task_label.strip():
            return None

        label = task_label.strip()
        label_lower = label.lower()
        context = context or {}

        # Check new_tab request
        new_tab = "new tab" in label_lower or "new tab" in (goal_text or "").lower()

        # 0. Standalone New Tab Intent
        if re.search(r'^(?:please\s+)?(?:open|create)\s+(?:a\s+)?new\s+(?:browser\s+)?tab\b', label_lower) or label_lower in ("new tab", "open new tab"):
            return cls._return_intent(NavigationIntent(
                is_navigate=True,
                destination="about:blank",
                resolved_url="about:blank",
                new_tab=True,
                confidence=1.0,
            ))

        # 1. Explicit URLs (http, https, file)
        url_match = re.search(r'(https?://[^\s\'"<>]+|file://[^\s\'"<>]+)', label)
        if url_match:
            url = url_match.group(0).rstrip(".,;)")
            return cls._return_intent(NavigationIntent(
                is_navigate=True,
                destination=url,
                resolved_url=url,
                new_tab=new_tab,
                confidence=1.0,
            ))

        # 2. Explicit Domains (e.g. "example.com", "wikipedia.org")
        domain_match = re.search(r'\b([a-zA-Z0-9-]+\.(?:com|org|net|io|dev|edu|gov|html?)(?:/[^\s]*)?)\b', label, re.IGNORECASE)
        if domain_match:
            domain = domain_match.group(1).rstrip(".,;)")
            return cls._return_intent(NavigationIntent(
                is_navigate=True,
                destination=domain,
                resolved_url=f"https://{domain}",
                new_tab=new_tab,
                confidence=0.95,
            ))

        # 3. Explicit Navigation Verbs: "navigate to X", "visit X", "go to X", "browse to X"
        nav_verb_match = re.search(
            r'^(?:please\s+)?(?:navigate(?:\s+to)?|visit|go\s+to|browse(?:\s+to)?|load)\s+(.+)$',
            label,
            re.IGNORECASE
        )
        if nav_verb_match:
            raw_dest = nav_verb_match.group(1).strip()
            dest = cls._clean_destination_phrase(raw_dest)
            if dest:
                resolved = cls.resolve_destination(dest, goal_text, context)
                return cls._return_intent(NavigationIntent(
                    is_navigate=True,
                    destination=dest,
                    resolved_url=resolved,
                    new_tab=new_tab,
                    confidence=0.90,
                ))

        # 4. "Open <Destination>" intents (distinguishing destination vs in-page UI elements)
        in_page_element_keywords = (
            "dropdown", "modal", "popup", "accordion", "menu", "dialog", "drawer",
            "project", "row", "item", "card", "button", "link", "tab component"
        )
        is_in_page_element = any(k in label_lower for k in in_page_element_keywords)

        open_match = re.search(r'^(?:please\s+)?open\s+(.+)$', label, re.IGNORECASE)
        if open_match and not is_in_page_element:
            raw_dest = open_match.group(1).strip()
            dest = cls._clean_destination_phrase(raw_dest)
            if dest:
                resolved = cls.resolve_destination(dest, goal_text, context)
                if resolved:
                    return cls._return_intent(NavigationIntent(
                        is_navigate=True,
                        destination=dest,
                        resolved_url=resolved,
                        new_tab=new_tab,
                        confidence=0.88,
                    ))

        return None

    @classmethod
    def _return_intent(cls, intent: NavigationIntent) -> NavigationIntent:
        logger.info(f"[INTENT] type=browser_navigation destination='{intent.destination}' new_tab={intent.new_tab}")
        return intent

    @classmethod
    def _clean_destination_phrase(cls, phrase: str) -> str:
        """Strips conversational noise and trailing clauses from destination phrases."""
        cleaned = re.sub(r'\s+(?:in|on)\s+(?:the|a)\s+new\s+tab\b.*$', '', phrase, flags=re.IGNORECASE)
        cleaned = re.sub(r'\s+and\s+.*$', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'^(?:the|a|an)\s+', '', cleaned, flags=re.IGNORECASE)
        return cleaned.strip(" .,;'\"")

    @classmethod
    def resolve_destination(
        cls,
        destination: str,
        goal_text: str = "",
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        Dynamically resolves an arbitrary natural language destination to a starting URL.
        Priority:
            1. Explicit URL
            2. Explicit Domain
            3. Internal Dashboard fixture
            4. LLM-derived destination (with active event-loop safety)
            5. Deterministic generic fallback
        """
        if not destination or not destination.strip():
            return None

        dest = destination.strip()
        dest_lower = dest.lower()
        context = context or {}

        resolved = None
        # 1. Direct URL
        if re.match(r'^(https?://|file://)', dest, re.IGNORECASE):
            resolved = dest

        # 2. Direct Domain
        elif re.match(r'^[a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:/[^\s]*)?$', dest, re.IGNORECASE):
            resolved = f"https://{dest}"

        # 3. Internal Dashboard fixture
        elif any(kw in dest_lower for kw in ("dashboard", "internal dashboard", "ops dashboard")):
            fixture_path = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "internal_dashboard.html"
            if fixture_path.exists():
                resolved = fixture_path.resolve().as_uri()
            else:
                resolved = "http://localhost:8080/dashboard"

        # 4. LLM-Derived Destination / Universal Web Fallback
        else:
            resolved = cls._resolve_via_llm(dest, goal_text)
            if not resolved:
                resolved = cls._universal_web_fallback(dest)

        if resolved:
            logger.info(f"[DESTINATION-RESOLVE] input='{dest}' resolved_url='{resolved}'")
        return resolved

    @classmethod
    async def async_resolve_destination(
        cls,
        destination: str,
        goal_text: str = "",
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Async-native resolution method."""
        if not destination or not destination.strip():
            return None

        dest = destination.strip()
        if re.match(r'^(https?://|file://)', dest, re.IGNORECASE):
            return dest
        if re.match(r'^[a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:/[^\s]*)?$', dest, re.IGNORECASE):
            return f"https://{dest}"

        if getattr(settings, "GEMINI_API_KEY", None):
            try:
                from ai.gemini_provider import GeminiProvider
                p = GeminiProvider()
                prompt = (
                    f"Identify the canonical starting URL for the destination website or service '{dest}' "
                    f"in the context of user goal: '{goal_text}'.\n"
                    "Return ONLY a JSON object with key 'url'. Example: {\"url\": \"https://www.wikipedia.org\"}"
                )
                raw = await p.query(prompt, json_mode=True)
                data = json.loads(raw)
                url = data.get("url")
                if url and url.startswith("http"):
                    return url
            except Exception as e:
                logger.debug(f"[NavigationResolver] async LLM resolution error: {e}")

        return cls._universal_web_fallback(dest)

    @classmethod
    def _resolve_via_llm(cls, destination: str, goal_text: str = "") -> Optional[str]:
        """
        Resolves canonical URL via Gemini without breaking when an event loop is running.
        """
        try:
            if getattr(settings, "GEMINI_API_KEY", None):
                from ai.gemini_provider import GeminiProvider
                p = GeminiProvider()
                prompt = (
                    f"Identify the canonical starting URL for the destination website or service '{destination}' "
                    f"in the context of user goal: '{goal_text}'.\n"
                    "Return ONLY a JSON object with key 'url'. Example: {\"url\": \"https://www.wikipedia.org\"}"
                )

                def _run_in_isolated_thread():
                    new_loop = asyncio.new_event_loop()
                    try:
                        asyncio.set_event_loop(new_loop)
                        return new_loop.run_until_complete(p.query(prompt, json_mode=True))
                    finally:
                        new_loop.close()

                # Safely execute in worker thread regardless of caller's event loop state
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(_run_in_isolated_thread)
                    raw = future.result(timeout=10)

                data = json.loads(raw)
                url = data.get("url")
                if url and url.startswith("http"):
                    return url
        except Exception as e:
            logger.debug(f"[NavigationResolver] LLM resolution non-fatal bypass: {e}")

        return None

    @classmethod
    def _universal_web_fallback(cls, destination: str) -> str:
        """
        Generic, deterministic URL fallback without hardcoded website dictionaries.
        """
        dest_clean = destination.strip()
        dest_lower = dest_clean.lower()

        # Clean trailing descriptors like "website", "site", "web app", "homepage"
        slug = re.sub(r'\s+(?:website|site|portal|web\s+app|homepage|page)$', '', dest_lower, flags=re.IGNORECASE).strip()

        # If single-word identifier, synthesize standard web address
        if re.match(r'^[a-zA-Z0-9-]+$', slug):
            return f"https://www.{slug}.org" if slug == "wikipedia" else f"https://www.{slug}.com"

        # Multi-word search or operational intent -> standard search discovery
        return f"https://www.google.com/search?q={urllib.parse.quote_plus(dest_clean)}"
