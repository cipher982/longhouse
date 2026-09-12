"""Proxy-aware client address helpers for request throttling."""

from __future__ import annotations

import os

from starlette.requests import Request


def _trusted_proxy_hops() -> int:
    """How many appending reverse proxies sit in front of this instance."""
    try:
        return max(int(os.getenv("TRUSTED_PROXY_HOPS", "0")), 0)
    except ValueError:
        return 0


def get_client_ip(request: Request) -> str:
    """Return the caller address using only explicitly trusted proxy hops.

    Proxies append to ``X-Forwarded-For``. Counting from the right keeps the
    left side, which the caller can forge, out of the rate-limit key.
    """
    hops = _trusted_proxy_hops()
    if hops:
        forwarded = request.headers.get("x-forwarded-for")
        chain = [part.strip() for part in (forwarded or "").split(",") if part.strip()]
        if len(chain) >= hops:
            return chain[-hops]
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


__all__ = ["get_client_ip"]
