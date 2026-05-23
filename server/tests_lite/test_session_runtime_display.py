from datetime import datetime
from datetime import timedelta
from datetime import timezone
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.models.agents import SessionRuntimeState
from zerg.services.agents.kernel_capabilities import KernelSessionCapabilities
from zerg.services.session_capabilities import build_session_capability_display
from zerg.services.session_runtime import SessionRuntimeView
from zerg.services.session_runtime import build_runtime_view
from zerg.services.session_runtime_display import build_session_runtime_display
from zerg.session_execution_home import SessionExecutionHome  # noqa: F401  # legacy import kept for downstream usage


def _make_kernel_capabilities(
    *,
    live: bool = False,
    reattach: bool = False,
    control_plane: str | None = None,
) -> KernelSessionCapabilities:
    return KernelSessionCapabilities(
        session_id="00000000-0000-0000-0000-000000000000",
        thread_id=None,
        run_id=None,
        connection_id=None,
        control_plane=control_plane,
        connection_state="attached" if live else ("detached" if reattach else None),
        control_label="live" if live else ("reattach" if reattach else "imported"),
        live_control_available=live,
        host_reattach_available=reattach or live,
        observe_only=False,
        search_only=not (live or reattach),
        can_send_input=live,
        can_interrupt=live,
        can_terminate=live,
        can_tail_output=live,
        can_resume=live or reattach,
        staleness_reason=None if live else ("connection_released" if reattach else "imported_only"),
    )


def _capabilities(*, managed: bool = False) -> KernelSessionCapabilities:
    return _make_kernel_capabilities(
        live=managed,
        reattach=managed,
        control_plane="claude_channel_bridge" if managed else None,
    )


def _runtime_view(**overrides) -> SessionRuntimeView:
    now = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    values = {
        "signal_tier": "none",
        "runtime_phase": None,
        "phase_started_at": now,
        "last_progress_at": now,
        "runtime_source": "fallback",
        "terminal_state": None,
        "terminal_reason": None,
        "terminal_source": None,
        "runtime_version": 0,
        "status": "idle",
        "presence_state": None,
        "presence_tool": None,
        "presence_updated_at": None,
        "last_live_at": None,
        "display_phase": "Recent",
        "active_tool": None,
        "confidence": None,
        "timeline_anchor_at": now,
    }
    values.update(overrides)
    return SessionRuntimeView(**values)


