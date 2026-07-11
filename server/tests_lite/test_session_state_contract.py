from __future__ import annotations

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.services.agents.kernel_capabilities import KernelSessionCapabilities
from zerg.services.session_liveness_facts import ActivityObservation
from zerg.services.session_liveness_facts import ControlObservation
from zerg.services.session_liveness_facts import HostObservation
from zerg.services.session_liveness_facts import LifecycleFact
from zerg.services.session_liveness_facts import PhaseObservation
from zerg.services.session_liveness_facts import ProcessObservation
from zerg.services.session_liveness_facts import SessionLivenessFacts
from zerg.services.session_runtime import SessionRuntimeView
from zerg.services.session_state_contract import build_session_state_facts

NOW = datetime(2026, 7, 11, 18, 0, tzinfo=timezone.utc)


def _session(**overrides):
    values = {
        "started_at": NOW - timedelta(hours=1),
        "ended_at": None,
        "launch_surface": None,
        "transcript_revision": 3,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _runtime(
    *,
    phase: str | None,
    confidence: str | None = "live",
    terminal_state: str | None = None,
    tool: str | None = None,
    source: str = "codex_bridge",
):
    observed_at = NOW - timedelta(seconds=5) if phase is not None else None
    return SessionRuntimeView(
        signal_tier="phase_signal" if phase is not None else "none",
        runtime_phase=phase,
        phase_started_at=observed_at,
        last_progress_at=observed_at,
        runtime_source=source,
        terminal_state=terminal_state,
        terminal_reason=terminal_state,
        terminal_source=source if terminal_state else None,
        runtime_version=1,
        status="idle",
        presence_state=phase,
        presence_tool=tool,
        presence_updated_at=observed_at,
        last_live_at=observed_at,
        display_phase="legacy copy must not matter",
        active_tool=tool,
        confidence=confidence,
        timeline_anchor_at=NOW,
        freshness_expires_at=NOW + timedelta(minutes=5) if confidence == "live" else NOW - timedelta(seconds=1),
    )


def _capabilities(
    *,
    label: str = "live",
    live: bool = True,
    reattach: bool = True,
    observe: bool = False,
    search: bool = False,
    run_id: str | None = "00000000-0000-0000-0000-000000000002",
):
    return KernelSessionCapabilities(
        session_id="00000000-0000-0000-0000-000000000001",
        thread_id="00000000-0000-0000-0000-000000000003",
        run_id=run_id,
        connection_id=7 if live or reattach else None,
        control_plane="codex_bridge" if live or reattach else None,
        connection_state="attached" if live else "detached" if reattach else None,
        control_label=label,
        live_control_available=live,
        host_reattach_available=reattach,
        observe_only=observe,
        search_only=search,
        can_send_input=live,
        can_interrupt=live,
        can_terminate=live,
        can_tail_output=live or observe,
        can_resume=live or reattach,
        staleness_reason=None if live else "connection_released" if reattach else "imported_only",
    )


def _liveness(*, managed: bool = True, expires_at: datetime | None = None, process: str = "unknown"):
    expires_at = expires_at if expires_at is not None else NOW + timedelta(minutes=5)
    return SessionLivenessFacts(
        control_path="managed" if managed else "unmanaged",
        control=ControlObservation(
            state="online" if managed else "none",
            source="machine_heartbeat" if managed else None,
            last_seen_at=NOW - timedelta(seconds=5) if managed else None,
            expires_at=expires_at if managed else None,
        ),
        process_state="running" if process == "observed" else "unknown",
        host=HostObservation(state="online", last_seen_at=NOW - timedelta(seconds=5), source="machine_heartbeat"),
        process=ProcessObservation(status=process, source="machine_process_scan"),
        phase=PhaseObservation(kind=None, tool=None, source=None, observed_at=None, expires_at=None),
        activity=ActivityObservation(last_transcript_at=NOW, last_runtime_signal_at=NOW, last_progress_at=NOW),
        lifecycle=LifecycleFact(state="open"),
    )


def _facts(*, runtime=None, capabilities=None, liveness=None, session=None, **kwargs):
    params = {
        "last_activity_at": NOW - timedelta(seconds=10),
        "user_messages": 2,
        "assistant_messages": 2,
        "now": NOW,
    }
    params.update(kwargs)
    return build_session_state_facts(
        session=session or _session(),
        runtime_view=runtime,
        capabilities=capabilities or _capabilities(),
        liveness=liveness or _liveness(),
        **params,
    )


def test_expired_activity_with_live_control_is_unknown_plus_live_control():
    facts = _facts(runtime=_runtime(phase="running", confidence="stale", tool="Bash"))

    assert facts.activity.state == "unknown"
    assert facts.presentation.primary is not None
    assert facts.presentation.primary.label == "Activity unknown"
    assert facts.presentation.access is not None
    assert facts.presentation.access.label == "Live control"
    assert "Ready" not in facts.model_dump_json()


def test_idle_and_ordinary_needs_user_normalize_to_quiescent_idle():
    for phase in ("idle", "needs_user"):
        facts = _facts(runtime=_runtime(phase=phase))
        assert facts.activity.state == "quiescent"
        assert facts.activity.raw_kind == phase
        assert facts.presentation.primary is not None
        assert facts.presentation.primary.label == "Idle"
        assert facts.pending_interaction is None


def test_pending_question_outranks_quiescent_without_mutating_activity():
    facts = _facts(
        runtime=_runtime(phase="needs_user"),
        pause_request={
            "id": "pause-1",
            "kind": "structured_question",
            "status": "pending",
            "occurred_at": NOW - timedelta(seconds=4),
            "can_respond": True,
        },
    )

    assert facts.activity.state == "quiescent"
    assert facts.pending_interaction is not None
    assert facts.pending_interaction.kind == "question"
    assert facts.presentation.primary is not None
    assert facts.presentation.primary.label == "Needs answer"


def test_transcript_lag_never_becomes_provider_working():
    facts = _facts(
        runtime=_runtime(phase="idle"),
        has_pending_response_turn=True,
        user_messages=3,
        assistant_messages=2,
    )

    assert facts.activity.state == "quiescent"
    assert facts.transcript.convergence == "lagging"
    assert facts.presentation.primary is not None
    assert facts.presentation.primary.label == "Idle"
    assert facts.presentation.transcript is not None
    assert facts.presentation.transcript.label == "Transcript catching up"
    assert "Working" not in facts.model_dump_json()


def test_process_gone_ends_run_but_does_not_close_session():
    facts = _facts(
        runtime=_runtime(phase=None, confidence="stale", terminal_state="process_gone"),
        session=_session(ended_at=NOW - timedelta(seconds=2)),
    )

    assert facts.disposition.state == "open"
    assert facts.run is not None
    assert facts.run.lifecycle == "ended"
    assert facts.presentation.primary is not None
    assert facts.presentation.primary.label == "Ended"


def test_explicit_user_close_dominates_all_other_axes():
    facts = _facts(
        runtime=_runtime(phase="running", terminal_state="user_closed", tool="Bash"),
        session=_session(ended_at=NOW),
    )

    assert facts.disposition.state == "closed"
    assert facts.presentation.primary is not None
    assert facts.presentation.primary.label == "Closed"


def test_no_run_means_no_primary_runtime_claim():
    facts = _facts(
        runtime=None,
        capabilities=_capabilities(label="imported", live=False, reattach=False, search=True, run_id=None),
        liveness=_liveness(managed=False),
    )

    assert facts.run is None
    assert facts.presentation.primary is None
    assert facts.presentation.access is not None
    assert facts.presentation.access.label == "Search only"


def test_shadow_fresh_activity_is_observe_only_not_managed():
    facts = _facts(
        runtime=_runtime(phase="thinking", source="claude_hook"),
        capabilities=_capabilities(
            label="search-only",
            live=False,
            reattach=False,
            observe=True,
            search=False,
            run_id="00000000-0000-0000-0000-000000000004",
        ),
        liveness=_liveness(managed=False, process="observed"),
    )

    assert facts.mode == "shadow"
    assert facts.presentation.primary is not None
    assert facts.presentation.primary.label == "Thinking"
    assert facts.presentation.access is not None
    assert facts.presentation.access.label == "Observe only"


def test_expired_control_demotes_actions_without_changing_activity():
    facts = _facts(
        runtime=_runtime(phase="thinking"),
        liveness=_liveness(expires_at=NOW - timedelta(milliseconds=1)),
    )

    assert facts.activity.state == "thinking"
    assert facts.control.connection == "unknown"
    assert facts.control.actions.send_input.state == "unknown"
    assert facts.presentation.primary is not None
    assert facts.presentation.primary.label == "Thinking"
    assert facts.presentation.access is not None
    assert facts.presentation.access.label == "Control unknown"


def test_unknown_provider_phase_is_preserved_but_not_coerced_to_idle():
    facts = _facts(runtime=_runtime(phase="provider_magic"))

    assert facts.activity.state == "unknown"
    assert facts.activity.raw_kind == "provider_magic"
    assert facts.presentation.primary is not None
    assert facts.presentation.primary.label == "Activity unknown"
