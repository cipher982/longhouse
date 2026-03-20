"""Oikos run history endpoints."""

import json
import logging
import os
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Dict
from typing import List
from typing import Literal
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload
from sse_starlette.sse import EventSourceResponse

from zerg.database import get_db
from zerg.dependencies.oikos_auth import get_current_oikos_user
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionTurnReview
from zerg.models.enums import RunStatus
from zerg.models.models import Fiche
from zerg.models.models import Run
from zerg.models.models import ThreadMessage
from zerg.models.run_event import RunEvent
from zerg.models.work import OikosWakeup
from zerg.services.session_turn_reviews import approve_pending_turn_review
from zerg.services.session_turn_reviews import dismiss_pending_turn_review
from zerg.utils.time import UTCBaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["oikos"])


class OikosRunSummary(UTCBaseModel):
    """Minimal run summary for Oikos run triage."""

    id: int
    automation_id: int
    thread_id: Optional[int] = None
    automation_name: str
    status: str
    summary: Optional[str] = None
    signal: Optional[str] = None
    signal_source: Optional[str] = None
    error: Optional[str] = None
    last_event_type: Optional[str] = None
    last_event_message: Optional[str] = None
    last_event_at: Optional[datetime] = None
    continuation_of_run_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None


class OikosWakeupSummary(UTCBaseModel):
    """Minimal proactive wakeup summary for operator-mode review."""

    id: int
    source: str
    trigger_type: str
    status: str
    reason: Optional[str] = None
    session_id: Optional[str] = None
    conversation_id: Optional[str] = None
    wakeup_key: Optional[str] = None
    run_id: Optional[int] = None
    payload: Optional[Dict[str, Any]] = None
    created_at: datetime


class SessionTurnReviewSummary(UTCBaseModel):
    """Deterministic review of one completed assistant turn."""

    id: int
    session_id: str
    assistant_event_id: int
    turn_index: int
    trigger_type: str
    loop_mode: str
    decision: str
    summary: str
    rationale: Optional[str] = None
    turn_excerpt: Optional[str] = None
    mode_capability: Optional[str] = None
    mode_summary: Optional[str] = None
    execution_state: Optional[str] = None
    recommended_action: Optional[str] = None
    follow_up_prompt: Optional[str] = None
    blocked_reasons: List[str] = []
    status: str
    reason: Optional[str] = None
    run_id: Optional[int] = None
    actual_outcome: Optional[str] = None
    shadow_alignment: Optional[str] = None
    created_at: datetime


class LoopInboxItem(UTCBaseModel):
    """Thin mobile-friendly summary of one session that needs attention."""

    card_id: int
    session_id: str
    title: str
    project: Optional[str] = None
    machine: Optional[str] = None
    provider: Optional[str] = None
    loop_mode: str
    decision: str
    execution_state: Optional[str] = None
    summary: str
    recommended_action: Optional[str] = None
    follow_up_prompt: Optional[str] = None
    blocked_reasons: List[str] = []
    last_turn_at: datetime
    card_state: str = "active"
    card_state_reason: Optional[str] = None
    superseded_by_card_id: Optional[int] = None
    requires_attention: bool


class LoopActionCard(LoopInboxItem):
    """Action-card payload for a single phone-first session follow-up."""

    rationale: Optional[str] = None
    mode_capability: Optional[str] = None
    mode_summary: Optional[str] = None
    last_user_text: Optional[str] = None
    last_assistant_text: Optional[str] = None
    available_actions: List[str] = []


class LoopInboxActionRequest(UTCBaseModel):
    """Bounded phone-first action request for one inbox item."""

    action: Literal["approve_recommended_action", "not_now"]


class LoopInboxActionResult(UTCBaseModel):
    """Result of acting on one loop inbox item."""

    session_id: str
    review_id: int
    action: str
    status: str
    reason: Optional[str] = None
    queued_job_id: Optional[int] = None


def _get_owned_run(db: Session, *, run_id: int, owner_id: int) -> Run | None:
    query = db.query(Run).join(Fiche, Fiche.id == Run.fiche_id)
    query = query.filter(Run.id == run_id)
    query = query.filter(Fiche.owner_id == owner_id)
    return query.first()


