from datetime import datetime
from datetime import timedelta
from datetime import timezone
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.services.agents.kernel_capabilities import KernelSessionCapabilities
from zerg.services.provisional_events import EVENT_ORIGIN_LIVE_PROVISIONAL
from zerg.services.provisional_events import TranscriptPreview
from zerg.services.session_runtime import SessionRuntimeView
from zerg.services.session_runtime_display import TRANSCRIPT_SYNC_DISPLAY_WINDOW
from zerg.services.session_runtime_display import TRANSCRIPT_SYNC_STATE
from zerg.services.session_runtime_display import build_session_runtime_display
from zerg.services.session_views import PROVISIONAL_TRANSCRIPT_COMPLETE_FRESHNESS
from zerg.services.session_views import PROVISIONAL_TRANSCRIPT_PARTIAL_FRESHNESS
from zerg.services.session_views import build_session_transcript_preview_response


PINNED_NOW = datetime(2026, 5, 24, 18, 0, tzinfo=timezone.utc)


def _preview(
    *,
    timestamp: datetime | None,
    complete: bool = False,
) -> TranscriptPreview:
    return TranscriptPreview(
        event_id=42,
        text="provider output",
        event_origin=EVENT_ORIGIN_LIVE_PROVISIONAL,
        timestamp=timestamp,  # type: ignore[arg-type]
        provisional_cursor="bridge:session:thread:turn:7",
        provisional_complete=complete,
    )


def _managed_capabilities() -> KernelSessionCapabilities:
    return KernelSessionCapabilities(
        session_id="00000000-0000-0000-0000-000000000000",
        thread_id=None,
        run_id=None,
        connection_id=None,
        control_plane="claude_channel_bridge",
        connection_state="attached",
        control_label="live",
        live_control_available=True,
        host_reattach_available=True,
        observe_only=False,
        search_only=False,
        can_send_input=True,
        can_interrupt=True,
        can_terminate=True,
        can_tail_output=True,
        can_resume=True,
        staleness_reason=None,
    )


def _runtime_view(*, signal_at: datetime) -> SessionRuntimeView:
    return SessionRuntimeView(
        signal_tier="phase_signal",
        runtime_phase="idle",
        phase_started_at=signal_at,
        last_progress_at=signal_at,
        runtime_source="managed_local_transport",
        terminal_state=None,
        terminal_reason=None,
        terminal_source=None,
        runtime_version=1,
        status="idle",
        presence_state="idle",
        presence_tool=None,
        presence_updated_at=signal_at,
        last_live_at=signal_at,
        display_phase="Idle",
        active_tool=None,
        confidence="live",
        timeline_anchor_at=signal_at,
    )


def test_transcript_preview_freshness_contract_uses_backend_clock_boundaries():
    partial_boundary = build_session_transcript_preview_response(
        _preview(timestamp=PINNED_NOW - PROVISIONAL_TRANSCRIPT_PARTIAL_FRESHNESS),
        now=PINNED_NOW,
    )
    partial_expired = build_session_transcript_preview_response(
        _preview(timestamp=PINNED_NOW - PROVISIONAL_TRANSCRIPT_PARTIAL_FRESHNESS - timedelta(microseconds=1)),
        now=PINNED_NOW,
    )
    complete_boundary = build_session_transcript_preview_response(
        _preview(timestamp=PINNED_NOW - PROVISIONAL_TRANSCRIPT_COMPLETE_FRESHNESS, complete=True),
        now=PINNED_NOW,
    )
    complete_expired = build_session_transcript_preview_response(
        _preview(timestamp=PINNED_NOW - PROVISIONAL_TRANSCRIPT_COMPLETE_FRESHNESS - timedelta(microseconds=1), complete=True),
        now=PINNED_NOW,
    )

    assert partial_boundary is not None
    assert partial_boundary.is_stale is False
    assert partial_boundary.stale_reason is None
    assert partial_expired is not None
    assert partial_expired.is_stale is True
    assert partial_expired.stale_reason == "freshness_window_expired"
    assert complete_boundary is not None
    assert complete_boundary.is_stale is False
    assert complete_boundary.stale_reason is None
    assert complete_expired is not None
    assert complete_expired.is_stale is True
    assert complete_expired.stale_reason == "freshness_window_expired"


def test_transcript_preview_freshness_contract_prefers_durable_activity_over_age():
    preview_at = PINNED_NOW - timedelta(seconds=10)

    superseded = build_session_transcript_preview_response(
        _preview(timestamp=preview_at),
        last_activity_at=PINNED_NOW,
        now=PINNED_NOW,
    )
    still_current = build_session_transcript_preview_response(
        _preview(timestamp=preview_at),
        last_activity_at=preview_at - timedelta(microseconds=1),
        now=PINNED_NOW,
    )
    missing_timestamp = build_session_transcript_preview_response(
        _preview(timestamp=None),
        now=PINNED_NOW,
    )

    assert superseded is not None
    assert superseded.is_stale is True
    assert superseded.stale_reason == "superseded_by_durable"
    assert still_current is not None
    assert still_current.is_stale is False
    assert still_current.stale_reason is None
    assert missing_timestamp is not None
    assert missing_timestamp.is_stale is True
    assert missing_timestamp.stale_reason == "missing_preview_timestamp"


def test_runtime_transcript_sync_freshness_contract_uses_backend_clock_window():
    signal_at = PINNED_NOW - TRANSCRIPT_SYNC_DISPLAY_WINDOW
    display = build_session_runtime_display(
        runtime_view=_runtime_view(signal_at=signal_at),
        capabilities=_managed_capabilities(),
        ended_at=None,
        last_activity_at=signal_at - timedelta(microseconds=1),
        user_messages=2,
        assistant_messages=1,
        has_visible_transcript_preview=False,
        now=PINNED_NOW,
    )

    assert display.state == TRANSCRIPT_SYNC_STATE
    assert display.headline == "Syncing"
    assert display.detail == "Waiting for transcript"

    expired_signal_at = PINNED_NOW - TRANSCRIPT_SYNC_DISPLAY_WINDOW - timedelta(microseconds=1)
    expired = build_session_runtime_display(
        runtime_view=_runtime_view(signal_at=expired_signal_at),
        capabilities=_managed_capabilities(),
        ended_at=None,
        last_activity_at=expired_signal_at - timedelta(microseconds=1),
        user_messages=2,
        assistant_messages=1,
        has_visible_transcript_preview=False,
        now=PINNED_NOW,
    )

    assert expired.state == "idle"
    assert expired.headline == "Idle"
    assert expired.detail == "Waiting for next prompt"


def test_runtime_transcript_sync_freshness_contract_suppresses_when_evidence_catches_up():
    signal_at = PINNED_NOW
    visible_preview = build_session_runtime_display(
        runtime_view=_runtime_view(signal_at=signal_at),
        capabilities=_managed_capabilities(),
        ended_at=None,
        last_activity_at=signal_at - timedelta(microseconds=1),
        user_messages=2,
        assistant_messages=1,
        has_visible_transcript_preview=True,
        now=PINNED_NOW,
    )
    durable_newer_than_signal = build_session_runtime_display(
        runtime_view=_runtime_view(signal_at=signal_at),
        capabilities=_managed_capabilities(),
        ended_at=None,
        last_activity_at=signal_at + timedelta(microseconds=1),
        user_messages=2,
        assistant_messages=1,
        has_visible_transcript_preview=False,
        now=PINNED_NOW,
    )

    assert visible_preview.state == "idle"
    assert visible_preview.headline == "Idle"
    assert durable_newer_than_signal.state == "idle"
    assert durable_newer_than_signal.headline == "Idle"
