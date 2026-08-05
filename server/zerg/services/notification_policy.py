"""User notification preferences and delivery policy for session attention."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.notification_client_presence import NotificationClientPresence
from zerg.models.user import User
from zerg.services.notification_policy_core import WEB_CLIENT_PRESENCE_SUPPRESSION_WINDOW
from zerg.services.notification_policy_core import AttentionDeliveryAction
from zerg.services.notification_policy_core import AttentionDeliveryDecision
from zerg.services.notification_policy_core import _parse_hhmm
from zerg.services.notification_policy_core import evaluate_tier1_delivery_with_facts
from zerg.services.notification_policy_core import in_quiet_hours
from zerg.services.notification_policy_core import next_quiet_hours_end
from zerg.services.notification_policy_core import user_apns_notifications_enabled
from zerg.services.notification_policy_core import user_notify_only_when_away
from zerg.services.notification_policy_core import user_time_sensitive_blocked


def _user_prefs(user: User | None) -> dict:
    return dict(getattr(user, "prefs", None) or {})


def session_notifications_muted(session: AgentSession | None) -> bool:
    if session is None:
        return False
    from zerg.services.session_preferences import load_session_preferences

    return load_session_preferences(session.id, standalone_session=session).notification_muted


def recent_visible_web_client_exists(db: Session, *, owner_id: int, occurred_at: datetime) -> bool:
    threshold = occurred_at - WEB_CLIENT_PRESENCE_SUPPRESSION_WINDOW
    from zerg.database import live_catalog_enabled

    if live_catalog_enabled():
        from zerg.services.catalog_read_gateway import CatalogReadError
        from zerg.services.catalog_read_gateway import recent_visible_web_presence

        try:
            return recent_visible_web_presence(owner_id=owner_id, threshold=threshold.isoformat())
        except CatalogReadError:
            return False
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
    except OperationalError:
        return False


def evaluate_tier1_delivery(
    db: Session,
    *,
    user: User | None,
    session: AgentSession | None,
    occurred_at: datetime,
    event_type: str,
) -> AttentionDeliveryDecision:
    return evaluate_tier1_delivery_with_facts(
        db,
        user=user,
        session_muted=session_notifications_muted(session),
        visible_web_client=recent_visible_web_client_exists(db, owner_id=int(user.id), occurred_at=occurred_at)
        if user is not None
        else False,
        occurred_at=occurred_at,
        event_type=event_type,
    )


def evaluate_tier2_delivery(
    db: Session,
    *,
    user: User | None,
    session: AgentSession | None,
    occurred_at: datetime,
) -> AttentionDeliveryDecision:
    if not user_apns_notifications_enabled(user):
        return AttentionDeliveryDecision(AttentionDeliveryAction.SUPPRESS, "apns_disabled")
    if session_notifications_muted(session):
        return AttentionDeliveryDecision(AttentionDeliveryAction.SUPPRESS, "session_muted")

    if recent_visible_web_client_exists(db, owner_id=int(user.id), occurred_at=occurred_at):
        return AttentionDeliveryDecision(AttentionDeliveryAction.SUPPRESS, "web_presence")

    if in_quiet_hours(user, occurred_at):
        queue_until = next_quiet_hours_end(user, occurred_at)
        if queue_until is not None:
            return AttentionDeliveryDecision(AttentionDeliveryAction.QUEUE, "quiet_hours", queue_until)

    return AttentionDeliveryDecision(AttentionDeliveryAction.DELIVER)


def tier_label(tier: Literal[1, 2]) -> str:
    return "tier1" if tier == 1 else "tier2"


@dataclass(frozen=True)
class UserNotificationPrefs:
    apns_enabled: bool
    notify_only_when_away: bool
    time_sensitive_blocked: bool
    quiet_hours_start: str | None
    quiet_hours_end: str | None


def load_user_notification_prefs(user: User | None) -> UserNotificationPrefs:
    from zerg.services.apns_sender import user_apns_enabled

    prefs = _user_prefs(user)
    return UserNotificationPrefs(
        apns_enabled=user_apns_enabled(user),
        notify_only_when_away=user_notify_only_when_away(user),
        time_sensitive_blocked=user_time_sensitive_blocked(user),
        quiet_hours_start=_parse_hhmm(prefs.get("quiet_hours_start")) and str(prefs.get("quiet_hours_start")).strip(),
        quiet_hours_end=_parse_hhmm(prefs.get("quiet_hours_end")) and str(prefs.get("quiet_hours_end")).strip(),
    )


def apply_user_notification_prefs(user: User, patch: dict[str, object]) -> UserNotificationPrefs:
    from zerg.services.apns_sender import set_user_apns_enabled

    prefs = _user_prefs(user)
    if "apns_enabled" in patch:
        set_user_apns_enabled(user, bool(patch["apns_enabled"]))
        prefs = _user_prefs(user)
    for key in ("notify_only_when_away", "time_sensitive_blocked"):
        if key in patch:
            prefs[key] = bool(patch[key])
    for key in ("quiet_hours_start", "quiet_hours_end"):
        if key in patch:
            value = patch[key]
            if value is None or value == "":
                prefs.pop(key, None)
            else:
                parsed = _parse_hhmm(value)
                if parsed is None:
                    raise ValueError(f"invalid {key}")
                prefs[key] = str(value).strip()
    user.prefs = prefs
    return load_user_notification_prefs(user)
