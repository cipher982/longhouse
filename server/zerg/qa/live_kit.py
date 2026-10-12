"""What every producer that drives a live Longhouse does, once.

The producers each drive a real provider through a Runtime Host they own and
watch what Longhouse does with it. The small things all of them need for that
were written again in each: the Runtime Host's address and the guard against
pointing a qualification run at a host a person uses. This module is the one
copy. It imports nothing from the producers, so any of them can import it.
"""

from __future__ import annotations

import ipaddress
import os
import urllib.parse

RUNTIME_API_URL_ENV = "LONGHOUSE_RUNTIME_API_URL"
RUNTIME_AGENTS_TOKEN_ENV = "LONGHOUSE_RUNTIME_AGENTS_TOKEN"
HOSTED_TARGET_ENV = "LONGHOUSE_QA_HOSTED_TARGET"


def require_disposable_runtime(api_url: str | None) -> None:
    """Refuse a live QA run against a non-loopback Runtime Host unless it is named.

    Qualification, canaries and agent QA drive a Runtime Host they own: the
    factory's per-tick host, ``make dev``, simlab, or the dedicated canary. A
    hosted instance a person uses must never absorb that traffic by accident.
    """

    if not api_url:
        return
    host = urllib.parse.urlparse(api_url).hostname or ""
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
