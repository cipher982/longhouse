from dataclasses import asdict
from datetime import datetime
from datetime import timezone

import pytest

from zerg.services.session_capabilities import SessionCapabilityFlags
from zerg.services.session_runtime import SessionRuntimeView
from zerg.services.session_runtime_display import build_session_runtime_display
from zerg.services.session_timeline_card import build_timeline_card_presentation
from zerg.session_execution_home import SessionExecutionHome


NOW = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
SEEN_AT = datetime(2026, 4, 26, 11, 0, tzinfo=timezone.utc)


def _capabilities(*, managed: bool = False) -> SessionCapabilityFlags:
    return SessionCapabilityFlags(
        execution_home=SessionExecutionHome.MANAGED_LOCAL if managed else SessionExecutionHome.LEGACY,
        managed_transport=None,
        live_control_available=managed,
        host_reattach_available=managed,
        reply_to_live_session_available=managed,
        can_queue_next_input=managed,
        can_steer_active_turn=False,
        home_label="On this Mac" if managed else None,
    )


def _runtime_view(**overrides) -> SessionRuntimeView:
    values = {
        "signal_tier": "none",
        "runtime_phase": "idle",
        "phase_started_at": NOW,
        "last_progress_at": NOW,
        "runtime_source": "fallback",
        "terminal_state": None,
        "runtime_version": 0,
        "status": "idle",
        "presence_state": None,
        "presence_tool": None,
        "presence_updated_at": None,
        "last_live_at": None,
        "display_phase": "Idle",
        "active_tool": None,
        "confidence": None,
        "timeline_anchor_at": NOW,
    }
    values.update(overrides)
    return SessionRuntimeView(**values)


def _timeline_card(
    *,
    managed: bool,
    view_overrides: dict,
    last_activity_at: datetime | None = NOW,
    binding_host_state: str | None = None,
    binding_terminal_reason: str | None = None,
) -> dict:
    runtime_view = _runtime_view(**view_overrides)
    capabilities = _capabilities(managed=managed)
    runtime_display = build_session_runtime_display(
        runtime_view=runtime_view,
        capabilities=capabilities,
        ended_at=None,
        binding_host_state=binding_host_state,
        binding_terminal_reason=binding_terminal_reason,
    )
    card = build_timeline_card_presentation(
        runtime_display=runtime_display,
        last_live_at=runtime_view.last_live_at,
        last_activity_at=last_activity_at,
        managed_fallback=managed,
    )
    return asdict(card)


