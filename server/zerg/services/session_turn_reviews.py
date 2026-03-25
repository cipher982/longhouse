"""Turn-end loop evaluation for completed assistant turns."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import desc
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.models import CommisJob
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPresence
from zerg.models.agents import SessionTurnReview
from zerg.models.user import User
from zerg.services.managed_local_control import send_text_to_managed_local_session
from zerg.services.managed_local_runtime import persist_managed_local_turn_idle
from zerg.services.managed_local_runtime import persist_managed_local_turn_needs_user
from zerg.services.oikos_operator_policy import OikosOperatorPolicy
from zerg.services.oikos_operator_policy import get_operator_policy
from zerg.services.presence_cache import get_presence_cache
from zerg.services.session_loop_controller import build_loop_controller_payload
from zerg.services.session_loop_controller import evaluate_session_turn_with_llm
from zerg.services.write_serializer import get_write_serializer
from zerg.session_execution_home import SessionExecutionHome
from zerg.session_loop_mode import SessionLoopMode

logger = logging.getLogger(__name__)

_TURN_TRIGGER_TYPE = "turn.completed"
_TURN_OPERATOR_SOURCE = "turn_loop"
_TURN_OPERATOR_CONVERSATION_ID = "operator:main"
_RECENT_EVENT_LIMIT = 160
_TURN_EXCERPT_MAX_CHARS = 4000
_TURN_REVIEW_FRESH_WINDOW_MINUTES = 10
_TURN_REVIEW_ACTIVE_SETTLE_SECONDS = 3.0
_TURN_CONTROLLER_TIMEOUT_SECONDS = 15.0
_EXPECTED_IGNORE_OUTCOME = "ignore"
_EXPECTED_NOTIFY_OUTCOME = "notify_user"
_EXPECTED_CONTINUE_OUTCOME = "continue_session"
_ACTIVE_PRESENCE_STATES = {"thinking", "running"}
_ATTENTION_EXECUTION_STATES = {"awaiting_user_approval", "needs_human"}


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
    assistant_timestamp: datetime


@dataclass(frozen=True)
class TurnOutcome:
    decision: str  # continue | ask_user | wait | done | escalate
    summary: str
    rationale: str
    recommended_action: str | None = None
    follow_up_prompt: str | None = None
    blocked_reasons: tuple[str, ...] = ()


def _normalize_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stamp_review_timing_fields(
    review: SessionTurnReview,
    *,
    assistant_turn_finished_at: datetime | None = None,
    turn_loop_enqueued_at: datetime | None = None,
    turn_loop_claimed_at: datetime | None = None,
    controller_started_at: datetime | None = None,
    controller_completed_at: datetime | None = None,
    turn_loop_completed_at: datetime | None = None,
) -> bool:
    changed = False
    normalized_finished_at = _normalize_utc(assistant_turn_finished_at)
    normalized_enqueued_at = _normalize_utc(turn_loop_enqueued_at)
    normalized_claimed_at = _normalize_utc(turn_loop_claimed_at)
    normalized_controller_started_at = _normalize_utc(controller_started_at)
    normalized_controller_completed_at = _normalize_utc(controller_completed_at)
    normalized_completed_at = _normalize_utc(turn_loop_completed_at)

    if normalized_finished_at is not None and review.assistant_turn_finished_at is None:
        review.assistant_turn_finished_at = normalized_finished_at
        changed = True
    if normalized_enqueued_at is not None and review.turn_loop_enqueued_at is None:
        review.turn_loop_enqueued_at = normalized_enqueued_at
        changed = True
    if normalized_claimed_at is not None and review.turn_loop_claimed_at is None:
        review.turn_loop_claimed_at = normalized_claimed_at
        changed = True
    if normalized_controller_started_at is not None and review.controller_started_at is None:
        review.controller_started_at = normalized_controller_started_at
        changed = True
    if normalized_controller_completed_at is not None and review.controller_completed_at is None:
        review.controller_completed_at = normalized_controller_completed_at
        changed = True
    if normalized_completed_at is not None and review.turn_loop_completed_at is None:
        review.turn_loop_completed_at = normalized_completed_at
        changed = True
    return changed


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


def _is_managed_local_codex_session(session: AgentSession) -> bool:
    return (session.provider or "").strip().lower() == "codex" and (
        str(getattr(session, "execution_home", "") or "").strip() == SessionExecutionHome.MANAGED_LOCAL.value
    )


def _is_managed_local_session(session: AgentSession | None) -> bool:
    if session is None:
        return False
    execution_home = str(getattr(session, "execution_home", "") or "").strip()
    return execution_home == SessionExecutionHome.MANAGED_LOCAL.value


def _supports_same_session_continue(session: AgentSession) -> bool:
    provider = (session.provider or "").strip().lower()
    return provider == "claude" or _is_managed_local_codex_session(session)


def _resume_backend_for_session(session: AgentSession) -> str | None:
    if not _supports_resume(session):
        return None
    # Claude resume on the hosted commis path uses hatch's Claude-compatible
    # runtime via the z.ai-backed wrapper.
    return "zai"


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
    current_last_timestamp: datetime | None = None
    last_user_text: str | None = None

    def _flush() -> None:
        nonlocal current_role
        nonlocal current_texts
        nonlocal current_last_event_id
        nonlocal current_last_timestamp
        nonlocal last_user_text
        if current_role is None or current_last_event_id is None:
            return
        turn_text = "\n".join(current_texts).strip()
        turns.append(
            {
                "role": current_role,
                "text": turn_text,
                "assistant_event_id": current_last_event_id if current_role == "assistant" else None,
                "assistant_timestamp": current_last_timestamp if current_role == "assistant" else None,
                "last_user_text": last_user_text,
            }
        )
        if current_role == "user":
            if turn_text:
                last_user_text = turn_text
        current_role = None
        current_texts = []
        current_last_event_id = None
        current_last_timestamp = None

    for message in messages:
        if message.role != current_role:
            _flush()
            current_role = message.role
        current_texts.append(message.text)
        current_last_event_id = message.event_id
        current_last_timestamp = message.timestamp
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
    assistant_timestamp = _normalize_utc(latest_turn.get("assistant_timestamp"))
    if not text or assistant_event_id is None or assistant_timestamp is None:
        return None
    return CompletedAssistantTurn(
        assistant_event_id=int(assistant_event_id),
        turn_index=len(turns) - 1,
        text=text,
        last_user_text=str(latest_turn.get("last_user_text") or "").strip() or None,
        assistant_timestamp=assistant_timestamp,
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
    if policy.allow_continue and _supports_same_session_continue(session):
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
            "follow_up_prompt": outcome.follow_up_prompt,
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
    review: SessionTurnReview,
) -> str:
    lines = [
        "System/turn loop: a coding session just finished an assistant turn.",
        "",
        f"Trigger: {_TURN_TRIGGER_TYPE}",
        f"Session ID: {session.id}",
        f"Assistant Event ID: {review.assistant_event_id}",
    ]
    if session.provider:
        lines.append(f"Provider: {session.provider}")
    if session.project:
        lines.append(f"Project: {session.project}")
    if session.cwd:
        lines.append(f"CWD: {session.cwd}")
    if review.turn_excerpt:
        lines.extend(
            [
                "",
                "Latest assistant turn excerpt:",
                review.turn_excerpt[:2000],
            ]
        )
    if review.summary:
        lines.extend(
            [
                "",
                f"Turn decision: {review.decision}",
                f"Decision summary: {review.summary}",
            ]
        )
    if review.follow_up_prompt:
        lines.append(f"Suggested follow-up prompt: {review.follow_up_prompt}")
    if review.blocked_reasons:
        clean_reasons = [str(item).strip() for item in review.blocked_reasons if str(item).strip()]
        lines.append(f"Blocked reasons: {'; '.join(clean_reasons)}")
    lines.extend(
        [
            "",
            "Stay within the deterministic turn review and keep any user-facing follow-up concise.",
        ]
    )
    return "\n".join(lines)


def _serialize_recorded_turn_review_payload(
    *,
    session: AgentSession,
    review: SessionTurnReview,
) -> dict[str, Any]:
    blocked_reasons = [str(reason).strip() for reason in (review.blocked_reasons or []) if str(reason).strip()]
    return {
        "trigger_type": _TURN_TRIGGER_TYPE,
        "conversation_id": _TURN_OPERATOR_CONVERSATION_ID,
        "session_id": str(session.id),
        "turn_review": {
            "decision": {
                "decision": review.decision,
                "summary": review.summary,
                "rationale": review.rationale,
                "recommended_action": review.recommended_action,
                "follow_up_prompt": review.follow_up_prompt,
                "blocked_reasons": blocked_reasons,
            },
            "loop_review": {
                "loop_mode": review.loop_mode,
                "mode_capability": review.mode_capability,
                "mode_summary": review.mode_summary,
                "execution_state": review.execution_state,
                "recommended_action": review.recommended_action,
                "blocked_reasons": blocked_reasons,
                "would_notify_user": review.execution_state in {"awaiting_user_approval", "needs_human"},
                "would_continue_session": review.execution_state == "would_auto_continue",
            },
            "context": {
                "trigger": {
                    "type": review.trigger_type,
                    "source_session_id": str(session.id),
                    "assistant_event_id": review.assistant_event_id,
                    "turn_index": review.turn_index,
                },
                "primary_session": {
                    "session_id": str(session.id),
                    "provider": session.provider,
                    "project": session.project,
                    "cwd": session.cwd,
                    "summary_title": session.summary_title,
                    "summary": session.summary,
                    "loop_mode": review.loop_mode,
                },
                "latest_turn": {
                    "assistant_event_id": review.assistant_event_id,
                    "turn_index": review.turn_index,
                    "text": review.turn_excerpt,
                },
            },
        },
    }


def _resolve_owner_id(db: Session) -> int | None:
    owner = db.query(User.id).order_by(User.id).first()
    if owner is None:
        return None
    return int(owner[0])


def _load_policy(db: Session, owner_id: int | None) -> OikosOperatorPolicy:
    if owner_id is None:
        return OikosOperatorPolicy(enabled=False)
    return get_operator_policy(db, owner_id)


def _session_title(session: AgentSession) -> str:
    if session.summary_title and str(session.summary_title).strip():
        return str(session.summary_title).strip()
    if session.project and str(session.project).strip():
        return str(session.project).strip()
    if session.cwd and str(session.cwd).strip():
        return os.path.basename(str(session.cwd).rstrip("/")) or str(session.cwd).strip()
    return f"Session {str(session.id)[:8]}"


def _public_loop_url(review_id: int) -> str | None:
    settings = get_settings()
    base_url = str(settings.app_public_url or settings.public_site_url or "").strip().rstrip("/")
    if not base_url:
        return None
    return f"{base_url}/loop/card/{review_id}"


def _build_turn_review_notification_text(*, review: SessionTurnReview, session: AgentSession) -> str:
    title = _session_title(session)
    attention_label = "Needs approval" if review.execution_state == "awaiting_user_approval" else "Needs attention"
    lines = [
        f"**{title}**",
        attention_label,
        review.summary,
    ]
    if review.follow_up_prompt:
        lines.append(f"Suggested next step: {review.follow_up_prompt}")
    loop_url = _public_loop_url(int(review.id))
    if loop_url:
        lines.append(f"Open in Loop: {loop_url}")
    return "\n".join(line.strip() for line in lines if str(line).strip())


def _supersede_older_actionable_reviews(
    *,
    db: Session,
    review: SessionTurnReview,
    commit: bool = True,
) -> int:
    from zerg.services.oikos_wakeup_ledger import WAKEUP_STATUS_IGNORED
    from zerg.services.oikos_wakeup_ledger import finalize_wakeups_for_run

    rows = (
        db.query(SessionTurnReview)
        .filter(
            SessionTurnReview.session_id == review.session_id,
            SessionTurnReview.id < review.id,
            SessionTurnReview.execution_state.in_(("awaiting_user_approval", "needs_human")),
            SessionTurnReview.status.in_(("recorded", "enqueued")),
        )
        .all()
    )
    if not rows:
        return 0

    for row in rows:
        if row.run_id is not None:
            finalize_wakeups_for_run(
                db,
                run_id=row.run_id,
                status=WAKEUP_STATUS_IGNORED,
                reason="superseded",
                payload_updates={"outcome": _EXPECTED_IGNORE_OUTCOME},
            )
        row.status = "ignored"
        row.reason = "superseded"
        row.actual_outcome = _EXPECTED_IGNORE_OUTCOME
        row.shadow_alignment = _classify_alignment(_expected_outcome(row), _EXPECTED_IGNORE_OUTCOME)

    if commit:
        db.commit()
        db.refresh(review)
    return len(rows)


async def _persist_review_timing_fields(
    *,
    db: Session,
    review_id: int,
    label: str,
    assistant_turn_finished_at: datetime | None = None,
    turn_loop_enqueued_at: datetime | None = None,
    turn_loop_claimed_at: datetime | None = None,
    controller_started_at: datetime | None = None,
    controller_completed_at: datetime | None = None,
    turn_loop_completed_at: datetime | None = None,
) -> bool:
    ws = get_write_serializer()

    def _do_update(wdb: Session) -> bool:
        row = wdb.query(SessionTurnReview).filter(SessionTurnReview.id == review_id).first()
        if row is None:
            return False
        return _stamp_review_timing_fields(
            row,
            assistant_turn_finished_at=assistant_turn_finished_at,
            turn_loop_enqueued_at=turn_loop_enqueued_at,
            turn_loop_claimed_at=turn_loop_claimed_at,
            controller_started_at=controller_started_at,
            controller_completed_at=controller_completed_at,
            turn_loop_completed_at=turn_loop_completed_at,
        )

    return await ws.execute_or_direct(_do_update, db, label=label)


async def _send_turn_review_telegram_notification(
    *,
    db: Session,
    review: SessionTurnReview,
    session: AgentSession,
) -> bool:
    if not _review_requires_mobile_attention(review):
        return False

    user = db.query(User).filter(User.id == review.owner_id).first()
    if user is None:
        return False

    chat_id = str((user.context or {}).get("telegram_chat_id", "")).strip()
    if not chat_id:
        return False

    from zerg.channels.registry import get_registry
    from zerg.channels.types import ChannelMessage
    from zerg.services.telegram_bridge import _format_for_telegram

    channel = get_registry().get("telegram")
    if not channel:
        return False

    message = _build_turn_review_notification_text(review=review, session=session)
    result = await channel.send_message(
        ChannelMessage(
            channel_id="telegram",
            to=chat_id,
            text=_format_for_telegram(message),
            parse_mode="html",
            disable_web_page_preview=True,
        )
    )
    return bool(result.get("success"))


def _send_turn_review_push_notification(
    *,
    db: Session,
    review: SessionTurnReview,
    session: AgentSession,
) -> bool:
    if not _review_requires_mobile_attention(review):
        return False
    from zerg.services.loop_push import send_loop_push_nudge

    return send_loop_push_nudge(
        db=db,
        owner_id=int(review.owner_id),
        review=review,
        session=session,
    )


async def _send_turn_review_mobile_notification(
    *,
    db: Session,
    review: SessionTurnReview,
    session: AgentSession,
) -> bool:
    if not _review_requires_mobile_attention(review):
        return False
    if _send_turn_review_push_notification(db=db, review=review, session=session):
        return True
    return await _send_turn_review_telegram_notification(db=db, review=review, session=session)


def _review_requires_mobile_attention(review: SessionTurnReview) -> bool:
    if review.owner_id is None:
        return False
    if review.execution_state not in {"awaiting_user_approval", "needs_human"}:
        return False
    if review.status not in {"recorded", "enqueued"}:
        return False
    return True


def _latest_presence_snapshot(db: Session, session_id: str) -> tuple[str | None, datetime | None]:
    db_presence = db.query(SessionPresence).filter(SessionPresence.session_id == session_id).first()
    best_state = str(getattr(db_presence, "state", "") or "").strip() or None
    best_updated_at = _normalize_utc(getattr(db_presence, "updated_at", None))

    cache_entry = get_presence_cache().get(session_id)
    cache_state = str(getattr(cache_entry, "state", "") or "").strip() or None
    cache_updated_at = _normalize_utc(getattr(cache_entry, "updated_at", None))
    if cache_state and cache_updated_at is not None and (best_updated_at is None or cache_updated_at >= best_updated_at):
        return cache_state, cache_updated_at
    return best_state, best_updated_at


def _should_wait_for_active_presence(
    *,
    db: Session,
    session_id: str,
    assistant_timestamp: datetime,
    now: datetime,
    session: AgentSession | None = None,
) -> bool:
    presence_state, updated_at = _latest_presence_snapshot(db, session_id)
    if updated_at is None or presence_state not in _ACTIVE_PRESENCE_STATES:
        return False
    normalized_assistant_at = _normalize_utc(assistant_timestamp)
    if normalized_assistant_at is None:
        return False
    if (now - updated_at).total_seconds() > (_TURN_REVIEW_FRESH_WINDOW_MINUTES * 60):
        return False
    if updated_at <= normalized_assistant_at:
        return False
    if not _is_managed_local_session(session):
        return True
    # Presence is only a short settle signal for hot managed-local turns.
    # Once the assistant turn has been stable for a few seconds, do not let
    # a sticky thinking/running state strand review creation indefinitely.
    return (now - normalized_assistant_at).total_seconds() <= _TURN_REVIEW_ACTIVE_SETTLE_SECONDS


def turn_loop_retry_needed(
    *,
    db: Session,
    session_id: str,
    freshness_reference_at: datetime | None = None,
) -> bool:
    if not _has_turn_review_table(db):
        return False
    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    turn = load_latest_completed_assistant_turn(db, session_id)
    if turn is None:
        return False
    now = datetime.now(timezone.utc)
    fresh_window_seconds = _TURN_REVIEW_FRESH_WINDOW_MINUTES * 60
    if (now - turn.assistant_timestamp).total_seconds() > fresh_window_seconds:
        reference_time = _normalize_utc(freshness_reference_at)
        if reference_time is None or (reference_time - turn.assistant_timestamp).total_seconds() > fresh_window_seconds:
            return False

    existing = (
        db.query(SessionTurnReview)
        .filter(
            SessionTurnReview.session_id == session_id,
            SessionTurnReview.assistant_event_id == turn.assistant_event_id,
        )
        .first()
    )
    if existing is not None:
        return False

    return _should_wait_for_active_presence(
        db=db,
        session_id=session_id,
        assistant_timestamp=turn.assistant_timestamp,
        now=now,
        session=session,
    )


async def _record_session_turn_review(
    *,
    db: Session,
    session_id: str,
    freshness_reference_at: datetime | None = None,
    turn_loop_claimed_at: datetime | None = None,
) -> tuple[SessionTurnReview | None, bool]:
    if not _has_turn_review_table(db):
        return None, False
    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if session is None:
        return None, False
    if session.user_state in {"archived", "snoozed"}:
        return None, False
    turn = load_latest_completed_assistant_turn(db, session_id)
    if turn is None:
        return None, False
    now = datetime.now(timezone.utc)
    fresh_window_seconds = _TURN_REVIEW_FRESH_WINDOW_MINUTES * 60
    if (now - turn.assistant_timestamp).total_seconds() > fresh_window_seconds:
        reference_time = _normalize_utc(freshness_reference_at)
        if reference_time is None or (reference_time - turn.assistant_timestamp).total_seconds() > fresh_window_seconds:
            return None, False

    if _should_wait_for_active_presence(
        db=db,
        session_id=session_id,
        assistant_timestamp=turn.assistant_timestamp,
        now=now,
        session=session,
    ):
        return None, False

    existing = (
        db.query(SessionTurnReview)
        .filter(
            SessionTurnReview.session_id == session_id,
            SessionTurnReview.assistant_event_id == turn.assistant_event_id,
        )
        .first()
    )
    if existing is not None:
        existing_id = int(existing.id)
        updated = await _persist_review_timing_fields(
            db=db,
            review_id=existing_id,
            label="turn-review-existing",
            assistant_turn_finished_at=turn.assistant_timestamp,
            turn_loop_enqueued_at=freshness_reference_at,
            turn_loop_claimed_at=turn_loop_claimed_at,
        )
        if updated:
            db.expire_all()
            existing = db.query(SessionTurnReview).filter(SessionTurnReview.id == existing_id).first()
        return existing, False

    owner_id = _resolve_owner_id(db)
    policy = _load_policy(db, owner_id)
    auto_continue_streak = _load_auto_continue_streak(db, session_id)
    dialog_tail = _serialize_dialog_tail(_load_recent_dialog_messages(db, session_id))
    review_status = "recorded"
    review_reason: str | None = None
    controller_started_at: datetime | None = None
    controller_completed_at: datetime | None = None

    if owner_id is None:
        outcome = _failure_outcome(
            "Loop controller could not run because no owner is configured.",
            "Session loop decisions require a valid owner context to create the per-session loop thread.",
            blocked_reason="Loop controller owner context missing.",
        )
        review_status = "failed"
        review_reason = "missing_owner"
    else:
        controller_started_at = datetime.now(timezone.utc)
        try:
            controller_decision = await asyncio.wait_for(
                evaluate_session_turn_with_llm(
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
                ),
                timeout=_TURN_CONTROLLER_TIMEOUT_SECONDS,
            )
            outcome = TurnOutcome(
                decision=controller_decision.decision,
                summary=controller_decision.summary,
                rationale=controller_decision.rationale,
                recommended_action=controller_decision.recommended_action,
                follow_up_prompt=controller_decision.follow_up_prompt,
                blocked_reasons=controller_decision.blocked_reasons,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Loop controller evaluation timed out for session %s event %s after %.1fs",
                session.id,
                turn.assistant_event_id,
                _TURN_CONTROLLER_TIMEOUT_SECONDS,
            )
            outcome = _failure_outcome(
                "Loop controller timed out, so Oikos is surfacing this completed turn for review.",
                "The loop controller did not answer quickly enough for the hot path, so Oikos is falling back to a "
                "conservative user-approval checkpoint instead of delaying the loop card.",
                blocked_reason="Loop controller timed out.",
            )
        except Exception:
            logger.exception(
                "Loop controller evaluation failed for session %s event %s",
                session.id,
                turn.assistant_event_id,
            )
            failure_rationale = " ".join(
                [
                    "The AI loop controller failed, so this session should stay conservative",
                    "until the next explicit review.",
                ]
            )
            outcome = _failure_outcome(
                "Loop controller could not decide this completed turn.",
                failure_rationale,
                blocked_reason="Loop controller evaluation failed.",
            )
            review_status = "failed"
            review_reason = "controller_error"
        finally:
            controller_completed_at = datetime.now(timezone.utc)
    mode_application = _build_mode_application(session=session, policy=policy, outcome=outcome)
    recommended_action_value = mode_application["recommended_action"]

    ws = get_write_serializer()

    def _create_review(wdb: Session) -> int:
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
            follow_up_prompt=outcome.follow_up_prompt,
            blocked_reasons=list(outcome.blocked_reasons),
            status=review_status,
            reason=review_reason,
            assistant_turn_finished_at=turn.assistant_timestamp,
            turn_loop_enqueued_at=_normalize_utc(freshness_reference_at),
            turn_loop_claimed_at=_normalize_utc(turn_loop_claimed_at),
            controller_started_at=_normalize_utc(controller_started_at),
            controller_completed_at=_normalize_utc(controller_completed_at),
            created_at=datetime.now(timezone.utc),
        )
        wdb.add(review)
        wdb.flush()
        _supersede_older_actionable_reviews(db=wdb, review=review, commit=False)
        return int(review.id)

    review_id = await ws.execute_or_direct(_create_review, db, label="turn-review-create")
    db.expire_all()
    review = db.query(SessionTurnReview).filter(SessionTurnReview.id == review_id).first()
    return review, True


async def maybe_record_session_turn_review(
    *,
    db: Session,
    session_id: str,
    freshness_reference_at: datetime | None = None,
    turn_loop_claimed_at: datetime | None = None,
) -> SessionTurnReview | None:
    review, _created = await _record_session_turn_review(
        db=db,
        session_id=session_id,
        freshness_reference_at=freshness_reference_at,
        turn_loop_claimed_at=turn_loop_claimed_at,
    )
    return review


def _mark_review_outcome(
    db: Session,
    *,
    review: SessionTurnReview,
    status: str,
    reason: str,
    actual_outcome: str,
) -> None:
    review.status = status
    review.reason = reason
    review.actual_outcome = actual_outcome
    review.shadow_alignment = _classify_alignment(_expected_outcome(review), actual_outcome)
    db.commit()
    db.refresh(review)


async def maybe_enqueue_turn_review_operator_wakeup(*, db: Session, review: SessionTurnReview) -> int | None:
    if review.status != "recorded":
        return None
    if review.execution_state not in {"awaiting_user_approval", "needs_human"}:
        return None
    if review.owner_id is None:
        _mark_review_outcome(
            db,
            review=review,
            status="failed",
            reason="missing_owner",
            actual_outcome="failed",
        )
        return None

    session = db.query(AgentSession).filter(AgentSession.id == review.session_id).first()
    if session is None:
        _mark_review_outcome(
            db,
            review=review,
            status="failed",
            reason="missing_session",
            actual_outcome="failed",
        )
        return None

    from zerg.services.oikos_service import invoke_oikos
    from zerg.services.oikos_wakeup_ledger import WAKEUP_STATUS_ENQUEUED
    from zerg.services.oikos_wakeup_ledger import WAKEUP_STATUS_FAILED
    from zerg.services.oikos_wakeup_ledger import append_wakeup
    from zerg.services.oikos_wakeup_ledger import classify_wakeup_outcome_for_run
    from zerg.services.oikos_wakeup_ledger import finalize_wakeups_for_run
    from zerg.surfaces.adapters.operator import OperatorSurfaceAdapter

    wakeup_payload = _serialize_recorded_turn_review_payload(session=session, review=review)
    wakeup_key = f"{_TURN_OPERATOR_SOURCE}:{review.session_id}:{review.assistant_event_id}"
    message_id = str(uuid4())

    try:
        run_id = await invoke_oikos(
            review.owner_id,
            _build_turn_loop_message(session=session, review=review),
            message_id,
            source="operator",
            surface_adapter=OperatorSurfaceAdapter(
                owner_id=review.owner_id,
                conversation_id=_TURN_OPERATOR_CONVERSATION_ID,
            ),
            surface_payload=wakeup_payload,
        )
    except Exception:
        logger.exception(
            "Failed to invoke operator wakeup for turn review %s session %s",
            review.id,
            review.session_id,
        )
        append_wakeup(
            db,
            owner_id=review.owner_id,
            source=_TURN_OPERATOR_SOURCE,
            trigger_type=_TURN_TRIGGER_TYPE,
            status=WAKEUP_STATUS_FAILED,
            reason="invoke_failed",
            session_id=str(review.session_id),
            conversation_id=_TURN_OPERATOR_CONVERSATION_ID,
            wakeup_key=wakeup_key,
            payload=wakeup_payload,
        )
        _mark_review_outcome(
            db,
            review=review,
            status="failed",
            reason="invoke_failed",
            actual_outcome="failed",
        )
        return None

    append_wakeup(
        db,
        owner_id=review.owner_id,
        source=_TURN_OPERATOR_SOURCE,
        trigger_type=_TURN_TRIGGER_TYPE,
        status=WAKEUP_STATUS_ENQUEUED,
        session_id=str(review.session_id),
        conversation_id=_TURN_OPERATOR_CONVERSATION_ID,
        wakeup_key=wakeup_key,
        run_id=run_id,
        payload=wakeup_payload,
    )
    review.status = "enqueued"
    review.reason = "notify_user"
    review.run_id = run_id
    db.commit()
    db.refresh(review)

    from zerg.models.enums import RunStatus
    from zerg.models.models import Run

    run_row = db.query(Run).filter(Run.id == run_id).first()
    if run_row is not None:
        run_status = run_row.status.value if hasattr(run_row.status, "value") else str(run_row.status)
        if run_status in {RunStatus.SUCCESS.value, RunStatus.WAITING.value}:
            wakeups_changed = classify_wakeup_outcome_for_run(db, run_id=run_id)
            reviews_changed = classify_turn_review_outcome_for_run(db, run_id=run_id)
            if wakeups_changed or reviews_changed:
                db.commit()
                db.refresh(review)
        elif run_status == RunStatus.CANCELLED.value:
            wakeups_changed = finalize_wakeups_for_run(
                db,
                run_id=run_id,
                status=WAKEUP_STATUS_FAILED,
                reason="run_cancelled",
                payload_updates={"outcome": "failed"},
            )
            reviews_changed = finalize_turn_reviews_for_run(
                db,
                run_id=run_id,
                status="failed",
                reason="run_cancelled",
                actual_outcome="failed",
            )
            if wakeups_changed or reviews_changed:
                db.commit()
                db.refresh(review)
        elif run_status == RunStatus.FAILED.value:
            wakeups_changed = finalize_wakeups_for_run(
                db,
                run_id=run_id,
                status=WAKEUP_STATUS_FAILED,
                reason="run_failed",
                payload_updates={"outcome": "failed"},
            )
            reviews_changed = finalize_turn_reviews_for_run(
                db,
                run_id=run_id,
                status="failed",
                reason="run_failed",
                actual_outcome="failed",
            )
            if wakeups_changed or reviews_changed:
                db.commit()
                db.refresh(review)
    return run_id


async def maybe_execute_recorded_turn_review(*, db: Session, review: SessionTurnReview) -> CommisJob | None:
    if review.status != "recorded":
        return None
    if review.execution_state != "would_auto_continue":
        return None
    if review.recommended_action != "continue_session":
        return None
    try:
        return await approve_pending_turn_review(
            db=db,
            review=review,
            expected_execution_state="would_auto_continue",
        )
    except (ValueError, RuntimeError):
        return None


def _enqueue_same_session_continue_job(
    *,
    db: Session,
    review: SessionTurnReview,
    session: AgentSession,
) -> CommisJob | None:
    if review.owner_id is None:
        _mark_review_outcome(
            db,
            review=review,
            status="failed",
            reason="missing_owner",
            actual_outcome="failed",
        )
        return None

    follow_up_prompt = str(review.follow_up_prompt or "").strip()
    if not follow_up_prompt:
        _mark_review_outcome(
            db,
            review=review,
            status="failed",
            reason="missing_follow_up_prompt",
            actual_outcome="failed",
        )
        return None

    backend = _resume_backend_for_session(session)
    if not backend:
        _mark_review_outcome(
            db,
            review=review,
            status="failed",
            reason="resume_not_supported",
            actual_outcome="failed",
        )
        return None

    try:
        job = CommisJob(
            owner_id=review.owner_id,
            task=follow_up_prompt,
            reasoning_effort="none",
            status="queued",
            config={
                "execution_mode": "workspace",
                "resume_session_id": str(session.id),
                "backend": backend,
                "trigger": "turn_loop",
                "assistant_event_id": review.assistant_event_id,
            },
        )
        db.add(job)
        db.commit()
        db.refresh(job)
    except Exception:
        logger.exception(
            "Failed to enqueue same-session auto-continue for review %s session %s",
            review.id,
            review.session_id,
        )
        db.rollback()
        return None

    return job


async def _continue_managed_local_session(
    *,
    db: Session,
    review: SessionTurnReview,
    session: AgentSession,
) -> bool:
    if review.owner_id is None:
        _mark_review_outcome(
            db,
            review=review,
            status="failed",
            reason="missing_owner",
            actual_outcome="failed",
        )
        return False

    follow_up_prompt = str(review.follow_up_prompt or "").strip()
    if not follow_up_prompt:
        _mark_review_outcome(
            db,
            review=review,
            status="failed",
            reason="missing_follow_up_prompt",
            actual_outcome="failed",
        )
        return False

    send_result = await _send_follow_up_to_managed_local_session(
        db=db,
        owner_id=int(review.owner_id),
        session=session,
        text=follow_up_prompt,
        commis_id=f"turn-review-{review.id}",
        timeout_secs=15,
    )
    if send_result.ok:
        return True

    error_reason = str(send_result.error or "managed_local_send_failed")
    logger.warning(
        "Managed local continue failed for review %s session %s: %s",
        review.id,
        review.session_id,
        error_reason,
    )
    _mark_review_outcome(
        db,
        review=review,
        status="failed",
        reason="managed_local_send_failed",
        actual_outcome="failed",
    )
    return False


async def _send_follow_up_to_managed_local_session(
    *,
    db: Session,
    owner_id: int,
    session: AgentSession,
    text: str,
    commis_id: str | None = None,
    timeout_secs: int = 15,
):
    return await send_text_to_managed_local_session(
        db=db,
        owner_id=owner_id,
        session=session,
        text=text,
        commis_id=commis_id,
        timeout_secs=timeout_secs,
        verify_turn_started=True,
        verification_timeout_secs=float(timeout_secs),
    )


async def approve_pending_turn_review(
    *,
    db: Session,
    review: SessionTurnReview,
    expected_execution_state: str = "awaiting_user_approval",
) -> CommisJob | None:
    if review.status not in {"recorded", "enqueued"}:
        raise ValueError("turn review is no longer actionable")
    if review.execution_state != expected_execution_state:
        raise ValueError("turn review is not in the expected actionable state")
    if review.recommended_action != "continue_session":
        raise ValueError("turn review does not support continue approval")

    session = db.query(AgentSession).filter(AgentSession.id == review.session_id).first()
    if session is None:
        _mark_review_outcome(
            db,
            review=review,
            status="failed",
            reason="missing_session",
            actual_outcome="failed",
        )
        raise RuntimeError("missing_session")
    job: CommisJob | None
    if str(getattr(session, "execution_home", "") or "").strip() == SessionExecutionHome.MANAGED_LOCAL.value:
        sent = await _continue_managed_local_session(db=db, review=review, session=session)
        if not sent:
            raise RuntimeError(str(review.reason or "managed_local_send_failed"))
        job = None
    else:
        job = _enqueue_same_session_continue_job(db=db, review=review, session=session)
        if job is None:
            if review.status != "failed":
                _mark_review_outcome(
                    db,
                    review=review,
                    status="failed",
                    reason="enqueue_failed",
                    actual_outcome="failed",
                )
            raise RuntimeError(str(review.reason or "enqueue_failed"))

    if review.run_id is not None:
        from zerg.services.oikos_wakeup_ledger import WAKEUP_STATUS_ACTED
        from zerg.services.oikos_wakeup_ledger import finalize_wakeups_for_run

        finalize_wakeups_for_run(
            db,
            run_id=review.run_id,
            status=WAKEUP_STATUS_ACTED,
            reason="continue_session",
            payload_updates={
                "outcome": _EXPECTED_CONTINUE_OUTCOME,
                "job_ids": [int(job.id)] if job is not None else [],
                "resume_session_ids": [str(session.id)],
            },
        )

    _mark_review_outcome(
        db,
        review=review,
        status="acted",
        reason="continue_session",
        actual_outcome=_EXPECTED_CONTINUE_OUTCOME,
    )
    return job


async def reply_to_pending_turn_review(
    *,
    db: Session,
    review: SessionTurnReview,
    reply_text: str,
) -> None:
    if review.status not in {"recorded", "enqueued"}:
        raise ValueError("turn review is no longer actionable")
    if review.execution_state not in {"awaiting_user_approval", "needs_human"}:
        raise ValueError("turn review does not accept replies")

    session = db.query(AgentSession).filter(AgentSession.id == review.session_id).first()
    if session is None:
        _mark_review_outcome(
            db,
            review=review,
            status="failed",
            reason="missing_session",
            actual_outcome="failed",
        )
        raise RuntimeError("missing_session")

    if str(getattr(session, "execution_home", "") or "").strip() != SessionExecutionHome.MANAGED_LOCAL.value:
        raise ValueError("reply is only supported for managed local sessions")
    clean_reply = str(reply_text or "").strip()
    if not clean_reply:
        raise ValueError("reply text must not be empty")

    send_result = await send_text_to_managed_local_session(
        db=db,
        owner_id=int(review.owner_id or 0),
        session=session,
        text=clean_reply,
        commis_id=f"turn-review-reply-{review.id}",
        timeout_secs=15,
        verify_turn_started=True,
        verification_timeout_secs=15.0,
    )
    if not send_result.ok:
        _mark_review_outcome(
            db,
            review=review,
            status="failed",
            reason="managed_local_send_failed",
            actual_outcome="failed",
        )
        raise RuntimeError(str(send_result.error or "managed_local_send_failed"))

    if review.run_id is not None:
        from zerg.services.oikos_wakeup_ledger import WAKEUP_STATUS_ACTED
        from zerg.services.oikos_wakeup_ledger import finalize_wakeups_for_run

        finalize_wakeups_for_run(
            db,
            run_id=review.run_id,
            status=WAKEUP_STATUS_ACTED,
            reason="reply_to_session",
            payload_updates={
                "outcome": "delegated_follow_up",
                "reply_sent": True,
                "resume_session_ids": [str(session.id)],
            },
        )

    _mark_review_outcome(
        db,
        review=review,
        status="acted",
        reason="reply_to_session",
        actual_outcome="delegated_follow_up",
    )


def dismiss_pending_turn_review(*, db: Session, review: SessionTurnReview, reason: str = "not_now") -> None:
    if review.status not in {"recorded", "enqueued"}:
        raise ValueError("turn review is no longer actionable")
    if review.execution_state not in {"awaiting_user_approval", "needs_human"}:
        raise ValueError("turn review does not require user attention")

    if review.run_id is not None:
        from zerg.services.oikos_wakeup_ledger import WAKEUP_STATUS_IGNORED
        from zerg.services.oikos_wakeup_ledger import finalize_wakeups_for_run

        finalize_wakeups_for_run(
            db,
            run_id=review.run_id,
            status=WAKEUP_STATUS_IGNORED,
            reason=reason,
            payload_updates={"outcome": _EXPECTED_IGNORE_OUTCOME},
        )

    _mark_review_outcome(
        db,
        review=review,
        status="acted",
        reason=reason,
        actual_outcome=_EXPECTED_IGNORE_OUTCOME,
    )


async def maybe_process_session_turn_loop(
    *,
    db: Session,
    session_id: str,
    freshness_reference_at: datetime | None = None,
    turn_loop_claimed_at: datetime | None = None,
) -> SessionTurnReview | None:
    review, created = await _record_session_turn_review(
        db=db,
        session_id=session_id,
        freshness_reference_at=freshness_reference_at,
        turn_loop_claimed_at=turn_loop_claimed_at,
    )
    if review is None:
        return None
    await maybe_execute_recorded_turn_review(db=db, review=review)
    await maybe_enqueue_turn_review_operator_wakeup(db=db, review=review)
    session = db.query(AgentSession).filter(AgentSession.id == review.session_id).first()
    execution_home = str(getattr(session, "execution_home", "") or "").strip() if session is not None else ""
    is_managed_local_session = execution_home == SessionExecutionHome.MANAGED_LOCAL.value
    if created:
        if session is not None:
            try:
                await _send_turn_review_mobile_notification(db=db, review=review, session=session)
            except Exception:
                logger.exception(
                    "Failed to send turn-loop mobile notification for review %s session %s",
                    review.id,
                    review.session_id,
                )
    completed_at = datetime.now(timezone.utc)
    review_id = int(review.id)
    updated = await _persist_review_timing_fields(
        db=db,
        review_id=review_id,
        label="turn-review-complete",
        turn_loop_completed_at=completed_at,
    )
    if updated:
        db.expire_all()
        review = db.query(SessionTurnReview).filter(SessionTurnReview.id == review_id).first()
    if is_managed_local_session and session is not None:
        if review.execution_state in _ATTENTION_EXECUTION_STATES:
            await persist_managed_local_turn_needs_user(
                db,
                session=session,
                dedupe_suffix=f"review-{review.id}",
            )
        elif review.actual_outcome != _EXPECTED_CONTINUE_OUTCOME:
            await persist_managed_local_turn_idle(
                db,
                session=session,
                dedupe_suffix=f"review-{review.id}",
            )
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
        logger.debug("Skipping turn review classification because the table is unavailable", exc_info=True)
        return 0
    if not rows:
        return 0

    jobs = db.query(CommisJob).filter(CommisJob.oikos_run_id == run_id).all()
    if not jobs:
        for row in rows:
            expected_outcome = _expected_outcome(row)
            if expected_outcome == _EXPECTED_NOTIFY_OUTCOME:
                row.status = "enqueued"
                row.reason = "notify_user"
                row.actual_outcome = _EXPECTED_NOTIFY_OUTCOME
                row.shadow_alignment = _classify_alignment(expected_outcome, _EXPECTED_NOTIFY_OUTCOME)
                continue
            row.status = "ignored"
            row.reason = "no_action"
            row.actual_outcome = _EXPECTED_IGNORE_OUTCOME
            row.shadow_alignment = _classify_alignment(expected_outcome, _EXPECTED_IGNORE_OUTCOME)
        return len(rows)

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