@pytest.mark.parametrize(
    "case",
    [
        {
            "id": "managed-running-live",
            "managed": True,
            "view": {
                "signal_tier": "phase_signal",
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
                "control_path": "managed",
                "signal_tier": "phase_signal",
                "lifecycle": "open",
                "state": "running",
                "tone": "running",
                "headline": "Working",
                "phase_label": "Using Shell",
                "activity_recency": "live",
                "needs_attention": False,
            },
        },
        {
            "id": "managed-blocked-live",
            "managed": True,
            "view": {
                "signal_tier": "phase_signal",
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
                "control_path": "managed",
                "signal_tier": "phase_signal",
                "lifecycle": "open",
                "state": "blocked",
                "tone": "blocked",
                "headline": "Needs permission",
                "phase_label": "Blocked on Shell",
                "activity_recency": "live",
                "needs_attention": True,
            },
        },
        {
            "id": "managed-needs-user-live",
            "managed": True,
            "view": {
                "signal_tier": "phase_signal",
                "runtime_phase": "needs_user",
                "runtime_source": "managed_local_transport",
                "status": "idle",
                "presence_state": "needs_user",
                "confidence": "live",
                "display_phase": "Idle",
            },
            "expect": {
                "control_path": "managed",
                "signal_tier": "phase_signal",
                "lifecycle": "open",
                "state": "needs_user",
                "tone": "idle",
                "headline": "Idle",
                "phase_label": "Idle",
                "activity_recency": "live",
                "needs_attention": False,
            },
        },
        {
            "id": "managed-stale-running",
            "managed": True,
            "view": {
                "signal_tier": "phase_signal",
                "runtime_phase": "running",
                "runtime_source": "managed_local_transport",
                "status": "idle",
                "presence_state": None,
                "confidence": "stale",
                "display_phase": "",
                "last_live_at": datetime(2026, 4, 26, 11, 0, tzinfo=timezone.utc),
            },
            "expect": {
                "control_path": "managed",
                "signal_tier": "phase_signal",
                "lifecycle": "open",
                "state": None,
                "tone": "inactive",
                "headline": "Not connected",
                "phase_label": "Inactive",
                "activity_recency": "stale",
                "needs_attention": False,
            },
        },
        {
            "id": "managed-disconnected",
            "managed": True,
            "view": {"signal_tier": "none", "runtime_source": "fallback", "display_phase": "Recent"},
            "expect": {
                "control_path": "managed",
                "signal_tier": "none",
                "lifecycle": "open",
                "state": None,
                "tone": "inactive",
                "headline": "Not connected",
                "phase_label": "Recent",
                "activity_recency": "none",
                "needs_attention": False,
            },
        },
        {
            "id": "managed-terminal",
            "managed": True,
            "view": {
                "signal_tier": "phase_signal",
                "runtime_phase": "finished",
                "terminal_state": "session_ended",
                "status": "completed",
                "display_phase": "Completed",
            },
            "expect": {
                "control_path": "managed",
                "signal_tier": "phase_signal",
                "lifecycle": "closed",
                "state": None,
                "tone": "inactive",
                "headline": "Closed",
                "phase_label": "Closed",
                "activity_recency": "none",
                "needs_attention": False,
            },
        },
        {
            "id": "unmanaged-binding-online",
            "managed": False,
            "binding_host_state": "online",
            "view": {"signal_tier": "none", "runtime_source": "fallback", "display_phase": "Recent"},
            "expect": {
                "control_path": "unmanaged",
                "signal_tier": "process_binding",
                "lifecycle": "open",
                "state": None,
                "tone": "active",
                "headline": "Active",
                "phase_label": "Process running",
                "activity_recency": "live",
                "needs_attention": False,
            },
        },
        {
            "id": "unmanaged-binding-process-gone",
            "managed": False,
            "binding_terminal_reason": "process_gone",
            "view": {"signal_tier": "none", "runtime_source": "fallback", "display_phase": "Recent"},
            "expect": {
                "control_path": "unmanaged",
                "signal_tier": "process_binding",
                "lifecycle": "closed",
                "state": None,
                "tone": "inactive",
                "headline": "Closed",
                "phase_label": "Closed",
                "activity_recency": "none",
                "needs_attention": False,
            },
        },
        {
            "id": "unmanaged-transcript-progress",
            "managed": False,
            "view": {
                "signal_tier": "transcript_progress",
                "runtime_source": "progress",
                "status": "idle",
                "confidence": "stale",
                "display_phase": "Inactive",
            },
            "expect": {
                "control_path": "unmanaged",
                "signal_tier": "transcript_progress",
                "lifecycle": "open",
                "state": None,
                "tone": "inactive",
                "headline": "Inactive",
                "phase_label": "Inactive",
                "activity_recency": "stale",
                "needs_attention": False,
            },
        },
        {
            "id": "unmanaged-stale-progress",
            "managed": False,
            "view": {
                "signal_tier": "transcript_progress",
                "runtime_source": "progress",
                "status": "idle",
                "confidence": "stale",
                "display_phase": "Recent",
                "last_live_at": datetime(2026, 4, 26, 11, 0, tzinfo=timezone.utc),
            },
            "expect": {
                "control_path": "unmanaged",
                "signal_tier": "transcript_progress",
                "lifecycle": "open",
                "state": None,
                "tone": "inactive",
                "headline": "Inactive",
                "phase_label": "Recent",
                "activity_recency": "stale",
                "needs_attention": False,
            },
        },
        {
            "id": "unmanaged-terminal",
            "managed": False,
            "view": {
                "signal_tier": "transcript_progress",
                "runtime_phase": "finished",
                "terminal_state": "session_ended",
                "status": "completed",
                "display_phase": "Completed",
            },
            "expect": {
                "control_path": "unmanaged",
                "signal_tier": "transcript_progress",
                "lifecycle": "closed",
                "state": None,
                "tone": "inactive",
                "headline": "Closed",
                "phase_label": "Closed",
                "activity_recency": "none",
                "needs_attention": False,
            },
        },
        {
            "id": "unmanaged-no-signal",
            "managed": False,
            "view": {"signal_tier": "none", "runtime_source": "fallback", "display_phase": "Recent"},
            "expect": {
                "control_path": "unmanaged",
                "signal_tier": "none",
                "lifecycle": "open",
                "state": None,
                "tone": "inactive",
                "headline": "Inactive",
                "phase_label": "Recent",
                "activity_recency": "none",
                "needs_attention": False,
            },
        },
    ],
    ids=lambda case: case["id"],
)
def test_session_runtime_display_matrix(case):
    display = build_session_runtime_display(
        runtime_view=_runtime_view(**case["view"]),
        capabilities=_capabilities(managed=case["managed"]),
        ended_at=None,
        binding_host_state=case.get("binding_host_state"),
        binding_terminal_reason=case.get("binding_terminal_reason"),
    )

    for field, expected in case["expect"].items():
        assert getattr(display, field) == expected


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


