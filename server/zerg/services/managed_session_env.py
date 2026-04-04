"""Managed-session environment helpers.

The explicit Longhouse launchers inject a current managed-session id into the
process environment so local CLI/MCP commands can act "as" the running
session. Keep that contract internal to managed launch plumbing; user-facing
copy should talk about "the current managed session", not the raw env name.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping

MANAGED_SESSION_ENV = "LONGHOUSE_MANAGED_SESSION_ID"
LEGACY_MANAGED_SESSION_ENV = "LONGHOUSE_SESSION_ID"
MANAGED_SESSION_ENV_NAMES = (MANAGED_SESSION_ENV, LEGACY_MANAGED_SESSION_ENV)
CURRENT_SESSION_HEADER = "X-Longhouse-Session-Id"


def get_managed_session_id(env: Mapping[str, str] | None = None) -> str | None:
    """Return the current managed-session id from the environment, if any."""

    source = os.environ if env is None else env
    for name in MANAGED_SESSION_ENV_NAMES:
        value = str(source.get(name) or "").strip()
        if value:
            return value
    return None


def build_managed_session_env_exports(session_id: str) -> list[str]:
    """Build shell exports for the current managed-session id.

    Export the new internal name first, while mirroring the legacy name during
    the transition so already-installed hooks and older tooling keep working.
    """

    normalized = str(session_id or "").strip()
    if not normalized:
        return []
    quoted = shlex.quote(normalized)
    return [
        f"export {MANAGED_SESSION_ENV}={quoted}",
        f"export {LEGACY_MANAGED_SESSION_ENV}={quoted}",
    ]


__all__ = [
    "CURRENT_SESSION_HEADER",
    "LEGACY_MANAGED_SESSION_ENV",
    "MANAGED_SESSION_ENV",
    "MANAGED_SESSION_ENV_NAMES",
    "build_managed_session_env_exports",
    "get_managed_session_id",
]
