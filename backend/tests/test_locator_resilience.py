"""
test_locator_resilience.py — Locator Resilience and Selector Fallback Chain Tests (Phase 2 Task 4)
=================================================================================================
Validates:
1. Priority fallback order (data-testid -> ID -> role & name -> aria-label -> attributes -> CSS).
2. Special character escaping in ID selectors.
3. Deduplication of candidate selectors while preserving priority.
4. Bounded LRU locator cache operation and eviction.
5. 5-minute TTL expiration of cached locators.
6. Automatic invalidation on page navigation.
7. ResilienceEngine fallback probing and interactability verification.
"""

import time
import pytest
from unittest.mock import AsyncMock, MagicMock

from execution.browser.locator_resilience import (
    generate_fallback_selectors,
    LocatorCache,
    LocatorResilienceEngine,
)


# ── Test 1: Selector Fallback Priority Chain ────────────────────────────────

def test_selector_fallback_priority_order():
    hints = {
        "data-testid": "submit-btn",
        "id": "btn-primary",
        "role": "button",
        "text": "Submit Form",
        "aria_label": "Submit this registration form",
        "name": "submit_action",
        "tag": "button",
    }
    primary = "div.container > button.btn"

    candidates = generate_fallback_selectors(primary=primary, hints=hints)

    # Verify priority ordering
    assert candidates[0] == '[data-testid="submit-btn"]'
    assert candidates[1] == '[data-test="submit-btn"]'
    assert candidates[2] == '[data-qa="submit-btn"]'
    assert '#btn-primary' in candidates
    assert candidates.index('#btn-primary') < candidates.index('[aria-label="Submit this registration form"]')
    assert candidates.index('[aria-label="Submit this registration form"]') < candidates.index('[name="submit_action"]')
    assert primary in candidates


def test_special_character_id_handling():
    # IDs with dots or colons must be wrapped in [id="..."]
    candidates = generate_fallback_selectors(element_id="input:username.v1")
    assert '[id="input:username.v1"]' in candidates
    assert '#input:username.v1' not in candidates

    # Clean IDs use clean hash syntax
    clean_candidates = generate_fallback_selectors(element_id="clean_id_123")
    assert '#clean_id_123' in clean_candidates


def test_deduplication_preserves_priority():
    # If primary is already data-testid, don't duplicate it
    primary = '[data-testid="search-box"]'
    candidates = generate_fallback_selectors(primary=primary, data_testid="search-box")
    assert candidates.count('[data-testid="search-box"]') == 1


# ── Test 2: Bounded LRU Locator Cache ────────────────────────────────────────

def test_locator_cache_store_and_retrieve():
    cache = LocatorCache(ttl_seconds=300.0)
    session_id = "sess_test_1"
    url = "https://example.com/checkout"
    element_key = "btn.checkout"

    cache.set(session_id, url, element_key, "#submit-order")
    resolved = cache.get(session_id, url, element_key)
    assert resolved == "#submit-order"

    # Different URL returns None
    assert cache.get(session_id, "https://example.com/cart", element_key) is None
    # Different session returns None
    assert cache.get("sess_other", url, element_key) is None


def test_locator_cache_ttl_expiration():
    # 0.1 second TTL
    cache = LocatorCache(ttl_seconds=0.1)
    session_id = "sess_ttl"
    url = "https://example.com/login"
    element_key = "input.password"

    cache.set(session_id, url, element_key, '[data-testid="pass"]')
    assert cache.get(session_id, url, element_key) == '[data-testid="pass"]'

    time.sleep(0.15)
    # Must expire
    assert cache.get(session_id, url, element_key) is None


def test_locator_cache_invalidation_on_navigation():
    cache = LocatorCache(ttl_seconds=300.0)
    session_id = "sess_nav"
    url1 = "https://example.com/page1"
    url2 = "https://example.com/page2"

    cache.set(session_id, url1, "item1", "#item1")
    cache.set(session_id, url1, "item2", "#item2")
    cache.set(session_id, url2, "item3", "#item3")
    assert len(cache) == 3

    # Navigate away from url1
    evicted = cache.invalidate_page(session_id, url1)
    assert evicted == 2
    assert cache.get(session_id, url1, "item1") is None
    # url2 remains intact
    assert cache.get(session_id, url2, "item3") == "#item3"


def test_locator_cache_lru_eviction():
    cache = LocatorCache(ttl_seconds=300.0, max_entries=3)
    session_id = "sess_lru"
    url = "https://example.com/store"

    cache.set(session_id, url, "key1", "sel1")
    cache.set(session_id, url, "key2", "sel2")
    cache.set(session_id, url, "key3", "sel3")
    assert len(cache) == 3

    # Access key1 to make it most recently used
    _ = cache.get(session_id, url, "key1")

    # Add 4th entry -> key2 should be evicted (oldest)
    cache.set(session_id, url, "key4", "sel4")
    assert len(cache) == 3
    assert cache.get(session_id, url, "key2") is None
    assert cache.get(session_id, url, "key1") == "sel1"
    assert cache.get(session_id, url, "key4") == "sel4"


# ── Test 3: ResilienceEngine Fallback Probing & Verification ──────────────────

@pytest.mark.asyncio
async def test_resilience_engine_selects_interactable_fallback():
    # Setup mock page
    mock_page = MagicMock()
    mock_page.url = "https://example.com/dashboard"

    # Primary selector '#flaky-id' fails (count=0)
    loc_primary = AsyncMock()
    loc_primary.count.return_value = 0

    # Fallback '[data-testid="confirm-btn"]' succeeds and is visible/enabled
    loc_fallback = AsyncMock()
    loc_fallback.count.return_value = 1
    mock_first = AsyncMock()
    mock_first.is_visible.return_value = True
    mock_first.is_enabled.return_value = True
    loc_fallback.first = mock_first

    def locator_side_effect(selector):
        if selector == "#flaky-id":
            return loc_primary
        elif selector == '[data-testid="confirm-btn"]':
            return loc_fallback
        mock_empty = AsyncMock()
        mock_empty.count.return_value = 0
        return mock_empty

    mock_page.locator.side_effect = locator_side_effect

    hints = {"data-testid": "confirm-btn"}
    resolved = await LocatorResilienceEngine.resolve_resilient_selector(
        page=mock_page,
        primary_selector="#flaky-id",
        hints=hints,
        session_id="sess_mock",
    )

    assert resolved == '[data-testid="confirm-btn"]'

    # Second call should hit cache directly
    cached = LocatorResilienceEngine.cache.get("sess_mock", mock_page.url, "#flaky-id")
    assert cached == '[data-testid="confirm-btn"]'

