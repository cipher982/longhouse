"""Keep live QA producers on a disposable Runtime Host.

Qualification, canaries and agent QA drive a Runtime Host they own: the
factory's per-tick host, ``make dev``, simlab, or the dedicated canary. A hosted
instance a person uses must never absorb that traffic by accident, so a
non-loopback target has to be named explicitly.
"""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlparse

HOSTED_TARGET_ENV = "LONGHOUSE_QA_HOSTED_TARGET"


def require_disposable_runtime(api_url: str | None) -> None:
    """Refuse a live run against a non-loopback Runtime Host unless it is named."""

    if not api_url:
        return
    host = urlparse(api_url).hostname or ""
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    if os.environ.get(HOSTED_TARGET_ENV, "").rstrip("/") == api_url.rstrip("/"):
        return
    raise RuntimeError(
        f"refusing live QA against {api_url}: run it against a local Runtime Host "
        f"(make dev, simlab) or the canary; set {HOSTED_TARGET_ENV}={api_url} to target it deliberately"
    )
