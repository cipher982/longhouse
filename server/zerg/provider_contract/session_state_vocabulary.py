"""Pure session-state vocabulary shared by the product and the verifier.

Leaf module (stdlib and pydantic only). The transcript and host axes of the
served session-state facts, and the projector for the transcript axis, live
here so the verifier's harness builds the same classes the product serves
without importing the product's session-state contract (and its closure).
``zerg.services.session_state_contract`` re-exports every name below.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict

TranscriptConvergence = Literal["current", "lagging", "unknown"]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)


def normalize_utc(value: datetime | None) -> datetime | None:
    """None stays None, naive is treated as UTC, aware is converted to UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _clean(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


class SessionTranscriptFacts(FrozenModel):
    convergence: TranscriptConvergence
    source_revision: int | None = None
    durable_revision: int | None = None
    render_revision: int | None = None
    # The commit that last changed anything the transcript rows show: the one
    # coordinate clients compare to know their rows are behind
    # (transcript-revision-sync.md). 0 before the first transcript write.
    transcript_revision: int = 0
    last_append_at: datetime | None = None
    searchable: bool = False
    live_observation: bool = False


class SessionHostFacts(FrozenModel):
    state: Literal["online", "stale", "offline", "unknown"]
    observed_at: datetime | None = None


def _transcript(
    *,
    session: Any,
    last_activity_at: datetime | None,
    has_visible_transcript_preview: bool,
    has_pending_response_turn: bool,
    user_messages: int,
    assistant_messages: int,
    archive_state: str | None,
    live_observation: bool,
    source_revision: int | None = None,
    durable_revision: int | None = None,
    render_revision: int | None = None,
    transcript_revision: int | None = None,
    last_append_at: datetime | None = None,
) -> SessionTranscriptFacts:
    legacy_revision = int(getattr(session, "transcript_revision", 0) or 0)
    normalized_archive = _clean(archive_state)
    revisions = tuple(value for value in (source_revision, durable_revision, render_revision) if value is not None)
    has_expected_content = bool(
        any(value > 0 for value in revisions)
        or legacy_revision > 0
        or user_messages > 0
        or assistant_messages > 0
        or has_pending_response_turn
        or has_visible_transcript_preview
    )
    # `source_revision` and `render_revision` are content watermarks. The
    # durable revision is the last mutation of the session row on the global
    # catalog sequence, so ordinary metadata/read-state writes can advance it
    # after a render is current. It is not a transcript coverage coordinate.
    revision_lag = source_revision > render_revision if source_revision is not None and render_revision is not None else None
    # Missing replies describe provider progress, not missing transcript bytes.
    # Keep that legacy hint only when content coverage coordinates are unknown.
    lagging = bool(
        revision_lag
        or (normalized_archive == "pending" and has_expected_content)
        or (
            revision_lag is None
            and not has_visible_transcript_preview
            and (has_pending_response_turn or user_messages > assistant_messages)
        )
    )
    if lagging:
        convergence: TranscriptConvergence = "lagging"
    elif normalized_archive == "pending" and not has_expected_content:
        # A newly-created control-only Console shell has no source revision to
        # catch up to.  Calling it "syncing" turns ordinary empty state into a
        # repair incident and can persist forever until the first message.
        convergence = "current"
    elif revision_lag is False or normalized_archive in {"current", "legacy_hot"}:
        convergence = "current"
    else:
        convergence = "unknown"
    return SessionTranscriptFacts(
        convergence=convergence,
        source_revision=source_revision,
        durable_revision=durable_revision,
        render_revision=render_revision,
        transcript_revision=int(transcript_revision or 0),
        last_append_at=normalize_utc(last_append_at),
        searchable=bool(legacy_revision > 0 or user_messages > 0 or assistant_messages > 0),
        live_observation=live_observation,
    )


def project_transcript_facts(
    *,
    session: Any,
    last_activity_at: datetime | None,
    has_visible_transcript_preview: bool = False,
    has_pending_response_turn: bool = False,
    user_messages: int = 0,
    assistant_messages: int = 0,
    archive_state: str | None = None,
    live_observation: bool = False,
    source_revision: int | None = None,
    durable_revision: int | None = None,
    render_revision: int | None = None,
    transcript_revision: int | None = None,
    transcript_last_append_at: datetime | None = None,
) -> SessionTranscriptFacts:
    """Project the current bounded transcript axis from catalog coordinates."""

    return _transcript(
        session=session,
        last_activity_at=last_activity_at,
        has_visible_transcript_preview=has_visible_transcript_preview,
        has_pending_response_turn=has_pending_response_turn,
        user_messages=user_messages,
        assistant_messages=assistant_messages,
        archive_state=archive_state,
        live_observation=live_observation,
        source_revision=source_revision,
        durable_revision=durable_revision,
        render_revision=render_revision,
        transcript_revision=transcript_revision,
        last_append_at=transcript_last_append_at,
    )
