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
# A session snapshot joins the canonical session, control connection, run,
# readiness, and transcript facts. A cold hosted restart plus an eight-request
# events/workspace burst measured one late duplicate at 2.42 s while the shared
# catalog read completed for its peers. Keep owner/auth lookups fast and each
# attempt at one second, but let this composed product snapshot wait through a
# brief queue-free lane collision instead of returning a cold-start 503.
_SESSION_SNAPSHOT_DEADLINE_SECONDS = 4.25
_SESSION_SNAPSHOT_ATTEMPT_SECONDS = 1.0
_SESSION_SNAPSHOT_TRANSPORT_ATTEMPTS = 4
# The real 5,000-session hosted timeline measures about 0.39s at p50 and
# 0.7-0.8s during browser QA. The old 0.35s attempt budget timed out ordinary
# successful work and amplified load with an immediate retry. Keep this
# heavier snapshot bounded separately without weakening fast auth/machine reads.
_TIMELINE_DEADLINE_SECONDS = 3.25
_TIMELINE_ATTEMPT_SECONDS = 1.5
# Workspace ranking scans bounded provenance pages rather than one lookup. On
# the 48 GB dogfood catalog it measures 0.24-0.56s, so the generic 0.35s budget
# converts healthy work into a retry storm. Keep its budget explicit and below
# the launch sheet's human-perception threshold.
_WORKSPACE_DEADLINE_SECONDS = 2.25
_WORKSPACE_ATTEMPT_SECONDS = 1.0
# Search hydrates a small, concurrency-bounded set of canonical session cards.
# On the 30k-session dogfood catalog each owner-scoped reducer snapshot is
# about 0.33-0.37s, so the generic 0.35s attempt budget turns healthy reads
# into deterministic 503s. Keep the measured budget local to this operation;
# callers still bound fan-out and the route retains its outer timeout.
_SHADOW_STATE_DEADLINE_SECONDS = 2.25
_SHADOW_STATE_ATTEMPT_SECONDS = 1.0
# Title health computes the durable debt shape across the full session catalog.
# Hosted david010 measured 1.22s at 30k sessions after the presentation-policy
# backfill, so the generic point-read budget made a healthy dependency report
# unavailable. Keep this operator read explicitly bounded without slowing the
# ordinary catalog gateway paths.
_TITLE_HEALTH_DEADLINE_SECONDS = 4.25
_TITLE_HEALTH_ATTEMPT_SECONDS = 2.0

_READ_BUDGETS = {
    "session.read.v2": (_SESSION_SNAPSHOT_DEADLINE_SECONDS, _SESSION_SNAPSHOT_ATTEMPT_SECONDS),
    "session.read.batch.v2": (_SESSION_SNAPSHOT_DEADLINE_SECONDS, _SESSION_SNAPSHOT_ATTEMPT_SECONDS),
    "session.timeline.list.v2": (_TIMELINE_DEADLINE_SECONDS, _TIMELINE_ATTEMPT_SECONDS),
    "machine.workspace.list.v2": (_WORKSPACE_DEADLINE_SECONDS, _WORKSPACE_ATTEMPT_SECONDS),
    "session.shadow_state.read.v2": (_SHADOW_STATE_DEADLINE_SECONDS, _SHADOW_STATE_ATTEMPT_SECONDS),
    "session.shadow_state.read.batch.v2": (_TIMELINE_DEADLINE_SECONDS, _TIMELINE_ATTEMPT_SECONDS),
    "storage.session.title.dependency.health.v2": (_TITLE_HEALTH_DEADLINE_SECONDS, _TITLE_HEALTH_ATTEMPT_SECONDS),
}


