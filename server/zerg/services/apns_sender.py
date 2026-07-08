"""APNs registration helpers and attention-push fan-out."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from hashlib import sha256
from typing import Literal
from uuid import UUID

import httpx
import jwt
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPauseRequest
from zerg.models.agents import SessionRuntimeState
from zerg.models.apns_device_registration import APNSDeviceRegistration
from zerg.models.apns_live_activity_registration import APNSLiveActivityRegistration
from zerg.models.apns_widget_push_state import APNSWidgetPushState
from zerg.models.machine_presence import MachinePresence
from zerg.models.notification_client_presence import NotificationClientPresence
from zerg.models.notification_event import NotificationEvent
from zerg.models.user import User
from zerg.services.agents.kernel_capabilities import project_session_capabilities
from zerg.services.notification_policy import AttentionDeliveryAction
from zerg.services.notification_policy import evaluate_tier1_delivery
from zerg.services.notification_policy import evaluate_tier2_delivery
from zerg.services.notification_policy import user_time_sensitive_blocked
from zerg.services.session_kernel_projection import project_session_control_fields
from zerg.services.session_pause_requests import PAUSE_KIND_STRUCTURED_QUESTION
from zerg.services.session_pause_requests import load_active_pause_request_for_session
from zerg.services.session_pause_requests import serialize_pause_request_projection
from zerg.services.session_runtime import load_runtime_state_map
from zerg.services.session_runtime import resolve_runtime_overlay
from zerg.services.session_runtime_display import build_session_runtime_display
from zerg.services.write_serializer import execute_post_write

logger = logging.getLogger(__name__)

ATTENTION_PUSH_STATES = {"blocked", "needs_answer"}
RESOLVABLE_ATTENTION_PUSH_STATES = ATTENTION_PUSH_STATES | {"needs_user"}
ATTENTION_PUSH_DEBOUNCE = timedelta(seconds=30)
BLOCKED_REMINDER_DELAY = timedelta(minutes=15)
LONG_RUN_WAITING_THRESHOLD = timedelta(minutes=30)
LONG_RUN_WAITING_IDLE_10M_THRESHOLD = timedelta(minutes=15)
LONG_RUN_WAITING_LOCKED_THRESHOLD = timedelta(minutes=10)
LONG_RUN_WAITING_MIN_MEANINGFUL_RUN = timedelta(minutes=5)
MACHINE_PRESENCE_FRESHNESS_WINDOW = timedelta(seconds=90)
MACHINE_ACTIVE_SUPPRESSION_GRACE_WINDOW = timedelta(minutes=3)
WEB_CLIENT_PRESENCE_SUPPRESSION_WINDOW = timedelta(seconds=90)
WIDGET_PUSH_DEBOUNCE = timedelta(seconds=30)
WIDGET_PUSH_PLATFORM = "ios_widget"
LIVE_ACTIVITY_PUSH_DEBOUNCE = timedelta(seconds=15)
ATTENTION_NOTIFICATION_CATEGORY = "LONGHOUSE_SESSION_ATTENTION"
ATTENTION_NOTIFICATION_THREAD_PREFIX = "longhouse-session"
NOTIFICATION_EVENT_SESSION_BLOCKED = "session_blocked"
NOTIFICATION_EVENT_SESSION_BLOCKED_REMINDER = "session_blocked_reminder"
NOTIFICATION_EVENT_SESSION_NEEDS_ANSWER = "session_needs_answer"
NOTIFICATION_EVENT_LONG_RUN_WAITING = "long_run_waiting"
NOTIFICATION_CHANNEL_APNS_IOS = "apns_ios"
PROVIDER_DISPLAY_NAMES = {
    "claude": "Claude",
    "codex": "Codex",
    "cursor": "Cursor",
    "antigravity": "Antigravity",
    "openai": "OpenAI",
    "zai": "z.ai",
    "z.ai": "z.ai",
}
_APNS_PROVIDER_TOKEN_TTL = timedelta(minutes=50)

_cached_provider_token: str | None = None
_cached_provider_token_expires_at: datetime | None = None

# Sentinel for prepare_* `targets` kwargs: distinguishes "caller did not pre-fetch"
# (look up internally — preserves single-event call sites in presence.py) from
# "caller pre-fetched and the owner has no targets" (None; skip).
_TARGETS_SENTINEL: object = object()


@dataclass(frozen=True)
class APNSDeviceTarget:
    device_token: str
    push_environment: Literal["sandbox", "production"]


@dataclass(frozen=True)
class SessionAttentionPush:
    session_id: str
    state: Literal["blocked", "needs_user", "needs_answer"]
    occurred_at: datetime
    title: str
    summary: str
    project: str | None
    provider: str | None
    tool_name: str | None
    alert_title: str
    alert_body: str
    collapse_id: str
    targets: tuple[APNSDeviceTarget, ...]
    event_type: str = NOTIFICATION_EVENT_SESSION_BLOCKED
    notification_event_id: str | None = None
    pause_request_id: str | None = None
    previous_stamp_state: str | None = None
    previous_stamp_at: datetime | None = None
    stamp_state: str = "blocked"
    time_sensitive: bool = False


@dataclass(frozen=True)
class SessionAttentionResolutionPush:
    session_id: str
    previous_state: Literal["needs_user", "blocked", "needs_answer"]
    current_state: str
    occurred_at: datetime
    attention_push_at: datetime
    collapse_id: str
    targets: tuple[APNSDeviceTarget, ...]


@dataclass(frozen=True)
class WidgetTimelinePush:
    owner_id: int
    state_hash: str
    previous_state_hash: str | None
    previous_push_at: datetime | None
    occurred_at: datetime
    collapse_id: str
    targets: tuple[APNSDeviceTarget, ...]


@dataclass(frozen=True)
class LiveActivityPush:
    registration_id: str
    owner_id: int
    session_id: str
    activity_id: str
    push_token: str
    push_environment: Literal["sandbox", "production"]
    state_hash: str
    previous_state_hash: str | None
    previous_push_at: datetime | None
    occurred_at: datetime
    title: str
    provider: str
    project: str | None
    presence_state: str
    display_phase: str
    active_tool: str | None
    is_attention: bool


def user_apns_enabled(user: User | None) -> bool:
    if user is None:
        return False
    prefs = dict(getattr(user, "prefs", None) or {})
    value = prefs.get("apns_enabled")
    if value is None:
        return True
    return bool(value)


def set_user_apns_enabled(user: User, enabled: bool) -> dict:
    prefs = dict(getattr(user, "prefs", None) or {})
    prefs["apns_enabled"] = bool(enabled)
    user.prefs = prefs
    return prefs


def record_notification_delivery_result(
    db: Session,
    *,
    event_id: str | None,
    channel: str,
    accepted: bool,
    occurred_at: datetime,
) -> bool:
    """Record the result of one notification channel delivery attempt."""

    if not event_id:
        return False
    event = db.query(NotificationEvent).filter(NotificationEvent.id == event_id).first()
    if event is None:
        return False

    occurred_at_utc = _as_aware_utc(occurred_at)
    channel_results = dict(getattr(event, "channel_results", None) or {})
    channel_results[channel] = {
        "accepted": bool(accepted),
        "attempted_at": occurred_at_utc.isoformat() if occurred_at_utc else None,
    }
    event.channel_results = channel_results
    if accepted:
        event.delivered_at = occurred_at
    else:
        event.failed_at = occurred_at
        # No delivered alert exists to resolve. Close the audit row so future
        # unresolved-attention queries do not accumulate failed attempts.
        event.resolved_at = occurred_at
    return True


def rollback_session_attention_push_stamp(db: Session, *, notification: SessionAttentionPush) -> bool:
    """Rollback a pre-send attention stamp after no APNs target accepted it."""

    if notification.event_type in {NOTIFICATION_EVENT_SESSION_BLOCKED_REMINDER, NOTIFICATION_EVENT_LONG_RUN_WAITING}:
        return restore_session_attention_push_stamp(
            db,
            session_id=notification.session_id,
            expected_state=notification.stamp_state,
            expected_at=notification.occurred_at,
            previous_state=notification.previous_stamp_state,
            previous_at=notification.previous_stamp_at,
        )
    return clear_session_attention_push_stamp(
        db,
        session_id=notification.session_id,
        state=notification.state,
        occurred_at=notification.occurred_at,
    )


def restore_session_attention_push_stamp(
    db: Session,
    *,
    session_id: str,
    expected_state: str,
    expected_at: datetime,
    previous_state: str | None,
    previous_at: datetime | None,
) -> bool:
    """Restore a previous debounce stamp after a reminder send failure."""

    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if session is None:
        return False
    if str(session.last_attention_push_state or "").strip() != expected_state:
        return False
    if not _same_instant(session.last_attention_push_at, expected_at):
        return False
    session.last_attention_push_state = previous_state
    session.last_attention_push_at = previous_at
    return True


def clear_session_attention_push_stamp(
    db: Session,
    *,
    session_id: str,
    state: str,
    occurred_at: datetime,
) -> bool:
    """Clear a pre-send debounce stamp when no APNs target accepted the push."""

    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if session is None:
        return False
    current_state = str(session.last_attention_push_state or "").strip()
    if current_state != state and _base_attention_state(current_state) != state:
        return False
    if not _same_instant(session.last_attention_push_at, occurred_at):
        return False
    session.last_attention_push_at = None
    session.last_attention_push_state = None
    return True


def clear_session_attention_resolution_stamp(
    db: Session,
    *,
    session_id: str,
    state: str,
    attention_push_at: datetime,
) -> bool:
    """Clear a pre-send resolution stamp when no APNs target accepted the push."""

    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if session is None:
        return False
    if str(session.last_attention_push_state or "").strip() != _resolved_attention_state(state):
        return False
    if not _same_instant(session.last_attention_push_at, attention_push_at):
        return False
    session.last_attention_push_state = state
    return True


def _create_notification_event(
    db: Session,
    *,
    owner_id: int,
    session_id: str,
    event_type: str,
    state_key: str,
    collapse_key: str,
    occurred_at: datetime,
    channel_results: dict | None = None,
    eligible_at: datetime | None = None,
) -> NotificationEvent:
    event = NotificationEvent(
        owner_id=owner_id,
        session_id=session_id,
        event_type=event_type,
        state_key=state_key,
        collapse_key=collapse_key,
        event_started_at=occurred_at,
        eligible_at=eligible_at or occurred_at,
        channel_results=channel_results or {},
    )
    db.add(event)
    db.flush()
    return event


def _load_owner_user(db: Session, owner_id: int | None) -> User | None:
    if owner_id is None:
        return None
    return db.query(User).filter(User.id == owner_id).first()


def _record_attention_policy_decision(
    db: Session,
    *,
    owner_id: int,
    session_id: str,
    event_type: str,
    state_key: str,
    collapse_key: str,
    occurred_at: datetime,
    reason: str,
    queue_until: datetime | None = None,
) -> None:
    if db.query(User.id).filter(User.id == owner_id).first() is None:
        return
    existing = (
        db.query(NotificationEvent.id)
        .filter(
            NotificationEvent.owner_id == owner_id,
            NotificationEvent.collapse_key == collapse_key,
            NotificationEvent.event_type == event_type,
            NotificationEvent.delivered_at.is_(None),
            NotificationEvent.resolved_at.is_(None),
        )
        .first()
    )
    if existing is not None:
        return
    channel_results: dict[str, object] = {"suppressed": reason}
    eligible_at = occurred_at
    if queue_until is not None:
        channel_results["queued"] = True
        eligible_at = queue_until
    _create_notification_event(
        db,
        owner_id=owner_id,
        session_id=session_id,
        event_type=event_type,
        state_key=state_key,
        collapse_key=collapse_key,
        occurred_at=occurred_at,
        eligible_at=eligible_at,
        channel_results=channel_results,
    )


def _tier1_policy_allows_delivery(
    db: Session,
    *,
    owner_id: int,
    session: AgentSession,
    event_type: str,
    state_key: str,
    collapse_key: str,
    occurred_at: datetime,
) -> bool:
    user = _load_owner_user(db, owner_id)
    decision = evaluate_tier1_delivery(
        db,
        user=user,
        session=session,
        occurred_at=occurred_at,
        event_type=event_type,
    )
    if decision.action == AttentionDeliveryAction.DELIVER:
        return True
    _record_attention_policy_decision(
        db,
        owner_id=owner_id,
        session_id=str(session.id),
        event_type=event_type,
        state_key=state_key,
        collapse_key=collapse_key,
        occurred_at=occurred_at,
        reason=str(decision.reason or decision.action.value),
        queue_until=decision.queue_until,
    )
    return False


def _tier2_policy_allows_delivery(
    db: Session,
    *,
    owner_id: int,
    session: AgentSession,
    event_type: str,
    state_key: str,
    collapse_key: str,
    occurred_at: datetime,
) -> bool:
    user = _load_owner_user(db, owner_id)
    decision = evaluate_tier2_delivery(
        db,
        user=user,
        session=session,
        occurred_at=occurred_at,
    )
    if decision.action == AttentionDeliveryAction.DELIVER:
        return True
    _record_attention_policy_decision(
        db,
        owner_id=owner_id,
        session_id=str(session.id),
        event_type=event_type,
        state_key=state_key,
        collapse_key=collapse_key,
        occurred_at=occurred_at,
        reason=str(decision.reason or decision.action.value),
        queue_until=decision.queue_until,
    )
    return False


def _record_no_ios_targets(
    db: Session,
    *,
    owner_id: int,
    session_id: str,
    event_type: str,
    state_key: str,
    collapse_key: str,
    occurred_at: datetime,
) -> None:
    _record_attention_policy_decision(
        db,
        owner_id=owner_id,
        session_id=session_id,
        event_type=event_type,
        state_key=state_key,
        collapse_key=collapse_key,
        occurred_at=occurred_at,
        reason="no_ios_targets",
    )


def _mark_attention_events_resolved(
    db: Session,
    *,
    owner_id: int,
    session_id: str,
    occurred_at: datetime,
) -> None:
    events = (
        db.query(NotificationEvent)
        .filter(
            NotificationEvent.owner_id == owner_id,
            NotificationEvent.session_id == session_id,
            NotificationEvent.event_type.in_(
                [
                    NOTIFICATION_EVENT_SESSION_BLOCKED,
                    NOTIFICATION_EVENT_SESSION_BLOCKED_REMINDER,
                    NOTIFICATION_EVENT_SESSION_NEEDS_ANSWER,
                    NOTIFICATION_EVENT_LONG_RUN_WAITING,
                ]
            ),
            NotificationEvent.resolved_at.is_(None),
        )
        .all()
    )
    for event in events:
        event.resolved_at = occurred_at


def clear_widget_timeline_push_stamp(
    db: Session,
    *,
    owner_id: int,
    state_hash: str,
    previous_state_hash: str | None,
    previous_push_at: datetime | None,
) -> bool:
    """Rollback a widget-set push stamp when APNs accepts no widget targets."""

    state = db.query(APNSWidgetPushState).filter(APNSWidgetPushState.owner_id == owner_id).first()
    if state is None or state.state_hash != state_hash:
        return False
    state.state_hash = previous_state_hash
    state.last_push_at = previous_push_at
    return True


def clear_live_activity_push_stamp(
    db: Session,
    *,
    registration_id: str,
    state_hash: str,
    previous_state_hash: str | None,
    previous_push_at: datetime | None,
) -> bool:
    """Rollback a Live Activity push stamp when APNs accepts no update."""

    query = db.query(APNSLiveActivityRegistration)
    registration = query.filter(APNSLiveActivityRegistration.id == registration_id).first()
    if registration is None or registration.last_state_hash != state_hash:
        return False
    registration.last_state_hash = previous_state_hash
    registration.last_push_at = previous_push_at
    return True


def prepare_session_attention_push(
    db: Session,
    *,
    owner_id: int | None,
    session_id,
    previous_state: str | None,
    current_state: str | None,
    occurred_at: datetime,
    current_tool_name: str | None = None,
    targets: tuple[APNSDeviceTarget, ...] | None | object = _TARGETS_SENTINEL,
) -> SessionAttentionPush | None:
    if owner_id is None or current_state != "blocked" or previous_state == current_state:
        return None

    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if session is None:
        return None

    last_attention_push_at = _as_aware_utc(session.last_attention_push_at)
    last_attention_push_state = _base_attention_state(session.last_attention_push_state)
    is_repeat_attention_state = last_attention_push_state == current_state
    if (
        is_repeat_attention_state
        and last_attention_push_at is not None
        and (occurred_at - last_attention_push_at) < ATTENTION_PUSH_DEBOUNCE
    ):
        return None

    collapse_id = _attention_collapse_id(str(session.id))
    state_key = f"{current_state}:{occurred_at.isoformat()}"
    if not _tier1_policy_allows_delivery(
        db,
        owner_id=owner_id,
        session=session,
        event_type=NOTIFICATION_EVENT_SESSION_BLOCKED,
        state_key=state_key,
        collapse_key=collapse_id,
        occurred_at=occurred_at,
    ):
        return None

    if targets is _TARGETS_SENTINEL:
        targets = _active_ios_targets_for_owner(db, owner_id=owner_id, log_context="attention push")
    if not targets:
        _record_no_ios_targets(
            db,
            owner_id=owner_id,
            session_id=str(session.id),
            event_type=NOTIFICATION_EVENT_SESSION_BLOCKED,
            state_key=state_key,
            collapse_key=collapse_id,
            occurred_at=occurred_at,
        )
        return None

    if previous_state in RESOLVABLE_ATTENTION_PUSH_STATES and previous_state != current_state:
        _mark_attention_events_resolved(
            db,
            owner_id=owner_id,
            session_id=str(session.id),
            occurred_at=occurred_at,
        )
    session.last_attention_push_at = occurred_at
    session.last_attention_push_state = current_state
    notification_event = _create_notification_event(
        db,
        owner_id=owner_id,
        session_id=str(session.id),
        event_type=NOTIFICATION_EVENT_SESSION_BLOCKED,
        state_key=f"{current_state}:{occurred_at.isoformat()}",
        collapse_key=collapse_id,
        occurred_at=occurred_at,
    )

    provider = _clean_label(getattr(session, "provider", None))
    project = _clean_label(getattr(session, "project", None))
    tool_name = _clean_label(current_tool_name)
    title = _session_title(session, db=db)
    summary = str(getattr(session, "summary", "") or "").strip() or title
    alert_title = _attention_alert_title(state=current_state, provider=provider)
    alert_body = _attention_alert_body(state=current_state, project=project, title=title, tool_name=tool_name)
    owner_user = _load_owner_user(db, owner_id)
    time_sensitive = user_time_sensitive_blocked(owner_user) and current_state == "blocked"

    return SessionAttentionPush(
        session_id=str(session.id),
        state=current_state,
        occurred_at=occurred_at,
        title=title,
        summary=summary,
        project=project,
        provider=provider,
        tool_name=tool_name,
        alert_title=alert_title,
        alert_body=alert_body,
        collapse_id=collapse_id,
        targets=targets,
        event_type=NOTIFICATION_EVENT_SESSION_BLOCKED,
        notification_event_id=str(notification_event.id),
        previous_stamp_state=last_attention_push_state,
        previous_stamp_at=last_attention_push_at,
        stamp_state=current_state,
        time_sensitive=time_sensitive,
    )


def prepare_session_needs_answer_push(
    db: Session,
    *,
    owner_id: int | None,
    session_id,
    pause_request: SessionPauseRequest | None,
    previous_state: str | None,
    occurred_at: datetime,
    targets: tuple[APNSDeviceTarget, ...] | None | object = _TARGETS_SENTINEL,
) -> SessionAttentionPush | None:
    if owner_id is None or session_id is None or pause_request is None:
        return None
    if str(getattr(pause_request, "kind", "") or "").strip() != PAUSE_KIND_STRUCTURED_QUESTION:
        return None

    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if session is None:
        return None

    pause_request_id = str(pause_request.id)
    stamp_state = f"needs_answer:{pause_request_id}"
    previous_stamp_state = str(session.last_attention_push_state or "").strip() or None
    previous_stamp_at = _as_aware_utc(session.last_attention_push_at)
    if previous_stamp_state == stamp_state:
        return None

    collapse_id = _attention_collapse_id(str(session.id))
    if not _tier1_policy_allows_delivery(
        db,
        owner_id=owner_id,
        session=session,
        event_type=NOTIFICATION_EVENT_SESSION_NEEDS_ANSWER,
        state_key=stamp_state,
        collapse_key=collapse_id,
        occurred_at=occurred_at,
    ):
        return None

    if targets is _TARGETS_SENTINEL:
        targets = _active_ios_targets_for_owner(db, owner_id=owner_id, log_context="needs-answer push")
    if not targets:
        _record_no_ios_targets(
            db,
            owner_id=owner_id,
            session_id=str(session.id),
            event_type=NOTIFICATION_EVENT_SESSION_NEEDS_ANSWER,
            state_key=stamp_state,
            collapse_key=collapse_id,
            occurred_at=occurred_at,
        )
        return None

    replaces_previous_attention = previous_state != "needs_answer" or previous_stamp_state != stamp_state
    if previous_state in RESOLVABLE_ATTENTION_PUSH_STATES and replaces_previous_attention:
        _mark_attention_events_resolved(
            db,
            owner_id=owner_id,
            session_id=str(session.id),
            occurred_at=occurred_at,
        )
    session.last_attention_push_at = occurred_at
    session.last_attention_push_state = stamp_state
    notification_event = _create_notification_event(
        db,
        owner_id=owner_id,
        session_id=str(session.id),
        event_type=NOTIFICATION_EVENT_SESSION_NEEDS_ANSWER,
        state_key=stamp_state,
        collapse_key=collapse_id,
        occurred_at=occurred_at,
    )

    provider = _clean_label(getattr(session, "provider", None))
    project = _clean_label(getattr(session, "project", None))
    title = _session_title(session, db=db)
    summary = str(getattr(session, "summary", "") or "").strip() or title
    pause_title = _clean_label(getattr(pause_request, "title", None))
    pause_title = pause_title or _clean_label(getattr(pause_request, "summary", None))

    return SessionAttentionPush(
        session_id=str(session.id),
        state="needs_answer",
        occurred_at=occurred_at,
        title=title,
        summary=summary,
        project=project,
        provider=provider,
        tool_name=_clean_label(getattr(pause_request, "tool_name", None)),
        alert_title="Needs answer",
        alert_body=_needs_answer_alert_body(project=project, pause_title=pause_title, title=title),
        collapse_id=collapse_id,
        targets=targets,
        event_type=NOTIFICATION_EVENT_SESSION_NEEDS_ANSWER,
        notification_event_id=str(notification_event.id),
        pause_request_id=pause_request_id,
        previous_stamp_state=previous_stamp_state,
        previous_stamp_at=previous_stamp_at,
        stamp_state=stamp_state,
        time_sensitive=user_time_sensitive_blocked(_load_owner_user(db, owner_id)),
    )


def prepare_session_blocked_reminder_push(
    db: Session,
    *,
    owner_id: int | None,
    session_id,
    current_state: str | None,
    occurred_at: datetime,
    current_tool_name: str | None = None,
    targets: tuple[APNSDeviceTarget, ...] | None | object = _TARGETS_SENTINEL,
) -> SessionAttentionPush | None:
    if owner_id is None or session_id is None or current_state != "blocked":
        return None

    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if session is None:
        return None

    previous_stamp_state = str(session.last_attention_push_state or "").strip() or None
    previous_stamp_at = _as_aware_utc(session.last_attention_push_at)
    if _base_attention_state(previous_stamp_state) != "blocked" or previous_stamp_at is None:
        return None
    if previous_stamp_state and previous_stamp_state.endswith(":resolved"):
        return None
    if previous_stamp_state and previous_stamp_state.endswith(":reminded"):
        return None
    if (occurred_at - previous_stamp_at) < BLOCKED_REMINDER_DELAY:
        return None

    collapse_id = _collapse_id("lh-attn-reminder", str(session.id))
    state_key = f"blocked:{previous_stamp_at.isoformat()}"
    if not _tier1_policy_allows_delivery(
        db,
        owner_id=owner_id,
        session=session,
        event_type=NOTIFICATION_EVENT_SESSION_BLOCKED_REMINDER,
        state_key=state_key,
        collapse_key=collapse_id,
        occurred_at=occurred_at,
    ):
        return None

    if targets is _TARGETS_SENTINEL:
        targets = _active_ios_targets_for_owner(db, owner_id=owner_id, log_context="blocked reminder push")
    if not targets:
        _record_no_ios_targets(
            db,
            owner_id=owner_id,
            session_id=str(session.id),
            event_type=NOTIFICATION_EVENT_SESSION_BLOCKED_REMINDER,
            state_key=state_key,
            collapse_key=collapse_id,
            occurred_at=occurred_at,
        )
        return None

    provider = _clean_label(getattr(session, "provider", None))
    project = _clean_label(getattr(session, "project", None))
    tool_name = _clean_label(current_tool_name)
    title = _session_title(session, db=db)
    summary = str(getattr(session, "summary", "") or "").strip() or title
    stamp_state = "blocked:reminded"
    notification_event = _create_notification_event(
        db,
        owner_id=owner_id,
        session_id=str(session.id),
        event_type=NOTIFICATION_EVENT_SESSION_BLOCKED_REMINDER,
        state_key=state_key,
        collapse_key=collapse_id,
        occurred_at=occurred_at,
    )
    session.last_attention_push_at = occurred_at
    session.last_attention_push_state = stamp_state

    return SessionAttentionPush(
        session_id=str(session.id),
        state="blocked",
        occurred_at=occurred_at,
        title=title,
        summary=summary,
        project=project,
        provider=provider,
        tool_name=tool_name,
        alert_title="Still needs permission",
        alert_body=_attention_alert_body(state="blocked", project=project, title=title, tool_name=tool_name),
        collapse_id=collapse_id,
        targets=targets,
        event_type=NOTIFICATION_EVENT_SESSION_BLOCKED_REMINDER,
        notification_event_id=str(notification_event.id),
        previous_stamp_state=previous_stamp_state,
        previous_stamp_at=previous_stamp_at,
        stamp_state=stamp_state,
        time_sensitive=user_time_sensitive_blocked(_load_owner_user(db, owner_id)),
    )


def _recent_visible_web_client_exists(db: Session, *, owner_id: int, occurred_at: datetime) -> bool:
    threshold = occurred_at - WEB_CLIENT_PRESENCE_SUPPRESSION_WINDOW
    try:
        return (
            db.query(NotificationClientPresence.id)
            .filter(
                NotificationClientPresence.owner_id == owner_id,
                NotificationClientPresence.client_type == "web",
                NotificationClientPresence.visible.is_(True),
                NotificationClientPresence.last_seen_at >= threshold,
            )
            .first()
            is not None
        )
    except OperationalError as exc:
        if _is_missing_optional_table(exc):
            return False
        raise


def _machine_presence_rows_since(
    db: Session,
    *,
    owner_id: int,
    occurred_at: datetime,
    window: timedelta,
) -> list[MachinePresence]:
    threshold = occurred_at - window
    try:
        rows = db.query(MachinePresence).filter(MachinePresence.owner_id == owner_id).all()
    except OperationalError as exc:
        if _is_missing_optional_table(exc):
            return []
        raise
    recent_rows: list[MachinePresence] = []
    for row in rows:
        received_at = _as_aware_utc(row.received_at)
        if received_at is not None and received_at >= threshold:
            recent_rows.append(row)
    return recent_rows


def _long_run_waiting_threshold_for_owner(
    db: Session,
    *,
    owner_id: int,
    occurred_at: datetime,
) -> timedelta | None:
    if _recent_visible_web_client_exists(db, owner_id=owner_id, occurred_at=occurred_at):
        return None

    active_grace_rows = _machine_presence_rows_since(
        db,
        owner_id=owner_id,
        occurred_at=occurred_at,
        window=MACHINE_ACTIVE_SUPPRESSION_GRACE_WINDOW,
    )
    active_grace_states = {str(row.state or "").strip() for row in active_grace_rows}
    if "active" in active_grace_states:
        return None

    rows = _machine_presence_rows_since(
        db,
        owner_id=owner_id,
        occurred_at=occurred_at,
        window=MACHINE_PRESENCE_FRESHNESS_WINDOW,
    )
    states = {str(row.state or "").strip() for row in rows}
    if "locked" in states:
        return LONG_RUN_WAITING_LOCKED_THRESHOLD
    if states and states.issubset({"idle_10m"}):
        return LONG_RUN_WAITING_IDLE_10M_THRESHOLD
    return LONG_RUN_WAITING_THRESHOLD


def _session_execution_started_at(db: Session, *, session_id) -> datetime | None:
    runtime_state = (
        db.query(SessionRuntimeState)
        .filter(SessionRuntimeState.session_id == session_id)
        .order_by(SessionRuntimeState.updated_at.desc(), SessionRuntimeState.runtime_version.desc())
        .first()
    )
    if runtime_state is None:
        return None
    return _as_aware_utc(runtime_state.execution_started_at)


def _long_run_alert_body(*, project: str | None, title: str, elapsed: timedelta) -> str:
    elapsed_minutes = max(1, int(elapsed.total_seconds() // 60))
    parts: list[str] = []
    if project:
        parts.append(project)
    parts.append(f"Ran {elapsed_minutes}m")
    parts.append(title)
    return _trim_alert_text(" · ".join(parts))


def prepare_long_run_waiting_push(
    db: Session,
    *,
    owner_id: int | None,
    session_id,
    current_state: str | None,
    occurred_at: datetime,
    targets: tuple[APNSDeviceTarget, ...] | None | object = _TARGETS_SENTINEL,
) -> SessionAttentionPush | None:
    if owner_id is None or session_id is None or current_state != "needs_user":
        return None

    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if session is None:
        return None

    execution_started_at = _session_execution_started_at(db, session_id=session_id)
    if execution_started_at is None:
        return None
    elapsed = occurred_at - execution_started_at
    threshold = _long_run_waiting_threshold_for_owner(db, owner_id=owner_id, occurred_at=occurred_at)
    if threshold is None:
        return None
    threshold = max(threshold, LONG_RUN_WAITING_MIN_MEANINGFUL_RUN)
    if elapsed < threshold:
        return None

    previous_stamp_state = str(session.last_attention_push_state or "").strip() or None
    previous_stamp_at = _as_aware_utc(session.last_attention_push_at)
    if _base_attention_state(previous_stamp_state) == "needs_user":
        return None
    if _has_unresolved_attention(previous_stamp_state, "blocked"):
        return None
    if _has_unresolved_attention(previous_stamp_state, "needs_answer"):
        return None

    collapse_id = _collapse_id("lh-attn-longrun", str(session.id))
    state_key = f"needs_user:{execution_started_at.isoformat()}"
    if not _tier2_policy_allows_delivery(
        db,
        owner_id=owner_id,
        session=session,
        event_type=NOTIFICATION_EVENT_LONG_RUN_WAITING,
        state_key=state_key,
        collapse_key=collapse_id,
        occurred_at=occurred_at,
    ):
        return None

    if targets is _TARGETS_SENTINEL:
        targets = _active_ios_targets_for_owner(db, owner_id=owner_id, log_context="long-run waiting push")
    if not targets:
        _record_no_ios_targets(
            db,
            owner_id=owner_id,
            session_id=str(session.id),
            event_type=NOTIFICATION_EVENT_LONG_RUN_WAITING,
            state_key=state_key,
            collapse_key=collapse_id,
            occurred_at=occurred_at,
        )
        return None

    provider = _clean_label(getattr(session, "provider", None))
    project = _clean_label(getattr(session, "project", None))
    title = _session_title(session, db=db)
    summary = str(getattr(session, "summary", "") or "").strip() or title
    notification_event = _create_notification_event(
        db,
        owner_id=owner_id,
        session_id=str(session.id),
        event_type=NOTIFICATION_EVENT_LONG_RUN_WAITING,
        state_key=state_key,
        collapse_key=collapse_id,
        occurred_at=occurred_at,
    )
    session.last_attention_push_at = occurred_at
    stamp_state = "needs_user:long_run"
    session.last_attention_push_state = stamp_state

    return SessionAttentionPush(
        session_id=str(session.id),
        state="needs_user",
        occurred_at=occurred_at,
        title=title,
        summary=summary,
        project=project,
        provider=provider,
        tool_name=None,
        alert_title="Ready for you",
        alert_body=_long_run_alert_body(project=project, title=title, elapsed=elapsed),
        collapse_id=collapse_id,
        targets=targets,
        event_type=NOTIFICATION_EVENT_LONG_RUN_WAITING,
        notification_event_id=str(notification_event.id),
        previous_stamp_state=previous_stamp_state,
        previous_stamp_at=previous_stamp_at,
        stamp_state=stamp_state,
    )


def prepare_session_attention_resolution_push(
    db: Session,
    *,
    owner_id: int | None,
    session_id,
    previous_state: str | None,
    current_state: str | None,
    occurred_at: datetime,
    targets: tuple[APNSDeviceTarget, ...] | None | object = _TARGETS_SENTINEL,
) -> SessionAttentionResolutionPush | None:
    if (
        owner_id is None
        or session_id is None
        or previous_state not in RESOLVABLE_ATTENTION_PUSH_STATES
        or current_state in ATTENTION_PUSH_STATES
        or current_state == previous_state
    ):
        return None

    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if session is None:
        return None

    last_attention_push_at = _as_aware_utc(session.last_attention_push_at)
    last_attention_push_state = str(session.last_attention_push_state or "").strip() or None
    # The raw marker must match the unresolved state. A ":resolved" suffix means
    # this visible alert has already had one cleanup push scheduled.
    if (
        _base_attention_state(last_attention_push_state) != previous_state
        or last_attention_push_state == _resolved_attention_state(previous_state)
        or last_attention_push_at is None
    ):
        return None

    if targets is _TARGETS_SENTINEL:
        targets = _active_ios_targets_for_owner(db, owner_id=owner_id, log_context="attention resolution push")
    if not targets:
        return None

    session.last_attention_push_state = _resolved_attention_state(previous_state)
    _mark_attention_events_resolved(
        db,
        owner_id=owner_id,
        session_id=str(session.id),
        occurred_at=occurred_at,
    )

    return SessionAttentionResolutionPush(
        session_id=str(session.id),
        previous_state=previous_state,
        current_state=str(current_state or "unknown"),
        occurred_at=occurred_at,
        attention_push_at=last_attention_push_at,
        collapse_id=_collapse_id("lh-attn-resolved", str(session.id)),
        targets=targets,
    )


def prepare_widget_timeline_push(
    db: Session,
    *,
    owner_id: int | None,
    occurred_at: datetime,
    targets: tuple[APNSDeviceTarget, ...] | None | object = _TARGETS_SENTINEL,
) -> WidgetTimelinePush | None:
    if owner_id is None:
        return None

    try:
        widget_state = db.query(APNSWidgetPushState).filter(APNSWidgetPushState.owner_id == owner_id).first()
    except OperationalError as exc:
        if _is_missing_optional_table(exc):
            logger.warning("APNs widget state table unavailable; skipping widget timeline push for user %s", owner_id)
            return None
        raise

    if widget_state is not None:
        previous_push_at_utc = _as_aware_utc(widget_state.last_push_at)
        if previous_push_at_utc is not None and (occurred_at - previous_push_at_utc) < WIDGET_PUSH_DEBOUNCE:
            return None

    if targets is _TARGETS_SENTINEL:
        targets = _active_ios_targets_for_owner(
            db,
            owner_id=owner_id,
            platform=WIDGET_PUSH_PLATFORM,
            log_context="widget timeline push",
        )
    if not targets:
        return None

    state_hash = _widget_active_set_hash(db, now=occurred_at)
    if widget_state is None:
        widget_state = APNSWidgetPushState(owner_id=owner_id)
        db.add(widget_state)
        db.flush()

    previous_state_hash = widget_state.state_hash
    previous_push_at = widget_state.last_push_at
    if previous_state_hash == state_hash:
        return None

    widget_state.state_hash = state_hash
    widget_state.last_push_at = occurred_at

    return WidgetTimelinePush(
        owner_id=owner_id,
        state_hash=state_hash,
        previous_state_hash=previous_state_hash,
        previous_push_at=previous_push_at,
        occurred_at=occurred_at,
        collapse_id=_collapse_id("lh-widget", str(owner_id)),
        targets=targets,
    )


def prepare_session_live_activity_pushes(
    db: Session,
    *,
    owner_id: int | None,
    session_id: UUID | None,
    current_state: str | None,
    current_tool_name: str | None,
    occurred_at: datetime,
    runtime_state_map: dict | None | object = _TARGETS_SENTINEL,
) -> tuple[LiveActivityPush, ...]:
    if owner_id is None or session_id is None:
        return ()

    try:
        registrations = (
            db.query(APNSLiveActivityRegistration)
            .filter(
                APNSLiveActivityRegistration.owner_id == owner_id,
                APNSLiveActivityRegistration.session_id == str(session_id),
                APNSLiveActivityRegistration.ended_at.is_(None),
            )
            .order_by(APNSLiveActivityRegistration.last_seen_at.desc(), APNSLiveActivityRegistration.created_at.desc())
            .all()
        )
    except OperationalError as exc:
        if _is_missing_optional_table(exc):
            logger.warning(
                "APNs Live Activity table unavailable; skipping Live Activity push for session %s",
                session_id,
            )
            return ()
        raise

    if not registrations:
        return ()

    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if session is None:
        return ()

    provider = str(getattr(session, "provider", None) or "Session")
    if runtime_state_map is _TARGETS_SENTINEL:
        runtime_state_map = load_runtime_state_map(db, [session.id])
    runtime_overlay = resolve_runtime_overlay(
        session,
        last_activity_at=session.last_activity_at,
        runtime_state_map=runtime_state_map or {},
        now=occurred_at,
    )
    runtime_display = build_session_runtime_display(
        runtime_view=runtime_overlay,
        capabilities=project_session_capabilities(db, session_id=session.id),
        ended_at=session.ended_at,
        pause_request=serialize_pause_request_projection(load_active_pause_request_for_session(db, session.id)),
    )
    presence_state = runtime_display.state or str(current_state or getattr(session, "status", None) or "unknown")
    active_tool = runtime_display.compact_tool_label or str(current_tool_name or "").strip() or None
    display_phase = runtime_display.phase_label or _live_activity_display_phase(presence_state, active_tool)
    title = _session_title(session, db=db)
    project = _session_project(session)
    is_attention = runtime_display.needs_attention
    state_hash = _live_activity_state_hash(
        title=title,
        provider=provider,
        project=project,
        presence_state=presence_state,
        display_phase=display_phase,
        active_tool=active_tool,
        is_attention=is_attention,
    )

    notifications: list[LiveActivityPush] = []
    for registration in registrations:
        previous_state_hash = registration.last_state_hash
        previous_push_at = registration.last_push_at
        previous_push_at_utc = _as_aware_utc(previous_push_at)
        if previous_state_hash == state_hash:
            continue
        if previous_push_at_utc is not None and (occurred_at - previous_push_at_utc) < LIVE_ACTIVITY_PUSH_DEBOUNCE:
            continue

        push_environment: Literal["sandbox", "production"] = (
            "production" if str(registration.push_environment or "sandbox") == "production" else "sandbox"
        )

        registration.last_state_hash = state_hash
        registration.last_push_at = occurred_at
        notifications.append(
            LiveActivityPush(
                registration_id=str(registration.id),
                owner_id=owner_id,
                session_id=str(session_id),
                activity_id=str(registration.activity_id),
                push_token=str(registration.push_token),
                push_environment=push_environment,
                state_hash=state_hash,
                previous_state_hash=previous_state_hash,
                previous_push_at=previous_push_at,
                occurred_at=occurred_at,
                title=title,
                provider=provider,
                project=project,
                presence_state=presence_state,
                display_phase=display_phase,
                active_tool=active_tool,
                is_attention=is_attention,
            )
        )
    return tuple(notifications)


async def send_presence_pushes(
    *,
    attention_push: SessionAttentionPush | None,
    attention_resolution_push: SessionAttentionResolutionPush | None,
    widget_push: WidgetTimelinePush | None,
    live_activity_pushes: tuple[LiveActivityPush, ...],
    db: Session | None,
    ws,
    dispatch_label_prefix: str,
) -> None:
    """Send pre-prepared APNs pushes and roll back debounce stamps on reject.

    Caller is responsible for preparing the pushes atomically with the
    underlying state write (same WriteSerializer closure). This helper only
    performs the network send + rollback. ``db`` is a fallback for unconfigured
    or test serializers and may be ``None`` after production request-session
    release.
    """

    if attention_push is not None:
        push_sent = False
        try:
            push_sent = await send_session_attention_push(attention_push)
        except Exception:
            logger.exception("Failed to send APNs attention push for session %s", attention_push.session_id)

        def _record_attention_result(write_db: Session) -> bool:
            return record_notification_delivery_result(
                write_db,
                event_id=attention_push.notification_event_id,
                channel=NOTIFICATION_CHANNEL_APNS_IOS,
                accepted=push_sent,
                occurred_at=attention_push.occurred_at,
            )

        await execute_post_write(ws, _record_attention_result, db, label=f"{dispatch_label_prefix}-attention-record")
        if not push_sent:

            def _clear_attention(write_db: Session):
                rollback_session_attention_push_stamp(write_db, notification=attention_push)

            await execute_post_write(ws, _clear_attention, db, label=f"{dispatch_label_prefix}-attention-clear")

    if attention_resolution_push is not None:
        resolution_accepted = False
        try:
            resolution_accepted = await send_session_attention_resolution_push(attention_resolution_push)
        except Exception:
            logger.exception("Failed to send APNs resolution push for session %s", attention_resolution_push.session_id)
        if not resolution_accepted:

            def _clear_resolution(write_db: Session) -> bool:
                return clear_session_attention_resolution_stamp(
                    write_db,
                    session_id=attention_resolution_push.session_id,
                    state=attention_resolution_push.previous_state,
                    attention_push_at=attention_resolution_push.attention_push_at,
                )

            await execute_post_write(ws, _clear_resolution, db, label=f"{dispatch_label_prefix}-resolution-clear")

    if widget_push is not None:
        widget_accepted = False
        try:
            widget_accepted = await send_widget_timeline_push(widget_push)
        except Exception:
            logger.exception("Failed to send APNs widget push for user %s", widget_push.owner_id)
        if not widget_accepted:

            def _clear_widget(write_db: Session) -> bool:
                return clear_widget_timeline_push_stamp(
                    write_db,
                    owner_id=widget_push.owner_id,
                    state_hash=widget_push.state_hash,
                    previous_state_hash=widget_push.previous_state_hash,
                    previous_push_at=widget_push.previous_push_at,
                )

            await execute_post_write(ws, _clear_widget, db, label=f"{dispatch_label_prefix}-widget-clear")

    for live_activity_push in live_activity_pushes:
        accepted = False
        try:
            accepted = await send_session_live_activity_push(live_activity_push)
        except Exception:
            logger.exception(
                "Failed to send APNs Live Activity push for session %s",
                live_activity_push.session_id,
            )
        if not accepted:

            def _clear_live(write_db: Session, push=live_activity_push) -> bool:
                return clear_live_activity_push_stamp(
                    write_db,
                    registration_id=push.registration_id,
                    state_hash=push.state_hash,
                    previous_state_hash=push.previous_state_hash,
                    previous_push_at=push.previous_push_at,
                )

            await execute_post_write(ws, _clear_live, db, label=f"{dispatch_label_prefix}-live-clear")


async def send_session_attention_push(notification: SessionAttentionPush) -> bool:
    settings = get_settings()
    if settings.testing or not settings.apns_enabled:
        return False

    provider_token = _provider_token()
    topic = str(settings.apns_topic or "ai.longhouse.ios").strip() or "ai.longhouse.ios"
    payload = build_session_attention_payload(notification)
    expiration = str(int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()))
    accepted = False

    async with httpx.AsyncClient(http2=True, timeout=10.0) as client:
        for target in notification.targets:
            host = _apns_host(target.push_environment)
            headers = {
                "authorization": f"bearer {provider_token}",
                "apns-topic": topic,
                "apns-push-type": "alert",
                "apns-priority": "10",
                "apns-collapse-id": notification.collapse_id,
                "apns-expiration": expiration,
            }
            if notification.time_sensitive:
                headers["apns-interruption-level"] = "time-sensitive"
            url = f"https://{host}/3/device/{target.device_token}"
            try:
                response = await client.post(url, headers=headers, json=payload)
            except Exception as exc:  # noqa: BLE001
                logger.warning("APNs send failed for session %s: %s", notification.session_id, exc)
                continue
            if response.status_code >= 300:
                logger.warning(
                    "APNs rejected push for session %s (%s): %s %s",
                    notification.session_id,
                    target.push_environment,
                    response.status_code,
                    response.text,
                )
            else:
                accepted = True
    return accepted


async def send_session_attention_resolution_push(notification: SessionAttentionResolutionPush) -> bool:
    settings = get_settings()
    if settings.testing or not settings.apns_enabled:
        return False

    provider_token = _provider_token()
    topic = str(settings.apns_topic or "ai.longhouse.ios").strip() or "ai.longhouse.ios"
    payload = build_session_attention_resolution_payload(notification)
    expiration = str(int((datetime.now(timezone.utc) + timedelta(minutes=30)).timestamp()))
    accepted = False

    async with httpx.AsyncClient(http2=True, timeout=10.0) as client:
        for target in notification.targets:
            host = _apns_host(target.push_environment)
            headers = {
                "authorization": f"bearer {provider_token}",
                "apns-topic": topic,
                "apns-push-type": "background",
                "apns-priority": "5",
                "apns-collapse-id": notification.collapse_id,
                "apns-expiration": expiration,
            }
            url = f"https://{host}/3/device/{target.device_token}"
            try:
                response = await client.post(url, headers=headers, json=payload)
            except Exception as exc:  # noqa: BLE001
                logger.warning("APNs resolution push failed for session %s: %s", notification.session_id, exc)
                continue
            if response.status_code >= 300:
                logger.warning(
                    "APNs rejected resolution push for session %s (%s): %s %s",
                    notification.session_id,
                    target.push_environment,
                    response.status_code,
                    response.text,
                )
            else:
                accepted = True
    return accepted


async def send_widget_timeline_push(notification: WidgetTimelinePush) -> bool:
    settings = get_settings()
    if settings.testing or not settings.apns_enabled:
        return False

    provider_token = _provider_token()
    topic = f"{str(settings.apns_topic or 'ai.longhouse.ios').strip() or 'ai.longhouse.ios'}.push-type.widgets"
    payload = build_widget_timeline_payload()
    expiration = str(int((datetime.now(timezone.utc) + timedelta(minutes=30)).timestamp()))
    accepted = False

    async with httpx.AsyncClient(http2=True, timeout=10.0) as client:
        for target in notification.targets:
            host = _apns_host(target.push_environment)
            headers = {
                "authorization": f"bearer {provider_token}",
                "apns-topic": topic,
                "apns-push-type": "widgets",
                "apns-priority": "5",
                "apns-collapse-id": notification.collapse_id,
                "apns-expiration": expiration,
            }
            url = f"https://{host}/3/device/{target.device_token}"
            try:
                response = await client.post(url, headers=headers, json=payload)
            except Exception as exc:  # noqa: BLE001
                logger.warning("APNs widget push failed for user %s: %s", notification.owner_id, exc)
                continue
            if response.status_code >= 300:
                logger.warning(
                    "APNs rejected widget push for user %s (%s): %s %s",
                    notification.owner_id,
                    target.push_environment,
                    response.status_code,
                    response.text,
                )
            else:
                accepted = True
    return accepted


async def send_session_live_activity_push(notification: LiveActivityPush) -> bool:
    settings = get_settings()
    if settings.testing or not settings.apns_enabled:
        return False

    provider_token = _provider_token()
    topic = f"{str(settings.apns_topic or 'ai.longhouse.ios').strip() or 'ai.longhouse.ios'}.push-type.liveactivity"
    payload = build_session_live_activity_payload(notification)
    expiration = str(int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()))
    headers = {
        "authorization": f"bearer {provider_token}",
        "apns-topic": topic,
        "apns-push-type": "liveactivity",
        "apns-priority": "10" if notification.is_attention else "5",
        "apns-collapse-id": _collapse_id("lh-live", notification.activity_id),
        "apns-expiration": expiration,
    }
    host = _apns_host(notification.push_environment)
    url = f"https://{host}/3/device/{notification.push_token}"

    async with httpx.AsyncClient(http2=True, timeout=10.0) as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "APNs Live Activity push failed for session %s activity %s: %s",
                notification.session_id,
                notification.activity_id,
                exc,
            )
            return False
        if response.status_code >= 300:
            logger.warning(
                "APNs rejected Live Activity push for session %s activity %s (%s): %s %s",
                notification.session_id,
                notification.activity_id,
                notification.push_environment,
                response.status_code,
                response.text,
            )
            return False
    return True


def build_session_attention_payload(notification: SessionAttentionPush) -> dict:
    return {
        "aps": {
            "alert": {
                "title": notification.alert_title,
                "body": notification.alert_body,
            },
            "category": ATTENTION_NOTIFICATION_CATEGORY,
            "thread-id": f"{ATTENTION_NOTIFICATION_THREAD_PREFIX}-{notification.session_id}",
            "sound": "default",
        },
        "session_id": notification.session_id,
        "title": _trim_alert_text(notification.title, limit=200),
        "summary": _trim_alert_text(notification.summary, limit=500),
        "state": notification.state,
        "attention_state": notification.state,
        "project": notification.project,
        "provider": notification.provider,
        "tool_name": notification.tool_name,
        "pause_request_id": notification.pause_request_id,
    }


def build_session_attention_resolution_payload(notification: SessionAttentionResolutionPush) -> dict:
    return {
        "aps": {
            "content-available": 1,
        },
        "event": "attention_resolved",
        "session_id": notification.session_id,
        "state": notification.current_state,
        "attention_state": "resolved",
        "previous_attention_state": notification.previous_state,
    }


def build_widget_timeline_payload() -> dict:
    return {
        "aps": {
            "content-changed": True,
        },
    }


def build_session_live_activity_payload(notification: LiveActivityPush) -> dict:
    timestamp = int(notification.occurred_at.timestamp())
    return {
        "aps": {
            "timestamp": timestamp,
            "event": "update",
            "content-state": {
                "presenceState": notification.presence_state,
                "displayPhase": notification.display_phase,
                "activeTool": notification.active_tool,
                "updatedAt": timestamp,
                "isAttention": notification.is_attention,
            },
            "stale-date": timestamp + 300,
            "relevance-score": 80 if notification.is_attention else 50,
        }
    }


def active_ios_targets_for_owner(
    db: Session,
    *,
    owner_id: int,
    platform: str = "ios",
    log_context: str,
) -> tuple[APNSDeviceTarget, ...] | None:
    """Public alias for `_active_ios_targets_for_owner` (kept for legacy callers)."""
    return _active_ios_targets_for_owner(db, owner_id=owner_id, platform=platform, log_context=log_context)


def _active_ios_targets_for_owner(
    db: Session,
    *,
    owner_id: int,
    platform: str = "ios",
    log_context: str,
) -> tuple[APNSDeviceTarget, ...] | None:
    try:
        user = db.query(User).filter(User.id == owner_id).first()
    except OperationalError as exc:
        if _is_missing_optional_table(exc):
            logger.debug("Skipping APNs %s; users table is unavailable", log_context, exc_info=exc)
            return None
        raise
    if not user_apns_enabled(user):
        return None

    try:
        registrations = (
            db.query(APNSDeviceRegistration)
            .filter(
                APNSDeviceRegistration.owner_id == owner_id,
                APNSDeviceRegistration.platform == platform,
                APNSDeviceRegistration.revoked_at.is_(None),
            )
            .order_by(APNSDeviceRegistration.last_seen_at.desc(), APNSDeviceRegistration.created_at.desc())
            .all()
        )
    except OperationalError as exc:
        if _is_missing_optional_table(exc):
            logger.debug("Skipping APNs %s; registration table is unavailable", log_context, exc_info=exc)
            return None
        raise

    targets = tuple(
        APNSDeviceTarget(
            device_token=registration.device_token,
            push_environment="production" if registration.push_environment == "production" else "sandbox",
        )
        for registration in registrations
    )
    return targets or None


def _widget_active_set_hash(db: Session, *, now: datetime) -> str:
    since = now - timedelta(days=14)
    sessions = (
        db.query(AgentSession)
        .filter(
            AgentSession.user_state == "active",
            AgentSession.started_at >= since,
        )
        .order_by(AgentSession.last_activity_at.desc(), AgentSession.started_at.desc())
        .limit(8)
        .all()
    )
    runtime_state_map = load_runtime_state_map(db, [session.id for session in sessions])
    parts: list[str] = []
    for session in sessions:
        runtime_overlay = resolve_runtime_overlay(
            session,
            last_activity_at=session.last_activity_at,
            runtime_state_map=runtime_state_map,
            now=now,
        )
        parts.append(f"{session.id}:{runtime_overlay.presence_state or 'unknown'}")
    return sha256("|".join(parts).encode("utf-8")).hexdigest()


def _live_activity_state_hash(
    *,
    title: str,
    provider: str,
    project: str | None,
    presence_state: str,
    display_phase: str,
    active_tool: str | None,
    is_attention: bool,
) -> str:
    parts = [
        title,
        provider,
        project or "",
        presence_state,
        display_phase,
        active_tool or "",
        "attention" if is_attention else "normal",
    ]
    return sha256("|".join(parts).encode("utf-8")).hexdigest()


def _live_activity_display_phase(presence_state: str, active_tool: str | None) -> str:
    match presence_state:
        case "running":
            return f"Running {active_tool}" if active_tool else "Running"
        case "thinking":
            return "Thinking"
        case "needs_user":
            return "Idle"
        case "blocked":
            return f"Blocked on {active_tool}" if active_tool else "Needs permission"
        case "idle":
            return "Idle"
        case _:
            return "Unknown"


def _session_title(session: AgentSession, *, db: Session | None = None) -> str:
    managed_session_name = project_session_control_fields(db, session).managed_session_name if db is not None else None
    return (
        str(getattr(session, "summary_title", "") or "").strip()
        or str(managed_session_name or "").strip()
        or str(getattr(session, "project", "") or "").strip()
        or str(getattr(session, "provider", "") or "").strip()
        or "Longhouse session"
    )


def _session_project(session: AgentSession) -> str | None:
    project = str(getattr(session, "project", "") or "").strip()
    return project or None


def _attention_alert_title(*, state: str, provider: str | None) -> str:
    return "Needs permission"


def _attention_alert_body(*, state: str, project: str | None, title: str, tool_name: str | None) -> str:
    parts: list[str] = []
    if project:
        parts.append(project)
    if state == "blocked":
        parts.append(f"Blocked on {tool_name}" if tool_name else "Blocked")
    parts.append(title)
    return _trim_alert_text(" · ".join(parts))


def _needs_answer_alert_body(*, project: str | None, pause_title: str | None, title: str) -> str:
    parts: list[str] = []
    if project:
        parts.append(project)
    if pause_title:
        parts.append(pause_title)
    parts.append(title)
    return _trim_alert_text(" · ".join(parts))


def _clean_label(value: object) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _provider_display_name(provider: str) -> str:
    cleaned = str(provider or "").strip()
    if not cleaned:
        return "Session"
    return PROVIDER_DISPLAY_NAMES.get(cleaned.lower(), cleaned.replace("_", " ").title())


def _attention_collapse_id(session_id: str) -> str:
    return _collapse_id("lh-attn", session_id)


def _collapse_id(prefix: str, identifier: str) -> str:
    candidate = f"{prefix}-{identifier}"
    if len(candidate.encode("utf-8")) <= 64:
        return candidate
    digest = sha256(identifier.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


def _trim_alert_text(value: str, limit: int = 180) -> str:
    compact = " ".join(str(value or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _resolved_attention_state(state: str) -> str:
    return f"{state}:resolved"


def _has_unresolved_attention(stamp_state: str | None, state: str) -> bool:
    return _base_attention_state(stamp_state) == state and stamp_state != _resolved_attention_state(state)


def _base_attention_state(state: str | None) -> str | None:
    value = str(state or "").strip()
    if ":" in value:
        value = value.split(":", 1)[0]
    return value if value in RESOLVABLE_ATTENTION_PUSH_STATES else None


def _same_instant(left: datetime | None, right: datetime) -> bool:
    if left is None:
        return False
    left = _as_aware_utc(left)
    right = _as_aware_utc(right)
    if left is None or right is None:
        return False
    return abs((left - right).total_seconds()) < 0.001


def _as_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _apns_host(push_environment: Literal["sandbox", "production"]) -> str:
    return "api.push.apple.com" if push_environment == "production" else "api.sandbox.push.apple.com"


def _is_missing_optional_table(exc: OperationalError) -> bool:
    return "no such table" in str(exc).lower()


def _normalized_private_key(raw: str) -> str:
    raw = str(raw or "").strip()
    if "\\n" in raw and "\n" not in raw:
        raw = raw.replace("\\n", "\n")
    return raw


def _provider_token() -> str:
    global _cached_provider_token, _cached_provider_token_expires_at

    now = datetime.now(timezone.utc)
    cached_token_is_fresh = _cached_provider_token_expires_at is not None and now < _cached_provider_token_expires_at
    if _cached_provider_token is not None and cached_token_is_fresh:
        return _cached_provider_token

    settings = get_settings()
    team_id = str(settings.apns_team_id or "").strip()
    key_id = str(settings.apns_key_id or "").strip()
    private_key = _normalized_private_key(settings.apns_private_key_p8 or "")
    if not team_id or not key_id or not private_key:
        raise RuntimeError("APNs settings are incomplete")

    payload = {
        "iss": team_id,
        "iat": int(now.timestamp()),
    }
    headers = {
        "alg": "ES256",
        "kid": key_id,
    }
    token = jwt.encode(payload, private_key, algorithm="ES256", headers=headers)
    _cached_provider_token = token
    _cached_provider_token_expires_at = now + _APNS_PROVIDER_TOKEN_TTL
    return token