def test_transcript_progress_does_not_create_active_display():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            signal_tier="transcript_progress",
            runtime_source="progress",
            status="idle",
            confidence="stale",
            display_phase="Inactive",
        ),
        capabilities=_capabilities(),
        ended_at=None,
    )

    assert display.truth_tier == "stale"
    assert display.signal_tier == "transcript_progress"
    assert display.headline == "Inactive"
    assert display.has_signal is True


def test_managed_idle_after_user_prompt_displays_transcript_sync():
    now = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            signal_tier="phase_signal",
            runtime_phase="idle",
            runtime_source="managed_local_transport",
            status="idle",
            presence_state="idle",
            presence_updated_at=now,
            last_live_at=now,
            confidence="live",
            display_phase="Idle",
        ),
        capabilities=_capabilities(managed=True),
        ended_at=None,
        last_activity_at=now - timedelta(milliseconds=500),
        user_messages=2,
        assistant_messages=1,
        has_visible_transcript_preview=False,
        now=now,
    )

    assert display.state == "syncing_transcript"
    assert display.headline == "Syncing"
    assert display.detail == "Waiting for transcript"
    assert display.phase_label == "Syncing transcript"
    assert display.tone == "active"
    assert display.is_idle is False


def test_managed_idle_after_pending_turn_displays_transcript_sync_without_archive_counts():
    now = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            signal_tier="phase_signal",
            runtime_phase="idle",
            runtime_source="managed_local_transport",
            status="idle",
            presence_state="idle",
            presence_updated_at=now,
            last_live_at=now,
            confidence="live",
            display_phase="Idle",
        ),
        capabilities=_capabilities(managed=True),
        ended_at=None,
        last_activity_at=now - timedelta(seconds=20),
        user_messages=1,
        assistant_messages=1,
        has_visible_transcript_preview=False,
        has_pending_response_turn=True,
        now=now,
    )

    assert display.state == "syncing_transcript"
    assert display.headline == "Syncing"
    assert display.detail == "Waiting for transcript"
    assert display.is_idle is False


def test_managed_idle_after_user_prompt_keeps_idle_when_preview_is_visible():
    now = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            signal_tier="phase_signal",
            runtime_phase="idle",
            runtime_source="managed_local_transport",
            status="idle",
            presence_state="idle",
            presence_updated_at=now,
            last_live_at=now,
            confidence="live",
            display_phase="Idle",
        ),
        capabilities=_capabilities(managed=True),
        ended_at=None,
        last_activity_at=now - timedelta(milliseconds=500),
        user_messages=2,
        assistant_messages=1,
        has_visible_transcript_preview=True,
        now=now,
    )

    assert display.state == "idle"
    assert display.headline == "Idle"
    assert display.phase_label == "Idle"


