"""
Client metadata extraction.

Centralizes the logic for figuring out who is talking to us, so routes
don't repeat themselves and so all the "trust the proxy headers" details
live in one place.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

from fastapi import Request


# Our own host(s). A request whose Origin header matches one of these is
# treated as same-origin (the public web UI). Anything else is "foreign"
# and gets the stricter rate limit bucket.
_SAME_ORIGIN_HOSTS = {"drop.pensa.ar", "localhost"}


def client_ip(request: Request) -> str:
    """
    Return the real client IP.

    Trust order: X-Forwarded-For (leftmost), then X-Real-IP, then the
    direct peer. uvicorn's proxy_headers handling already overwrites
    request.client when it sees X-Forwarded-For, but we still parse XFF
    explicitly because we want to be tolerant of multi-hop chains and we
    want a defined behavior for malformed headers.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        candidate = xff.split(",")[0].strip()
        if _is_valid_ip(candidate):
            return candidate

    xri = request.headers.get("x-real-ip", "").strip()
    if xri and _is_valid_ip(xri):
        return xri

    return request.client.host if request.client else "0.0.0.0"


def _is_valid_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def origin_host(request: Request) -> str | None:
    """Return the hostname from the Origin header, or None if absent/malformed."""
    raw = request.headers.get("origin")
    if not raw:
        return None
    try:
        return urlparse(raw).hostname
    except Exception:
        return None


def is_foreign_origin(request: Request) -> bool:
    """
    True if there's an Origin header and it's NOT one of our own hosts.
    No Origin header (typical for curl) is treated as same-origin — those
    callers are not browsers, and CORS isn't relevant to them.
    """
    host = origin_host(request)
    if host is None:
        return False
    return host not in _SAME_ORIGIN_HOSTS