@router.get("/runs", response_model=List[OikosRunSummary])
def list_oikos_runs(
    limit: int = 50,
    automation_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
) -> List[OikosRunSummary]:
    """List recent Oikos runs for the authenticated user."""
    # Get recent runs scoped to the authenticated user.
    query = db.query(Run).options(selectinload(Run.fiche))
    query = query.join(Fiche, Fiche.id == Run.fiche_id)
    query = query.filter(Fiche.owner_id == current_user.id)

    if automation_id is not None:
        query = query.filter(Run.fiche_id == automation_id)

    runs = query.order_by(Run.created_at.desc()).limit(limit).all()

    run_ids = [run.id for run in runs]
    thread_ids = [run.thread_id for run in runs if run.thread_id]

    last_events_by_run = _get_latest_run_events(db, run_ids)
    last_messages_by_thread = _get_latest_assistant_messages(db, thread_ids)

    summaries = []
    for run in runs:
        automation_name = run.fiche.name if run.fiche else f"Automation {run.fiche_id}"

        summary = getattr(run, "summary", None)

        last_event = last_events_by_run.get(run.id)
        last_event_type = getattr(last_event, "event_type", None) if last_event else None
        last_event_at = getattr(last_event, "created_at", None) if last_event else None
        last_event_message = _extract_event_message(getattr(last_event, "payload", None)) if last_event else None

        signal = summary if summary else None
        signal_source = "summary" if summary else None

        if not signal:
            run_error = getattr(run, "error", None)
            if run_error:
                signal = run_error
                signal_source = "error"

        if not signal and run.thread_id:
            last_message = last_messages_by_thread.get(run.thread_id)
            if last_message:
                signal = last_message
                signal_source = "last_message"

        if not signal and last_event_message:
            signal = last_event_message
            signal_source = "last_event"

        signal = _truncate_signal(signal, 240)

        summaries.append(
            OikosRunSummary(
                id=run.id,
                automation_id=run.fiche_id,
                thread_id=run.thread_id,
                automation_name=automation_name,
                status=run.status.value if hasattr(run.status, "value") else str(run.status),
                summary=summary,
                signal=signal,
                signal_source=signal_source,
                error=getattr(run, "error", None),
                last_event_type=last_event_type,
                last_event_message=last_event_message,
                last_event_at=last_event_at,
                continuation_of_run_id=getattr(run, "continuation_of_run_id", None),
                created_at=run.created_at,
                updated_at=run.updated_at,
                completed_at=run.finished_at,
            )
        )

    return summaries


@router.get("/wakeups", response_model=List[OikosWakeupSummary])
def list_oikos_wakeups(
    limit: int = 50,
    status: Optional[str] = None,
    trigger_type: Optional[str] = None,
    session_id: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
) -> List[OikosWakeupSummary]:
    """List recent proactive Oikos wakeups for the authenticated owner."""
    query = db.query(OikosWakeup).filter(OikosWakeup.owner_id == current_user.id)

    if status:
        query = query.filter(OikosWakeup.status == status)
    if trigger_type:
        query = query.filter(OikosWakeup.trigger_type == trigger_type)
    if session_id:
        query = query.filter(OikosWakeup.session_id == session_id)

    rows = query.order_by(OikosWakeup.created_at.desc(), OikosWakeup.id.desc()).limit(limit).all()
    return [
        OikosWakeupSummary(
            id=row.id,
            source=row.source,
            trigger_type=row.trigger_type,
            status=row.status,
            reason=row.reason,
            session_id=row.session_id,
            conversation_id=row.conversation_id,
            wakeup_key=row.wakeup_key,
            run_id=row.run_id,
            payload=row.payload,
            created_at=row.created_at,
        )
        for row in rows
    ]


_ATTENTION_EXECUTION_STATES = {"awaiting_user_approval", "needs_human"}
_ACTIONABLE_REVIEW_STATUSES = {"recorded", "enqueued"}
_TURN_CONTEXT_EVENT_LIMIT = 160


def _clean_blocked_reasons(value: Any) -> List[str]:
    return [str(reason).strip() for reason in (value or []) if str(reason).strip()]


def _session_title(session: AgentSession | None, session_id: str) -> str:
    if session is not None:
        if session.summary_title and str(session.summary_title).strip():
            return str(session.summary_title).strip()
        if session.project and str(session.project).strip():
            return str(session.project).strip()
        if session.cwd and str(session.cwd).strip():
            return os.path.basename(str(session.cwd).rstrip("/")) or str(session.cwd).strip()
    return f"Session {session_id[:8]}"


