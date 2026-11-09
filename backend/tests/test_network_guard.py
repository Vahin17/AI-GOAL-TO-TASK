"""
test_network_guard.py — SSRF & Private Network Guard Tests (Phase 2 Task 3)
=============================================================================
Tests all security boundaries of NetworkGuard and Playwright integration:
1. Forbidden schemes (file://, gopher://, ftp://, dict://).
2. Private IPv4 ranges (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16).
3. Loopback addresses (127.0.0.1, localhost, ::1).
4. Cloud metadata services (169.254.169.254).
5. IPv6 private/unique-local and IPv4-mapped addresses.
6. Forbidden service ports (8000, 5432, 5436, 27017, 6379).
7. DNS rebinding defense (resolving hostname to private IP blocked).
8. Playwright Browser Executor navigation guard integration.
"""

import socket
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from execution.browser.network_guard import NetworkGuard
from execution.browser.browser_models import BrowserAction, BrowserActionType
from execution.browser.playwright_executor import PlaywrightBrowserExecutor


# ── Scheme Filtering ────────────────────────────────────────────────────────

def test_blocked_schemes():
    blocked = [
        "file:///etc/passwd",
        "file:///C:/Windows/win.ini",
        "gopher://127.0.0.1:70/",
        "ftp://ftp.debian.org/",
        "dict://dict.org/",
        "data:text/html,<h1>test</h1>",
        "javascript:alert(1)",
        "ldap://localhost:389/dc=example",
    ]
    for url in blocked:
        verdict = NetworkGuard.validate_url(url)
        assert verdict.allowed is False, f"Expected {url} to be blocked!"
        assert "Blocked scheme" in verdict.reason or "Malformed" in verdict.reason


def test_allowed_schemes_and_about_blank():
    # about:blank is permitted for fresh tabs
    assert NetworkGuard.validate_url("about:blank").allowed is True


# ── IP Address Filtering ───────────────────────────────────────────────────

def test_blocked_private_ipv4_networks():
    private_urls = [
        "http://10.0.0.1/",
        "http://10.254.1.100:8080/admin",
        "http://172.16.0.1/",
        "http://172.31.255.255/",
        "http://192.168.1.1/",
        "http://192.168.100.50/",
        "http://0.0.0.0/",
    ]
    for url in private_urls:
        verdict = NetworkGuard.validate_url(url)
        assert verdict.allowed is False, f"Expected {url} to be blocked!"
        assert "SSRF Protection" in verdict.reason or "forbidden" in verdict.reason.lower()


def test_blocked_loopback_addresses():
    loopback_urls = [
        "http://127.0.0.1/",
        "http://127.0.0.2:80/",
        "http://127.127.127.127/",
    ]
    for url in loopback_urls:
        verdict = NetworkGuard.validate_url(url)
        assert verdict.allowed is False, f"Expected {url} to be blocked!"
        assert "forbidden" in verdict.reason.lower() or "blocked" in verdict.reason.lower()


def test_blocked_cloud_metadata_services():
    metadata_urls = [
        "http://169.254.169.254/latest/meta-data/",
        "http://169.254.169.254/computeMetadata/v1/",
        "http://169.254.1.1/",
    ]
    for url in metadata_urls:
        verdict = NetworkGuard.validate_url(url)
        assert verdict.allowed is False, f"Expected {url} to be blocked!"
        assert "forbidden" in verdict.reason.lower() or "blocked" in verdict.reason.lower()


def test_blocked_ipv6_addresses():
    ipv6_urls = [
        "http://[::1]/",
        "http://[fc00::1]/",
        "http://[fd12:3456:789a::1]/",
        "http://[fe80::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:192.168.1.1]/",
    ]
    for url in ipv6_urls:
        verdict = NetworkGuard.validate_url(url)
        assert verdict.allowed is False, f"Expected {url} to be blocked!"
        assert "forbidden" in verdict.reason.lower() or "blocked" in verdict.reason.lower()


# ── Blocked Sensitive Service Ports ─────────────────────────────────────────

def test_blocked_ports():
    ports_to_test = [22, 23, 25, 5432, 5436, 6379, 8000, 9200, 27017]
    for port in ports_to_test:
        url = f"http://example.com:{port}/"
        verdict = NetworkGuard.validate_url(url)
        assert verdict.allowed is False, f"Expected port {port} to be blocked!"
        assert "Blocked internal service port" in verdict.reason


# ── DNS Rebinding Defense ───────────────────────────────────────────────────

def test_dns_rebinding_defense_private_ip():
    """If a domain resolves to a private IP, it must be blocked."""
    # Simulate DNS resolving 'rebind.attacker.com' to 192.168.1.5
    fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.1.5', 80))]
    with patch("socket.getaddrinfo", return_value=fake_addrinfo):
        verdict = NetworkGuard.validate_url("http://rebind.attacker.com/evil")
        assert verdict.allowed is False
        assert "SSRF Protection" in verdict.reason
        assert "192.168.1.5" in verdict.reason


