"""
network_guard.py — SSRF, Private Network & DNS-Rebinding Protection (Phase 2 Task 3)
=====================================================================================
Protects the Playwright browser automation execution subsystem from Server-Side
Request Forgery (SSRF), internal private network access, cloud metadata exfiltration,
and DNS rebinding attacks.

Intercepts:
1. Navigation target URLs.
2. Background subresources (fetch/XHR/images/scripts/stylesheets/fonts).
3. Popups and newly created tabs/windows.
4. HTTP 3xx redirect destinations.

Guarantees:
- Strict scheme enforcement (http:// and https:// only; blocks file://, gopher://, dict://, etc.).
- Strict IP filtering against all RFC 1918 private IPv4 ranges (10/8, 172.16/12, 192.168/16).
- Strict loopback blocking (127.0.0.0/8, ::1).
- Strict link-local / cloud metadata blocking (169.254.0.0/16, AWS/GCP/Azure metadata 169.254.169.254).
- Strict IPv6 blocking (fc00::/7 unique local, fe80::/10 link-local, ::ffff:0:0/96 IPv4-mapped).
- Internal sensitive infrastructure port blocking (FastAPI 8000, Postgres 5432/5436, Mongo 27017, Redis 6379, etc.).
- Complete DNS resolution validation before network dispatch.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ── Blocked Network CIDRs ───────────────────────────────────────────────────

BLOCKED_IPV4_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),          # Current network (only valid as source)
    ipaddress.ip_network("10.0.0.0/8"),         # RFC 1918 Private Class A
    ipaddress.ip_network("100.64.0.0/10"),      # Shared Address Space (CGNAT)
    ipaddress.ip_network("127.0.0.0/8"),       # Loopback
    ipaddress.ip_network("169.254.0.0/16"),     # Link-Local / Cloud Instance Metadata
    ipaddress.ip_network("172.16.0.0/12"),      # RFC 1918 Private Class B
    ipaddress.ip_network("192.0.0.0/24"),       # IETF Protocol Assignments
    ipaddress.ip_network("192.0.2.0/24"),       # TEST-NET-1 (Documentation)
    ipaddress.ip_network("192.168.0.0/16"),     # RFC 1918 Private Class C
    ipaddress.ip_network("198.18.0.0/15"),      # Network Benchmark Tests
    ipaddress.ip_network("198.51.100.0/24"),    # TEST-NET-2 (Documentation)
    ipaddress.ip_network("203.0.113.0/24"),     # TEST-NET-3 (Documentation)
    ipaddress.ip_network("224.0.0.0/4"),        # Multicast
    ipaddress.ip_network("240.0.0.0/4"),        # Reserved for future use
    ipaddress.ip_network("255.255.255.255/32"), # Limited Broadcast
]

BLOCKED_IPV6_NETWORKS = [
    ipaddress.ip_network("::1/128"),            # Loopback
    ipaddress.ip_network("::/128"),             # Unspecified address
    ipaddress.ip_network("::ffff:0:0/96"),      # IPv4-mapped IPv6
    ipaddress.ip_network("64:ff9b::/96"),       # IPv4/IPv6 translation
    ipaddress.ip_network("100::/64"),           # Discard-Only Prefix
    ipaddress.ip_network("2001::/23"),          # IETF Protocol Assignments
    ipaddress.ip_network("2001:db8::/32"),      # Documentation
    ipaddress.ip_network("fc00::/7"),           # Unique Local Address (ULA)
    ipaddress.ip_network("fe80::/10"),          # Link-Local Unicast
    ipaddress.ip_network("ff00::/8"),           # Multicast
]

# ── Blocked Service & Infrastructure Ports ──────────────────────────────────

BLOCKED_PORTS: Set[int] = {
    21,     # FTP
    22,     # SSH
    23,     # Telnet
    25,     # SMTP
    110,    # POP3
    143,    # IMAP
    465,    # SMTPS
    587,    # SMTP submission
    2375,   # Docker unencrypted daemon
    2376,   # Docker TLS daemon
    5432,   # PostgreSQL default
    5433,   # PostgreSQL secondary
    5436,   # ARDO PostgreSQL port
    6379,   # Redis
    8000,   # FastAPI / ARDO backend
    8080,   # Common internal HTTP proxy / admin
    9200,   # Elasticsearch HTTP
    9300,   # Elasticsearch transport
    10250,  # Kubelet API
    10255,  # Kubelet Read-Only
    11211,  # Memcached
    27017,  # MongoDB default
    27018,  # MongoDB shard
}

ALLOWED_SCHEMES: Set[str] = {"http", "https"}


def parse_alternate_ip(host: str) -> Optional[Union[ipaddress.IPv4Address, ipaddress.IPv6Address]]:
    """
    Parses and normalizes standard and alternate IP address representations:
    - Decimal dotted-quad: '127.0.0.1'
    - Hexadecimal: '0x7f000001', '0x7f.0.0.1', '0x7f.1'
    - Octal dotted-quad: '0177.0.0.1', '017700000001'
    - Dword / Integer: '2130706433', '2852039166'
    - IPv6 standard and IPv4-mapped: '[::1]', '[::ffff:127.0.0.1]'
    Returns an IPv4Address or IPv6Address, or None if not an IP address representation.
    """
    if not host:
        return None

    cleaned = host.strip()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]

    # 1. Standard Python ipaddress parser
    try:
        return ipaddress.ip_address(cleaned)
    except ValueError:
        pass

    # 2. Pure Dword / Integer / Octal single-token format (e.g. 2130706433, 017700000001)
    if re.match(r"^\d+$", cleaned):
        try:
            # If leading zero and all digits 0-7, parse as octal first
            if cleaned.startswith("0") and len(cleaned) > 1 and all(c in "01234567" for c in cleaned):
                val = int(cleaned, 8)
            else:
                val = int(cleaned, 10)
            if 0 <= val <= 0xFFFFFFFF:
                return ipaddress.IPv4Address(val)
        except (ValueError, OverflowError):
            pass

    # 3. Pure Hex integer format (e.g. 0x7f000001 = 127.0.0.1)
    if re.match(r"^0x[0-9a-fA-F]+$", cleaned):
        try:
            val = int(cleaned, 16)
            if 0 <= val <= 0xFFFFFFFF:
                return ipaddress.IPv4Address(val)
        except (ValueError, OverflowError):
            pass

    # 4. Octal or mixed-base dotted parts (e.g. 0177.0.0.1, 0x7f.0.0.1, 127.1)
    if "." in cleaned:
        parts = cleaned.split(".")
        if 1 <= len(parts) <= 4:
            parsed_parts: List[int] = []
            for p in parts:
                p_str = p.strip()
                if not p_str:
                    return None
                try:
                    if p_str.startswith(("0x", "0X")):
                        parsed_parts.append(int(p_str, 16))
                    elif p_str.startswith("0") and len(p_str) > 1 and all(c in "01234567" for c in p_str):
                        parsed_parts.append(int(p_str, 8))
                    else:
                        parsed_parts.append(int(p_str, 10))
                except (ValueError, OverflowError):
                    return None

            # Validate range and recombine based on POSIX inet_aton rules
            try:
                if len(parsed_parts) == 4:
                    if all(0 <= x <= 255 for x in parsed_parts):
                        val = (parsed_parts[0] << 24) | (parsed_parts[1] << 16) | (parsed_parts[2] << 8) | parsed_parts[3]
                        return ipaddress.IPv4Address(val)
                elif len(parsed_parts) == 3:
                    if 0 <= parsed_parts[0] <= 255 and 0 <= parsed_parts[1] <= 255 and 0 <= parsed_parts[2] <= 65535:
                        val = (parsed_parts[0] << 24) | (parsed_parts[1] << 16) | parsed_parts[2]
                        return ipaddress.IPv4Address(val)
                elif len(parsed_parts) == 2:
                    if 0 <= parsed_parts[0] <= 255 and 0 <= parsed_parts[1] <= 16777215:
                        val = (parsed_parts[0] << 24) | parsed_parts[1]
                        return ipaddress.IPv4Address(val)
                elif len(parsed_parts) == 1:
                    if 0 <= parsed_parts[0] <= 0xFFFFFFFF:
                        return ipaddress.IPv4Address(parsed_parts[0])
            except (ValueError, OverflowError):
                pass

    return None


@dataclass
class NetworkGuardVerdict:
    """Evaluation verdict returned by NetworkGuard."""
    allowed: bool
    reason: Optional[str] = None
    resolved_ips: List[str] = field(default_factory=list)
    url: str = ""


class NetworkGuard:
    """
    Authoritative network guard defending browser automation against SSRF.
    """

    @classmethod
    def validate_url(cls, url: str) -> NetworkGuardVerdict:
        """
        Validates a URL before navigation or subresource fetch.
        Resolves hostname to authoritative IP addresses and verifies:
        - Scheme is http or https.
        - Host is not a private/reserved/loopback IP or resolving to one.
        - Port is not an internal service or blocked infrastructure port.
        """
        if not url:
            return NetworkGuardVerdict(allowed=False, reason="Empty URL rejected", url=url)

        # Allow about:blank for fresh tab initialization
        if url.strip() == "about:blank":
            return NetworkGuardVerdict(allowed=True, reason="about:blank allowed", url=url)

        try:
            parsed = urlparse(url.strip())
        except Exception as e:
            return NetworkGuardVerdict(allowed=False, reason=f"Malformed URL: {e}", url=url)

        # 1. Scheme Validation
        scheme = (parsed.scheme or "").lower()
        if scheme not in ALLOWED_SCHEMES:
            return NetworkGuardVerdict(
                allowed=False,
                reason=f"Blocked scheme '{scheme}'. Only HTTP and HTTPS are permitted.",
                url=url,
            )

        # 2. Hostname extraction
        hostname = parsed.hostname
        if not hostname:
            return NetworkGuardVerdict(allowed=False, reason="Missing hostname in URL", url=url)

        # 3. Port Validation
        port = parsed.port
        if port is not None and port in BLOCKED_PORTS:
            return NetworkGuardVerdict(
                allowed=False,
                reason=f"Blocked internal service port {port}. Access to infrastructure services is forbidden.",
                url=url,
            )

        # 4. Direct IP address check (standard and alternate representations) or DNS resolution
        resolved_ips: List[str] = []
        alt_ip = parse_alternate_ip(hostname)
        if alt_ip is not None:
            resolved_ips.append(str(alt_ip))
        else:
            # Hostname: resolve via socket.getaddrinfo
            try:
                addr_info = socket.getaddrinfo(hostname, None)
                for item in addr_info:
                    sockaddr = item[4]
                    ip_str = sockaddr[0]
                    if ip_str not in resolved_ips:
                        resolved_ips.append(ip_str)
            except socket.gaierror as e:
                # DNS failure or unreachable host
                return NetworkGuardVerdict(
                    allowed=False,
                    reason=f"DNS resolution failure for host '{hostname}': {e}",
                    url=url,
                )
            except Exception as e:
                return NetworkGuardVerdict(
                    allowed=False,
                    reason=f"Error resolving host '{hostname}': {e}",
                    url=url,
                )

        if not resolved_ips:
            return NetworkGuardVerdict(
                allowed=False,
                reason=f"No IP addresses resolved for host '{hostname}'",
                url=url,
            )

        # 5. Check each resolved IP against all blocked networks
        for ip_str in resolved_ips:
            ip_verdict = cls._is_ip_blocked(ip_str)
            if ip_verdict is not None:
                logger.warning(
                    f"[NetworkGuard] SSRF ATTEMPT BLOCKED: URL='{url}' host='{hostname}' "
                    f"resolved_ip='{ip_str}' reason='{ip_verdict}'"
                )
                return NetworkGuardVerdict(
                    allowed=False,
                    reason=f"SSRF Protection: Host resolves to blocked IP {ip_str} ({ip_verdict})",
                    resolved_ips=resolved_ips,
                    url=url,
                )

        return NetworkGuardVerdict(
            allowed=True,
            reason="URL and resolved IPs passed security verification",
            resolved_ips=resolved_ips,
            url=url,
        )

    @classmethod
    def _clean_ip_string(cls, raw: str) -> str:
        """
        Cleans and normalizes raw IP strings returned by CDP or network headers:
        - Strips whitespace and quotes.
        - Unpacks bracketed IPv6 with optional port: '[2606:4700:...]:443' -> '2606:4700:...'
        - Unpacks bracketed IPv6: '[2606:4700:...]' -> '2606:4700:...'
        - Strips IPv4 port suffix: '1.2.3.4:8080' -> '1.2.3.4'
        - Strips interface scope/zone identifier: 'fe80::1%eth0' -> 'fe80::1'
        """
        if not raw:
            return ""
        s = raw.strip().strip("'\"")
        # Handle bracketed IPv6: [2606:...] or [2606:...]:port
        if s.startswith("["):
            bracket_end = s.find("]")
            if bracket_end != -1:
                s = s[1:bracket_end]
        else:
            # Check for IPv4 with port: exactly one ':' and dots present
            if s.count(":") == 1 and "." in s:
                host_part, _, port_part = s.partition(":")
                if port_part.isdigit():
                    s = host_part
        # Strip scope identifier if present (e.g. fe80::1%eth0 or fe80::1%1)
        if "%" in s:
            s = s.split("%", 1)[0]
        return s.strip()

    @classmethod
    def _is_ip_blocked(cls, ip_str: str) -> Optional[str]:
        """
        Returns block reason if IP is private, loopback, non-global, or metadata; None if permitted.
        Properly handles bracketed IPv6, scoped IPv6, and IPv4-mapped IPv6.
        """
        if not ip_str:
            return None

        cleaned = cls._clean_ip_string(ip_str)
        if not cleaned:
            return None

        try:
            ip = ipaddress.ip_address(cleaned)
        except ValueError:
            alt_ip = parse_alternate_ip(cleaned)
            if alt_ip is not None:
                ip = alt_ip
            else:
                return "Invalid IP representation"

        # Check IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1 or ::ffff:192.168.1.1)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped

        # Built-in python checks
        if ip.is_loopback:
            return "Loopback address forbidden"
        if ip.is_private:
            return "RFC 1918 / Private network address forbidden"
        if ip.is_link_local:
            return "Link-local / Cloud metadata address forbidden"
        if ip.is_multicast:
            return "Multicast address forbidden"
        if ip.is_reserved:
            return "Reserved IP address forbidden"
        if ip.is_unspecified:
            return "Unspecified IP address forbidden"

        # Specific CIDR checks
        if isinstance(ip, ipaddress.IPv4Address):
            for net in BLOCKED_IPV4_NETWORKS:
                if ip in net:
                    return f"Matches blocked IPv4 network {net}"
        elif isinstance(ip, ipaddress.IPv6Address):
            for net in BLOCKED_IPV6_NETWORKS:
                if ip in net:
                    return f"Matches blocked IPv6 network {net}"

        # Global routability check: non-global addresses (e.g. unique local, private) are blocked
        if not ip.is_global:
            return "Non-globally routable address forbidden"

        return None

    @classmethod
    async def attach_to_context(cls, context: Any) -> None:
        """
        Attaches route interception and response security monitors to a Playwright browser context:
        1. Intercepts all network dispatches (**/*) covering navigations, subresources (fetch/XHR,
           images, scripts, styles, fonts), and redirect hops.
        2. Inspects redirect hops (request.redirected_from) to ensure no intermediate redirect targets
           private CIDRs or cloud metadata.
        3. Attaches response listeners to verify actual server socket IP addresses via CDP (response.server_addr()),
           immediately terminating any page that received bytes from a rebinding private IP.

        Architectural Distinction & Operational Limitations:
        - APPLICATION LAYER: NetworkGuard enforces URL validation, scheme restrictions, alternate IP
          representation decoding (decimal/hex/octal/dword), redirect destination validation, and CDP
          response socket verification.
        - KERNEL / NETWORK LAYER: If an attacker employs a malicious authoritative nameserver with 0s TTL
          and Chromium's socket pool connects to a rebound IP without CDP reporting in time, complete
          isolation requires host/container-level egress firewalls (e.g. iptables/nftables/AWS security
          groups dropping outbound 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, and 169.254.169.254) or
          a dedicated outbound egress proxy (Squid/Envoy).
        """
        if context is None:
            return

        async def route_interceptor(route: Any, request: Any):
            req_url = request.url

            # Log redirect hops if present
            if getattr(request, "redirected_from", None) is not None:
                orig_url = request.redirected_from.url
                logger.info(f"[NetworkGuard] Validating redirect hop: {orig_url} -> {req_url}")

            verdict = cls.validate_url(req_url)
            if not verdict.allowed:
                logger.warning(
                    f"[NetworkGuard] Aborted network request: {req_url} ({verdict.reason})"
                )
                try:
                    await route.abort("blockedbyclient")
                except Exception as e:
                    logger.debug(f"[NetworkGuard] route.abort note: {e}")
            else:
                try:
                    await route.continue_()
                except Exception as e:
                    logger.debug(f"[NetworkGuard] route.continue_ note: {e}")

        async def response_handler(response: Any):
            """Inspects response server socket IP to detect and abort DNS rebinding."""
            try:
                if hasattr(response, "server_addr"):
                    server_addr = await response.server_addr()
                    if server_addr and isinstance(server_addr, dict) and "ipAddress" in server_addr:
                        raw_ip = server_addr["ipAddress"]
                        if not raw_ip or not isinstance(raw_ip, str):
                            return
                        clean_ip = cls._clean_ip_string(raw_ip)
                        blocked_reason = cls._is_ip_blocked(clean_ip)
                        if blocked_reason and blocked_reason != "Invalid IP representation":
                            logger.critical(
                                f"[NetworkGuard] DNS REBINDING / IP LEAK DETECTED: "
                                f"Response for {response.url} was delivered by blocked IP {raw_ip} ({blocked_reason}). "
                                f"Terminating page immediately to prevent data exfiltration."
                            )
                            frame = getattr(response, "frame", None)
                            page = getattr(frame, "page", None) if frame else None
                            if page and hasattr(page, "close"):
                                await page.close()
                        elif blocked_reason == "Invalid IP representation":
                            logger.warning(
                                f"[NetworkGuard] Unparseable server_addr IP '{raw_ip}' for {response.url} — skipping socket termination."
                            )
            except Exception as e:
                logger.debug(f"[NetworkGuard] response_handler check note: {e}")

        try:
            # Route all network requests through the interceptor
            await context.route("**/*", route_interceptor)
            logger.info("[NetworkGuard] Route interception attached to Playwright context.")
        except Exception as e:
            logger.error(f"[NetworkGuard] Failed to attach route interceptor: {e}")

        try:
            # Monitor responses for socket IP leaks
            if hasattr(context, "on"):
                context.on("response", response_handler)
        except Exception as e:
            logger.debug(f"[NetworkGuard] Could not attach response handler: {e}")

