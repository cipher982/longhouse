from datetime import datetime
from datetime import timezone

import pytest

from zerg.services.session_runtime import SessionRuntimeView
from zerg.services.session_views import SessionRuntimeDisplayResponse
from zerg.services.session_views import _timeline_status_from_display


def _runtime_view(
    *,
    presence_updated_at: datetime | None = None,
    last_progress_at: datetime | None = None,
    last_live_at: datetime | None = None,
) -> SessionRuntimeView:
    anchor = datetime(2026, 3, 21, 12, 0, tzinfo=timezone.utc)
    return SessionRuntimeView(
        signal_tier="none",
        runtime_phase=None,
        phase_started_at=None,
        last_progress_at=last_progress_at,
        runtime_source="",
        terminal_state=None,
        terminal_reason=None,
        terminal_source=None,
        runtime_version=0,
        status="",
        presence_state=None,
        presence_tool=None,
        presence_updated_at=presence_updated_at,
        last_live_at=last_live_at,
        display_phase="",
        active_tool=None,
        confidence=None,
        timeline_anchor_at=anchor,
    )


def _display(
    *,
    state: str | None = None,
    compact_tool_label: str | None = None,
    lifecycle: str = "open",
    signal_tier: str = "none",
    host_state: str = "unknown",
    control_path: str = "managed",
) -> SessionRuntimeDisplayResponse:
    return SessionRuntimeDisplayResponse(
        truth_tier="none",
        signal_tier=signal_tier,
        state=state,
        tone="inactive",
        headline="",
        detail=None,
        phase_label="",
        compact_tool_label=compact_tool_label,
        is_live=False,
        is_executing=False,
        needs_attention=False,
        is_idle=False,
        is_stalled=False,
        is_managed_local_truth=False,
        has_signal=False,
        control_path=control_path,
        activity_recency="none",
        lifecycle=lifecycle,
        host_state=host_state,
        terminal_reason=None,
    )


@pytest.mark.parametrize(
    ("state", "expected_label", "expected_tone"),
    [
        ("thinking", "Thinking", "thinking"),
        ("running", "Using Shell", "running"),
        ("blocked", "Blocked Shell", "blocked"),
        ("stalled", "Stalled", "stalled"),
        ("idle", "Idle", "idle"),
        ("needs_user", "Idle", "idle"),
    ],
)
def test_timeline_status_preserves_observed_phase_tones(state, expected_label, expected_tone):
    presence_at = datetime(2026, 3, 21, 12, 0, tzinfo=timezone.utc)
    status = _timeline_status_from_display(
        _display(state=state, compact_tool_label="Shell"),
        runtime_view=_runtime_view(presence_updated_at=presence_at),
    )

    assert status.label == expected_label
    assert status.tone == expected_tone
    assert status.seen_at_prefix == "Updated"


def test_timeline_status_marks_process_binding_active_without_phase_state():
    progress_at = datetime(2026, 3, 21, 11, 45, tzinfo=timezone.utc)
    status = _timeline_status_from_display(
        _display(
            state=None,
            signal_tier="process_binding",
            host_state="online",
            control_path="unmanaged",
        ),
        runtime_view=_runtime_view(last_progress_at=progress_at),
    )

    assert status.label == "Running"
    assert status.tone == "inactive"
    assert status.seen_at == progress_at
    assert status.seen_at_prefix == "Verified"


def test_timeline_status_closed_label_is_generic():
    progress_at = datetime(2026, 3, 21, 11, 30, tzinfo=timezone.utc)
    status = _timeline_status_from_display(
        _display(lifecycle="closed"),
        runtime_view=_runtime_view(last_progress_at=progress_at),
    )

    assert status.label == "Closed"
    assert status.tone == "closed"
    assert status.seen_at == progress_at
    assert status.seen_at_prefix == "Closed"


def test_timeline_status_no_live_signal_uses_last_live_at():
    seen_at = datetime(2026, 3, 21, 11, 30, tzinfo=timezone.utc)
    status = _timeline_status_from_display(
        _display(),
        runtime_view=_runtime_view(last_live_at=seen_at),
    )

    assert status.label == "No live signal"
    assert status.tone == "inactive"
    assert status.seen_at == seen_at
    assert status.seen_at_prefix == "Last signal"


def test_timeline_status_no_runtime_view_falls_back_to_checked():
    status = _timeline_status_from_display(_display(), runtime_view=None)

    assert status.label == "No live signal"
    assert status.tone == "inactive"
    assert status.seen_at is None
    assert status.seen_at_prefix == "Checked"
