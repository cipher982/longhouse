"""The served attention axis (`presentation.signal`).

Every client used to rebuild this dot from activity, tone and keys, and five
copies disagreed. The server now decides it once from the primary label; these
cases pin the mapping and the clock each claim carries.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from zerg.services.session_state_contract import SessionActionAvailability
from zerg.services.session_state_contract import SessionActivityFacts
from zerg.services.session_state_contract import SessionControlActions
from zerg.services.session_state_contract import SessionControlFacts
from zerg.services.session_state_contract import SessionDelegationFacts
from zerg.services.session_state_contract import SessionDispositionFacts
from zerg.services.session_state_contract import SessionHostFacts
from zerg.services.session_state_contract import SessionLaunchFacts
from zerg.services.session_state_contract import SessionPendingInteractionFacts
from zerg.services.session_state_contract import SessionRunFacts
from zerg.services.session_state_contract import SessionTranscriptFacts
from zerg.services.session_state_contract import assemble_session_state_facts

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(seconds=90)


def _unavailable() -> SessionActionAvailability:
    return SessionActionAvailability(state="unavailable", reason="test")


def _control() -> SessionControlFacts:
    return SessionControlFacts(
        ownership="owned",
        connection="connected",
        actions=SessionControlActions(
            send_input=_unavailable(),
            interrupt=_unavailable(),
            terminate=_unavailable(),
            reattach=_unavailable(),
            resume=_unavailable(),
        ),
    )


def _signal(
    *,
    mode="helm",
    closed=False,
    launch=None,
    run=None,
    activity=None,
    delegation=None,
    interaction=None,
):
    facts = assemble_session_state_facts(
        mode=mode,
        disposition=SessionDispositionFacts(state="closed" if closed else "open"),
        launch=launch,
        run=run,
        activity=activity or SessionActivityFacts(state="unknown"),
        delegation=delegation or SessionDelegationFacts(),
        control=_control(),
        pending_interaction=interaction,
        transcript=SessionTranscriptFacts(convergence="current"),
        host=SessionHostFacts(state="online"),
    )
    return facts.presentation.primary, facts.presentation.signal


def _running_run() -> SessionRunFacts:
    return SessionRunFacts(id="run-1", lifecycle="running", started_at=NOW)


def test_closed_is_closed():
    _, signal = _signal(closed=True, activity=SessionActivityFacts(state="executing", valid_until=LATER))
    assert signal.state == "closed"
    assert signal.valid_until is None


@pytest.mark.parametrize("kind,key", [("question", "needs_answer"), ("approval", "needs_approval")])
def test_a_keyed_interaction_is_attention_without_a_clock(kind, key):
    primary, signal = _signal(
        run=_running_run(),
        activity=SessionActivityFacts(state="executing", tool="Bash", valid_until=LATER),
        interaction=SessionPendingInteractionFacts(id="p1", kind=kind, opened_at=NOW),
    )
    assert primary.key == key
    assert signal.state == "attention"
    # A question does not lapse because the activity that preceded it did.
    assert signal.valid_until is None


def test_stalled_is_attention_bounded_by_its_evidence():
    primary, signal = _signal(run=_running_run(), activity=SessionActivityFacts(state="stalled", valid_until=LATER))
    assert primary.key == "stalled"
    assert signal.state == "attention"
    assert signal.valid_until == LATER


@pytest.mark.parametrize(
    "launch,run,key",
    [
        (SessionLaunchFacts(state="failed"), None, "launch_failed"),
        (None, SessionRunFacts(lifecycle="ended", end_reason="provider_auth_required"), "provider_auth_required"),
    ],
)
def test_a_headline_the_user_must_act_on_is_attention(launch, run, key):
    primary, signal = _signal(launch=launch, run=run)
    assert primary.key == key
    assert signal.state == "attention"


def test_a_raw_provider_block_without_a_key_is_not_attention():
    primary, signal = _signal(run=_running_run(), activity=SessionActivityFacts(state="blocked", valid_until=LATER))
    assert primary is None or primary.tone not in {"blocked", "stalled"}
    assert signal.state != "attention"


@pytest.mark.parametrize("state,key", [("thinking", "thinking"), ("executing", "executing")])
def test_activity_work_is_working_until_its_evidence_lapses(state, key):
    primary, signal = _signal(run=_running_run(), activity=SessionActivityFacts(state=state, valid_until=LATER))
    assert primary.key == key
    assert signal.state == "working"
    assert signal.valid_until == LATER


def test_delegated_work_uses_the_delegation_clock():
    delegation_deadline = NOW + timedelta(minutes=10)
    primary, signal = _signal(
        run=_running_run(),
        activity=SessionActivityFacts(state="quiescent", valid_until=LATER),
        delegation=SessionDelegationFacts(
            state="pending",
            count=1,
            kinds={"subagent": 1},
            valid_until=delegation_deadline,
        ),
    )
    assert primary.key == "delegated_work"
    assert signal.state == "working"
    assert signal.valid_until == delegation_deadline


def test_starting_is_working_with_no_clock():
    primary, signal = _signal(run=SessionRunFacts(lifecycle="starting", started_at=NOW))
    assert primary.key == "starting"
    assert signal.state == "working"
    assert signal.valid_until is None


def test_idle_is_quiet():
    primary, signal = _signal(run=_running_run(), activity=SessionActivityFacts(state="quiescent", valid_until=LATER))
    assert primary.key == "idle"
    assert signal.state == "quiet"
    assert signal.valid_until is None


@pytest.mark.parametrize(
    "run,activity,key",
    [
        (_running_run(), SessionActivityFacts(state="unknown"), "activity_unknown"),
        (_running_run(), SessionActivityFacts(state="unknown", raw_kind="running"), "no_recent_activity"),
        (None, SessionActivityFacts(state="unknown"), "imported"),
    ],
)
def test_what_the_headline_cannot_vouch_for_is_unknown(run, activity, key):
    primary, signal = _signal(mode="shadow" if run is None else "helm", run=run, activity=activity)
    assert primary.key == key
    assert signal.state == "unknown"


def test_no_headline_is_unknown():
    primary, signal = _signal(mode="helm")
    assert primary is None
    assert signal.state == "unknown"


def test_the_served_literal_is_the_manifest_vocabulary():
    from typing import get_args

    from zerg.services.session_state_contract import SIGNAL_STATES
    from zerg.services.session_state_contract import SignalState

    assert get_args(SignalState) == SIGNAL_STATES
