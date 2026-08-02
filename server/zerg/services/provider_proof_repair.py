"""Repair historical provider-proof sessions into the test environment."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import TimelineCard
from zerg.services.internal_sessions import PROVIDER_NOREPLY_MARKER_SQL_LIKE
from zerg.services.internal_sessions import SQL_LIKE_ESCAPE
from zerg.services.internal_sessions import classify_provider_proof_environment
from zerg.services.internal_sessions import provider_proof_session_clause
from zerg.services.provider_interaction_semantics import seed_persisted_provider_interaction_context
from zerg.services.provider_interaction_semantics import seed_provider_interaction_sequence_context
from zerg.services.provider_interaction_semantics import semantic_projection_facts
from zerg.services.raw_json_compression import decode_raw_json


@dataclass(frozen=True)
class ProviderProofRepairResult:
    scanned_sessions: int
    repairable_sessions: int
    updated_sessions: int
    updated_timeline_cards: int
    skipped_false_positives: int
    session_ids: list[str]


def _first_user_event_text(db: Session, session_id: UUID) -> str | None:
    session = db.query(AgentSession).filter(AgentSession.id == session_id).one_or_none()
    provider = str(session.provider or "") if session is not None else ""
    events = (
        db.query(AgentEvent)
        .filter(AgentEvent.session_id == session_id)
        .filter(func.lower(func.coalesce(AgentEvent.role, "")) == "user")
        .filter(AgentEvent.content_text.isnot(None))
        .filter(func.trim(AgentEvent.content_text) != "")
        .order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc())
        .all()
    )
    interaction_sequence_context: dict[str, object] = {}
    raw_values = [decode_raw_json(event) for event in events]
    seed_provider_interaction_sequence_context(provider, raw_values, interaction_sequence_context)
    seed_persisted_provider_interaction_context(
        provider,
        events,
        interaction_sequence_context,
    )
    for event, raw_json in zip(events, raw_values, strict=True):
        semantics = semantic_projection_facts(
            provider,
            role=event.role,
            content_text=event.content_text,
            raw_json=raw_json,
            interaction_kind=getattr(event, "interaction_kind", None),
            title_eligible=(event.title_eligible if raw_json is None else None),
            sequence_context=interaction_sequence_context,
        )
        if semantics["title_eligible"]:
            return event.content_text
    return None


def repair_provider_proof_session_environments(
    db: Session,
    *,
    limit: int = 500,
    apply: bool = False,
    include_event_scan: bool = False,
) -> ProviderProofRepairResult:
    """Mark historical provider-proof rows as environment=test.

    Candidate selection is intentionally broad and the in-Python classifier is
    the authority. That lets the query stay simple while avoiding false-positive
    repairs for normal user text that happens to mention proof/no-reply flows.
    """
    if limit <= 0:
        return ProviderProofRepairResult(
            scanned_sessions=0,
            repairable_sessions=0,
            updated_sessions=0,
            updated_timeline_cards=0,
            skipped_false_positives=0,
            session_ids=[],
        )

    rows = (
        db.query(AgentSession)
        .filter(AgentSession.environment.notin_(["test", "e2e"]))
        .filter(provider_proof_session_clause(AgentSession))
        .order_by(AgentSession.last_activity_at.desc().nullslast(), AgentSession.started_at.desc())
        .limit(limit)
        .all()
    )
    seen_session_ids = {session.id for session in rows}
    if include_event_scan and len(rows) < limit:
        event_rows = (
            db.query(AgentSession)
            .join(AgentEvent, AgentEvent.session_id == AgentSession.id)
            .filter(AgentSession.environment.notin_(["test", "e2e"]))
            .filter(func.lower(func.coalesce(AgentEvent.role, "")) == "user")
            .filter(
                func.trim(func.coalesce(AgentEvent.content_text, "")).like(
                    PROVIDER_NOREPLY_MARKER_SQL_LIKE,
                    escape=SQL_LIKE_ESCAPE,
                )
            )
            .order_by(AgentSession.last_activity_at.desc().nullslast(), AgentSession.started_at.desc())
            .limit(limit)
            .all()
        )
        for session in event_rows:
            if session.id in seen_session_ids:
                continue
            rows.append(session)
            seen_session_ids.add(session.id)
            if len(rows) >= limit:
                break

    repairable: list[AgentSession] = []
    skipped_false_positives = 0
    for session in rows:
        # Provider proof sessions are expected to start with the no-reply
        # marker. Later user text is not enough to reclassify a real session.
        first_user_text = session.first_user_message_preview
        if include_event_scan and not first_user_text:
            first_user_text = _first_user_event_text(db, session.id)
        if classify_provider_proof_environment(cwd=session.cwd, first_user_text=first_user_text) != "test":
            skipped_false_positives += 1
            continue
        repairable.append(session)

    updated_sessions = 0
    updated_timeline_cards = 0
    if apply:
        for session in repairable:
            session.environment = "test"
            updated_sessions += 1
            updated_timeline_cards += (
                db.query(TimelineCard)
                .filter(TimelineCard.session_id == session.id)
                .update({"environment": "test"}, synchronize_session=False)
            )

    return ProviderProofRepairResult(
        scanned_sessions=len(rows),
        repairable_sessions=len(repairable),
        updated_sessions=updated_sessions,
        updated_timeline_cards=updated_timeline_cards,
        skipped_false_positives=skipped_false_positives,
        session_ids=[str(session.id) for session in repairable],
    )
