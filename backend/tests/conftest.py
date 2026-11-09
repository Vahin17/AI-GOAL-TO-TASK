"""
conftest.py — Global Test Configuration & Universal No-Network Guard
===================================================================
Enforces absolute zero-external-network isolation across all tests in backend/tests/.
Intercepts:
1. urllib.request.urlopen -> fails loudly if unmocked
2. http.client.HTTPConnection.connect -> permits loopback only (127.0.0.1, localhost, ::1)
3. http.client.HTTPSConnection.connect -> permits loopback only
4. socket.socket.connect -> fails loudly on any non-loopback destination IP/host
5. socket.create_connection -> permits loopback only

This universally protects:
- urllib / urllib3
- requests
- httpx / httpcore
- openai SDK
- google-genai / generativelanguage REST
- raw socket calls
"""

import socket
import http.client
import urllib.request
import pytest

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

_orig_socket_connect = socket.socket.connect
_orig_create_connection = socket.create_connection
_orig_http_connect = http.client.HTTPConnection.connect
_orig_https_connect = http.client.HTTPSConnection.connect


def _is_loopback(host: str) -> bool:
    if not host:
        return True
    host_clean = str(host).strip().lower().split("%")[0]
    if host_clean in LOOPBACK_HOSTS:
        return True
    if host_clean.startswith("127."):
        return True
    return False


def _guarded_socket_connect(self, address):
    host = address[0] if isinstance(address, (tuple, list)) and len(address) > 0 else str(address)
    if not _is_loopback(host):
        raise AssertionError(
            f"SECURITY VIOLATION: Non-loopback network connection to '{address}' was attempted during test!"
        )
    return _orig_socket_connect(self, address)


def _guarded_create_connection(address, *args, **kwargs):
    host = address[0] if isinstance(address, (tuple, list)) and len(address) > 0 else str(address)
    if not _is_loopback(host):
        raise AssertionError(
            f"SECURITY VIOLATION: Non-loopback socket.create_connection to '{address}' was attempted during test!"
        )
    return _orig_create_connection(address, *args, **kwargs)


def _guarded_http_connect(self, *args, **kwargs):
    host = getattr(self, "host", "")
    if not _is_loopback(host):
        raise AssertionError(
            f"SECURITY VIOLATION: Non-loopback http.client connection to '{host}' was attempted during test!"
        )
    return _orig_http_connect(self, *args, **kwargs)


def _guarded_https_connect(self, *args, **kwargs):
    host = getattr(self, "host", "")
    if not _is_loopback(host):
        raise AssertionError(
            f"SECURITY VIOLATION: Non-loopback http.client HTTPS connection to '{host}' was attempted during test!"
        )
    return _orig_https_connect(self, *args, **kwargs)


def _guarded_urlopen(*args, **kwargs):
    raise AssertionError(
        "SECURITY VIOLATION: Unmocked urllib.request.urlopen call was attempted during test!"
    )


@pytest.fixture(autouse=True, scope="session")
def universal_no_network_guard():
    """Session-scoped universal network guard intercepting all external network transports."""
    socket.socket.connect = _guarded_socket_connect
    socket.create_connection = _guarded_create_connection
    http.client.HTTPConnection.connect = _guarded_http_connect
    http.client.HTTPSConnection.connect = _guarded_https_connect
    
    orig_urlopen = urllib.request.urlopen
    urllib.request.urlopen = _guarded_urlopen

    yield

    socket.socket.connect = _orig_socket_connect
    socket.create_connection = _orig_create_connection
    http.client.HTTPConnection.connect = _orig_http_connect
    http.client.HTTPSConnection.connect = _orig_https_connect
    urllib.request.urlopen = orig_urlopen
