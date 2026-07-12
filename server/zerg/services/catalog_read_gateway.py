"""Typed Runtime Host boundary for bounded catalog reads.

The API process never opens the live SQLite catalog.  It asks ``catalogd`` for
one business snapshot and performs presentation projection from the returned
raw facts.  Archive/transcript reads intentionally live behind a different
boundary.
"""

from __future__ import annotations

import time
from typing import Any

from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.client import CatalogUnavailable
from zerg.catalogd.client import call_catalogd_sync
from zerg.services.catalogd_supervisor import catalogd_paths


class CatalogReadError(RuntimeError):
    """A bounded catalog read could not be completed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def timeline_snapshot(params: dict[str, Any]) -> dict[str, Any]:
    return _call("session.timeline.list.v2", params)


def session_snapshot(session_id: str) -> dict[str, Any]:
    return _call("session.read.v2", {"session_id": session_id})


def resolve_session_prefix(prefix: str) -> dict[str, Any]:
    return _call("session.prefix.resolve.v2", {"prefix": prefix})


def enrolled_machines(owner_id: int) -> dict[str, Any]:
    return _call("machine.enrollment.list.v2", {"owner_id": owner_id})


def active_owner_id() -> int | None:
    result = _call("auth.owner.get.v2", {})
    owner_id = result.get("owner_id")
    return int(owner_id) if result.get("found") is True and owner_id is not None else None


def machine_workspaces(
    *,
    owner_id: int,
    device_id: str,
    limit: int,
    days_back: int,
) -> dict[str, Any]:
    return _call(
        "machine.workspace.list.v2",
        {
            "owner_id": owner_id,
            "device_id": device_id,
            "limit": limit,
            "days_back": days_back,
        },
    )


def _call(method: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        _database_path, socket_path = catalogd_paths()
    except RuntimeError as exc:
        raise CatalogReadError("catalog_unavailable", "The live catalog is temporarily unavailable.") from exc
    deadline = time.monotonic() + 0.75
    last_unavailable: CatalogUnavailable | None = None
    for _attempt in range(2):
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            return call_catalogd_sync(
                socket_path,
                method,
                params=params,
                timeout_seconds=min(0.35, remaining),
            )
        except CatalogRemoteError as exc:
            raise CatalogReadError(exc.code, str(exc)) from exc
        except CatalogUnavailable as exc:
            last_unavailable = exc
    raise CatalogReadError("catalog_unavailable", "The live catalog is temporarily unavailable.") from last_unavailable


__all__ = [
    "CatalogReadError",
    "active_owner_id",
    "enrolled_machines",
    "machine_workspaces",
    "resolve_session_prefix",
    "session_snapshot",
    "timeline_snapshot",
]