def _available_loop_actions(review: SessionTurnReview) -> List[str]:
    actions = ["not_now", "open_full_session"]
    if review.execution_state == "awaiting_user_approval" and review.recommended_action == "continue_session":
        return ["approve_recommended_action", *actions]
    return actions


def _load_review_by_card_id(
    db: Session,
    *,
    owner_id: int,
    card_id: int,
) -> SessionTurnReview | None:
    return (
        db.query(SessionTurnReview)
        .filter(
            SessionTurnReview.owner_id == owner_id,
            SessionTurnReview.id == card_id,
        )
        .first()
    )


def _load_latest_attention_reviews(
    db: Session,
    *,
    owner_id: int,
    limit: int,
) -> List[SessionTurnReview]:
    latest_review_ids = (
        db.query(func.max(SessionTurnReview.id).label("review_id"))
        .filter(SessionTurnReview.owner_id == owner_id)
        .group_by(SessionTurnReview.session_id)
        .subquery()
    )
    return (
        db.query(SessionTurnReview)
        .join(latest_review_ids, SessionTurnReview.id == latest_review_ids.c.review_id)
        .filter(SessionTurnReview.execution_state.in_(tuple(_ATTENTION_EXECUTION_STATES)))
        .filter(SessionTurnReview.status.in_(tuple(_ACTIONABLE_REVIEW_STATUSES)))
        .order_by(SessionTurnReview.created_at.desc(), SessionTurnReview.id.desc())
        .limit(limit)
        .all()
    )


def _load_latest_attention_review_for_session(
    db: Session,
    *,
    owner_id: int,
    session_id: str,
) -> SessionTurnReview | None:
    row = (
        db.query(SessionTurnReview)
        .filter(
            SessionTurnReview.owner_id == owner_id,
            SessionTurnReview.session_id == session_id,
        )
        .order_by(SessionTurnReview.id.desc())
        .first()
    )
    if row is None:
        return None
    if str(row.status or "").strip() not in _ACTIONABLE_REVIEW_STATUSES:
        return None
    if str(row.execution_state or "").strip() not in _ATTENTION_EXECUTION_STATES:
        return None
    return row


def _load_latest_review_for_session(
    db: Session,
    *,
    owner_id: int,
    session_id: str,
) -> SessionTurnReview | None:
    return (
        db.query(SessionTurnReview)
        .filter(
            SessionTurnReview.owner_id == owner_id,
            SessionTurnReview.session_id == session_id,
        )
        .order_by(SessionTurnReview.id.desc())
        .first()
    )


def _load_latest_newer_review_id(
    db: Session,
    *,
    session_id: str,
    review_id: int,
) -> int | None:
    row = (
        db.query(SessionTurnReview.id)
        .filter(
            SessionTurnReview.session_id == session_id,
            SessionTurnReview.id > review_id,
        )
        .order_by(SessionTurnReview.id.desc())
        .first()
    )
    if row is None:
        return None
    return int(row[0])


def _derive_card_state(
    db: Session,
    *,
    review: SessionTurnReview,
) -> tuple[str, Optional[str], Optional[int]]:
    newer_card_id = _load_latest_newer_review_id(db, session_id=str(review.session_id), review_id=int(review.id))
    status = str(review.status or "").strip()
    reason = str(review.reason or "").strip()
    execution_state = str(review.execution_state or "").strip()

    is_attention_review = execution_state in _ATTENTION_EXECUTION_STATES

    if status in _ACTIONABLE_REVIEW_STATUSES and is_attention_review and newer_card_id is not None:
        return "superseded", "A newer turn replaced this follow-up.", newer_card_id
    if status in _ACTIONABLE_REVIEW_STATUSES and is_attention_review:
        return "active", None, None
    if status == "acted":
        if reason == "not_now":
            return "dismissed", "You dismissed this follow-up.", None
        return "acted", "This follow-up already ran or was handled.", None
    if status == "failed":
        return "failed", "Longhouse could not complete this follow-up automatically.", None
    if status == "ignored":
        if reason == "superseded" or newer_card_id is not None:
            return "superseded", "A newer turn replaced this follow-up.", newer_card_id
        return "expired", "This follow-up no longer needs attention.", None
    return "expired", "This follow-up is no longer actionable.", newer_card_id


def _load_session_map(db: Session, session_ids: List[Any]) -> Dict[str, AgentSession]:
    if not session_ids:
        return {}
    rows = db.query(AgentSession).filter(AgentSession.id.in_(session_ids)).all()
    return {str(row.id): row for row in rows}


