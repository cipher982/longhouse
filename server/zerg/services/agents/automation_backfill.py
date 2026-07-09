"""Conservative repair helpers for Hatch automation session origin."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionThread
from zerg.models.agents import TimelineCard

HATCH_AUTOMATION_ORIGIN_KIND = "hatch_automation"
_HATCH_BACKED_PROVIDERS = {"opencode", "claude", "codex", "cursor"}
_HATCH_PROMPT_HINTS = (
    "code review",
    "review this branch",
    "review the current branch",
    "final review",
    "quick phase review",
    "phase review",
    "drill down",
)


@dataclass(frozen=True)
class AutomationBackfillResult:
    """Result payload for the report-only Hatch automation repair."""

    applied_session_ids: list[str]
    missing_session_ids: list[str]
    already_marked_session_ids: list[str]
    heuristic_candidates: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied_session_ids": self.applied_session_ids,
            "missing_session_ids": self.missing_session_ids,
            "already_marked_session_ids": self.already_marked_session_ids,
            "heuristic_candidate_count": len(self.heuristic_candidates),
            "heuristic_candidates": self.heuristic_candidates,
        }


def _normalize_session_id(value: str | UUID) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _event_preview(db: Session, session_id: UUID) -> str:
    row = (
        db.query(AgentEvent.content_text)
        .filter(AgentEvent.session_id == session_id)
        .filter(AgentEvent.role == "user")
        .order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc())
        .first()
    )
    return str(row[0] or "").strip() if row else ""


def _source_path_preview(db: Session, session_id: UUID) -> str:
    row = (
        db.query(AgentEvent.source_path)
        .filter(AgentEvent.session_id == session_id)
        .filter(AgentEvent.source_path.is_not(None))
        .order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc())
        .first()
    )
    return str(row[0] or "").strip() if row else ""


def _candidate_dict(db: Session, session: AgentSession, thread: SessionThread) -> dict[str, Any]:
    prompt = (session.first_user_message_preview or _event_preview(db, session.id))[:500]
    source_path = _source_path_preview(db, session.id)
    return {
        "session_id": str(session.id),
        "thread_id": str(thread.id),
        "provider": session.provider,
        "project": session.project,
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "user_messages": session.user_messages,
        "branch_kind": thread.branch_kind,
        "confidence": "medium",
        "reason": "hatch-shaped prompt/provider/root-thread; report-only until reviewed",
        "prompt_preview": prompt,
        "source_path_preview": source_path,
    }


def find_hatch_automation_candidates(db: Session, *, limit: int = 100) -> list[dict[str, Any]]:
    """Report medium-confidence Hatch-shaped rows without mutating them."""

    rows = (
        db.query(AgentSession, SessionThread)
        .join(SessionThread, SessionThread.session_id == AgentSession.id)
        .filter(SessionThread.is_primary == 1)
        .filter(SessionThread.branch_kind == "root")
        .filter(AgentSession.provider.in_(_HATCH_BACKED_PROVIDERS))
        .filter(or_(AgentSession.origin_kind.is_(None), AgentSession.origin_kind == ""))
        .filter(AgentSession.hidden_from_default_timeline == 0)
        .filter(AgentSession.user_messages <= 2)
        .order_by(AgentSession.started_at.desc())
        .limit(max(1, min(limit, 500)))
        .all()
    )
    candidates: list[dict[str, Any]] = []
    for session, thread in rows:
        prompt = (session.first_user_message_preview or _event_preview(db, session.id)).lower()
        if not any(hint in prompt for hint in _HATCH_PROMPT_HINTS):
            continue
        candidates.append(_candidate_dict(db, session, thread))
    return candidates


def classify_reviewed_hatch_automation_sessions(
    db: Session,
    *,
    session_ids: list[str | UUID],
    apply: bool,
    candidate_limit: int = 100,
) -> AutomationBackfillResult:
    """Mark explicit reviewed rows as Hatch automation; heuristics stay report-only."""

    normalized_ids = [_normalize_session_id(value) for value in session_ids]
    sessions_by_id: dict[UUID, AgentSession] = {}
    if normalized_ids:
        sessions = db.query(AgentSession).filter(AgentSession.id.in_(normalized_ids)).all()
        sessions_by_id = {session.id: session for session in sessions}

    missing = [str(session_id) for session_id in normalized_ids if session_id not in sessions_by_id]
    already_marked: list[str] = []
    applied: list[str] = []

    if apply:
        for session_id in normalized_ids:
            session = sessions_by_id.get(session_id)
            if session is None:
                continue
            if session.origin_kind == HATCH_AUTOMATION_ORIGIN_KIND and session.hidden_from_default_timeline == 1:
                already_marked.append(str(session_id))
                continue

            session.origin_kind = HATCH_AUTOMATION_ORIGIN_KIND
            session.hidden_from_default_timeline = 1
            for thread in db.query(SessionThread).filter(SessionThread.session_id == session_id).all():
                thread.origin_kind = HATCH_AUTOMATION_ORIGIN_KIND
                thread.hidden_from_default_timeline = 1
            card = db.get(TimelineCard, session_id)
            if card is not None:
                card.origin_kind = HATCH_AUTOMATION_ORIGIN_KIND
                card.hidden_from_default_timeline = 1
            applied.append(str(session_id))
        db.commit()

    return AutomationBackfillResult(
        applied_session_ids=applied,
        missing_session_ids=missing,
        already_marked_session_ids=already_marked,
        heuristic_candidates=find_hatch_automation_candidates(db, limit=candidate_limit),
    )
