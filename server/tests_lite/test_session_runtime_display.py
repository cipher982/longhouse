from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace

from zerg.models.agents import SessionRuntimeState
from zerg.services.session_capabilities import SessionCapabilityFlags
from zerg.services.session_capabilities import build_session_capability_display
from zerg.services.session_runtime import SessionRuntimeView
from zerg.services.session_runtime import build_runtime_view
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
        "signal_tier": "none",
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
    assert display.signal_tier == "none"
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
    assert display.signal_tier == "transcript_progress"
    assert display.headline == "Active"
    assert display.heuristic_active is True
    assert display.has_signal is True


def test_stale_progress_source_is_inactive():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            runtime_phase="running",
            runtime_source="progress",
            status="idle",
            confidence="stale",
            display_phase="Recent",
            last_live_at=datetime(2026, 4, 26, 11, 0, tzinfo=timezone.utc),
        ),
        capabilities=_capabilities(),
        ended_at=None,
    )

    assert display.truth_tier == "stale"
    assert display.signal_tier == "transcript_progress"
    assert display.headline == "Inactive"
    assert display.phase_label == "Recent"
    assert display.heuristic_active is False
    assert display.is_idle is True
    assert display.activity_recency == "stale"


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
    assert display.signal_tier == "managed_phase"
    assert display.headline == "Working"
    assert display.detail == "Running Shell"
    assert display.has_signal is True


def test_three_axis_fields_unmanaged_no_renderable_signal():
    # Fallback view with no presence and no last_live_at produces no
    # renderable signal. Without a distinct "last activity" timestamp we
    # cannot tell "never observed" apart from "observed long ago", so the
    # honest answer is "none" for recency. (Phase 4 introduces
    # last_activity_at as a first-class field to separate the two.)
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


def test_three_axis_fields_managed_stale_after_live():
    # Managed session whose freshness window has elapsed should surface
    # as "stale" recency rather than "none".
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            runtime_source="managed_local_transport",
            presence_state="idle",
            display_phase="Idle",
            confidence="stale",
            last_live_at=datetime(2026, 4, 26, 11, 0, tzinfo=timezone.utc),
        ),
        capabilities=_capabilities(managed=True),
        ended_at=None,
    )

    assert display.activity_recency == "stale"


def test_three_axis_fields_managed_hosted_without_transport():
    # MANAGED_HOSTED sessions whose transport is None must still be
    # classified as "managed" on the control_path axis.
    capabilities = SessionCapabilityFlags(
        execution_home=SessionExecutionHome.MANAGED_HOSTED,
        managed_transport=None,
        live_control_available=False,
        host_reattach_available=False,
        reply_to_live_session_available=False,
        can_queue_next_input=False,
        can_steer_active_turn=False,
        home_label=None,
    )
    display = build_session_runtime_display(
        runtime_view=_runtime_view(),
        capabilities=capabilities,
        ended_at=None,
    )

    assert display.control_path == "managed"


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
    assert display.signal_tier == "managed_phase"
    assert display.activity_recency == "live"
    assert display.lifecycle == "open"


def test_unmanaged_online_binding_promotes_signal_tier():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            signal_tier="none",
            runtime_source="fallback",
            status="idle",
            confidence=None,
            display_phase="Idle",
        ),
        capabilities=_capabilities(managed=False),
        ended_at=None,
        binding_host_state="online",
    )

    assert display.control_path == "unmanaged"
    assert display.signal_tier == "unmanaged_binding"
    assert display.host_state == "online"


def test_managed_stale_thinking_without_active_tool_is_stalled():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            runtime_phase="thinking",
            runtime_source="managed_local_transport",
            status="idle",
            presence_state=None,
            presence_tool=None,
            active_tool=None,
            confidence="stale",
            display_phase="Thinking",
            last_live_at=datetime(2026, 4, 26, 11, 0, tzinfo=timezone.utc),
        ),
        capabilities=_capabilities(managed=True),
        ended_at=None,
    )

    assert display.control_path == "managed"
    assert display.is_stalled is True
    assert display.state == "stalled"
    assert display.tone == "stalled"
    assert display.headline == "Stalled"
    assert display.detail == "No recent managed-session progress"
    assert display.phase_label == "Stalled"
    assert display.is_executing is False
    assert display.needs_attention is False
    assert display.activity_recency == "stale"
    assert display.lifecycle == "open"


