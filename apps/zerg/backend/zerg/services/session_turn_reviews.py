"""Turn-end loop evaluation for completed assistant turns."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any

from sqlalchemy import desc
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from zerg.models import CommisJob
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPresence
from zerg.models.agents import SessionTurnReview
from zerg.models.user import User
from zerg.services.oikos_operator_policy import OikosOperatorPolicy
from zerg.services.oikos_operator_policy import get_operator_policy
from zerg.services.session_loop_controller import build_loop_controller_payload
from zerg.services.session_loop_controller import evaluate_session_turn_with_llm
from zerg.session_loop_mode import SessionLoopMode

logger = logging.getLogger(__name__)

_TURN_TRIGGER_TYPE = "turn.completed"
_RECENT_EVENT_LIMIT = 160
_TURN_EXCERPT_MAX_CHARS = 4000
_TURN_REVIEW_FRESH_WINDOW_MINUTES = 10
_EXPECTED_IGNORE_OUTCOME = "ignore"
_EXPECTED_NOTIFY_OUTCOME = "notify_user"
_EXPECTED_CONTINUE_OUTCOME = "continue_session"
_ACTIVE_PRESENCE_STATES = {"thinking", "running"}


@dataclass(frozen=True)
class _TurnMessage:
    event_id: int
    role: str
    text: str
    timestamp: datetime


@dataclass(frozen=True)
class CompletedAssistantTurn:
    assistant_event_id: int
    turn_index: int
    text: str
    last_user_text: str | None


@dataclass(frozen=True)
class TurnOutcome:
    decision: str  # continue | ask_user | wait | done | escalate
    summary: str
    rationale: str
    recommended_action: str | None = None
    blocked_reasons: tuple[str, ...] = ()


def _normalize_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _has_turn_review_table(db: Session) -> bool:
    try:
        bind = db.get_bind()
    except Exception:
        return False
    if bind is None:
        return False
    try:
        return bool(sa_inspect(bind).has_table(SessionTurnReview.__tablename__))
    except Exception:
        logger.debug("Failed to inspect turn review table availability", exc_info=True)
        return False


def _coerce_loop_mode(value: str | None) -> SessionLoopMode:
    try:
        return SessionLoopMode(str(value or SessionLoopMode.MANUAL.value).strip())
    except ValueError:
        return SessionLoopMode.MANUAL


def _supports_resume(session: AgentSession) -> bool:
    return (session.provider or "").strip().lower() == "claude"


def _load_recent_dialog_messages(
    db: Session,
    session_id: str,
    *,
    limit: int = _RECENT_EVENT_LIMIT,
) -> list[_TurnMessage]:
    rows = (
        db.query(AgentEvent.id, AgentEvent.role, AgentEvent.content_text, AgentEvent.timestamp)
        .filter(
            AgentEvent.session_id == session_id,
            AgentEvent.role.in_(("user", "assistant")),
            AgentEvent.content_text.isnot(None),
        )
        .order_by(desc(AgentEvent.id))
        .limit(limit)
        .all()
    )
    messages = [
        _TurnMessage(
            event_id=int(row.id),
            role=str(row.role),
            text=str(row.content_text or "").strip(),
            timestamp=row.timestamp,
        )
        for row in reversed(rows)
        if str(row.content_text or "").strip()
    ]
    return messages


def load_latest_completed_assistant_turn(db: Session, session_id: str) -> CompletedAssistantTurn | None:
    messages = _load_recent_dialog_messages(db, session_id)
    if not messages:
        return None

    turns: list[dict[str, Any]] = []
    current_role: str | None = None
    current_texts: list[str] = []
    current_last_event_id: int | None = None
    last_user_text: str | None = None

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
        if current_role == "user":
            if turn_text:
                last_user_text = turn_text
        current_role = None
        current_texts = []
        current_last_event_id = None

    for message in messages:
        if message.role != current_role:
            _flush()
            current_role = message.role
        current_texts.append(message.text)
        current_last_event_id = message.event_id
        if message.role == "user":
            last_user_text = message.text
    _flush()

    if not turns:
        return None
    latest_turn = turns[-1]
    if latest_turn["role"] != "assistant":
        return None
    text = str(latest_turn["text"] or "").strip()
    assistant_event_id = latest_turn["assistant_event_id"]
    if not text or assistant_event_id is None:
        return None
    return CompletedAssistantTurn(
        assistant_event_id=int(assistant_event_id),
        turn_index=len(turns) - 1,
        text=text,
        last_user_text=str(latest_turn.get("last_user_text") or "").strip() or None,
    )


def _load_auto_continue_streak(db: Session, session_id: str) -> int:
    if not _has_turn_review_table(db):
        return 0
    query = db.query(SessionTurnReview).filter(SessionTurnReview.session_id == session_id)
    rows = query.order_by(SessionTurnReview.id.desc()).limit(5).all()
    streak = 0
    for row in rows:
        actual_outcome = str(row.actual_outcome or "").strip().lower()
        if actual_outcome == _EXPECTED_CONTINUE_OUTCOME:
            streak += 1
            continue
        break
    return streak


def _failure_outcome(summary: str, rationale: str, *, blocked_reason: str | None = None) -> TurnOutcome:
    blocked_reasons = (blocked_reason,) if blocked_reason else ()
    return TurnOutcome(
        decision="ask_user",
        summary=summary,
        rationale=rationale,
        recommended_action="ask_user",
        blocked_reasons=blocked_reasons,
    )


def _serialize_dialog_tail(messages: list[_TurnMessage]) -> list[dict[str, Any]]:
    return [
        {
            "event_id": message.event_id,
            "role": message.role,
            "text": message.text,
            "timestamp": message.timestamp.isoformat() if message.timestamp else None,
        }
        for message in messages
    ]


def _loop_mode_profile(session: AgentSession, policy: OikosOperatorPolicy) -> tuple[str, str]:
    loop_mode = _coerce_loop_mode(getattr(session, "loop_mode", None))
    if loop_mode == SessionLoopMode.MANUAL:
        return (
            "observe_only",
            "Observe only. Oikos records the turn decision but does not proactively intervene.",
        )
    if loop_mode == SessionLoopMode.ASSIST:
        if policy.allow_notify:
            return (
                "notify_only",
                "Suggest or escalate from completed turns, but wait for user approval before continuing.",
            )
        return (
            "observe_only",
            "Assist mode is set, but proactive notifications are disabled right now.",
        )
    if policy.allow_continue and _supports_resume(session):
        return (
            "bounded_autonomy",
            "Continue one bounded next step automatically when the finished turn clearly leaves one.",
        )
    if policy.allow_notify:
        return (
            "notify_only",
            "Autopilot is set, but this session cannot auto-continue right now, so Oikos can only notify the user.",
        )
    return (
        "observe_only",
        "Autopilot is set, but both continuation and proactive notifications are disabled, so Oikos will observe only.",
    )


def _build_mode_application(
    *,
    session: AgentSession,
    policy: OikosOperatorPolicy,
    outcome: TurnOutcome,
) -> dict[str, Any]:
    mode_capability, mode_summary = _loop_mode_profile(session, policy)
    loop_mode = _coerce_loop_mode(getattr(session, "loop_mode", None)).value
    supports_notify = mode_capability in {"notify_only", "bounded_autonomy"}
    supports_continue = mode_capability == "bounded_autonomy"

    execution_state = "no_action"
    would_notify_user = False
    would_continue_session = False
    recommended_action = outcome.recommended_action

    if outcome.decision == "continue":
        if supports_continue:
            execution_state = "would_auto_continue"
            would_continue_session = True
        elif supports_notify:
            execution_state = "awaiting_user_approval"
            would_notify_user = True
        else:
            execution_state = "observe_only"
    elif outcome.decision in {"ask_user", "wait"}:
        if supports_notify:
            execution_state = "needs_human"
            would_notify_user = True
        else:
            execution_state = "observe_only"
    elif outcome.decision == "escalate":
        if supports_notify:
            execution_state = "needs_human"
            would_notify_user = True
        else:
            execution_state = "observe_only"
    elif outcome.decision == "done":
        execution_state = "no_action"

    return {
        "loop_mode": loop_mode,
        "mode_capability": mode_capability,
        "mode_summary": mode_summary,
        "execution_state": execution_state,
        "would_notify_user": would_notify_user,
        "would_continue_session": would_continue_session,
        "recommended_action": recommended_action,
        "blocked_reasons": list(outcome.blocked_reasons),
    }


def _serialize_turn_review_payload(
    *,
    session: AgentSession,
    turn: CompletedAssistantTurn,
    outcome: TurnOutcome,
    mode_application: dict[str, Any],
) -> dict[str, Any]:
    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "decision": {
            "decision": outcome.decision,
            "summary": outcome.summary,
            "rationale": outcome.rationale,
            "recommended_action": outcome.recommended_action,
            "blocked_reasons": list(outcome.blocked_reasons),
        },
        "loop_review": dict(mode_application),
        "context": {
            "trigger": {
                "type": _TURN_TRIGGER_TYPE,
                "source_session_id": str(session.id),
                "assistant_event_id": turn.assistant_event_id,
                "turn_index": turn.turn_index,
            },
            "primary_session": {
                "session_id": str(session.id),
                "provider": session.provider,
                "project": session.project,
                "cwd": session.cwd,
                "summary_title": session.summary_title,
                "summary": session.summary,
            },
            "latest_turn": {
                "assistant_event_id": turn.assistant_event_id,
                "turn_index": turn.turn_index,
                "text": turn.text,
                "last_user_text": turn.last_user_text,
            },
        },
    }


def _build_turn_loop_message(
    *,
    session: AgentSession,
    turn: CompletedAssistantTurn,
    outcome: TurnOutcome,
) -> str:
    lines = [
        "System/turn loop: a coding session just finished an assistant turn.",
        "",
        f"Trigger: {_TURN_TRIGGER_TYPE}",
        f"Session ID: {session.id}",
        f"Assistant Event ID: {turn.assistant_event_id}",
    ]
    if session.provider:
        lines.append(f"Provider: {session.provider}")
    if session.project:
        lines.append(f"Project: {session.project}")
    if session.cwd:
        lines.append(f"CWD: {session.cwd}")
    lines.extend(
        [
            "",
            "Latest assistant turn:",
            turn.text[:2000],
        ]
    )
    if outcome.summary:
        lines.extend(
            [
                "",
                f"Deterministic turn decision: {outcome.decision}",
                f"Decision summary: {outcome.summary}",
            ]
        )
    lines.extend(
        [
            "",
            "Decide whether to continue the same session, ask the user, wait, or stop.",
            "Do nothing if no action is warranted.",
        ]
    )
    return "\n".join(lines)


def _resolve_owner_id(db: Session) -> int | None:
    owner = db.query(User.id).order_by(User.id).first()
    if owner is None:
        return None
    return int(owner[0])


def _load_policy(db: Session, owner_id: int | None) -> OikosOperatorPolicy:
    if owner_id is None:
        return OikosOperatorPolicy(enabled=False)
    return get_operator_policy(db, owner_id)


async def maybe_record_session_turn_review(*, db: Session, session_id: str) -> SessionTurnReview | None:
    if not _has_turn_review_table(db):
        return None
    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if session is None:
        return None
    if session.user_state in {"archived", "snoozed"}:
        return None
    ended_at = _normalize_utc(session.ended_at)
    if ended_at is None:
        return None
    now = datetime.now(timezone.utc)
    if (now - ended_at).total_seconds() > (_TURN_REVIEW_FRESH_WINDOW_MINUTES * 60):
        return None

    presence = db.query(SessionPresence).filter(SessionPresence.session_id == session_id).first()
    if presence is not None:
        updated_at = _normalize_utc(presence.updated_at)
        if (
            updated_at is not None
            and (now - updated_at).total_seconds() <= (_TURN_REVIEW_FRESH_WINDOW_MINUTES * 60)
            and presence.state in _ACTIVE_PRESENCE_STATES
        ):
            return None

    turn = load_latest_completed_assistant_turn(db, session_id)
    if turn is None:
        return None

    existing = (
        db.query(SessionTurnReview)
        .filter(
            SessionTurnReview.session_id == session_id,
            SessionTurnReview.assistant_event_id == turn.assistant_event_id,
        )
        .first()
    )
    if existing is not None:
        return existing

    owner_id = _resolve_owner_id(db)
    policy = _load_policy(db, owner_id)
    auto_continue_streak = _load_auto_continue_streak(db, session_id)
    dialog_tail = _serialize_dialog_tail(_load_recent_dialog_messages(db, session_id))
    review_status = "recorded"
    review_reason: str | None = None

    if owner_id is None:
        outcome = _failure_outcome(
            "Loop controller could not run because no owner is configured.",
            "Session loop decisions require a valid owner context to create the per-session loop thread.",
            blocked_reason="Loop controller owner context missing.",
        )
        review_status = "failed"
        review_reason = "missing_owner"
    else:
        try:
            controller_decision = await evaluate_session_turn_with_llm(
                db=db,
                owner_id=owner_id,
                session=session,
                payload=build_loop_controller_payload(
                    session=session,
                    turn_text=turn.text,
                    last_user_text=turn.last_user_text,
                    turn_index=turn.turn_index,
                    assistant_event_id=turn.assistant_event_id,
                    auto_continue_streak=auto_continue_streak,
                    dialog_tail=dialog_tail,
                ),
                metadata={
                    "session_id": str(session.id),
                    "assistant_event_id": turn.assistant_event_id,
                    "turn_index": turn.turn_index,
                    "trigger_type": _TURN_TRIGGER_TYPE,
                },
            )
            outcome = TurnOutcome(
                decision=controller_decision.decision,
                summary=controller_decision.summary,
                rationale=controller_decision.rationale,
                recommended_action=controller_decision.recommended_action,
                blocked_reasons=controller_decision.blocked_reasons,
            )
        except Exception:
            logger.exception(
                "Loop controller evaluation failed for session %s event %s",
                session.id,
                turn.assistant_event_id,
            )
            outcome = _failure_outcome(
                "Loop controller could not decide this completed turn.",
                ("The AI loop controller failed, so this session should stay " "conservative until the next explicit review."),
                blocked_reason="Loop controller evaluation failed.",
            )
            review_status = "failed"
            review_reason = "controller_error"
    mode_application = _build_mode_application(session=session, policy=policy, outcome=outcome)
    recommended_action_value = mode_application["recommended_action"]

    review = SessionTurnReview(
        session_id=session.id,
        owner_id=owner_id,
        assistant_event_id=turn.assistant_event_id,
        turn_index=turn.turn_index,
        trigger_type=_TURN_TRIGGER_TYPE,
        loop_mode=_coerce_loop_mode(getattr(session, "loop_mode", None)).value,
        decision=outcome.decision,
        summary=outcome.summary,
        rationale=outcome.rationale,
        turn_excerpt=turn.text[:_TURN_EXCERPT_MAX_CHARS],
        mode_capability=str(mode_application["mode_capability"]),
        mode_summary=str(mode_application["mode_summary"]),
        execution_state=str(mode_application["execution_state"]),
        recommended_action=str(recommended_action_value) if recommended_action_value else None,
        blocked_reasons=list(outcome.blocked_reasons),
        status=review_status,
        reason=review_reason,
    )
    db.add(review)
    db.commit()
    db.refresh(review)
    return review


def _expected_outcome(review: SessionTurnReview) -> str:
    if review.execution_state == "would_auto_continue":
        return _EXPECTED_CONTINUE_OUTCOME
    if review.execution_state in {"awaiting_user_approval", "needs_human"}:
        return _EXPECTED_NOTIFY_OUTCOME
    return _EXPECTED_IGNORE_OUTCOME


def _classify_alignment(expected_outcome: str | None, actual_outcome: str | None) -> str | None:
    if not expected_outcome or not actual_outcome:
        return None
    if expected_outcome == actual_outcome:
        return "matched"
    if actual_outcome == "failed":
        return "failed"
    if expected_outcome == _EXPECTED_CONTINUE_OUTCOME and actual_outcome == _EXPECTED_IGNORE_OUTCOME:
        return "more_conservative"
    if expected_outcome == _EXPECTED_NOTIFY_OUTCOME and actual_outcome == _EXPECTED_IGNORE_OUTCOME:
        return "more_conservative"
    if expected_outcome == _EXPECTED_IGNORE_OUTCOME and actual_outcome in {
        _EXPECTED_CONTINUE_OUTCOME,
        "delegated_follow_up",
    }:
        return "more_aggressive"
    if expected_outcome == _EXPECTED_NOTIFY_OUTCOME and actual_outcome in {
        _EXPECTED_CONTINUE_OUTCOME,
        "delegated_follow_up",
    }:
        return "more_aggressive"
    return "different"


def finalize_turn_reviews_for_run(
    db: Session,
    *,
    run_id: int,
    status: str,
    reason: str | None = None,
    actual_outcome: str | None = None,
) -> int:
    if not _has_turn_review_table(db):
        return 0
    try:
        rows = (
            db.query(SessionTurnReview)
            .filter(
                SessionTurnReview.run_id == run_id,
                SessionTurnReview.status == "enqueued",
            )
            .all()
        )
    except OperationalError:
        logger.debug("Skipping turn review finalization because the table is unavailable", exc_info=True)
        return 0
    if not rows:
        return 0
    for row in rows:
        row.status = status
        row.reason = reason
        row.actual_outcome = actual_outcome
        row.shadow_alignment = _classify_alignment(_expected_outcome(row), actual_outcome)
    return len(rows)


def classify_turn_review_outcome_for_run(db: Session, *, run_id: int) -> int:
    jobs = db.query(CommisJob).filter(CommisJob.oikos_run_id == run_id).all()
    if not jobs:
        return finalize_turn_reviews_for_run(
            db,
            run_id=run_id,
            status="ignored",
            reason="no_action",
            actual_outcome=_EXPECTED_IGNORE_OUTCOME,
        )

    resumed_session_ids: list[str] = []
    for job in jobs:
        config = job.config if isinstance(job.config, dict) else {}
        resume_session_id = config.get("resume_session_id")
        if resume_session_id:
            resumed_session_ids.append(str(resume_session_id))
    if resumed_session_ids:
        return finalize_turn_reviews_for_run(
            db,
            run_id=run_id,
            status="acted",
            reason="continue_session",
            actual_outcome=_EXPECTED_CONTINUE_OUTCOME,
        )

    return finalize_turn_reviews_for_run(
        db,
        run_id=run_id,
        status="acted",
        reason="delegated_follow_up",
        actual_outcome="delegated_follow_up",
    )