class CatalogReadError(RuntimeError):
    """A bounded catalog read could not be completed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def canonical_timeline_snapshot(params: dict[str, Any], *, owner_id: int) -> dict[str, Any]:
    return _call(
        "session.timeline.list.v2",
        {**params, "owner_id": owner_id, "include_state_heads": True},
    )


def session_snapshot(session_id: str, *, owner_id: int) -> dict[str, Any]:
    return _call("session.read.v2", {"session_id": session_id, "owner_id": owner_id})


def shadow_session_state_snapshot(session_id: str, *, owner_id: int) -> dict[str, Any]:
    return _call(
        "session.shadow_state.read.v2",
        {"session_id": session_id, "owner_id": owner_id},
    )


def shadow_session_states_snapshot(session_ids: list[str], *, owner_id: int) -> dict[str, Any]:
    return _call(
        "session.shadow_state.read.batch.v2",
        {"session_ids": session_ids, "owner_id": owner_id},
    )


def shadow_session_state_health(*, owner_id: int) -> dict[str, Any]:
    return _call("session.shadow_state.health.v2", {"owner_id": owner_id})


def session_batch_snapshot(session_ids: list[str], *, owner_id: int) -> dict[str, Any]:
    return _call("session.read.batch.v2", {"session_ids": session_ids, "owner_id": owner_id})


def owned_session_ids(session_ids: list[str], *, owner_id: int) -> frozenset[str]:
    """Authorize a bounded legacy-id set through catalogd's owner predicate."""

    owned: set[str] = set()
    canonical = list(dict.fromkeys(str(value) for value in session_ids))
    for offset in range(0, len(canonical), 20):
        snapshot = session_batch_snapshot(canonical[offset : offset + 20], owner_id=owner_id)
        for facts in snapshot.get("facts") or []:
            catalog = facts.get("catalog") if isinstance(facts, dict) else None
            session_id = catalog.get("session_id") if isinstance(catalog, dict) else None
            if isinstance(session_id, str):
                owned.add(session_id)
    return frozenset(owned)


def internal_session_batch_snapshot(session_ids: list[str]) -> dict[str, Any]:
    """Read process-internal facts with no user-object authorization claim."""

    return _call("session.read.batch.v2", {"session_ids": session_ids})


def active_session_ids(*, limit: int, days_back: int, observed_at: str) -> dict[str, Any]:
    return _call(
        "session.active.list.v2",
        {"limit": limit, "days_back": days_back, "observed_at": observed_at},
    )


def resolve_session_prefix(prefix: str, *, owner_id: int) -> dict[str, Any]:
    return _call("session.prefix.resolve.v2", {"prefix": prefix, "owner_id": owner_id})


def resolve_session_alias(provider_session_id: str, *, owner_id: int) -> dict[str, Any]:
    return _call("session.alias.resolve.v2", {"provider_session_id": provider_session_id, "owner_id": owner_id})


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


def title_dependency_health() -> dict[str, Any]:
    return _call("storage.session.title.dependency.health.v2", {})


def _call(method: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        _database_path, socket_path = catalogd_paths()
    except RuntimeError as exc:
        raise CatalogReadError("catalog_unavailable", "The live catalog is temporarily unavailable.") from exc
    deadline_seconds, attempt_seconds = _READ_BUDGETS.get(
        method,
        (_DEFAULT_DEADLINE_SECONDS, _DEFAULT_ATTEMPT_SECONDS),
    )
    transport_attempt_limit = _SESSION_SNAPSHOT_TRANSPORT_ATTEMPTS if method in {"session.read.v2", "session.read.batch.v2"} else 2
    deadline = time.monotonic() + deadline_seconds
    last_unavailable: CatalogUnavailable | None = None
    transport_attempts = 0
    retry_delay_seconds = 0.025
    while True:
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
            if exc.retryable:
                retry_seconds = max(
                    retry_delay_seconds,
                    max(0, exc.retry_after_ms or 0) / 1_000,
                )
                remaining = deadline - time.monotonic()
                if retry_seconds < remaining:
                    time.sleep(retry_seconds)
                    retry_delay_seconds = min(retry_delay_seconds * 2, 0.25)
                    continue
            raise CatalogReadError(exc.code, str(exc)) from exc
        except CatalogUnavailable as exc:
            last_unavailable = exc
            transport_attempts += 1
            if transport_attempts >= transport_attempt_limit:
                break
    raise CatalogReadError("catalog_unavailable", "The live catalog is temporarily unavailable.") from last_unavailable


__all__ = [
    "active_session_ids",
    "CatalogReadError",
    "active_owner_id",
    "enrolled_machines",
    "machine_operation",
    "machine_heartbeats",
    "machine_workspaces",
    "owned_session_ids",
    "internal_session_batch_snapshot",
    "recent_visible_web_presence",
    "rename_machine",
    "resolve_session_alias",
    "resolve_session_prefix",
    "session_snapshot",
    "shadow_session_state_snapshot",
    "shadow_session_states_snapshot",
    "shadow_session_state_health",
    "session_batch_snapshot",
    "canonical_timeline_snapshot",
    "title_dependency_health",
]
