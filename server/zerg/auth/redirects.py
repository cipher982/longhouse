"""Redirect/return-target helpers for auth flows."""

from __future__ import annotations

import urllib.parse


def normalize_local_return_to(return_to: str | None) -> str | None:
    """Allow only local absolute paths like ``/timeline`` or ``/loop/card/123``."""
    if not return_to:
        return None

    parsed = urllib.parse.urlparse(return_to)
    if parsed.scheme or parsed.netloc:
        return None
    if not return_to.startswith("/") or return_to.startswith("//"):
        return None
    return return_to
