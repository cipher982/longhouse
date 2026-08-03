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

_DEFAULT_DEADLINE_SECONDS = 0.75
_DEFAULT_ATTEMPT_SECONDS = 0.35
# The real 5,000-session hosted timeline measures about 0.39s at p50 and
# 0.7-0.8s during browser QA. The old 0.35s attempt budget timed out ordinary
# successful work and amplified load with an immediate retry. Keep this
# heavier snapshot bounded separately without weakening fast auth/machine reads.
_TIMELINE_DEADLINE_SECONDS = 3.25
_TIMELINE_ATTEMPT_SECONDS = 1.5


class CatalogReadError(RuntimeError):
    """A bounded catalog read could not be completed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def timeline_snapshot(params: dict[str, Any]) -> dict[str, Any]:
    return _call("session.timeline.list.v2", params)


def canonical_timeline_snapshot(params: dict[str, Any], *, owner_id: int) -> dict[str, Any]:
    return _call(
        "session.timeline.list.v2",
        {**params, "owner_id": owner_id, "include_state_heads": True},
    )


def session_snapshot(session_id: str, *, owner_id: int | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"session_id": session_id}
    if owner_id is not None:
        params["owner_id"] = owner_id
    return _call("session.read.v2", params)


def shadow_session_state_snapshot(session_id: str, *, owner_id: int) -> dict[str, Any]:
    return _call(
        "session.shadow_state.read.v2",
        {"session_id": session_id, "owner_id": owner_id},
    )


def shadow_session_state_health(*, owner_id: int) -> dict[str, Any]:
    return _call("session.shadow_state.health.v2", {"owner_id": owner_id})


def session_batch_snapshot(session_ids: list[str]) -> dict[str, Any]:
    return _call("session.read.batch.v2", {"session_ids": session_ids})


def active_session_ids(*, limit: int, days_back: int, observed_at: str) -> dict[str, Any]:
    return _call(
        "session.active.list.v2",
        {"limit": limit, "days_back": days_back, "observed_at": observed_at},
    )


def resolve_session_prefix(prefix: str) -> dict[str, Any]:
    return _call("session.prefix.resolve.v2", {"prefix": prefix})


def resolve_session_alias(provider_session_id: str) -> dict[str, Any]:
    return _call("session.alias.resolve.v2", {"provider_session_id": provider_session_id})


def enrolled_machines(owner_id: int) -> dict[str, Any]:
    return _call("machine.enrollment.list.v2", {"owner_id": owner_id})


def machine_heartbeats(
    *,
    owner_id: int,
    device_id: str | None,
    recent_after: str | None,
    limit: int,
) -> dict[str, Any]:
    return _call(
        "machine.health.list.v2",
        {
            "owner_id": owner_id,
            "device_id": device_id,
            "recent_after": recent_after,
            "limit": limit,
        },
    )


def rename_machine(*, owner_id: int, device_id: str, machine_name: str) -> dict[str, Any]:
    return _call(
        "machine.enrollment.rename.v2",
        {"owner_id": owner_id, "device_id": device_id, "machine_name": machine_name},
    )


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


def machine_operation(*, owner_id: int, operation_id: str) -> dict[str, Any]:
    return _call(
        "machine.operation.read.v2",
        {"owner_id": owner_id, "operation_id": operation_id},
    )


def recent_visible_web_presence(*, owner_id: int, threshold: str) -> bool:
    result = _call(
        "notification.presence.visible.read.v2",
        {"owner_id": owner_id, "threshold": threshold},
    )
    return result.get("visible") is True


def _call(method: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        _database_path, socket_path = catalogd_paths()
    except RuntimeError as exc:
        raise CatalogReadError("catalog_unavailable", "The live catalog is temporarily unavailable.") from exc
    if method == "session.timeline.list.v2":
        deadline_seconds = _TIMELINE_DEADLINE_SECONDS
        attempt_seconds = _TIMELINE_ATTEMPT_SECONDS
    else:
        deadline_seconds = _DEFAULT_DEADLINE_SECONDS
        attempt_seconds = _DEFAULT_ATTEMPT_SECONDS
    deadline = time.monotonic() + deadline_seconds
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
                timeout_seconds=min(attempt_seconds, remaining),
            )
        except CatalogRemoteError as exc:
            raise CatalogReadError(exc.code, str(exc)) from exc
        except CatalogUnavailable as exc:
            last_unavailable = exc
    raise CatalogReadError("catalog_unavailable", "The live catalog is temporarily unavailable.") from last_unavailable


__all__ = [
    "active_session_ids",
    "CatalogReadError",
    "active_owner_id",
    "enrolled_machines",
    "machine_operation",
    "machine_heartbeats",
    "machine_workspaces",
    "recent_visible_web_presence",
    "rename_machine",
    "resolve_session_alias",
    "resolve_session_prefix",
    "session_snapshot",
    "shadow_session_state_snapshot",
    "shadow_session_state_health",
    "session_batch_snapshot",
    "timeline_snapshot",
]
