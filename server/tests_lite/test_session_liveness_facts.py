from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace

from zerg.services.agents.kernel_capabilities import KernelSessionCapabilities
from zerg.services.session_liveness_facts import build_session_liveness_facts
from zerg.services.session_runtime import SessionRuntimeView

NOW = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)


def _capabilities(*, managed: bool = False) -> KernelSessionCapabilities:
    return KernelSessionCapabilities(
        session_id="00000000-0000-0000-0000-000000000000",
        thread_id=None,
        run_id=None,
        connection_id=None,
        control_plane="claude_channel_bridge" if managed else None,
        connection_state="attached" if managed else None,
        control_label="live" if managed else "imported",
        live_control_available=managed,
        host_reattach_available=managed,
        observe_only=False,
        search_only=not managed,
        can_send_input=managed,
        can_interrupt=managed,
        can_terminate=managed,
        can_tail_output=managed,
        can_resume=managed,
        staleness_reason=None if managed else "imported_only",
    )


def _runtime_view(**overrides) -> SessionRuntimeView:
    values = {
        "signal_tier": "none",
        "runtime_phase": "idle",
        "phase_started_at": NOW,
        "last_progress_at": None,
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
        "display_phase": "Idle",
        "active_tool": None,
        "confidence": None,
        "timeline_anchor_at": NOW,
        "freshness_expires_at": None,
    }
    values.update(overrides)
    return SessionRuntimeView(**values)


def test_transcript_activity_does_not_create_phase_or_lifecycle_truth():
    last_activity = NOW - timedelta(minutes=1)
    facts = build_session_liveness_facts(
        runtime_view=_runtime_view(
            signal_tier="transcript_progress",
            runtime_phase=None,
            runtime_source="progress",
            last_progress_at=last_activity,
            confidence="stale",
        ),
        capabilities=_capabilities(),
        last_activity_at=last_activity,
    )

    assert facts.control_path == "unmanaged"
    assert facts.phase.kind is None
    assert facts.activity.last_transcript_at == last_activity
    assert facts.lifecycle.state == "unknown"


def test_unmanaged_process_observation_is_a_fact_not_active_copy():
    observed_at = NOW - timedelta(seconds=5)
    facts = build_session_liveness_facts(
        runtime_view=_runtime_view(),
        capabilities=_capabilities(),
        last_activity_at=NOW - timedelta(hours=11),
        binding_overlay=SimpleNamespace(
            host_state="online",
            terminal_reason=None,
            host_last_seen_at=NOW,
            pid=20293,
            process_start_time=NOW - timedelta(hours=12),
            observed_at=observed_at,
            last_seen_at=NOW,
            source_mtime=NOW - timedelta(hours=11),
            source_path="/Users/david/.codex/sessions/rollout.jsonl",
        ),
    )

    assert facts.process.status == "observed"
    assert facts.process_state == "running"
    assert facts.process.pid == 20293
    assert facts.process.observed_at == observed_at
    assert facts.lifecycle.state == "open"
    assert facts.lifecycle.reason == "process_observed"


def test_online_host_without_pid_does_not_observe_process():
    facts = build_session_liveness_facts(
        runtime_view=_runtime_view(),
        capabilities=_capabilities(),
        last_activity_at=NOW - timedelta(hours=11),
        binding_overlay=SimpleNamespace(
            host_state="online",
            terminal_reason=None,
            host_last_seen_at=NOW,
            pid=None,
            process_start_time=None,
            observed_at=NOW,
            last_seen_at=NOW,
            source_mtime=NOW - timedelta(hours=11),
            source_path="/Users/david/.codex/sessions/rollout.jsonl",
            binding_state="observed",
        ),
    )

    assert facts.host.state == "online"
    assert facts.process.status == "unknown"
    assert facts.process_state == "unknown"
    assert facts.lifecycle.state == "unknown"


def test_missing_unmanaged_process_scan_marks_process_closed():
    facts = build_session_liveness_facts(
        runtime_view=_runtime_view(),
        capabilities=_capabilities(),
        last_activity_at=NOW - timedelta(hours=3),
        binding_overlay=SimpleNamespace(
            host_state="online",
            terminal_reason="process_gone",
            host_last_seen_at=NOW,
            pid=20293,
            process_start_time=NOW - timedelta(hours=4),
            observed_at=NOW - timedelta(hours=4),
            last_seen_at=NOW - timedelta(minutes=20),
            source_mtime=NOW - timedelta(hours=3),
            source_path="/Users/david/.codex/sessions/rollout.jsonl",
        ),
    )

    assert facts.process.status == "not_observed"
    assert facts.process.reason == "process_gone"
    assert facts.process_state == "closed"
    assert facts.lifecycle.state == "closed"
    assert facts.lifecycle.reason == "process_gone"


