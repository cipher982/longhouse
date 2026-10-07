"""One vocabulary for a machine's state, decided once for every client.

The Machines page, the machine page, the iOS Machines screen and the launch
picker all show the same words and tone. They used to derive them separately
and disagreed (iOS said "Ready" where the web said "Online, idle"). The
contract is control-plane/docs/specs/machines-surface.md.

Three independent axes feed it and none is inferred from another: the live
control connection (`machine.online`, `launch`), what the Timeline shows as
live on that machine (`activity.live_count`), and whether the Machine Agent is
shipping (`sync`). Ordinary offline is gray, never red: red is reserved for a
fault someone has to repair.
"""

from __future__ import annotations

from typing import Any

from zerg.generated.provider_brands import provider_display_name
from zerg.schemas.machines import MachineActivity
from zerg.schemas.machines import MachineDirectoryEntry
from zerg.schemas.machines import MachineStatus
from zerg.schemas.machines import MachineSync

_REPAIR_REASONS = frozenset({"auth_failed", "runtime_unreachable"})
_REPAIR_HINT = "Run longhouse local-health on this machine to inspect the fault"


def _sign_in_need(machine: MachineDirectoryEntry) -> tuple[str, str] | None:
    unavailable = machine.launch.unavailable_providers
    signed_out = [item for item in unavailable if item.reason == "not_authenticated"]
    # A missing CLI only matters when it leaves nothing to start sessions with;
    # otherwise it is an agent this person simply does not use.
    missing = [item for item in unavailable if item.reason == "cli_missing"] if not machine.launch.providers else []
    actionable = signed_out or missing
    if not actionable:
        return None
    verb = "signed out" if signed_out else "not installed"
    if len(actionable) == 1:
        item = actionable[0]
        name = provider_display_name(item.provider, fallback="Unknown")
        fallback = f"Sign in to {name} on {machine.machine_name}" if signed_out else f"Install {name} on {machine.machine_name}"
        return f"{name} {verb}", item.remediation or fallback
    names = sorted(provider_display_name(item.provider, fallback="Unknown") for item in actionable)
    action = "Sign in to" if signed_out else "Install"
    return f"{len(actionable)} agents {verb}", f"{action} {' and '.join(names)} on {machine.machine_name}"


def machine_status(
    machine: MachineDirectoryEntry,
    *,
    activity: MachineActivity | None = None,
    sync: MachineSync | None = None,
) -> MachineStatus:
    """The status line for one machine.

    `activity` is None for a directory-only read (the launch picker). Without it
    a connected machine reads "Ready" or "Online", never idle: nothing was
    measured that could say it is idle.
    """

    live = activity.live_count if activity is not None else 0
    started = activity.sessions_started if activity is not None else 0
    blocked = machine.launch.blocked_by
    sync_fresh = sync is not None and not sync.stale

    # Repair comes first: a live session does not hide a broken shipping or
    # control path someone has to fix.
    if blocked in _REPAIR_REASONS or (sync_fresh and sync.status == "broken"):
        return MachineStatus(tone="fault", label="Needs repair", hint=_REPAIR_HINT)
    if machine.online:
        need = _sign_in_need(machine)
        if live > 0:
            return MachineStatus(tone="live", label=f"{live} live", hint=need[1] if need else None)
        if need is not None:
            return MachineStatus(tone="attention", label=need[0], hint=need[1])
        if blocked == "engine_too_old":
            return MachineStatus(tone="attention", label="Update required", hint="Update Longhouse on this machine")
        if blocked == "no_launch_support":
            return MachineStatus(tone="attention", label="Can't start sessions")
        if activity is None:
            return MachineStatus(tone="live", label="Ready" if machine.launch.providers else "Online")
        return MachineStatus(tone="idle", label="Online, idle")
    if live > 0:
        return MachineStatus(tone="live", label=f"{live} live")
    if sync_fresh:
        return MachineStatus(tone="quiet", label="Sync only")
    return MachineStatus(tone="off", label="Offline", quiet=activity is not None and started == 0)


def directory_entry_response(raw: dict[str, Any]) -> MachineDirectoryEntry:
    """Build a served directory entry with its directory-only status."""

    entry = MachineDirectoryEntry.model_validate({**raw, "status": {"tone": "off", "label": "Offline"}})
    return entry.model_copy(update={"status": machine_status(entry)})