def test_dns_rebinding_defense_loopback_ip():
    """If a domain resolves to 127.0.0.1, it must be blocked."""
    fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 80))]
    with patch("socket.getaddrinfo", return_value=fake_addrinfo):
        verdict = NetworkGuard.validate_url("http://local.attacker.com/")
        assert verdict.allowed is False
        assert "SSRF Protection" in verdict.reason


def test_public_fqdn_permitted():
    """A valid public domain resolving to a public IP must be permitted."""
    fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 80))]
    with patch("socket.getaddrinfo", return_value=fake_addrinfo):
        verdict = NetworkGuard.validate_url("http://example.com/")
        assert verdict.allowed is True
        assert "93.184.216.34" in verdict.resolved_ips


# ── Playwright Browser Executor Integration ─────────────────────────────────

@pytest.mark.asyncio
async def test_playwright_executor_blocks_ssrf_navigation():
    """PlaywrightBrowserExecutor must abort navigation to private IPs before network dispatch."""
    executor = PlaywrightBrowserExecutor(headless=True)
    
    # Try navigating to AWS metadata service
    action = BrowserAction(
        action_type=BrowserActionType.NAVIGATE,
        url="http://169.254.169.254/latest/meta-data/",
    )
    result = await executor.execute_action(action)
    assert result.success is False
    assert "SSRF Protection Blocked Navigation" in (result.error or "")

    # Try navigating to internal Postgres port
    action_port = BrowserAction(
        action_type=BrowserActionType.NAVIGATE,
        url="http://example.com:5432/db",
    )
    result_port = await executor.execute_action(action_port)
    assert result_port.success is False
    assert "Blocked internal service port 5432" in (result_port.error or "")


# ── Alternate IP Representation & Evasion Tests ─────────────────────────────

def test_alternate_ip_representations_blocked():
    """Verifies that dword, octal, hex, and mixed IPv4 notations are decoded and blocked."""
    alternate_evasions = [
        # 127.0.0.1 representations
        ("http://2130706433/", "127.0.0.1 (dword)"),
        ("http://0x7f000001/", "127.0.0.1 (hex dword)"),
        ("http://0x7f.0.0.1/", "127.0.0.1 (hex dotted)"),
        ("http://0177.0.0.1/", "127.0.0.1 (octal dotted)"),
        ("http://017700000001/", "127.0.0.1 (octal continuous)"),
        ("http://0x7f.1/", "127.0.0.1 (mixed 2-part)"),
        # 169.254.169.254 metadata representations
        ("http://2852039166/", "169.254.169.254 (dword)"),
        ("http://0xa9fea9fe/", "169.254.169.254 (hex)"),
        ("http://0251.0376.0251.0376/", "169.254.169.254 (octal)"),
        # 192.168.1.1 private representations
        ("http://3232235777/", "192.168.1.1 (dword)"),
        ("http://0xc0a80101/", "192.168.1.1 (hex)"),
        ("http://0300.0250.0001.0001/", "192.168.1.1 (octal)"),
    ]
    for url, desc in alternate_evasions:
        verdict = NetworkGuard.validate_url(url)
        assert verdict.allowed is False, f"Expected alternate IP evasion {desc} ({url}) to be blocked!"
        assert "SSRF Protection" in (verdict.reason or "") or "blocked" in (verdict.reason or "").lower()


# ── Route Interception, Redirects & Subresource Blocking Tests ──────────────

@pytest.mark.asyncio
async def test_route_interceptor_aborts_subresources_and_redirects():
    """Verifies that Playwright context route interception blocks private subresources and redirects."""
    mock_context = MagicMock()
    captured_routes = {}

    def fake_route(pattern, handler):
        captured_routes[pattern] = handler
        return MagicMock()

    mock_context.route = AsyncMock(side_effect=fake_route)
    mock_context.on = MagicMock()

    await NetworkGuard.attach_to_context(mock_context)
    assert "**/*" in captured_routes

    interceptor = captured_routes["**/*"]

    # 1. Test subresource to AWS metadata is aborted
    mock_subresource_req = MagicMock()
    mock_subresource_req.url = "http://169.254.169.254/latest/meta-data/"
    mock_subresource_req.redirected_from = None
    mock_route = MagicMock()
    mock_route.abort = AsyncMock()
    mock_route.continue_ = AsyncMock()

    await interceptor(mock_route, mock_subresource_req)
    mock_route.abort.assert_awaited_once_with("blockedbyclient")
    mock_route.continue_.assert_not_awaited()

    # 2. Test redirect hop to private IP is aborted
    mock_redirect_req = MagicMock()
    mock_redirect_req.url = "http://10.0.0.5/internal-api"
    mock_redirect_req.redirected_from = MagicMock(url="http://public-site.com/redirect")
    mock_route_redirect = MagicMock()
    mock_route_redirect.abort = AsyncMock()
    mock_route_redirect.continue_ = AsyncMock()

    await interceptor(mock_route_redirect, mock_redirect_req)
    mock_route_redirect.abort.assert_awaited_once_with("blockedbyclient")
    mock_route_redirect.continue_.assert_not_awaited()

    # 3. Test public request is permitted
    fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 80))]
    with patch("socket.getaddrinfo", return_value=fake_addrinfo):
        mock_public_req = MagicMock()
        mock_public_req.url = "http://example.com/logo.png"
        mock_public_req.redirected_from = None
        mock_route_pub = MagicMock()
        mock_route_pub.abort = AsyncMock()
        mock_route_pub.continue_ = AsyncMock()

        await interceptor(mock_route_pub, mock_public_req)
        mock_route_pub.continue_.assert_awaited_once()
        mock_route_pub.abort.assert_not_awaited()


