"""Machines directory view shared by agents and timeline routes.

Produces the small per-user machine list used by the launch sheet and the
machines page. Joins the in-memory machine control channel registry
(authoritative for online/supports/last_seen) with persisted device-token
rows (authoritative for machines the user has enrolled but that are not
currently connected).

Intentionally thin: this is not a health dashboard. ``agents/machines/health``
remains the richer view. The goal here is "what can I launch on right now,
and what else does this user own."
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Mapping

from zerg.services.machine_control_channel import MachineControlChannelRegistry
from zerg.services.machine_control_channel import get_machine_control_channel_registry
from zerg.services.managed_provider_contracts import machine_control_operations_by_provider

CONTROL_CONNECTED = "connected"
CONTROL_DISCONNECTED = "disconnected"
LAUNCH_BLOCKED_CONTROL_DOWN = "control_down"
LAUNCH_BLOCKED_NO_LAUNCH_SUPPORT = "no_launch_support"
LAUNCH_BLOCKED_PROVIDERS_NOT_READY = "providers_not_ready"
# Readiness states that mean a Console turn would fail before the provider
# does any work. "unknown" stays launchable: a provider Longhouse cannot probe
# is not evidence of a problem.
UNLAUNCHABLE_READINESS_STATES = frozenset({"not_authenticated", "cli_missing"})


@dataclass(frozen=True)
class MachineLaunchProviderOption:
    provider: str


@dataclass(frozen=True)
class MachineLaunchUnavailableProvider:
    provider: str
    reason: str
    remediation: str | None


@dataclass(frozen=True)
class MachineLaunchProjection:
    blocked_by: str | None
    providers: tuple[MachineLaunchProviderOption, ...]
    default_provider: str | None
    # Providers this engine could drive but whose readiness says a turn would
    # fail right now (for example, signed out). Listed separately so clients
    # never offer them as launchable, yet can say what to fix.
    unavailable_providers: tuple[MachineLaunchUnavailableProvider, ...] = ()

    def to_response(self) -> dict[str, object]:
        return {
            "blocked_by": self.blocked_by,
            "providers": [{"provider": option.provider} for option in self.providers],
            "default_provider": self.default_provider,
            "unavailable_providers": [
                {"provider": item.provider, "reason": item.reason, "remediation": item.remediation} for item in self.unavailable_providers
            ],
        }


@dataclass(frozen=True)
class MachineEntry:
    device_id: str
    machine_name: str
    online: bool
    control_channel_status: str
    supports: tuple[str, ...]
    control_operations_by_provider: dict[str, tuple[str, ...]]
    last_seen_at: datetime | None
    # When the current control channel connected. Distinct from last_seen_at:
    # together they separate a machine that has held one connection all day
    # from one that keeps reconnecting. Null when offline -- an ended
    # connection has no ongoing uptime to report.
    connected_since: datetime | None
    engine_build: str | None
    launch: MachineLaunchProjection
    # Why each provider can or cannot run here. Empty for an offline machine
    # and for engines predating readiness, both of which mean "not reported"
    # rather than "not ready".
    provider_readiness: dict[str, dict[str, str]]

    def to_response(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "machine_name": self.machine_name,
            "online": self.online,
            "control_channel_status": self.control_channel_status,
            "supports": list(self.supports),
            "control_operations_by_provider": {
                provider: list(operations) for provider, operations in sorted(self.control_operations_by_provider.items())
            },
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "connected_since": self.connected_since.isoformat() if self.connected_since else None,
            "engine_build": self.engine_build,
            "launch": self.launch.to_response(),
            "provider_readiness": {provider: dict(entry) for provider, entry in sorted(self.provider_readiness.items())},
        }


def _launch_projection(
    operations_by_provider: dict[str, tuple[str, ...]],
    *,
    connected: bool,
    provider_readiness: Mapping[str, Mapping[str, str]] | None = None,
) -> MachineLaunchProjection:
    readiness = provider_readiness or {}
    options: list[MachineLaunchProviderOption] = []
    unavailable: list[MachineLaunchUnavailableProvider] = []
    for provider, operations in sorted(operations_by_provider.items()):
        if "turn_start" not in operations:
            continue
        state = (readiness.get(provider) or {}).get("state")
        if state in UNLAUNCHABLE_READINESS_STATES:
            unavailable.append(
                MachineLaunchUnavailableProvider(
                    provider=provider,
                    reason=state,
                    remediation=(readiness.get(provider) or {}).get("remediation"),
                )
            )
            continue
        options.append(MachineLaunchProviderOption(provider=provider))

    if not options:
        if not connected:
            blocked_by = LAUNCH_BLOCKED_CONTROL_DOWN
        elif unavailable:
            blocked_by = LAUNCH_BLOCKED_PROVIDERS_NOT_READY
        else:
            blocked_by = LAUNCH_BLOCKED_NO_LAUNCH_SUPPORT
        return MachineLaunchProjection(
            blocked_by=blocked_by,
            providers=(),
            default_provider=None,
            unavailable_providers=tuple(unavailable),
        )

    candidates = [option.provider for option in options]
    default_provider = "codex" if "codex" in candidates else candidates[0]
    return MachineLaunchProjection(
        blocked_by=None,
        providers=tuple(options),
        default_provider=default_provider,
        unavailable_providers=tuple(unavailable),
    )


def _enrolled_device_ids(enrollments: list[dict[str, Any]]) -> dict[str, tuple[datetime | None, str | None]]:
    """Normalize the catalogd enrollment snapshot for directory merging."""

    latest: dict[str, tuple[datetime | None, str | None]] = {}
    for enrollment in enrollments:
        device_id = enrollment.get("device_id")
        if not device_id:
            continue
        key = str(device_id)
        best = _decode_datetime(enrollment.get("last_used_at") or enrollment.get("created_at"))
        name = str(enrollment.get("machine_name") or "").strip() or None
        existing = latest.get(key)
        if existing is None or (best is not None and (existing[0] is None or best > existing[0])):
            latest[key] = (best, name or (existing[1] if existing else None))
        elif name is not None and existing[1] is None:
            latest[key] = (existing[0], name)
    return latest


def build_machines_directory(
    *,
    owner_id: int,
    enrollments: list[dict[str, Any]],
    registry: MachineControlChannelRegistry | None = None,
) -> list[MachineEntry]:
    """Build the per-owner machines list.

    Online machines come from the in-memory control-channel registry and
    include current ``supports[]``. Offline-but-enrolled machines come from
    ``device_tokens`` and are returned with empty ``supports`` — last-known
    capabilities are intentionally not persisted to avoid implying stale
    truth.
    """
    reg = registry or get_machine_control_channel_registry()
    seen: dict[str, MachineEntry] = {}
    enrolled = _enrolled_device_ids(enrollments)

    # Online first — authoritative for supports, engine_build, and
    # last_seen_at.
    for conn_info in reg.list_for_owner(owner_id=owner_id):
        supports = tuple(sorted(conn_info.supports))
        control_operations_by_provider = machine_control_operations_by_provider(
            supports,
            connected=True,
        )
        launch = _launch_projection(
            control_operations_by_provider,
            connected=True,
            provider_readiness=conn_info.provider_readiness,
        )
        entry = MachineEntry(
            device_id=conn_info.device_id,
            machine_name=enrolled.get(conn_info.device_id, (None, None))[1] or conn_info.machine_name or conn_info.device_id,
            online=True,
            control_channel_status=CONTROL_CONNECTED,
            supports=supports,
            control_operations_by_provider=control_operations_by_provider,
            last_seen_at=conn_info.last_seen_at,
            connected_since=conn_info.connected_at,
            engine_build=conn_info.engine_build,
            launch=launch,
            provider_readiness={provider: dict(entry) for provider, entry in conn_info.provider_readiness.items()},
        )
        seen[entry.device_id] = entry

    # Offline enrolled — fill in anything not already present from control
    # channel registry.
    for device_id, (last_used, machine_name) in enrolled.items():
        if device_id in seen:
            continue
        seen[device_id] = MachineEntry(
            device_id=device_id,
            machine_name=machine_name or device_id,
            online=False,
            control_channel_status=CONTROL_DISCONNECTED,
            supports=(),
            control_operations_by_provider={},
            last_seen_at=_as_utc(last_used),
            # Offline: there is no live connection, so no uptime and no
            # readiness. Reporting last-known values here would present stale
            # capability as current truth.
            connected_since=None,
            engine_build=None,
            launch=_launch_projection({}, connected=False),
            provider_readiness={},
        )

    return sorted(
        seen.values(),
        key=lambda m: (
            0 if m.launch.providers else 1 if m.online else 2,
            m.machine_name.lower(),
            m.device_id,
        ),
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _decode_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    if not isinstance(value, str):
        raise ValueError("catalog enrollment datetime must be a string or null")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _as_utc(parsed)
