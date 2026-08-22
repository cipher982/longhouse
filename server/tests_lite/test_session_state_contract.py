from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.services.agents.kernel_capabilities import KernelSessionCapabilities
from zerg.services.console_control_projection import project_console_control
from zerg.services.managed_provider_contracts import managed_provider_names
from zerg.services.session_liveness_facts import ActivityObservation
from zerg.services.session_liveness_facts import ControlObservation
from zerg.services.session_liveness_facts import HostObservation
from zerg.services.session_liveness_facts import LifecycleFact
from zerg.services.session_liveness_facts import PhaseObservation
from zerg.services.session_liveness_facts import ProcessObservation
from zerg.services.session_liveness_facts import SessionLivenessFacts
from zerg.services.session_runtime import SessionRuntimeView
from zerg.services.session_state_contract import build_archive_session_state_facts
from zerg.services.session_state_contract import build_session_state_facts
from zerg.services.session_state_contract import project_transcript_facts

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
        lease_generation=f"7:{(NOW - timedelta(minutes=1)).isoformat()}" if live or reattach else None,
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
    assert facts.presentation.primary.key == "no_recent_activity"
    assert facts.presentation.primary.label == "No recent activity (last: running a tool)"
    assert facts.presentation.access is not None
    assert facts.presentation.access.label == "Live control"
    assert "Ready" not in facts.model_dump_json()


def test_mode_does_not_consume_the_rolled_up_control_label():
    live = _facts(
        runtime=None,
        capabilities=_capabilities(label="deliberately-wrong-label", live=True, reattach=False),
    )
    reattachable = _facts(
        runtime=None,
        capabilities=_capabilities(label="imported", live=False, reattach=True),
    )
    shadow = _facts(
        runtime=None,
        capabilities=_capabilities(label="live", live=False, reattach=False, observe=True, run_id=None),
        liveness=_liveness(managed=False),
    )
    console = _facts(
        runtime=None,
        session=_session(origin_kind="console"),
        capabilities=replace(
            _capabilities(label="live", live=False, reattach=False),
            control_owned=True,
        ),
        execution_lifetime="one_shot",
    )

    assert live.mode == "helm"
    assert reattachable.mode == "helm"
    assert shadow.mode == "shadow"
    assert console.mode == "console"


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


def test_transcript_coordinates_advance_independently():
    facts = project_transcript_facts(
        session=_session(),
        last_activity_at=NOW,
        user_messages=3,
        assistant_messages=2,
        archive_state="pending",
        source_revision=11,
        durable_revision=17,
        render_revision=13,
        transcript_last_append_at=NOW - timedelta(seconds=3),
    )

    assert facts.source_revision == 11
    assert facts.durable_revision == 17
    assert facts.render_revision == 13
    assert facts.last_append_at == NOW - timedelta(seconds=3)


def test_empty_pending_console_shell_is_current_not_syncing():
    facts = project_transcript_facts(
        session=_session(origin_kind="console", transcript_revision=0),
        last_activity_at=NOW,
        user_messages=0,
        assistant_messages=0,
        archive_state="pending",
        source_revision=None,
        durable_revision=None,
        render_revision=None,
    )

    assert facts.convergence == "current"
    assert facts.searchable is False


def test_zero_revision_pending_console_shell_is_current_not_syncing():
    facts = project_transcript_facts(
        session=_session(origin_kind="console", transcript_revision=0),
        last_activity_at=NOW,
        user_messages=0,
        assistant_messages=0,
        archive_state="pending",
        source_revision=0,
        durable_revision=0,
        render_revision=0,
    )

    assert facts.convergence == "current"
    assert facts.searchable is False