def test_managed_process_gone_binding_does_not_close_without_terminal_signal():
    facts = build_session_liveness_facts(
        runtime_view=_runtime_view(),
        capabilities=_capabilities(managed=True),
        last_activity_at=NOW - timedelta(hours=3),
        binding_overlay=SimpleNamespace(
            host_state="online",
            terminal_reason="process_gone",
            host_last_seen_at=NOW,
            pid=20293,
            process_start_time=NOW - timedelta(hours=4),
            observed_at=NOW - timedelta(hours=4),
            last_seen_at=NOW - timedelta(minutes=20),
            source_mtime=NOW - timedelta(hours=3),
            source_path="/Users/david/.codex/sessions/rollout.jsonl",
            binding_state="stale",
        ),
    )

    assert facts.control_path == "managed"
    assert facts.process.status == "unknown"
    assert facts.process.reason == "process_gone"
    assert facts.process_state == "unknown"
    assert facts.lifecycle.state == "unknown"


def test_host_expired_means_unverified_not_closed():
    facts = build_session_liveness_facts(
        runtime_view=_runtime_view(),
        capabilities=_capabilities(),
        last_activity_at=NOW - timedelta(days=8),
        binding_overlay=SimpleNamespace(
            host_state="offline",
            terminal_reason="host_expired",
            host_last_seen_at=NOW - timedelta(days=8),
            pid=20293,
            process_start_time=NOW - timedelta(days=9),
            observed_at=NOW - timedelta(days=8),
            last_seen_at=NOW - timedelta(days=8),
            source_mtime=NOW - timedelta(days=8),
            source_path="/Users/david/.codex/sessions/rollout.jsonl",
        ),
    )

    assert facts.host.state == "offline"
    assert facts.process.status == "unknown"
    assert facts.process.reason == "host_expired"
    assert facts.process_state == "unknown"
    assert facts.lifecycle.state == "unknown"


def test_stale_phase_signal_is_timestamped_not_current_lifecycle_truth():
    observed_at = NOW - timedelta(minutes=30)
    facts = build_session_liveness_facts(
        runtime_view=_runtime_view(
            signal_tier="phase_signal",
            runtime_phase="thinking",
            runtime_source="managed_local_transport",
            phase_started_at=observed_at,
            presence_updated_at=observed_at,
            last_live_at=observed_at,
            confidence="stale",
            freshness_expires_at=observed_at + timedelta(seconds=90),
        ),
        capabilities=_capabilities(managed=True),
        last_activity_at=observed_at,
    )

    assert facts.control_path == "managed"
    assert facts.process_state == "unknown"
    assert facts.phase.kind is None
    assert facts.phase.observed_at is None
    assert facts.phase.expires_at is None
    assert facts.activity.last_runtime_signal_at == observed_at
    assert facts.lifecycle.state == "unknown"


def test_stale_phase_with_fresh_control_lease_keeps_managed_lifecycle_open():
    observed_at = NOW - timedelta(minutes=30)
    facts = build_session_liveness_facts(
        runtime_view=_runtime_view(
            signal_tier="phase_signal",
            runtime_phase="idle",
            runtime_source="semantic",
            phase_started_at=observed_at,
            presence_updated_at=observed_at,
            last_live_at=observed_at,
            confidence="stale",
            freshness_expires_at=observed_at + timedelta(minutes=10),
        ),
        capabilities=_capabilities(managed=True),
        last_activity_at=observed_at,
        binding_host_state="online",
        control_overlay=SimpleNamespace(
            state="attached",
            source="managed_control_lease",
            last_control_seen_at=NOW,
            control_expires_at=NOW + timedelta(minutes=15),
            reason=None,
            transport="claude_channel_bridge",
        ),
        now=NOW,
    )

    assert facts.control_path == "managed"
    assert facts.phase.kind is None
    assert facts.control.state == "online"
    assert facts.control.reason is None
    assert facts.control.last_seen_at == NOW
    assert facts.lifecycle.state == "open"
    assert facts.lifecycle.reason == "control_observed"


def test_recent_activity_with_stale_control_lease_is_not_control_live():
    last_activity = NOW - timedelta(seconds=10)
    facts = build_session_liveness_facts(
        runtime_view=_runtime_view(
            signal_tier="transcript_progress",
            runtime_phase=None,
            runtime_source="progress",
            last_progress_at=last_activity,
            confidence="live",
        ),
        capabilities=_capabilities(managed=True),
        last_activity_at=last_activity,
        binding_host_state="online",
        control_overlay=SimpleNamespace(
            state="attached",
            source="managed_control_lease",
            last_control_seen_at=NOW - timedelta(minutes=20),
            control_expires_at=NOW - timedelta(minutes=5),
            reason=None,
            transport="claude_channel_bridge",
        ),
        now=NOW,
    )

    assert facts.activity.last_transcript_at == last_activity
    assert facts.phase.kind is None
    assert facts.control.state == "offline"
    assert facts.control.reason == "lease_stale"
    assert facts.lifecycle.state == "open"
    assert facts.lifecycle.reason == "control_observed"