def test_real_stale_runtime_view_without_presence_is_stalled():
    now = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    state = SessionRuntimeState(
        runtime_key="claude:stalled-runtime-view",
        provider="claude",
        device_id="agent-device",
        phase="thinking",
        phase_source="managed_local_transport",
        active_tool=None,
        phase_started_at=now - timedelta(minutes=10),
        last_runtime_signal_at=now - timedelta(minutes=10),
        last_progress_at=now - timedelta(minutes=10),
        last_live_at=now - timedelta(minutes=10),
        timeline_anchor_at=now - timedelta(minutes=10),
        freshness_expires_at=now - timedelta(minutes=8),
        terminal_state=None,
        runtime_version=3,
    )
    runtime_view = build_runtime_view(
        state=state,
        session=SimpleNamespace(started_at=now - timedelta(hours=1), ended_at=None),
        now=now,
    )

    assert runtime_view.runtime_phase == "thinking"
    assert runtime_view.presence_state is None
    assert runtime_view.confidence == "stale"

    display = build_session_runtime_display(
        runtime_view=runtime_view,
        capabilities=_capabilities(managed=True),
        ended_at=None,
    )

    assert display.is_stalled is True
    assert display.state == "stalled"
    assert display.needs_attention is False


def test_managed_stale_running_with_active_tool_is_not_stalled():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            runtime_phase="running",
            runtime_source="managed_local_transport",
            status="working",
            presence_state="running",
            presence_tool="bash",
            active_tool="bash",
            confidence="stale",
            display_phase="Running bash",
            last_live_at=datetime(2026, 4, 26, 11, 0, tzinfo=timezone.utc),
        ),
        capabilities=_capabilities(managed=True),
        ended_at=None,
    )

    assert display.is_stalled is False
    assert display.state == "running"
    assert display.compact_tool_label == "Shell"
    assert display.is_executing is True


def test_unmanaged_stale_thinking_without_active_tool_is_not_stalled():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            runtime_phase="thinking",
            runtime_source="managed_local_transport",
            status="working",
            presence_state="thinking",
            confidence="stale",
            display_phase="Thinking",
        ),
        capabilities=_capabilities(managed=False),
        ended_at=None,
    )

    assert display.control_path == "unmanaged"
    assert display.is_stalled is False
    assert display.state == "thinking"


def test_unmanaged_needs_user_without_online_host_is_not_actionable():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            runtime_phase="needs_user",
            runtime_source="semantic",
            status="active",
            presence_state="needs_user",
            confidence="live",
            display_phase="Needs you",
        ),
        capabilities=_capabilities(managed=False),
        ended_at=None,
    )

    assert display.control_path == "unmanaged"
    assert display.host_state == "unknown"
    assert display.state is None
    assert display.phase_label == "Recent"
    assert display.headline == "Inactive"
    assert display.is_idle is True
    assert display.tone == "inactive"
    assert display.needs_attention is False


def test_unmanaged_needs_user_with_online_host_stays_actionable():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            runtime_phase="needs_user",
            runtime_source="semantic",
            status="active",
            presence_state="needs_user",
            confidence="live",
            display_phase="Needs you",
        ),
        capabilities=_capabilities(managed=False),
        ended_at=None,
        binding_host_state="online",
    )

    assert display.control_path == "unmanaged"
    assert display.host_state == "online"
    assert display.state == "needs_user"
    assert display.phase_label == "Needs you"
    assert display.headline == "Active"
    assert display.needs_attention is True