def _load_turn_context(
    db: Session,
    *,
    session_id: str,
    assistant_event_id: int,
) -> tuple[Optional[str], Optional[str]]:
    rows = (
        db.query(AgentEvent.id, AgentEvent.role, AgentEvent.content_text)
        .filter(
            AgentEvent.session_id == session_id,
            AgentEvent.id <= assistant_event_id,
            AgentEvent.role.in_(("user", "assistant")),
            AgentEvent.content_text.isnot(None),
        )
        .order_by(AgentEvent.id.desc())
        .limit(_TURN_CONTEXT_EVENT_LIMIT)
        .all()
    )
    if not rows:
        return None, None

    messages = [
        {
            "event_id": int(row.id),
            "role": str(row.role),
            "text": str(row.content_text or "").strip(),
        }
        for row in reversed(rows)
        if str(row.content_text or "").strip()
    ]
    if not messages:
        return None, None

    turns: List[Dict[str, Any]] = []
    current_role: Optional[str] = None
    current_texts: List[str] = []
    current_last_event_id: Optional[int] = None
    last_user_text: Optional[str] = None

    def _flush() -> None:
        nonlocal current_role
        nonlocal current_texts
        nonlocal current_last_event_id
        nonlocal last_user_text
        if current_role is None or current_last_event_id is None:
            return
        turn_text = "\n".join(current_texts).strip()
        turns.append(
            {
                "role": current_role,
                "text": turn_text,
                "assistant_event_id": current_last_event_id if current_role == "assistant" else None,
                "last_user_text": last_user_text,
            }
        )
        if current_role == "user" and turn_text:
            last_user_text = turn_text
        current_role = None
        current_texts = []
        current_last_event_id = None

    for message in messages:
        if message["role"] != current_role:
            _flush()
            current_role = str(message["role"])
        current_texts.append(str(message["text"]))
        current_last_event_id = int(message["event_id"])
        if message["role"] == "user":
            last_user_text = str(message["text"])
    _flush()

    for turn in reversed(turns):
        if turn["role"] != "assistant":
            continue
        if int(turn["assistant_event_id"] or 0) != assistant_event_id:
            continue
        return (
            str(turn.get("last_user_text") or "").strip() or None,
            str(turn.get("text") or "").strip() or None,
        )
    return None, None


def _build_loop_inbox_item(
    review: SessionTurnReview,
    session: AgentSession | None,
    *,
    card_state: str = "active",
    card_state_reason: str | None = None,
    superseded_by_card_id: int | None = None,
) -> LoopInboxItem:
    session_id = str(review.session_id)
    return LoopInboxItem(
        card_id=int(review.id),
        session_id=session_id,
        title=_session_title(session, session_id),
        project=getattr(session, "project", None),
        machine=getattr(session, "device_id", None),
        provider=getattr(session, "provider", None),
        loop_mode=review.loop_mode,
        decision=review.decision,
        execution_state=review.execution_state,
        summary=review.summary,
        recommended_action=review.recommended_action,
        follow_up_prompt=review.follow_up_prompt,
        blocked_reasons=_clean_blocked_reasons(review.blocked_reasons),
        last_turn_at=review.created_at,
        card_state=card_state,
        card_state_reason=card_state_reason,
        superseded_by_card_id=superseded_by_card_id,
        requires_attention=card_state == "active",
    )