@pytest.mark.asyncio
async def test_response_server_addr_dns_rebinding_detection():
    """Verifies that post-response socket IP verification detects rebind and closes page."""
    mock_context = MagicMock()
    captured_listeners = {}

    def fake_on(event, handler):
        captured_listeners[event] = handler

    mock_context.route = MagicMock()
    mock_context.on = fake_on

    await NetworkGuard.attach_to_context(mock_context)
    assert "response" in captured_listeners

    response_handler = captured_listeners["response"]

    # Simulate response whose underlying socket connected to 127.0.0.1 (rebound DNS)
    mock_response = MagicMock()
    mock_response.url = "http://rebind.attacker.com/data"
    mock_response.server_addr = AsyncMock(return_value={"ipAddress": "127.0.0.1", "port": 80})
    mock_page = MagicMock()
    mock_page.close = AsyncMock()
    mock_response.frame.page = mock_page

    await response_handler(mock_response)
    mock_page.close.assert_awaited_once()


def test_ipv6_classification_and_cdp_address_formats():
    """
    Verifies that valid globally-routable IPv6 addresses (e.g. Cloudflare / YouTube)
    are permitted across bare, bracketed, port-suffixed, and CDP socket formats,
    while non-global, private, loopback, and metadata IPv6 are strictly blocked.
    """
    # 1. Valid Globally-Routable IPv6 addresses (Must be permitted without false-positive Invalid IP)
    valid_global_ipv6_samples = [
        "2606:4700:8ca5:72db:f2a7:dfd:ef6b:ff98",
        "[2606:4700:8ca5:72db:f2a7:dfd:ef6b:ff98]",
        "[2606:4700:8ca5:72db:f2a7:dfd:ef6b:ff98]:443",
        "2001:4860:4860::8888",
        "[2001:4860:4860::8888]:53",
    ]
    for sample in valid_global_ipv6_samples:
        verdict = NetworkGuard._is_ip_blocked(sample)
        assert verdict is None, f"Expected {sample} to be permitted, but got blocked: {verdict}"

    # 2. Blocked IPv6 addresses (loopback, link-local, unique-local, documentation)
    blocked_ipv6_samples = [
        ("::1", "Loopback"),
        ("[::1]", "Loopback bracketed"),
        ("[::1]:8080", "Loopback bracketed with port"),
        ("fc00::1", "Unique Local (ULA)"),
        ("[fc00::1]:80", "Unique Local bracketed"),
        ("fe80::1", "Link Local"),
        ("[fe80::1%eth0]", "Link Local with scope ID"),
        ("::ffff:127.0.0.1", "IPv4-mapped loopback"),
        ("::ffff:192.168.1.1", "IPv4-mapped private"),
    ]
    for sample, label in blocked_ipv6_samples:
        verdict = NetworkGuard._is_ip_blocked(sample)
        assert verdict is not None, f"Expected {label} ({sample}) to be blocked!"


@pytest.mark.asyncio
async def test_response_server_addr_permits_global_ipv6_cdp():
    """Verifies that Playwright CDP response.server_addr returning global IPv6 does NOT close the page."""
    mock_context = MagicMock()
    captured_listeners = {}

    def fake_on(event, handler):
        captured_listeners[event] = handler

    mock_context.route = MagicMock()
    mock_context.on = fake_on

    await NetworkGuard.attach_to_context(mock_context)
    assert "response" in captured_listeners

    response_handler = captured_listeners["response"]

    # Simulate response from YouTube / Cloudflare via CDP with bracketed IPv6
    mock_response = MagicMock()
    mock_response.url = "https://www.youtube.com/"
    mock_response.server_addr = AsyncMock(return_value={
        "ipAddress": "2606:4700:8ca5:72db:f2a7:dfd:ef6b:ff98",
        "port": 443
    })
    mock_page = MagicMock()
    mock_page.close = AsyncMock()
    mock_response.frame.page = mock_page

    await response_handler(mock_response)
    # Must NOT close page
    mock_page.close.assert_not_awaited()