def test_managed_stale_needs_user_without_presence_is_not_actionable():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            runtime_phase="needs_user",
            runtime_source="managed_local_transport",
            status="idle",
            presence_state=None,
            confidence="stale",
            display_phase="Recent",
            last_live_at=datetime(2026, 4, 26, 11, 0, tzinfo=timezone.utc),
        ),
        capabilities=_capabilities(managed=True),
        ended_at=None,
    )

    assert display.control_path == "managed"
    assert display.state is None
    assert display.phase_label == "Disconnected"
    assert display.headline == "Not connected"
    assert display.heuristic_active is False
    assert display.needs_attention is False
    assert display.tone == "inactive"


def test_unmanaged_stale_needs_user_phase_without_presence_is_recent():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            runtime_phase="needs_user",
            runtime_source="semantic",
            status="idle",
            presence_state=None,
            confidence="stale",
            display_phase="Recent",
            last_live_at=datetime(2026, 4, 26, 11, 0, tzinfo=timezone.utc),
        ),
        capabilities=_capabilities(managed=False),
        ended_at=None,
        binding_host_state="online",
    )

    assert display.control_path == "unmanaged"
    assert display.state is None
    assert display.phase_label == "Recent"
    assert display.headline == "Inactive"
    assert display.heuristic_active is False
    assert display.needs_attention is False


def test_managed_live_thinking_without_active_tool_is_not_stalled():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            runtime_phase="thinking",
            runtime_source="managed_local_transport",
            status="working",
            presence_state="thinking",
            confidence="live",
            display_phase="Thinking",
        ),
        capabilities=_capabilities(managed=True),
        ended_at=None,
    )

    assert display.is_stalled is False
    assert display.state == "thinking"
    assert display.activity_recency == "live"


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


def test_three_axis_fields_closed_with_process_gone_terminal():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            runtime_phase="finished",
            terminal_state="process_gone",
            status="completed",
            display_phase="Completed",
        ),
        capabilities=_capabilities(managed=True),
        ended_at=None,
    )

    assert display.control_path == "managed"
    assert display.lifecycle == "closed"
    assert display.terminal_reason == "process_gone"


def test_process_gone_closure_suppresses_stale_attention_copy():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            runtime_phase="needs_user",
            runtime_source="semantic",
            status="active",
            presence_state="needs_user",
            confidence="live",
            display_phase="Needs you",
        ),
        capabilities=_capabilities(),
        ended_at=None,
        binding_terminal_reason="process_gone",
    )

    assert display.lifecycle == "closed"
    assert display.terminal_reason == "process_gone"
    assert display.state is None
    assert display.headline == "Completed"
    assert display.phase_label == "Completed"
    assert display.needs_attention is False
    assert display.is_idle is True
    assert display.tone == "inactive"


def test_host_expired_closure_suppresses_stale_attention_copy_without_process_gone():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            runtime_phase="needs_user",
            runtime_source="semantic",
            status="active",
            presence_state="needs_user",
            confidence="live",
            display_phase="Needs you",
        ),
        capabilities=_capabilities(),
        ended_at=None,
        binding_host_state="offline",
        binding_terminal_reason="host_expired",
    )

    assert display.lifecycle == "closed"
    assert display.host_state == "offline"
    assert display.terminal_reason == "host_expired"
    assert display.state is None
    assert display.needs_attention is False
    assert display.is_idle is True


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


def test_capability_display_uses_action_label_without_host():
    flags = SessionCapabilityFlags(
        execution_home=SessionExecutionHome.MANAGED_LOCAL,
        managed_transport=None,
        live_control_available=True,
        host_reattach_available=True,
        reply_to_live_session_available=True,
        can_queue_next_input=True,
        can_steer_active_turn=False,
        home_label=None,
    )

    display = build_session_capability_display(flags)

    assert display.label == "Send"
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


def test_capability_display_names_imported_sessions_read_only():
    display = build_session_capability_display(_capabilities())

    assert display.label == "Read only"
    assert display.tone == "neutral"