@pytest.mark.parametrize(
    ("turn_state", "expected_run", "expected_primary"),
    [
        ("starting", "starting", "Starting"),
        ("active", "running", "Working"),
    ],
)
def test_console_turn_before_first_provider_phase_is_still_open_and_explained(
    turn_state,
    expected_run,
    expected_primary,
):
    projection = project_console_control(
        closed=False,
        execution_target_available=True,
        turn_state=turn_state,
        machine_online=True,
        adapter_available=True,
        interrupt_adapter_available=False,
    )
    capabilities = replace(
        _capabilities(label="live", live=False, reattach=False),
        control_owned=True,
        turn_state=turn_state,
        can_start_turn=True,
        console_control=projection,
    )

    facts = _facts(
        runtime=None,
        session=_session(origin_kind="console"),
        capabilities=capabilities,
        execution_lifetime="one_shot",
    )

    assert facts.mode == "console"
    assert facts.run is not None and facts.run.lifecycle == expected_run
    assert facts.working_set == "open"
    assert facts.presentation.primary is not None
    assert facts.presentation.primary.label == expected_primary
    assert facts.presentation.access is not None
    assert facts.presentation.access.key == "live_control"


@pytest.mark.parametrize(
    ("blocker", "machine_online", "adapter_available", "closed", "expected_key", "expected_label", "expected_tone"),
    [
        ("adapter_unavailable", True, False, False, "console_no_turn_path", "Can't send", "inactive"),
        ("machine_offline", False, False, False, "machine_offline", "Machine offline", "degraded"),
        ("execution_target_missing", True, True, False, "console_no_target", "No target", "inactive"),
    ],
)
def test_console_access_names_the_turn_blocker_not_a_control_lease(
    blocker,
    machine_online,
    adapter_available,
    closed,
    expected_key,
    expected_label,
    expected_tone,
):
    """Console dispatches turns; it has no lease to degrade.

    Reporting "Control degraded" for a finished Console run on a reachable
    machine that advertises no turn adapter read as a Longhouse fault and told
    the user nothing actionable. Only an unreachable machine is an outage.
    """

    projection = project_console_control(
        closed=closed,
        execution_target_available=blocker != "execution_target_missing",
        turn_state="idle",
        machine_online=machine_online,
        adapter_available=adapter_available,
        interrupt_adapter_available=False,
    )
    assert projection.start_turn_blocked_by == blocker

    facts = _facts(
        runtime=_runtime(phase=None, terminal_state="run_completed"),
        session=_session(origin_kind="console"),
        capabilities=replace(
            _capabilities(label="live", live=False, reattach=False),
            control_owned=True,
            can_start_turn=False,
            start_turn_blocked_by=blocker,
            console_control=projection,
        ),
        execution_lifetime="one_shot",
    )

    assert facts.mode == "console"
    assert facts.presentation.access is not None
    assert facts.presentation.access.key == expected_key
    assert facts.presentation.access.label == expected_label
    assert facts.presentation.access.tone == expected_tone
    assert facts.presentation.access.label != "Control degraded"


def test_closed_console_session_shows_no_access_chip_beside_its_closed_headline():
    """The primary label already says Closed; a second chip adds nothing.

    The Helm ladder used to answer here, and it read the machine channel rather
    than the session, so a closed Console session on a reachable machine
    projected "Live control".
    """

    projection = project_console_control(
        closed=True,
        execution_target_available=True,
        turn_state="idle",
        machine_online=True,
        adapter_available=True,
        interrupt_adapter_available=False,
    )
    facts = _facts(
        runtime=_runtime(phase=None, terminal_state="user_closed"),
        session=_session(origin_kind="console", closed_at=NOW - timedelta(seconds=5)),
        capabilities=replace(
            _capabilities(label="live", live=False, reattach=False),
            control_owned=True,
            can_start_turn=False,
            start_turn_blocked_by="session_closed",
            console_control=projection,
        ),
        execution_lifetime="one_shot",
    )

    assert facts.mode == "console"
    assert facts.presentation.primary is not None
    assert facts.presentation.primary.label == "Closed"
    assert facts.presentation.access is None


