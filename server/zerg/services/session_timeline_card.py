"""Timeline-card presentation contract.

``runtime_display`` exposes the runtime truth axes.  This module collapses
those axes into the small set of labels and tones that every timeline client
should render consistently.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from zerg.services.session_runtime_display import SessionRuntimeDisplay


@dataclass(frozen=True)
class TimelineBadgePresentation:
    label: str
    tone: str


@dataclass(frozen=True)
class TimelineStatusPresentation:
    label: str
    tone: str
    seen_at: datetime | None = None


@dataclass(frozen=True)
class TimelineCardPresentation:
    ownership: TimelineBadgePresentation
    status: TimelineStatusPresentation | None
    border_tone: str


def build_timeline_card_presentation(
    *,
    runtime_display: SessionRuntimeDisplay | None,
    last_live_at: datetime | None,
    last_activity_at: datetime | None,
    managed_fallback: bool,
) -> TimelineCardPresentation:
    control_path = runtime_display.control_path if runtime_display is not None else ("managed" if managed_fallback else "unmanaged")
    ownership = TimelineBadgePresentation(
        label="Managed" if control_path == "managed" else "Unmanaged",
        tone="neutral",
    )

    if runtime_display is None:
        return TimelineCardPresentation(
            ownership=ownership,
            status=TimelineStatusPresentation(label="Unknown", tone="inactive"),
            border_tone="inactive",
        )

    status = _status_presentation(
        runtime_display=runtime_display,
        last_live_at=last_live_at,
        last_activity_at=last_activity_at,
    )
    return TimelineCardPresentation(
        ownership=ownership,
        status=status,
        border_tone=status.tone,
    )


def _status_presentation(
    *,
    runtime_display: SessionRuntimeDisplay,
    last_live_at: datetime | None,
    last_activity_at: datetime | None,
) -> TimelineStatusPresentation:
    if runtime_display.lifecycle == "closed":
        return TimelineStatusPresentation(label="Closed", tone="closed")

    seen_at = last_live_at or last_activity_at
    if runtime_display.control_path == "managed":
        return _managed_status(runtime_display, seen_at=seen_at)
    return _unmanaged_status(runtime_display, seen_at=seen_at)


def _managed_status(
    runtime_display: SessionRuntimeDisplay,
    *,
    seen_at: datetime | None,
) -> TimelineStatusPresentation:
    if runtime_display.is_stalled or runtime_display.state == "stalled":
        return TimelineStatusPresentation(label="Stalled", tone="stalled", seen_at=seen_at)
    if runtime_display.is_executing or runtime_display.state in {"thinking", "running"}:
        tone = runtime_display.tone if runtime_display.tone in {"thinking", "running"} else "running"
        return TimelineStatusPresentation(label="Working", tone=tone)
    if runtime_display.needs_attention or runtime_display.state == "blocked":
        return TimelineStatusPresentation(label="Needs permission", tone="blocked")
    if runtime_display.is_idle or runtime_display.state in {"idle", "needs_user"}:
        return TimelineStatusPresentation(label="Ready", tone="idle")

    if runtime_display.activity_recency in {"live", "recent"} or runtime_display.heuristic_active:
        return TimelineStatusPresentation(label="Recent activity", tone="inferred")
    if runtime_display.activity_recency == "stale":
        return TimelineStatusPresentation(label="Disconnected", tone="inactive", seen_at=seen_at)
    return TimelineStatusPresentation(label="Unknown", tone="inactive")


def _unmanaged_status(
    runtime_display: SessionRuntimeDisplay,
    *,
    seen_at: datetime | None,
) -> TimelineStatusPresentation:
    if (
        runtime_display.activity_recency == "live"
        or runtime_display.is_executing
        or runtime_display.needs_attention
        or runtime_display.host_state == "online"
    ):
        return TimelineStatusPresentation(label="Active", tone="active")
    if runtime_display.activity_recency == "recent" or runtime_display.heuristic_active:
        return TimelineStatusPresentation(label="Recent activity", tone="inferred")
    if runtime_display.activity_recency == "stale":
        return TimelineStatusPresentation(label="Stale", tone="inactive", seen_at=seen_at)
    return TimelineStatusPresentation(label="Unknown", tone="inactive")