def test_explicit_provider_terminal_closes_session():
    terminal_at = NOW - timedelta(minutes=2)
    facts = build_session_liveness_facts(
        runtime_view=_runtime_view(
            signal_tier="phase_signal",
            runtime_phase="finished",
            runtime_source="semantic",
            terminal_state="session_ended",
            phase_started_at=terminal_at,
            presence_updated_at=terminal_at,
            last_live_at=terminal_at,
            status="completed",
        ),
        capabilities=_capabilities(managed=True),
        last_activity_at=terminal_at,
    )

    assert facts.lifecycle.state == "closed"
    assert facts.process_state == "closed"
    assert facts.lifecycle.reason == "session_ended"
    assert facts.lifecycle.observed_at == terminal_at


def test_explicit_terminal_reason_closes_with_specific_reason():
    terminal_at = NOW - timedelta(minutes=2)
    facts = build_session_liveness_facts(
        runtime_view=_runtime_view(
            signal_tier="phase_signal",
            runtime_phase="finished",
            runtime_source="semantic",
            terminal_state="session_ended",
            terminal_reason="bridge_stop",
            terminal_source="codex_bridge",
            phase_started_at=terminal_at,
            presence_updated_at=terminal_at,
            last_live_at=terminal_at,
            status="completed",
        ),
        capabilities=_capabilities(managed=True),
        last_activity_at=terminal_at,
    )

    assert facts.lifecycle.state == "closed"
    assert facts.lifecycle.reason == "bridge_stop"


def test_explicit_terminal_disconnected_closes_with_specific_reason():
    terminal_at = NOW - timedelta(minutes=2)
    facts = build_session_liveness_facts(
        runtime_view=_runtime_view(
            signal_tier="phase_signal",
            runtime_phase="finished",
            runtime_source="semantic",
            terminal_state="session_ended",
            terminal_reason="terminal_disconnected",
            terminal_source="codex_bridge",
            phase_started_at=terminal_at,
            presence_updated_at=terminal_at,
            last_live_at=terminal_at,
            status="completed",
        ),
        capabilities=_capabilities(managed=True),
        last_activity_at=terminal_at,
    )

    assert facts.lifecycle.state == "closed"
    assert facts.process_state == "closed"
    assert facts.lifecycle.reason == "terminal_disconnected"


def test_explicit_process_gone_terminal_closes_managed_session():
    terminal_at = NOW - timedelta(minutes=2)
    facts = build_session_liveness_facts(
        runtime_view=_runtime_view(
            signal_tier="phase_signal",
            runtime_phase="finished",
            runtime_source="semantic",
            terminal_state="process_gone",
            phase_started_at=terminal_at,
            presence_updated_at=terminal_at,
            last_live_at=terminal_at,
            status="completed",
        ),
        capabilities=_capabilities(managed=True),
        last_activity_at=terminal_at,
    )

    assert facts.control_path == "managed"
    assert facts.lifecycle.state == "closed"
    assert facts.process_state == "closed"
    assert facts.lifecycle.reason == "process_gone"
    assert facts.lifecycle.observed_at == terminal_at


def test_managed_fresh_phase_marks_lifecycle_open_without_process_scan():
    observed_at = NOW - timedelta(seconds=10)
    facts = build_session_liveness_facts(
        runtime_view=_runtime_view(
            signal_tier="phase_signal",
            runtime_phase="thinking",
            runtime_source="managed_local_transport",
            phase_started_at=observed_at,
            presence_updated_at=observed_at,
            last_live_at=observed_at,
            confidence="live",
            freshness_expires_at=NOW + timedelta(minutes=1),
        ),
        capabilities=_capabilities(managed=True),
        last_activity_at=observed_at,
        binding_host_state="online",
    )

    assert facts.control_path == "managed"
    assert facts.process.status == "unknown"
    assert facts.phase.kind == "thinking"
    assert facts.lifecycle.state == "open"
    assert facts.lifecycle.reason == "phase_observed"
    assert facts.process_state == "unknown"


def test_unmanaged_fresh_phase_marks_lifecycle_open_without_process_scan():
    observed_at = NOW - timedelta(seconds=10)
    facts = build_session_liveness_facts(
        runtime_view=_runtime_view(
            signal_tier="phase_signal",
            runtime_phase="running",
            runtime_source="semantic",
            active_tool="bash",
            phase_started_at=observed_at,
            presence_updated_at=observed_at,
            last_live_at=observed_at,
            confidence="live",
            freshness_expires_at=NOW + timedelta(minutes=1),
        ),
        capabilities=_capabilities(managed=False),
        last_activity_at=observed_at,
    )

    assert facts.control_path == "unmanaged"
    assert facts.process.status == "unknown"
    assert facts.phase.kind == "running"
    assert facts.phase.tool == "bash"
    assert facts.lifecycle.state == "open"
    assert facts.lifecycle.reason == "phase_observed"
    assert facts.process_state == "unknown"
