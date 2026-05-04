from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace

from zerg.services.session_capabilities import SessionCapabilityFlags
from zerg.services.session_liveness_facts import build_session_liveness_facts
from zerg.services.session_runtime import SessionRuntimeView
from zerg.session_execution_home import SessionExecutionHome


NOW = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)


def _capabilities(*, managed: bool = False) -> SessionCapabilityFlags:
    return SessionCapabilityFlags(
        execution_home=SessionExecutionHome.MANAGED_LOCAL if managed else SessionExecutionHome.UNMANAGED_LOCAL,
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
        "last_progress_at": None,
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
    assert facts.process.pid == 20293
    assert facts.process.observed_at == observed_at
    assert facts.lifecycle.state == "open"
    assert facts.lifecycle.reason == "process_observed"


def test_missing_unmanaged_process_scan_does_not_close_session():
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
    assert facts.lifecycle.state == "unknown"


def test_stale_managed_phase_is_timestamped_not_current_lifecycle_truth():
    observed_at = NOW - timedelta(minutes=30)
    facts = build_session_liveness_facts(
        runtime_view=_runtime_view(
            signal_tier="managed_phase",
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
    assert facts.phase.kind == "thinking"
    assert facts.phase.observed_at == observed_at
    assert facts.phase.expires_at == observed_at + timedelta(seconds=90)
    assert facts.lifecycle.state == "unknown"


def test_explicit_provider_terminal_closes_session():
    terminal_at = NOW - timedelta(minutes=2)
    facts = build_session_liveness_facts(
        runtime_view=_runtime_view(
            signal_tier="managed_phase",
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
    assert facts.lifecycle.reason == "session_ended"
    assert facts.lifecycle.observed_at == terminal_at
