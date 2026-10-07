"""Served machine status: one vocabulary for the web, iOS and the launch picker."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

from zerg.schemas.machines import MachineActivity
from zerg.schemas.machines import MachineHistorySync
from zerg.schemas.machines import MachineSync
from zerg.services.machine_status import directory_entry_response
from zerg.services.machine_status import machine_status


def _machine(**launch_overrides):
    online = launch_overrides.pop("online", True)
    launch = {
        "providers": [{"provider": "claude"}],
        "default_provider": "claude",
        "blocked_by": None,
        "unavailable_providers": [],
        **launch_overrides,
    }
    return directory_entry_response(
        {
            "device_id": "cinder",
            "machine_name": "cinder",
            "online": online,
            "control_channel_status": "connected" if online else "disconnected",
            "launch": launch,
        }
    )


def _offline():
    return _machine(online=False, providers=[], blocked_by="control_down")


def _activity(live: int = 0, started: int = 0) -> MachineActivity:
    return MachineActivity(sessions_started=started, daily=[], top_projects=[], live_count=live, live_sessions=[])


def _sync(status: str, *, stale: bool) -> MachineSync:
    return MachineSync(
        reported_at=datetime(2026, 10, 7, tzinfo=UTC),
        report_age_seconds=10,
        stale=stale,
        status=status,
        status_summary=status,
        history=MachineHistorySync(state="current"),
    )


def test_live_sessions_come_first_and_still_carry_a_sign_in_hint():
    machine = _machine(unavailable_providers=[{"provider": "codex", "reason": "not_authenticated", "remediation": None}])
    status = machine_status(machine, activity=_activity(live=9, started=254))
    assert (status.tone, status.label, status.hint) == ("live", "9 live", "Sign in to Codex on cinder")


def test_a_signed_out_provider_asks_for_sign_in():
    machine = _machine(unavailable_providers=[{"provider": "codex", "reason": "not_authenticated", "remediation": "Run codex login"}])
    status = machine_status(machine, activity=_activity())
    assert (status.tone, status.label, status.hint) == ("attention", "Codex signed out", "Run codex login")


def test_a_cli_the_machine_never_had_is_not_a_nag_while_others_run():
    machine = _machine(unavailable_providers=[{"provider": "antigravity", "reason": "cli_missing", "remediation": None}])
    status = machine_status(machine, activity=_activity())
    assert (status.tone, status.label, status.hint) == ("idle", "Online, idle", None)


def test_a_missing_cli_is_named_when_nothing_else_can_start_a_session():
    machine = _machine(
        providers=[],
        blocked_by="providers_not_ready",
        unavailable_providers=[{"provider": "claude", "reason": "cli_missing", "remediation": None}],
    )
    assert machine_status(machine, activity=_activity()).label == "Claude not installed"


def test_red_is_reserved_for_faults_that_need_repair():
    assert machine_status(_machine(providers=[], blocked_by="auth_failed")).tone == "fault"
    assert machine_status(_machine(), sync=_sync("broken", stale=False)).tone == "fault"
    # An old broken report on a machine that has since gone quiet is not a current fault.
    assert machine_status(_offline(), sync=_sync("broken", stale=True)).tone == "off"


def test_ordinary_offline_is_gray_and_folds_away_only_when_nothing_happened():
    folded = machine_status(_offline(), activity=_activity(started=0))
    assert (folded.tone, folded.label, folded.quiet) == ("off", "Offline", True)
    assert machine_status(_offline(), activity=_activity(started=8)).quiet is False


def test_a_disconnected_machine_that_still_ships_reads_sync_only():
    status = machine_status(_offline(), sync=_sync("healthy", stale=False))
    assert (status.tone, status.label, status.quiet) == ("quiet", "Sync only", False)


def test_the_directory_alone_never_claims_idle():
    # The launch picker has no activity read: connected reads Ready, never idle.
    assert (_machine().status.tone, _machine().status.label) == ("live", "Ready")
    no_providers = _machine(providers=[], blocked_by=None)
    assert no_providers.status.label == "Online"
    assert _offline().status.label == "Offline"
    assert _offline().status.quiet is False