def test_stale_progress_source_is_inactive():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            signal_tier="transcript_progress",
            runtime_phase="running",
            runtime_source="progress",
            status="idle",
            confidence="stale",
            display_phase="Inactive",
            last_live_at=datetime(2026, 4, 26, 11, 0, tzinfo=timezone.utc),
        ),
        capabilities=_capabilities(),
        ended_at=None,
    )

    assert display.truth_tier == "stale"
    assert display.signal_tier == "transcript_progress"
    assert display.headline == "Inactive"
    assert display.phase_label == "Inactive"
    assert display.tone == "inactive"
    assert display.is_idle is False
    assert display.activity_recency == "stale"


def test_managed_running_has_renderable_runtime_signal():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            signal_tier="phase_signal",
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
    assert display.signal_tier == "phase_signal"
    assert display.headline == "Working"
    assert display.detail == "Using Shell"
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
            display_phase="Recent",
            confidence="stale",
            last_live_at=datetime(2026, 4, 26, 11, 0, tzinfo=timezone.utc),
        ),
        capabilities=_capabilities(managed=True),
        ended_at=None,
    )

    assert display.activity_recency == "stale"


def test_three_axis_fields_legacy_execution_home_does_not_grant_managed_path():
    # Post-kernel, ``control_path == "managed"`` is sourced from the
    # kernel-projected capability flags (live_control_available or
    # host_reattach_available). A legacy ``MANAGED_HOSTED`` enum value
    # with no kernel evidence must NOT promote a session to "managed" —
    # the legacy columns are no longer authoritative.
    capabilities = _make_kernel_capabilities(live=False, reattach=False)
    display = build_session_runtime_display(
        runtime_view=_runtime_view(),
        capabilities=capabilities,
        ended_at=None,
    )

    assert display.control_path == "unmanaged"


def test_three_axis_fields_managed_live_running():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            signal_tier="phase_signal",
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
    assert display.signal_tier == "phase_signal"
    assert display.activity_recency == "live"
    assert display.lifecycle == "open"


def test_unmanaged_online_binding_promotes_signal_tier():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            signal_tier="none",
            runtime_source="fallback",
            status="idle",
            confidence=None,
            display_phase="Recent",
        ),
        capabilities=_capabilities(managed=False),
        ended_at=None,
        binding_host_state="online",
    )

    assert display.control_path == "unmanaged"
    assert display.signal_tier == "process_binding"
    assert display.host_state == "online"


def test_display_does_not_rederive_signal_tier_from_runtime_source():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            signal_tier="none",
            runtime_source="progress",
            status="active",
            confidence="stale",
            display_phase="Recent",
        ),
        capabilities=_capabilities(managed=False),
        ended_at=None,
    )

    assert display.signal_tier == "none"


def test_managed_stale_thinking_without_active_tool_is_not_current_state():
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
    assert display.is_stalled is False
    assert display.state is None
    assert display.tone == "inactive"
    assert display.headline == "Not connected"
    assert display.detail is None
    assert display.phase_label == "Inactive"
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

    assert runtime_view.runtime_phase is None
    assert runtime_view.presence_state is None
    assert runtime_view.confidence == "stale"

    display = build_session_runtime_display(
        runtime_view=runtime_view,
        capabilities=_capabilities(managed=True),
        ended_at=None,
    )

    assert display.is_stalled is False
    assert display.state is None
    assert display.headline == "Not connected"
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
    assert display.state is None
    assert display.compact_tool_label == "Shell"
    assert display.is_executing is False


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
    assert display.state is None


def test_unmanaged_needs_user_without_online_host_renders_idle():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            runtime_phase="needs_user",
            runtime_source="semantic",
            status="active",
            presence_state="needs_user",
            confidence="live",
            display_phase="Idle",
        ),
        capabilities=_capabilities(managed=False),
        ended_at=None,
    )

    assert display.control_path == "unmanaged"
    assert display.host_state == "unknown"
    assert display.state == "needs_user"
    assert display.phase_label == "Idle"
    assert display.headline == "Idle"
    assert display.is_idle is True
    assert display.tone == "idle"
    assert display.needs_attention is False


