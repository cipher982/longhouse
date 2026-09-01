from datetime import datetime
from datetime import timedelta
from datetime import timezone
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.services.provisional_events import EVENT_ORIGIN_LIVE_PROVISIONAL
from zerg.services.provisional_events import TranscriptPreview
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
