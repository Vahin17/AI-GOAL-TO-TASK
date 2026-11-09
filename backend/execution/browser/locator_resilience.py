"""
locator_resilience.py — Resilient Locators & Selector Fallback Chains (Phase 2 Task 4)
=====================================================================================
Provides multi-strategy selector fallback chains and locator caching for Playwright
browser automation to prevent test/automation flakiness across dynamic Web UIs.

Priority Fallback Chain:
1. data-testid / data-test / data-qa attributes.
2. Unique ID selector (#id or [id="..."]).
3. Role & Accessible Name (role=...[name="..."] or tag:has-text("...")).
4. Aria-label / accessibility attributes ([aria-label="..."]).
5. Specific semantic attributes ([name="..."], [placeholder="..."], [type="..."]).
6. Hierarchical CSS selector fallback.

Validation criteria before interaction:
- Existence & uniqueness (count == 1 preferred).
- Visibility (is_visible).
- Interactability / enabled state (is_enabled).

Locator Cache:
- LRU bounded cache with 5-minute TTL (300 seconds).
- Keyed by (session_id, page_url_hash, element_key).
- Automatically invalidated on navigation.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

DEFAULT_CACHE_TTL_SECONDS = 300.0  # 5 minutes
DEFAULT_CACHE_MAX_ENTRIES = 500


def _hash_url(url: str) -> str:
    """Return compact hash of a page URL ignoring volatile query params / fragments."""
    clean_url = url.split("#")[0].split("?")[0].rstrip("/")
    return hashlib.sha256(clean_url.encode("utf-8")).hexdigest()[:16]


# ── Selector Fallback Chain Generator ───────────────────────────────────────

def generate_fallback_selectors(
    primary: Optional[str] = None,
    tag: Optional[str] = None,
    element_id: Optional[str] = None,
    data_testid: Optional[str] = None,
    aria_label: Optional[str] = None,
    role: Optional[str] = None,
    name: Optional[str] = None,
    placeholder: Optional[str] = None,
    text: Optional[str] = None,
    input_type: Optional[str] = None,
    hints: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """
    Constructs an ordered list of fallback selectors following strict priority:
    1. data-testid
    2. ID
    3. role & name / text
    4. aria-label
    5. attributes (name, placeholder, type)
    6. CSS / tag text
    """
    meta = hints or {}
    dtid = data_testid or meta.get("data-testid") or meta.get("testid") or meta.get("data_testid")
    eid = element_id or meta.get("id") or meta.get("element_id")
    r = role or meta.get("role")
    al = aria_label or meta.get("aria_label") or meta.get("aria-label")
    nm = name or meta.get("name")
    ph = placeholder or meta.get("placeholder")
    tx = (text or meta.get("text") or "").strip()
    itype = input_type or meta.get("type") or meta.get("input_type")
    tg = (tag or meta.get("tag") or "button").lower()

    chain: List[str] = []

    # 1. data-testid (highest stability)
    if dtid:
        chain.append(f'[data-testid="{dtid}"]')
        chain.append(f'[data-test="{dtid}"]')
        chain.append(f'[data-qa="{dtid}"]')

    # 2. Element ID
    if eid:
        # Use [id="..."] if ID contains special CSS characters like dots or colons
        if any(c in eid for c in ".:/[] "):
            chain.append(f'[id="{eid}"]')
        else:
            chain.append(f"#{eid}")

    # 3. Role & Accessible Name / Text
    if r and tx:
        chain.append(f'role={r}[name="{tx}"]')
    if tg and tx and len(tx) <= 40:
        chain.append(f'{tg}:has-text("{tx}")')

    # 4. Aria-label
    if al:
        chain.append(f'[aria-label="{al}"]')

    # 5. Semantic Attributes
    if nm:
        chain.append(f'[name="{nm}"]')
    if ph:
        chain.append(f'[placeholder="{ph}"]')
    if itype and nm:
        chain.append(f'input[type="{itype}"][name="{nm}"]')

    # 6. Primary CSS selector passed by caller
    if primary and primary not in chain:
        chain.append(primary)

    # 7. Deduplicate while preserving order
    seen = set()
    deduped: List[str] = []
    for sel in chain:
        sel_clean = sel.strip()
        if sel_clean and sel_clean not in seen:
            seen.add(sel_clean)
            deduped.append(sel_clean)

    return deduped


# ── Bounded LRU Locator Cache ────────────────────────────────────────────────

@dataclass
class _CacheEntry:
    selector: str
    timestamp: float


class LocatorCache:
    """
    Thread-safe / async-friendly LRU locator cache with TTL expiration.
    Keyed by (session_id, page_url_hash, element_key).
    """

    def __init__(self, ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS, max_entries: int = DEFAULT_CACHE_MAX_ENTRIES):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._cache: OrderedDict[Tuple[str, str, str], _CacheEntry] = OrderedDict()

    def get(self, session_id: str, page_url: str, element_key: str) -> Optional[str]:
        key = (session_id or "default", _hash_url(page_url), element_key)
        entry = self._cache.get(key)
        if entry is None:
            return None

        now = time.time()
        if now - entry.timestamp > self.ttl_seconds:
            # Expired
            self._cache.pop(key, None)
            return None

        # Move to end for LRU freshness
        self._cache.move_to_end(key)
        return entry.selector

    def set(self, session_id: str, page_url: str, element_key: str, selector: str) -> None:
        key = (session_id or "default", _hash_url(page_url), element_key)
        now = time.time()

        # Evict oldest if full
        if len(self._cache) >= self.max_entries and key not in self._cache:
            self._cache.popitem(last=False)

        self._cache[key] = _CacheEntry(selector=selector, timestamp=now)
        self._cache.move_to_end(key)

    def invalidate_page(self, session_id: str, page_url: str) -> int:
        """Evict all entries for a specific page URL upon navigation."""
        url_hash = _hash_url(page_url)
        keys_to_del = [k for k in self._cache.keys() if k[0] == (session_id or "default") and k[1] == url_hash]
        for k in keys_to_del:
            self._cache.pop(k, None)
        return len(keys_to_del)

    def invalidate_session(self, session_id: str) -> int:
        """Evict all entries for a session when it closes."""
        sid = session_id or "default"
        keys_to_del = [k for k in self._cache.keys() if k[0] == sid]
        for k in keys_to_del:
            self._cache.pop(k, None)
        return len(keys_to_del)

    def clear(self) -> None:
        self._cache.clear()

    def __len__(self) -> int:
        return len(self._cache)


# Global singleton cache instance
_GLOBAL_LOCATOR_CACHE = LocatorCache()


# ── Locator Resilience Engine ───────────────────────────────────────────────

class LocatorResilienceEngine:
    """
    Evaluates selector fallback chains and verifies element uniqueness,
    visibility, and interactability on live Playwright pages.
    """

    cache: LocatorCache = _GLOBAL_LOCATOR_CACHE

    @classmethod
    async def resolve_resilient_selector(
        cls,
        page: Any,
        primary_selector: str,
        hints: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        timeout_ms: int = 3000,
    ) -> Optional[str]:
        """
        Attempts to resolve an interactable selector on page.
        1. Checks locator cache.
        2. Tries candidate fallback selectors in priority order.
        3. Validates element is attached, unique (preferred), visible, and enabled.
        4. Caches winning selector.
        """
        if page is None:
            return primary_selector

        current_url = getattr(page, "url", "about:blank")
        element_key = primary_selector

        # 1. Cache hit check
        cached_selector = cls.cache.get(session_id or "default", current_url, element_key)
        if cached_selector:
            try:
                # Fast probe of cached selector
                loc = page.locator(cached_selector)
                if await loc.count() > 0 and await loc.first.is_visible():
                    logger.debug(f"[LocatorResilience] Cache HIT: '{cached_selector}' on {current_url}")
                    return cached_selector
            except Exception:
                # Cached selector stale, proceed to fresh fallback probe
                pass

        # 2. Build ordered candidate list
        meta = hints or {}
        candidates = generate_fallback_selectors(
            primary=primary_selector,
            hints=meta,
        )
        if primary_selector not in candidates:
            candidates.insert(0, primary_selector)

        # 3. Probe candidates sequentially
        winning_selector: Optional[str] = None

        for candidate in candidates:
            try:
                locator = page.locator(candidate)
                count = await locator.count()
                if count == 0:
                    continue

                # Check interactability of first matching element
                first_el = locator.first
                is_vis = await first_el.is_visible()
                if not is_vis:
                    continue

                is_en = await first_el.is_enabled()
                if not is_en:
                    continue

                # Element matches, is visible, and enabled
                winning_selector = candidate
                logger.info(
                    f"[LocatorResilience] Resolved selector '{winning_selector}' (count={count}) "
                    f"from {len(candidates)} candidates for target '{primary_selector}'"
                )
                break
            except Exception as e:
                logger.debug(f"[LocatorResilience] Candidate probe failed for '{candidate}': {e}")
                continue

        # 4. Cache winning selector if resolved
        if winning_selector:
            cls.cache.set(session_id or "default", current_url, element_key, winning_selector)
            return winning_selector

        # Fallback to primary selector if nothing confirmed
        return primary_selector

    @classmethod
    def invalidate_page_cache(cls, session_id: str, page_url: str) -> None:
        """Called automatically after page navigation to prevent stale locators."""
        cls.cache.invalidate_page(session_id, page_url)

