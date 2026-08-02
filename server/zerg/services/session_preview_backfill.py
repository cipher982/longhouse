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
from zerg.services.provider_interaction_semantics import seed_persisted_provider_interaction_context
from zerg.services.provider_interaction_semantics import seed_provider_interaction_sequence_context
from zerg.services.provider_interaction_semantics import semantic_projection_facts
from zerg.services.provisional_events import durable_transcript_event_predicate
from zerg.services.provisional_events import visible_transcript_event_predicate
from zerg.services.raw_json_compression import decode_raw_json
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
    last_user_filled: int
    last_assistant_filled: int


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
                AgentSession.last_user_message_preview.is_(None),
                AgentSession.last_assistant_message_preview.is_(None),
                TimelineCard.first_user_message_preview.is_(None),
                TimelineCard.last_visible_text_preview.is_(None),
                TimelineCard.last_user_message_preview.is_(None),
                TimelineCard.last_assistant_message_preview.is_(None),
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
            last_user_filled=0,
            last_assistant_filled=0,
        )

    session_ids = [session.id for session in sessions]
    missing_first_ids = [session.id for session in sessions if not _has_preview(session.first_user_message_preview)]
    missing_last_ids = [session.id for session in sessions if not _has_preview(session.last_visible_text_preview)]
    missing_last_user_ids = [session.id for session in sessions if not _has_preview(session.last_user_message_preview)]
    missing_last_assistant_ids = [session.id for session in sessions if not _has_preview(session.last_assistant_message_preview)]

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
    last_user_map = _preview_map(
        db,
        session_ids=missing_last_user_ids,
        kind="last_user",
        max_len=SESSION_FIRST_USER_PREVIEW_CHARS,
    )
    last_assistant_map = _preview_map(
        db,
        session_ids=missing_last_assistant_ids,
        kind="last_assistant",
        max_len=SESSION_LAST_VISIBLE_PREVIEW_CHARS,
    )
    cards_by_session = {card.session_id: card for card in db.query(TimelineCard).filter(TimelineCard.session_id.in_(session_ids)).all()}

    updated_sessions = 0
    updated_cards: set[UUID] = set()
    first_user_filled = 0
    last_visible_filled = 0
    last_user_filled = 0
    last_assistant_filled = 0
    for session in sessions:
        session_changed = False
        card = cards_by_session.get(session.id)

        first_user = (
            session.first_user_message_preview if _has_preview(session.first_user_message_preview) else first_user_map.get(session.id)
        )
        if first_user and not _has_preview(session.first_user_message_preview):
            session.first_user_message_preview = first_user
            session_changed = True
            first_user_filled += 1
        if first_user and card is not None and not _has_preview(card.first_user_message_preview):
            card.first_user_message_preview = first_user
            updated_cards.add(session.id)

        last_visible = (
            session.last_visible_text_preview if _has_preview(session.last_visible_text_preview) else last_visible_map.get(session.id)
        )
        if last_visible and not _has_preview(session.last_visible_text_preview):
            session.last_visible_text_preview = last_visible
            session_changed = True
            last_visible_filled += 1
        if last_visible and card is not None and not _has_preview(card.last_visible_text_preview):
            card.last_visible_text_preview = last_visible
            updated_cards.add(session.id)

        last_user = session.last_user_message_preview if _has_preview(session.last_user_message_preview) else last_user_map.get(session.id)
        if last_user and not _has_preview(session.last_user_message_preview):
            session.last_user_message_preview = last_user
            session_changed = True
            last_user_filled += 1
        if last_user and card is not None and not _has_preview(card.last_user_message_preview):
            card.last_user_message_preview = last_user
            updated_cards.add(session.id)

        last_assistant = (
            session.last_assistant_message_preview
            if _has_preview(session.last_assistant_message_preview)
            else last_assistant_map.get(session.id)
        )
        if last_assistant and not _has_preview(session.last_assistant_message_preview):
            session.last_assistant_message_preview = last_assistant
            session_changed = True
            last_assistant_filled += 1
        if last_assistant and card is not None and not _has_preview(card.last_assistant_message_preview):
            card.last_assistant_message_preview = last_assistant
            updated_cards.add(session.id)

        if session_changed:
            updated_sessions += 1
        if session_changed or card is None:
            upsert_timeline_card_from_session(db, session)
            updated_cards.add(session.id)

    return SessionPreviewBackfillResult(
        selected_sessions=len(sessions),
        updated_sessions=updated_sessions,
        updated_timeline_cards=len(updated_cards),
        first_user_filled=first_user_filled,
        last_visible_filled=last_visible_filled,
        last_user_filled=last_user_filled,
        last_assistant_filled=last_assistant_filled,
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
    elif kind == "last_user":
        # Replay chronologically so a Claude command row can see an earlier
        # caveat before we choose the final eligible message.
        order_by = (AgentEvent.timestamp.asc(), AgentEvent.id.asc())
        transcript_predicate = durable_transcript_event_predicate()
        role_filter = AgentEvent.role == "user"
        content_filter = func.lower(func.trim(AgentEvent.content_text)) != "warmup"
    elif kind == "last_assistant":
        order_by = (AgentEvent.timestamp.desc(), AgentEvent.id.desc())
        transcript_predicate = visible_transcript_event_predicate()
        role_filter = AgentEvent.role == "assistant"
        content_filter = AgentEvent.tool_name.is_(None)
    elif kind == "last_visible":
        # The same ordering is required for provider sequence context. The
        # reducer below overwrites the candidate as later visible rows arrive.
        order_by = (AgentEvent.timestamp.asc(), AgentEvent.id.asc())
        transcript_predicate = visible_transcript_event_predicate()
        role_filter = AgentEvent.role.in_(("user", "assistant"))
        content_filter = or_(AgentEvent.role != "assistant", AgentEvent.tool_name.is_(None))
    else:
        raise ValueError(f"unsupported preview kind: {kind}")

    rows = list(
        db.execute(
            select(
                AgentEvent.session_id,
                AgentEvent.role,
                AgentEvent.content_text,
                AgentEvent.tool_name,
                AgentEvent.raw_json,
                AgentEvent.raw_json_z,
                AgentEvent.raw_json_codec,
                AgentEvent.interaction_kind,
                AgentEvent.interaction_context_key,
                AgentEvent.title_eligible,
                AgentSession.provider,
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
            .order_by(AgentEvent.session_id.asc(), *order_by)
        ).yield_per(256)
    )
    result: dict[UUID, str] = {}
    interaction_sequence_contexts: dict[UUID, dict[str, object]] = {}
    raw_values_by_session: dict[UUID, list[object]] = {}
    events_by_session: dict[UUID, list[AgentEvent]] = {}
    providers_by_session: dict[UUID, str] = {}
    for event in rows:
        raw_json = decode_raw_json(event)
        session_id = event.session_id
        providers_by_session.setdefault(session_id, str(event.provider or ""))
        raw_values_by_session.setdefault(session_id, []).append(raw_json)
        events_by_session.setdefault(session_id, []).append(event)
    for session_id, raw_values in raw_values_by_session.items():
        sequence_context = interaction_sequence_contexts.setdefault(session_id, {})
        seed_provider_interaction_sequence_context(
            providers_by_session[session_id],
            raw_values,
            sequence_context,
        )
        seed_persisted_provider_interaction_context(
            providers_by_session[session_id],
            events_by_session[session_id],
            sequence_context,
        )
    for event in rows:
        session_id = event.session_id
        content = event.content_text
        if session_id in result and kind in {"first_user", "last_assistant"}:
            continue
        sequence_context = interaction_sequence_contexts.setdefault(session_id, {})
        raw_json = decode_raw_json(event)
        semantics = semantic_projection_facts(
            event.provider,
            role=event.role,
            content_text=content,
            raw_json=raw_json,
            interaction_kind=event.interaction_kind,
            title_eligible=(event.title_eligible if raw_json is None else None),
            sequence_context=sequence_context,
        )
        if event.role == "user" and not semantics["title_eligible"]:
            continue
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