@pytest.mark.parametrize(
    "case",
    [
        {
            "id": "managed-running",
            "managed": True,
            "view": {
                "signal_tier": "managed_phase",
                "runtime_phase": "running",
                "runtime_source": "managed_local_transport",
                "status": "working",
                "presence_state": "running",
                "presence_tool": "bash",
                "active_tool": "bash",
                "confidence": "live",
                "display_phase": "Running bash",
            },
            "expect": {
                "ownership": {"label": "Managed", "tone": "neutral"},
                "status": {"label": "Working", "tone": "running", "seen_at": None},
                "border_tone": "running",
            },
        },
        {
            "id": "managed-blocked",
            "managed": True,
            "view": {
                "signal_tier": "managed_phase",
                "runtime_phase": "blocked",
                "runtime_source": "managed_local_transport",
                "status": "active",
                "presence_state": "blocked",
                "presence_tool": "bash",
                "active_tool": "bash",
                "confidence": "live",
                "display_phase": "Blocked on bash",
            },
            "expect": {
                "ownership": {"label": "Managed", "tone": "neutral"},
                "status": {"label": "Needs permission", "tone": "blocked", "seen_at": None},
                "border_tone": "blocked",
            },
        },
        {
            "id": "managed-ready",
            "managed": True,
            "view": {
                "signal_tier": "managed_phase",
                "runtime_phase": "needs_user",
                "runtime_source": "managed_local_transport",
                "status": "idle",
                "presence_state": "needs_user",
                "confidence": "live",
                "display_phase": "Ready",
            },
            "expect": {
                "ownership": {"label": "Managed", "tone": "neutral"},
                "status": {"label": "Ready", "tone": "idle", "seen_at": None},
                "border_tone": "idle",
            },
        },
        {
            "id": "managed-stalled",
            "managed": True,
            "view": {
                "signal_tier": "managed_phase",
                "runtime_phase": "running",
                "runtime_source": "managed_local_transport",
                "status": "idle",
                "confidence": "stale",
                "display_phase": "Recent",
                "last_live_at": SEEN_AT,
            },
            "expect": {
                "ownership": {"label": "Managed", "tone": "neutral"},
                "status": {"label": "Stalled", "tone": "stalled", "seen_at": SEEN_AT},
                "border_tone": "stalled",
            },
        },
        {
            "id": "managed-recent-progress",
            "managed": True,
            "view": {
                "signal_tier": "transcript_progress",
                "runtime_source": "progress",
                "status": "active",
                "confidence": "inferred",
                "display_phase": "Recent progress",
            },
            "expect": {
                "ownership": {"label": "Managed", "tone": "neutral"},
                "status": {"label": "Recent activity", "tone": "inferred", "seen_at": None},
                "border_tone": "inferred",
            },
        },
        {
            "id": "managed-closed",
            "managed": True,
            "view": {
                "signal_tier": "managed_phase",
                "runtime_phase": "finished",
                "terminal_state": "session_ended",
                "status": "completed",
                "display_phase": "Completed",
            },
            "expect": {
                "ownership": {"label": "Managed", "tone": "neutral"},
                "status": {"label": "Closed", "tone": "closed", "seen_at": None},
                "border_tone": "closed",
            },
        },
        {
            "id": "unmanaged-online-binding",
            "managed": False,
            "binding_host_state": "online",
            "view": {"signal_tier": "none", "runtime_source": "fallback", "display_phase": "Idle"},
            "expect": {
                "ownership": {"label": "Unmanaged", "tone": "neutral"},
                "status": {"label": "Active", "tone": "active", "seen_at": None},
                "border_tone": "active",
            },
        },
        {
            "id": "unmanaged-live",
            "managed": False,
            "view": {
                "signal_tier": "managed_phase",
                "runtime_phase": "running",
                "runtime_source": "semantic",
                "status": "working",
                "presence_state": "running",
                "confidence": "live",
                "display_phase": "Running",
            },
            "expect": {
                "ownership": {"label": "Unmanaged", "tone": "neutral"},
                "status": {"label": "Active", "tone": "active", "seen_at": None},
                "border_tone": "active",
            },
        },
        {
            "id": "unmanaged-recent-progress",
            "managed": False,
            "view": {
                "signal_tier": "transcript_progress",
                "runtime_source": "progress",
                "status": "active",
                "confidence": "inferred",
                "display_phase": "Recent progress",
            },
            "expect": {
                "ownership": {"label": "Unmanaged", "tone": "neutral"},
                "status": {"label": "Recent activity", "tone": "inferred", "seen_at": None},
                "border_tone": "inferred",
            },
        },
        {
            "id": "unmanaged-stale",
            "managed": False,
            "view": {
                "signal_tier": "transcript_progress",
                "runtime_source": "progress",
                "status": "idle",
                "confidence": "stale",
                "display_phase": "Recent",
                "last_live_at": SEEN_AT,
            },
            "expect": {
                "ownership": {"label": "Unmanaged", "tone": "neutral"},
                "status": {"label": "Stale", "tone": "inactive", "seen_at": SEEN_AT},
                "border_tone": "inactive",
            },
        },
        {
            "id": "unmanaged-no-signal",
            "managed": False,
            "view": {"signal_tier": "none", "runtime_source": "fallback", "display_phase": "Idle"},
            "expect": {
                "ownership": {"label": "Unmanaged", "tone": "neutral"},
                "status": {"label": "Unknown", "tone": "inactive", "seen_at": None},
                "border_tone": "inactive",
            },
        },
        {
            "id": "unmanaged-closed",
            "managed": False,
            "binding_terminal_reason": "process_gone",
            "view": {"signal_tier": "none", "runtime_source": "fallback", "display_phase": "Idle"},
            "expect": {
                "ownership": {"label": "Unmanaged", "tone": "neutral"},
                "status": {"label": "Closed", "tone": "closed", "seen_at": None},
                "border_tone": "closed",
            },
        },
    ],
    ids=lambda case: case["id"],
)
def test_timeline_card_contract_matrix(case):
    assert _timeline_card(
        managed=case["managed"],
        view_overrides=case["view"],
        binding_host_state=case.get("binding_host_state"),
        binding_terminal_reason=case.get("binding_terminal_reason"),
    ) == case["expect"]


def test_timeline_card_uses_last_activity_when_stale_live_signal_missing():
    last_activity_at = datetime(2026, 4, 25, 20, 0, tzinfo=timezone.utc)

    card = _timeline_card(
        managed=False,
        view_overrides={
            "signal_tier": "transcript_progress",
            "runtime_source": "progress",
            "status": "idle",
            "confidence": "stale",
            "display_phase": "Recent",
            "last_live_at": None,
        },
        last_activity_at=last_activity_at,
    )

    assert card["status"] == {"label": "Stale", "tone": "inactive", "seen_at": last_activity_at}


def test_timeline_card_marks_closed_when_runtime_display_is_suppressed_for_terminal_signal():
    card = build_timeline_card_presentation(
        runtime_display=None,
        last_live_at=None,
        last_activity_at=SEEN_AT,
        managed_fallback=False,
        terminal_reason="provider_signal",
    )

    assert asdict(card) == {
        "ownership": {"label": "Unmanaged", "tone": "neutral"},
        "status": {"label": "Closed", "tone": "closed", "seen_at": None},
        "border_tone": "closed",
    }


def test_timeline_card_keeps_unknown_when_no_runtime_or_terminal_truth_exists():
    card = build_timeline_card_presentation(
        runtime_display=None,
        last_live_at=None,
        last_activity_at=SEEN_AT,
        managed_fallback=True,
    )

    assert asdict(card) == {
        "ownership": {"label": "Managed", "tone": "neutral"},
        "status": {"label": "Unknown", "tone": "inactive", "seen_at": None},
        "border_tone": "inactive",
    }
