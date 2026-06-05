"""Maintenance backfill for hot session preview columns.

This module is intentionally not used by request-time list endpoints. It is the
bounded legacy bridge for sessions created before hot preview columns existed.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSessionBranch
from zerg.models.agents import TimelineCard
from zerg.services.provisional_events import durable_transcript_event_predicate
from zerg.services.provisional_events import visible_transcript_event_predicate
from zerg.services.session_hot_cards import upsert_timeline_card_from_session

SESSION_FIRST_USER_PREVIEW_CHARS = 300
SESSION_LAST_VISIBLE_PREVIEW_CHARS = 500


@dataclass(frozen=True)
class SessionPreviewBackfillResult:
    selected_sessions: int
    updated_sessions: int
    updated_timeline_cards: int
    first_user_filled: int
    last_visible_filled: int


def backfill_missing_session_previews(
    db: Session,
    *,
    limit: int = 500,
) -> SessionPreviewBackfillResult:
    """Fill missing hot preview columns from legacy events for a bounded batch.

    The caller owns transaction commit/rollback. Keeping this as an explicit
    maintenance primitive prevents list endpoints from quietly falling back to
    cold event-table reads.
    """

    if limit <= 0:
        raise ValueError("limit must be positive")

    sessions = (
        db.query(AgentSession)
        .outerjoin(TimelineCard, TimelineCard.session_id == AgentSession.id)
        .filter(
            or_(
                AgentSession.first_user_message_preview.is_(None),
                AgentSession.last_visible_text_preview.is_(None),
                TimelineCard.session_id.is_(None),
            )
        )
        .order_by(AgentSession.last_activity_at.desc().nullslast(), AgentSession.started_at.desc(), AgentSession.id.asc())
        .limit(limit)
        .all()
    )
    if not sessions:
        return SessionPreviewBackfillResult(
            selected_sessions=0,
            updated_sessions=0,
            updated_timeline_cards=0,
            first_user_filled=0,
            last_visible_filled=0,
        )

    session_ids = [session.id for session in sessions]
    missing_first_ids = [session.id for session in sessions if not _has_preview(session.first_user_message_preview)]
    missing_last_ids = [session.id for session in sessions if not _has_preview(session.last_visible_text_preview)]

    first_user_map = _preview_map(
        db,
        session_ids=missing_first_ids,
        kind="first_user",
        max_len=SESSION_FIRST_USER_PREVIEW_CHARS,
    )
    last_visible_map = _preview_map(
        db,
        session_ids=missing_last_ids,
        kind="last_visible",
        max_len=SESSION_LAST_VISIBLE_PREVIEW_CHARS,
    )
    cards_by_session = {card.session_id: card for card in db.query(TimelineCard).filter(TimelineCard.session_id.in_(session_ids)).all()}

    updated_sessions = 0
    updated_cards: set[UUID] = set()
    first_user_filled = 0
    last_visible_filled = 0
    for session in sessions:
        session_changed = False
        card = cards_by_session.get(session.id)

        first_user = first_user_map.get(session.id)
        if first_user and not _has_preview(session.first_user_message_preview):
            session.first_user_message_preview = first_user
            session_changed = True
            first_user_filled += 1
        if first_user and card is not None and not _has_preview(card.first_user_message_preview):
            card.first_user_message_preview = first_user
            updated_cards.add(session.id)

        last_visible = last_visible_map.get(session.id)
        if last_visible and not _has_preview(session.last_visible_text_preview):
            session.last_visible_text_preview = last_visible
            session_changed = True
            last_visible_filled += 1
        if last_visible and card is not None and not _has_preview(card.last_visible_text_preview):
            card.last_visible_text_preview = last_visible
            updated_cards.add(session.id)

        if session_changed:
            updated_sessions += 1
        if session_changed or card is None or session.id in updated_cards:
            upsert_timeline_card_from_session(db, session)
            updated_cards.add(session.id)

    return SessionPreviewBackfillResult(
        selected_sessions=len(sessions),
        updated_sessions=updated_sessions,
        updated_timeline_cards=len(updated_cards),
        first_user_filled=first_user_filled,
        last_visible_filled=last_visible_filled,
    )


def _preview_map(
    db: Session,
    *,
    session_ids: list[UUID],
    kind: str,
    max_len: int,
) -> dict[UUID, str]:
    if not session_ids:
        return {}

    head_branches = (
        select(
            AgentSessionBranch.session_id.label("session_id"),
            func.max(AgentSessionBranch.id).label("head_branch_id"),
        )
        .where(AgentSessionBranch.session_id.in_(session_ids))
        .where(AgentSessionBranch.is_head == 1)
        .group_by(AgentSessionBranch.session_id)
        .subquery()
    )
    if kind == "first_user":
        order_by = (AgentEvent.timestamp.asc(), AgentEvent.id.asc())
        transcript_predicate = durable_transcript_event_predicate()
        role_filter = AgentEvent.role == "user"
        content_filter = func.lower(func.trim(AgentEvent.content_text)) != "warmup"
    elif kind == "last_visible":
        order_by = (AgentEvent.timestamp.desc(), AgentEvent.id.desc())
        transcript_predicate = visible_transcript_event_predicate()
        role_filter = AgentEvent.role.in_(("user", "assistant"))
        content_filter = or_(AgentEvent.role != "assistant", AgentEvent.tool_name.is_(None))
    else:
        raise ValueError(f"unsupported preview kind: {kind}")

    row_number = (
        func.row_number()
        .over(
            partition_by=AgentEvent.session_id,
            order_by=order_by,
        )
        .label("rn")
    )
    subquery = (
        select(
            AgentEvent.session_id.label("session_id"),
            AgentEvent.content_text.label("content_text"),
            row_number,
        )
        .select_from(AgentEvent)
        .join(AgentSession, AgentSession.id == AgentEvent.session_id)
        .outerjoin(head_branches, AgentEvent.session_id == head_branches.c.session_id)
        .where(AgentEvent.session_id.in_(session_ids))
        .where(or_(head_branches.c.head_branch_id.is_(None), AgentEvent.branch_id == head_branches.c.head_branch_id))
        .where(or_(AgentSession.primary_thread_id.is_(None), AgentEvent.thread_id == AgentSession.primary_thread_id))
        .where(transcript_predicate)
        .where(role_filter)
        .where(AgentEvent.content_text.isnot(None))
        .where(content_filter)
        .subquery()
    )
    rows = db.execute(select(subquery.c.session_id, subquery.c.content_text).where(subquery.c.rn == 1)).all()
    result: dict[UUID, str] = {}
    for session_id, content in rows:
        preview = _bounded_preview(content, max_len=max_len)
        if preview:
            result[session_id] = preview
    return result


def _bounded_preview(value: str | None, *, max_len: int) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped[:max_len]


def _has_preview(value: str | None) -> bool:
    return bool(value and value.strip())