def test_unmanaged_needs_user_with_online_host_still_renders_idle():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            runtime_phase="needs_user",
            runtime_source="semantic",
            status="active",
            presence_state="needs_user",
            confidence="live",
            display_phase="Idle",
        ),
        capabilities=_capabilities(managed=False),
        ended_at=None,
        binding_host_state="online",
    )

    assert display.control_path == "unmanaged"
    assert display.host_state == "online"
    assert display.state == "needs_user"
    assert display.phase_label == "Idle"
    assert display.headline == "Idle"
    assert display.tone == "idle"
    assert display.needs_attention is False


def test_managed_stale_needs_user_without_presence_is_not_actionable():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            runtime_phase="needs_user",
            runtime_source="managed_local_transport",
            status="idle",
            presence_state=None,
            confidence="stale",
            display_phase="",
            last_live_at=datetime(2026, 4, 26, 11, 0, tzinfo=timezone.utc),
        ),
        capabilities=_capabilities(managed=True),
        ended_at=None,
    )

    assert display.control_path == "managed"
    assert display.state is None
    assert display.phase_label == "Inactive"
    assert display.headline == "Not connected"
    assert display.needs_attention is False
    assert display.tone == "inactive"


def test_unmanaged_stale_needs_user_phase_without_presence_uses_process_truth():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            runtime_phase="needs_user",
            runtime_source="semantic",
            status="idle",
            presence_state=None,
            confidence="stale",
            display_phase="",
            last_live_at=datetime(2026, 4, 26, 11, 0, tzinfo=timezone.utc),
        ),
        capabilities=_capabilities(managed=False),
        ended_at=None,
        binding_host_state="online",
    )

    assert display.control_path == "unmanaged"
    assert display.state is None
    assert display.phase_label == "Process running"
    assert display.headline == "Active"
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


def test_three_axis_fields_prefers_explicit_terminal_reason():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            runtime_phase="finished",
            terminal_state="session_ended",
            terminal_reason="bridge_stop",
            terminal_source="codex_bridge",
            status="completed",
            display_phase="Completed",
        ),
        capabilities=_capabilities(managed=True),
        ended_at=None,
    )

    assert display.lifecycle == "closed"
    assert display.terminal_reason == "bridge_stop"


def test_three_axis_fields_preserves_terminal_disconnected_reason_as_metadata():
    """terminal_reason remains on the model for metadata/debug/future-resume,
    but the user-facing labels collapse to generic "Closed"."""
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            runtime_phase="finished",
            terminal_state="session_ended",
            terminal_reason="terminal_disconnected",
            terminal_source="codex_bridge",
            status="completed",
            display_phase="Completed",
        ),
        capabilities=_capabilities(managed=True),
        ended_at=None,
    )

    assert display.lifecycle == "closed"
    assert display.terminal_reason == "terminal_disconnected"
    assert display.headline == "Closed"
    assert display.detail is None
    assert display.phase_label == "Closed"


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
    assert display.headline == "Closed"
    assert display.phase_label == "Closed"
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

    assert display.lifecycle == "open"
    assert display.host_state == "offline"
    assert display.terminal_reason is None
    assert display.state == "needs_user"
    assert display.needs_attention is False
    assert display.is_idle is True


def test_three_axis_fields_ended_at_without_terminal_stays_open():
    display = build_session_runtime_display(
        runtime_view=_runtime_view(
            signal_tier="transcript_progress",
            runtime_source="progress",
            status="active",
            confidence="stale",
            display_phase="Recent",
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
    flags = _make_kernel_capabilities(live=True, reattach=True)

    display = build_session_capability_display(flags)

    # Kernel capabilities always report a managed home label when control is
    # live, so the display label is rolled up from "Live on …" rather than the
    # bare "Send" we used pre-kernel.
    assert display.label.startswith("Live on")
    assert display.tone == "success"


def test_capability_display_names_control_offline():
    flags = _make_kernel_capabilities(live=False, reattach=True)

    display = build_session_capability_display(flags)

    assert display.label == "Control offline"
    assert display.tone == "warning"


def test_capability_display_names_imported_sessions_read_only():
    display = build_session_capability_display(_capabilities())

    assert display.label == "Read only"
    assert display.tone == "neutral"