def test_closed_console_session_never_advertises_live_control():
    """`user_closed` writes `closed_at` and leaves `ended_at` NULL.

    A Console blocker computed from `ended_at` alone therefore stays
    `available` on a closed session, which put "Live control" beside "Closed".
    The access chip is decided from the disposition, so both closure paths land
    the same way.
    """

    projection = project_console_control(
        closed=False,  # what an ended_at-only test computes for a user_closed session
        execution_target_available=True,
        turn_state="idle",
        machine_online=True,
        adapter_available=True,
        interrupt_adapter_available=False,
    )
    assert projection.can_start_turn is True

    facts = _facts(
        runtime=_runtime(phase=None, terminal_state="user_closed"),
        session=_session(origin_kind="console", closed_at=NOW - timedelta(seconds=5)),
        capabilities=replace(
            _capabilities(label="live", live=False, reattach=False),
            control_owned=True,
            can_start_turn=True,
            start_turn_blocked_by=None,
            console_control=projection,
        ),
        execution_lifetime="one_shot",
    )

    assert facts.disposition.state == "closed"
    assert facts.presentation.primary is not None
    assert facts.presentation.primary.label == "Closed"
    assert facts.presentation.access is None


def test_unowned_console_projection_keeps_the_searchable_history_label():
    """Archive and active-list rows are Console-moded with no control at all.

    Routing every Console row to the turn-blocker projector dropped their
    "Search only" chip, because `not_console` is not one of its blockers. Only
    an owned dispatch path answers the Console access question.
    """

    facts = build_archive_session_state_facts(
        session=_session(origin_kind="console", transcript_revision=7),
        capabilities=_capabilities(label="imported", live=False, reattach=False, search=True, run_id=None),
        execution_lifetime="one_shot",
    )

    assert facts.mode == "console"
    assert facts.control.ownership == "unowned"
    assert facts.presentation.access is not None
    assert facts.presentation.access.key == "search_only"


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


def test_launch_attempt_is_separate_from_activity_and_run():
    launching = _facts(
        runtime=None,
        capabilities=_capabilities(label="imported", live=False, reattach=False, search=False, run_id=None),
        liveness=_liveness(managed=False),
        launch_state="launching",
        execution_lifetime="one_shot",
    )
    failed = _facts(
        runtime=None,
        capabilities=_capabilities(label="imported", live=False, reattach=False, search=False, run_id=None),
        liveness=_liveness(managed=False),
        launch_state="launch_failed",
        launch_error_code="provider_unavailable",
        launch_error_message="Provider did not start",
        execution_lifetime="one_shot",
    )

    assert launching.mode == "console"
    assert launching.launch is not None and launching.launch.state == "pending"
    assert launching.run is not None and launching.run.lifecycle == "starting"
    assert launching.activity.state == "unknown"
    assert launching.presentation.primary is not None
    assert launching.presentation.primary.label == "Starting"
    assert failed.launch is not None and failed.launch.state == "failed"
    assert failed.run is None
    assert failed.presentation.primary is not None
    assert failed.presentation.primary.label == "Launch failed"


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


def test_degraded_control_revokes_commands_without_changing_activity_or_ownership():
    capabilities = replace(_capabilities(), connection_state="degraded")
    facts = _facts(runtime=_runtime(phase="thinking"), capabilities=capabilities)

    assert facts.mode == "helm"
    assert facts.activity.state == "thinking"
    assert facts.control.ownership == "owned"
    assert facts.control.connection == "degraded"
    assert facts.control.actions.send_input.state == "unavailable"
    assert facts.control.actions.interrupt.state == "unavailable"
    assert facts.control.actions.terminate.state == "unavailable"
    assert facts.presentation.access is not None
    assert facts.presentation.access.label == "Control degraded"


