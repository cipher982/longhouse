from datetime import datetime
from datetime import timezone

import pytest

from zerg.services.session_views import ActivityObservationResponse
from zerg.services.session_views import HostObservationResponse
from zerg.services.session_views import LifecycleFactResponse
from zerg.services.session_views import PhaseObservationResponse
from zerg.services.session_views import ProcessObservationResponse
from zerg.services.session_views import SessionLivenessFactsResponse
from zerg.services.session_views import _timeline_status_from_liveness_facts


def _facts(
    *,
    phase_kind: str | None = None,
    phase_tool: str | None = None,
    lifecycle_state: str = "open",
    lifecycle_reason: str | None = None,
    process_status: str = "unknown",
    process_state: str | None = None,
) -> SessionLivenessFactsResponse:
    now = datetime(2026, 3, 21, 12, 0, tzinfo=timezone.utc)
    return SessionLivenessFactsResponse(
        control_path="managed",
        process_state=process_state or ("running" if process_status == "observed" else "unknown"),
        host=HostObservationResponse(state="unknown", last_seen_at=None, source=None),
        process=ProcessObservationResponse(
            status=process_status,
            pid=123 if process_status == "observed" else None,
            observed_at=now if process_status == "observed" else None,
            source="machine_process_scan" if process_status == "observed" else None,
        ),
        phase=PhaseObservationResponse(
            kind=phase_kind,
            tool=phase_tool,
            source="managed_local_transport" if phase_kind else None,
            observed_at=now if phase_kind else None,
        ),
        activity=ActivityObservationResponse(),
        lifecycle=LifecycleFactResponse(
            state=lifecycle_state,
            reason=lifecycle_reason,
            observed_at=now if lifecycle_state == "closed" else None,
        ),
    )


@pytest.mark.parametrize(
    ("phase_kind", "expected_label", "expected_tone"),
    [
        ("thinking", "Thinking", "thinking"),
        ("running", "Using Shell", "running"),
        ("blocked", "Blocked Shell", "blocked"),
        ("stalled", "Stalled", "stalled"),
        ("idle", "Idle", "idle"),
        ("needs_user", "Idle", "idle"),
        ("reviewing", "Reviewing", "inactive"),
    ],
)
def test_timeline_status_preserves_observed_phase_tones(
    phase_kind,
    expected_label,
    expected_tone,
):
    status = _timeline_status_from_liveness_facts(
        _facts(phase_kind=phase_kind, phase_tool="bash")
    )

    assert status is not None
    assert status.label == expected_label
    assert status.tone == expected_tone
    assert status.seen_at_prefix == "Updated"


def test_timeline_status_marks_process_observed_active_without_phase_claim():
    status = _timeline_status_from_liveness_facts(_facts(process_status="observed"))

    assert status is not None
    assert status.label == "Running"
    assert status.tone == "inactive"
    assert status.seen_at_prefix == "Verified"


def test_timeline_status_preserves_terminal_disconnected_closed_reason():
    status = _timeline_status_from_liveness_facts(
        _facts(
            lifecycle_state="closed",
            lifecycle_reason="terminal_disconnected",
            process_state="closed",
        )
    )

    assert status is not None
    assert status.label == "Terminal disconnected"
    assert status.tone == "closed"
    assert status.seen_at_prefix == "Closed"


def test_timeline_status_keeps_other_closed_reasons_generic():
    status = _timeline_status_from_liveness_facts(
        _facts(
            lifecycle_state="closed",
            lifecycle_reason="process_gone",
            process_state="closed",
        )
    )

    assert status is not None
    assert status.label == "Closed"
    assert status.tone == "closed"
    assert status.seen_at_prefix == "Closed"