@router.get("/turn-reviews", response_model=List[SessionTurnReviewSummary])
def list_session_turn_reviews(
    limit: int = 50,
    session_id: Optional[str] = None,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
) -> List[SessionTurnReviewSummary]:
    """List recent completed-turn reviews for the authenticated owner."""

    query = db.query(SessionTurnReview).filter(SessionTurnReview.owner_id == current_user.id)
    if session_id:
        query = query.filter(SessionTurnReview.session_id == session_id)
    if status:
        query = query.filter(SessionTurnReview.status == status)

    rows = query.order_by(SessionTurnReview.created_at.desc(), SessionTurnReview.id.desc()).limit(limit).all()
    return [
        SessionTurnReviewSummary(
            id=row.id,
            session_id=str(row.session_id),
            assistant_event_id=row.assistant_event_id,
            turn_index=row.turn_index,
            trigger_type=row.trigger_type,
            loop_mode=row.loop_mode,
            decision=row.decision,
            summary=row.summary,
            rationale=row.rationale,
            turn_excerpt=row.turn_excerpt,
            mode_capability=row.mode_capability,
            mode_summary=row.mode_summary,
            execution_state=row.execution_state,
            recommended_action=row.recommended_action,
            follow_up_prompt=row.follow_up_prompt,
            blocked_reasons=[str(reason).strip() for reason in (row.blocked_reasons or []) if str(reason).strip()],
            status=row.status,
            reason=row.reason,
            run_id=row.run_id,
            actual_outcome=row.actual_outcome,
            shadow_alignment=row.shadow_alignment,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.get("/loop-inbox", response_model=List[LoopInboxItem])
def list_loop_inbox(
    limit: int = 25,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
) -> List[LoopInboxItem]:
    """List latest per-session turn reviews that still need phone-friendly attention."""

    rows = _load_latest_attention_reviews(db, owner_id=current_user.id, limit=limit)
    session_map = _load_session_map(db, [row.session_id for row in rows])
    return [_build_loop_inbox_item(row, session_map.get(str(row.session_id))) for row in rows]


@router.get("/loop-inbox/cards/{card_id}", response_model=LoopActionCard)
def get_loop_inbox_action_card_by_card_id(
    card_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
) -> LoopActionCard:
    """Return a compact action-card payload for one stable follow-up card."""

    review = _load_review_by_card_id(db, owner_id=current_user.id, card_id=card_id)
    if review is None:
        raise HTTPException(status_code=404, detail="No turn review found for this card")

    session_id = str(review.session_id)
    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    card_state, card_state_reason, superseded_by_card_id = _derive_card_state(db, review=review)
    last_user_text, last_assistant_text = _load_turn_context(
        db,
        session_id=session_id,
        assistant_event_id=review.assistant_event_id,
    )
    item = _build_loop_inbox_item(
        review,
        session,
        card_state=card_state,
        card_state_reason=card_state_reason,
        superseded_by_card_id=superseded_by_card_id,
    )
    return LoopActionCard(
        **item.model_dump(),
        rationale=review.rationale,
        mode_capability=review.mode_capability,
        mode_summary=review.mode_summary,
        last_user_text=last_user_text,
        last_assistant_text=last_assistant_text or review.turn_excerpt,
        available_actions=_available_loop_actions(review) if card_state == "active" else [],
    )


@router.get("/loop-inbox/{session_id}", response_model=LoopActionCard)
def get_loop_inbox_action_card_for_session(
    session_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
) -> LoopActionCard:
    """Compatibility lookup for older session-keyed links.

    Returns the latest review for the session, even when it is stale or no longer actionable.
    """

    review = _load_latest_review_for_session(db, owner_id=current_user.id, session_id=session_id)
    if review is None:
        raise HTTPException(status_code=404, detail="No turn review found for this session")
    return get_loop_inbox_action_card_by_card_id(
        card_id=int(review.id),
        db=db,
        current_user=current_user,
    )


@router.post("/loop-inbox/cards/{card_id}/actions", response_model=LoopInboxActionResult)
def act_on_loop_inbox_item(
    card_id: int,
    request: LoopInboxActionRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
) -> LoopInboxActionResult:
    """Apply one bounded phone-first action to one exact follow-up card."""

    review = _load_review_by_card_id(db, owner_id=current_user.id, card_id=card_id)
    if review is None:
        raise HTTPException(status_code=404, detail="No turn review found for this card")

    card_state, _card_state_reason, _superseded_by_card_id = _derive_card_state(db, review=review)
    if card_state != "active":
        raise HTTPException(status_code=409, detail="This follow-up card is no longer active")

    if request.action == "approve_recommended_action":
        if review.execution_state != "awaiting_user_approval" or review.recommended_action != "continue_session":
            raise HTTPException(status_code=409, detail="This turn review cannot be approved for continuation")
        try:
            job = approve_pending_turn_review(db=db, review=review)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        db.refresh(review)
        return LoopInboxActionResult(
            session_id=str(review.session_id),
            review_id=int(review.id),
            action=request.action,
            status=review.status,
            reason=review.reason,
            queued_job_id=int(job.id),
        )

    try:
        dismiss_pending_turn_review(db=db, review=review, reason="not_now")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(review)
    return LoopInboxActionResult(
        session_id=str(review.session_id),
        review_id=int(review.id),
        action=request.action,
        status=review.status,
        reason=review.reason,
        queued_job_id=None,
    )


def _extract_text_from_message_content(content: Any) -> Optional[str]:
    """Extract text from ThreadMessage content payloads."""
    if not content:
        return None

    # Handle string content (most common case)
    if isinstance(content, str):
        # Try to parse as JSON if it looks like structured content
        if content.startswith("[") or content.startswith("{"):
            try:
                parsed = json.loads(content)
                if isinstance(parsed, list):
                    # Handle array of content blocks
                    text_parts = []
                    for block in parsed:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif isinstance(block, str):
                            text_parts.append(block)
                    return " ".join(text_parts) if text_parts else content
            except (json.JSONDecodeError, TypeError):
                pass  # Not JSON, return as-is
        return content

    # Handle native list (if column supports JSON type)
    if isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif isinstance(block, str):
                text_parts.append(block)
        return " ".join(text_parts) if text_parts else None

    return str(content) if content else None


def _get_last_assistant_message(db: Session, thread_id: int) -> Optional[str]:
    """Get the last assistant message from a thread."""

    last_msg = (
        db.query(ThreadMessage)
        .filter(ThreadMessage.thread_id == thread_id)
        .filter(ThreadMessage.role == "assistant")
        .order_by(ThreadMessage.id.desc())
        .first()
    )

    if not last_msg or not last_msg.content:
        return None

    return _extract_text_from_message_content(last_msg.content)


def _get_latest_assistant_messages(db: Session, thread_ids: List[int]) -> Dict[int, str]:
    if not thread_ids:
        return {}

    subquery = (
        db.query(
            ThreadMessage.thread_id.label("thread_id"),
            ThreadMessage.content.label("content"),
            func.row_number()
            .over(
                partition_by=ThreadMessage.thread_id,
                order_by=ThreadMessage.id.desc(),
            )
            .label("rn"),
        )
        .filter(ThreadMessage.thread_id.in_(thread_ids))
        .filter(ThreadMessage.role == "assistant")
        .subquery()
    )

    rows = db.query(subquery).filter(subquery.c.rn == 1).all()
    output: Dict[int, str] = {}
    for row in rows:
        text = _extract_text_from_message_content(row.content)
        if text:
            output[row.thread_id] = text
    return output


def _get_latest_run_events(db: Session, run_ids: List[int]) -> Dict[int, Any]:
    if not run_ids:
        return {}

    subquery = (
        db.query(
            RunEvent.run_id.label("run_id"),
            RunEvent.event_type.label("event_type"),
            RunEvent.payload.label("payload"),
            RunEvent.created_at.label("created_at"),
            func.row_number()
            .over(
                partition_by=RunEvent.run_id,
                order_by=RunEvent.created_at.desc(),
            )
            .label("rn"),
        )
        .filter(RunEvent.run_id.in_(run_ids))
        .subquery()
    )

    rows = db.query(subquery).filter(subquery.c.rn == 1).all()
    return {row.run_id: row for row in rows}


def _extract_event_message(payload: Optional[Dict[str, Any]]) -> Optional[str]:
    if not payload or not isinstance(payload, dict):
        return None

    for key in ("message", "summary", "error", "result", "status"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value

    tool_name = payload.get("tool_name")
    if isinstance(tool_name, str) and tool_name.strip():
        return f"Tool: {tool_name}"

    return None


def _truncate_signal(signal: Optional[str], max_length: int) -> Optional[str]:
    if not signal:
        return signal
    normalized = " ".join(signal.split())
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 1].rstrip() + "…"


class RunStatusResponse(UTCBaseModel):
    """Detailed status of a specific run."""

    run_id: int
    status: str
    created_at: datetime
    finished_at: Optional[datetime] = None
    error: Optional[str] = None
    result: Optional[str] = None


@router.get("/runs/active")
def get_active_run(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
):
    """Get the user's currently active oikos run (RUNNING/WAITING/DEFERRED), or 204."""
    # Import here to avoid circular dependency
    from zerg.services.oikos_service import OikosService

    oikos_service = OikosService(db)
    oikos_fiche = oikos_service.get_or_create_oikos_fiche(current_user.id)

    # Prefer RUNNING runs. DEFERRED runs are "in-flight" only if they have not
    # already produced a successful continuation.
    active_run_query = db.query(Run).filter(Run.fiche_id == oikos_fiche.id)
    active_run = active_run_query.filter(Run.status == RunStatus.RUNNING).order_by(Run.created_at.desc()).first()

    if not active_run:
        # WAITING runs are interrupted via spawn_commis (oikos resume).
        active_run = (
            db.query(Run)
            .filter(Run.fiche_id == oikos_fiche.id)
            .filter(Run.status == RunStatus.WAITING)
            .order_by(Run.created_at.desc())
            .first()
        )

    if not active_run:
        from sqlalchemy import exists
        from sqlalchemy.orm import aliased

        from zerg.models.enums import RunTrigger

        Continuation = aliased(Run)
        has_terminal_continuation = exists().where(
            (Continuation.continuation_of_run_id == Run.id)
            & (Continuation.trigger == RunTrigger.CONTINUATION)
            & (Continuation.status.in_([RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED]))
        )

        active_run = (
            db.query(Run)
            .filter(Run.fiche_id == oikos_fiche.id)
            .filter(Run.status == RunStatus.DEFERRED)
            .filter(~has_terminal_continuation)
            .order_by(Run.created_at.desc())
            .first()
        )

    if not active_run:
        # No active run - return 204 No Content
        return JSONResponse(status_code=204, content=None)

    # Return run details for reconnection
    return JSONResponse(
        {
            "run_id": active_run.id,
            "status": active_run.status.value,
            "created_at": active_run.created_at.isoformat(),
        }
    )


@router.get("/runs/{run_id}", response_model=RunStatusResponse)
def get_run_status(
    run_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
) -> RunStatusResponse:
    """Get current status of a specific run."""
    # Multi-tenant security: only return runs owned by the current user
    run = _get_owned_run(db, run_id=run_id, owner_id=current_user.id)

    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # Include result only if run succeeded
    result = None
    if run.status == RunStatus.SUCCESS:
        result = _get_last_assistant_message(db, run.thread_id)

    return RunStatusResponse(
        run_id=run.id,
        status=run.status.value if hasattr(run.status, "value") else str(run.status),
        created_at=run.created_at,
        finished_at=run.finished_at,
        error=run.error,
        result=result,
    )


@router.get("/runs/{run_id}/stream")
async def attach_to_run_stream(
    run_id: int,
    current_user=Depends(get_current_oikos_user),
):
    """Attach to an existing run's SSE stream (or replay completion for finished runs)."""
    from zerg.database import db_session

    # CRITICAL: Use SHORT-LIVED session for security check and data retrieval
    # Don't use Depends(get_db) - it holds the session open for the entire
    # SSE stream duration, blocking TRUNCATE during E2E resets.
    with db_session() as db:
        # Multi-tenant security: only return runs owned by the current user
        run = _get_owned_run(db, run_id=run_id, owner_id=current_user.id)

        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        # Capture all values we need before session closes
        run_id_val = run.id
        run_status = run.status
        run_error = run.error
        run_finished_at = run.finished_at
        thread_id = run.thread_id

        # For completed runs, get result now (while session is open)
        result = None
        if run_status in (RunStatus.SUCCESS, RunStatus.FAILED):
            result = _get_last_assistant_message(db, thread_id)
    # Session is now closed - no DB connection held during streaming

    # Check run status
    if run_status == RunStatus.RUNNING:
        # Stream live events using existing stream_run_events
        from zerg.routers.stream import stream_run_events_live

        return EventSourceResponse(
            stream_run_events_live(
                run_id=run_id_val,
                owner_id=current_user.id,
            )
        )
    else:
        # Run is complete/failed - return single completion event and close
        async def completed_stream():
            # Single completion event for already-finished runs
            event_type = "oikos_complete" if run_status == RunStatus.SUCCESS else "error"
            payload = {
                "run_id": run_id_val,
                "status": run_status.value,
                "result": result,
                "error": run_error,
                "finished_at": run_finished_at.isoformat() if run_finished_at else None,
            }

            yield {
                "event": event_type,
                "data": json.dumps(
                    {
                        "type": event_type,
                        "payload": payload,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                ),
            }

        return EventSourceResponse(completed_stream())


class RunEventRecord(UTCBaseModel):
    """Single event from a run."""

    id: int
    event_type: str
    payload: Dict[str, Any]
    created_at: datetime


class RunEventsResponse(BaseModel):
    """Response for run events query."""

    run_id: int
    events: List[RunEventRecord]
    total: int


@router.get("/runs/{run_id}/events", response_model=RunEventsResponse)
def get_run_events(
    run_id: int,
    event_type: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
) -> RunEventsResponse:
    """Get events for a specific run, optionally filtered by type."""
    # Multi-tenant security: only return runs owned by the current user
    run = _get_owned_run(db, run_id=run_id, owner_id=current_user.id)

    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # Query events
    query = db.query(RunEvent).filter(RunEvent.run_id == run_id)

    if event_type:
        query = query.filter(RunEvent.event_type == event_type)

    events = query.order_by(RunEvent.created_at).limit(limit).all()

    return RunEventsResponse(
        run_id=run_id,
        events=[
            RunEventRecord(
                id=event.id,
                event_type=event.event_type,
                payload=event.payload or {},
                created_at=event.created_at,
            )
            for event in events
        ],
        total=len(events),
    )


class TimelineEvent(BaseModel):
    """Single event in a timeline with timing information."""

    phase: str
    timestamp: str  # ISO 8601
    offset_ms: int
    metadata: Optional[Dict[str, Any]] = None


class TimelineSummary(BaseModel):
    """Timing summary for a run."""

    total_duration_ms: int
    oikos_thinking_ms: Optional[int] = None
    commis_execution_ms: Optional[int] = None
    tool_execution_ms: Optional[int] = None


class TimelineResponse(BaseModel):
    """Full timeline response for a run."""

    correlation_id: Optional[str]
    run_id: int
    events: List[TimelineEvent]
    summary: TimelineSummary


@router.get("/runs/{run_id}/timeline", response_model=TimelineResponse)
def get_run_timeline(
    run_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
) -> TimelineResponse:
    """Get timing timeline for a specific run (phase events + summary stats)."""
    # Multi-tenant security: only return runs owned by the current user
    run = _get_owned_run(db, run_id=run_id, owner_id=current_user.id)

    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # Query all events for this run, ordered by created_at
    events = db.query(RunEvent).filter(RunEvent.run_id == run_id).order_by(RunEvent.created_at).all()

    if not events:
        # No events yet - return empty timeline
        return TimelineResponse(
            correlation_id=run.correlation_id,
            run_id=run_id,
            events=[],
            summary=TimelineSummary(total_duration_ms=0),
        )

    # Calculate offsets from first event
    first_timestamp = events[0].created_at
    timeline_events = []

    for event in events:
        offset_ms = int((event.created_at - first_timestamp).total_seconds() * 1000)
        timeline_events.append(
            TimelineEvent(
                phase=event.event_type,
                timestamp=event.created_at.isoformat(),
                offset_ms=offset_ms,
                metadata=event.payload if event.payload else None,
            )
        )

    # Calculate summary statistics
    last_timestamp = events[-1].created_at
    total_duration_ms = int((last_timestamp - first_timestamp).total_seconds() * 1000)

    # Find key phase transitions for summary
    oikos_started_time: Optional[datetime] = None
    oikos_complete_time: Optional[datetime] = None
    commis_spawned_time: Optional[datetime] = None
    commis_complete_time: Optional[datetime] = None
    first_tool_time: Optional[datetime] = None
    last_tool_time: Optional[datetime] = None

    for event in events:
        if event.event_type == "oikos_started" and not oikos_started_time:
            oikos_started_time = event.created_at
        elif event.event_type == "oikos_complete" and not oikos_complete_time:
            oikos_complete_time = event.created_at
        elif event.event_type == "commis_spawned" and not commis_spawned_time:
            commis_spawned_time = event.created_at
        elif event.event_type == "commis_complete" and not commis_complete_time:
            commis_complete_time = event.created_at
        elif event.event_type == "tool_started" and not first_tool_time:
            first_tool_time = event.created_at
        elif event.event_type in ("tool_completed", "tool_failed"):
            last_tool_time = event.created_at

    # Calculate derived metrics
    oikos_thinking_ms = None
    if oikos_started_time and commis_spawned_time:
        oikos_thinking_ms = int((commis_spawned_time - oikos_started_time).total_seconds() * 1000)

    commis_execution_ms = None
    if commis_spawned_time and commis_complete_time:
        commis_execution_ms = int((commis_complete_time - commis_spawned_time).total_seconds() * 1000)

    tool_execution_ms = None
    if first_tool_time and last_tool_time:
        tool_execution_ms = int((last_tool_time - first_tool_time).total_seconds() * 1000)

    summary = TimelineSummary(
        total_duration_ms=total_duration_ms,
        oikos_thinking_ms=oikos_thinking_ms,
        commis_execution_ms=commis_execution_ms,
        tool_execution_ms=tool_execution_ms,
    )

    return TimelineResponse(
        correlation_id=run.correlation_id,
        run_id=run_id,
        events=timeline_events,
        summary=summary,
    )
