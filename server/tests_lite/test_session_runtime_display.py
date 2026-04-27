from datetime import datetime
from datetime import timezone

from zerg.services.session_capabilities import SessionCapabilityFlags
from zerg.services.session_capabilities import build_session_capability_display
from zerg.services.session_runtime import SessionRuntimeView
from zerg.services.session_runtime_display import build_session_runtime_display
from zerg.session_execution_home import SessionExecutionHome


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
    now = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    values = {
        "runtime_phase": "idle",
        "phase_started_at": now,
        "last_progress_at": now,
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
        "timeline_anchor_at": now,
    }
    values.update(overrides)
    return SessionRuntimeView(**values)


def test_fallback_idle_has_no_renderable_runtime_signal():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(),
        capabilities=_capabilities(),
        ended_at=None,
    )

    assert display.truth_tier == "stale"
    assert display.headline == "Inactive"
    assert display.has_signal is False


def test_inferred_progress_has_renderable_runtime_signal():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            runtime_source="progress",
            status="active",
            confidence="inferred",
            display_phase="Recent progress",
        ),
        capabilities=_capabilities(),
        ended_at=None,
    )

    assert display.truth_tier == "inferred"
    assert display.headline == "Active"
    assert display.heuristic_active is True
    assert display.has_signal is True


def test_managed_running_has_renderable_runtime_signal():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            runtime_phase="running",
            runtime_source="managed_local_transport",
            status="working",
            presence_state="running",
            presence_tool="bash",
            active_tool="bash",
            confidence="live",
            display_phase="Running bash",
        ),
        capabilities=_capabilities(managed=True),
        ended_at=None,
    )

    assert display.truth_tier == "managed-local"
    assert display.headline == "Working"
    assert display.detail == "Running Shell"
    assert display.has_signal is True


def test_three_axis_fields_unmanaged_idle():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(),
        capabilities=_capabilities(),
        ended_at=None,
    )

    assert display.control_path == "unmanaged"
    assert display.activity_recency == "none"
    assert display.lifecycle == "open"
    assert display.host_state == "unknown"
    assert display.terminal_reason is None


def test_three_axis_fields_managed_live_running():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            runtime_phase="running",
            runtime_source="managed_local_transport",
            status="working",
            presence_state="running",
            presence_tool="bash",
            active_tool="bash",
            confidence="live",
            display_phase="Running bash",
        ),
        capabilities=_capabilities(managed=True),
        ended_at=None,
    )

    assert display.control_path == "managed"
    assert display.activity_recency == "live"
    assert display.lifecycle == "open"


def test_three_axis_fields_closed_with_explicit_terminal():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            runtime_phase="finished",
            terminal_state="session_ended",
            status="completed",
            display_phase="Completed",
        ),
        capabilities=_capabilities(),
        ended_at=None,
    )

    assert display.lifecycle == "closed"
    assert display.terminal_reason == "provider_signal"


def test_three_axis_fields_ended_at_without_terminal_stays_open():
    # Phase 1 contract: ended_at alone no longer implies closure. Phase 2
    # three-axis projection must agree.
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            runtime_source="progress",
            status="active",
            confidence="inferred",
            display_phase="Recent progress",
        ),
        capabilities=_capabilities(),
        ended_at=datetime(2026, 4, 26, 11, 30, tzinfo=timezone.utc),
    )

    assert display.lifecycle == "open"
    assert display.terminal_reason is None


def test_capability_display_names_live_control_host():
    display = build_session_capability_display(
        _capabilities(managed=True),
        host_label="On this Mac",
    )

    assert display.label == "Live on this Mac"
    assert display.tone == "success"


def test_capability_display_names_control_offline():
    flags = SessionCapabilityFlags(
        execution_home=SessionExecutionHome.MANAGED_LOCAL,
        managed_transport=None,
        live_control_available=False,
        host_reattach_available=True,
        reply_to_live_session_available=False,
        can_queue_next_input=False,
        can_steer_active_turn=False,
        home_label="On this Mac",
    )

    display = build_session_capability_display(flags)

    assert display.label == "Control offline"
    assert display.tone == "warning"


def test_capability_display_names_imported_sessions_search_only():
    display = build_session_capability_display(_capabilities())

    assert display.label == "Search only"
    assert display.tone == "neutral"