def test_control_lease_generation_is_stable_current_evidence_not_activity():
    first = _facts(runtime=_runtime(phase="thinking"))
    second = _facts(runtime=_runtime(phase="idle"))

    assert first.control.lease_generation == second.control.lease_generation
    assert first.control.lease_generation == f"7:{(NOW - timedelta(minutes=1)).isoformat()}"
    assert first.activity.state == "thinking"
    assert second.activity.state == "quiescent"


def test_unknown_provider_phase_is_preserved_but_not_coerced_to_idle():
    facts = _facts(runtime=_runtime(phase="provider_magic"))

    assert facts.activity.state == "unknown"
    assert facts.activity.raw_kind == "provider_magic"
    assert facts.presentation.primary is not None
    assert facts.presentation.primary.label == "Activity unknown"


@pytest.mark.parametrize("provider", sorted(managed_provider_names()))
def test_every_managed_provider_projects_the_same_semantic_axes(provider):
    facts = _facts(
        session=_session(provider=provider),
        runtime=_runtime(phase="running", tool="Bash"),
    )

    assert facts.activity.state == "executing"
    assert facts.activity.raw_kind == "running"
    assert facts.control.connection == "connected"
    assert facts.presentation.primary is not None
    assert facts.presentation.primary.key == "executing"
    assert facts.presentation.access is not None
    assert facts.presentation.access.key == "live_control"


def _working_set_facts(
    *,
    disposition_state="open",
    activity_state="quiescent",
    terminal_attached=None,
    interaction=None,
):
    """Exercise the working-set rule directly, without a full facts build.

    The rule is small and load-bearing enough that it deserves tests that
    cannot drift with unrelated projection changes.
    """
    from zerg.services.session_state_contract import SessionActionAvailability
    from zerg.services.session_state_contract import SessionActivityFacts
    from zerg.services.session_state_contract import SessionControlActions
    from zerg.services.session_state_contract import SessionControlFacts
    from zerg.services.session_state_contract import SessionDispositionFacts
    from zerg.services.session_state_contract import _working_set

    unavailable = SessionActionAvailability(state="unavailable", reason="test")
    return _working_set(
        disposition=SessionDispositionFacts(state=disposition_state),
        activity=SessionActivityFacts(state=activity_state),
        control=SessionControlFacts(
            ownership="owned",
            connection="connected",
            terminal_attached=terminal_attached,
            actions=SessionControlActions(
                send_input=unavailable,
                interrupt=unavailable,
                terminate=unavailable,
                reattach=unavailable,
                resume=unavailable,
            ),
        ),
        interaction=interaction,
    )


def test_attached_terminal_puts_a_session_in_the_working_set():
    assert _working_set_facts(terminal_attached=True) == "open"


def test_idle_session_without_a_terminal_is_history_despite_live_control():
    # The regression this whole tier exists for: control liveness outlives
    # terminals, so a connected-but-unattended session must not be promoted.
    assert _working_set_facts(terminal_attached=False) == "history"


def test_unobservable_attachment_does_not_promote():
    # None means "this provider cannot observe attachment", which is not
    # evidence of a terminal. Promoting on it would resurrect the old bug for
    # every provider lacking the signal.
    assert _working_set_facts(terminal_attached=None) == "history"


@pytest.mark.parametrize("state", ["thinking", "executing"])
def test_in_flight_work_is_in_the_working_set_without_a_terminal(state):
    # Console dispatches have no terminal by construction.
    assert _working_set_facts(activity_state=state, terminal_attached=None) == "open"


def test_blocked_on_the_user_is_in_the_working_set():
    from zerg.services.session_state_contract import SessionPendingInteractionFacts

    interaction = SessionPendingInteractionFacts(id="q1", kind="question")
    assert _working_set_facts(terminal_attached=None, interaction=interaction) == "open"


def test_closed_session_is_history_even_while_attached():
    assert (
        _working_set_facts(
            disposition_state="closed",
            activity_state="executing",
            terminal_attached=True,
        )
        == "history"
    )
