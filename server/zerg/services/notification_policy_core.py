"""Database-free notification delivery policy primitives.

The catalog daemon imports this module while it owns the live SQLite lane. It
must not pull in the Runtime Host database or ORM models just to evaluate a
user's notification preferences.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import time
from datetime import timedelta
from datetime import timezone
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

WEB_CLIENT_PRESENCE_SUPPRESSION_WINDOW = timedelta(seconds=90)


class AttentionDeliveryAction(str, Enum):
    DELIVER = "deliver"
    SUPPRESS = "suppress"
    QUEUE = "queue"


@dataclass(frozen=True)
class AttentionDeliveryDecision:
    action: AttentionDeliveryAction
    reason: str | None = None
    queue_until: datetime | None = None


def _user_prefs(user: Any | None) -> dict:
    return dict(getattr(user, "prefs", None) or {})


def user_apns_notifications_enabled(user: Any | None) -> bool:
    if user is None:
        return False
    value = _user_prefs(user).get("apns_enabled")
    return True if value is None else bool(value)


def user_notify_only_when_away(user: Any | None) -> bool:
    return bool(_user_prefs(user).get("notify_only_when_away"))


def user_time_sensitive_blocked(user: Any | None) -> bool:
    return bool(_user_prefs(user).get("time_sensitive_blocked"))


def _parse_hhmm(value: object) -> time | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) != 5 or text[2] != ":":
        return None
    try:
        hour = int(text[0:2])
        minute = int(text[3:5])
        return time(hour=hour, minute=minute)
    except ValueError:
        return None


def _user_timezone(user: Any | None) -> ZoneInfo:
    tz_name = str(_user_prefs(user).get("timezone") or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


def quiet_hours_window(user: Any | None) -> tuple[time | None, time | None]:
    prefs = _user_prefs(user)
    start = _parse_hhmm(prefs.get("quiet_hours_start"))
    end = _parse_hhmm(prefs.get("quiet_hours_end"))
    if start is None or end is None:
        return None, None
    return start, end


def in_quiet_hours(user: Any | None, occurred_at: datetime) -> bool:
    start, end = quiet_hours_window(user)
    if start is None or end is None:
        return False
    local = occurred_at.astimezone(_user_timezone(user)).timetz().replace(tzinfo=None)
    current = time(hour=local.hour, minute=local.minute)
    if start < end:
        return start <= current < end
    return current >= start or current < end


def next_quiet_hours_end(user: Any | None, occurred_at: datetime) -> datetime | None:
    start, end = quiet_hours_window(user)
    if start is None or end is None:
        return None
    tz = _user_timezone(user)
    local_dt = occurred_at.astimezone(tz)
    end_dt = datetime.combine(local_dt.date(), end, tzinfo=tz)
    if end <= start:
        if local_dt.time() >= start:
            end_dt = end_dt + timedelta(days=1)
        elif local_dt.time() >= end:
            end_dt = end_dt + timedelta(days=1)
    elif local_dt.time() >= end:
        end_dt = end_dt + timedelta(days=1)
    return end_dt.astimezone(timezone.utc)


def evaluate_tier1_delivery_with_facts(
    db: Any,
    *,
    user: Any | None,
    session_muted: bool,
    visible_web_client: bool,
    occurred_at: datetime,
    event_type: str,
) -> AttentionDeliveryDecision:
    """Evaluate Tier 1 policy from facts already owned by the caller."""

    del db
    if not user_apns_notifications_enabled(user):
        return AttentionDeliveryDecision(AttentionDeliveryAction.SUPPRESS, "apns_disabled")
    if session_muted:
        return AttentionDeliveryDecision(AttentionDeliveryAction.SUPPRESS, "session_muted")
    if user_notify_only_when_away(user) and visible_web_client:
        return AttentionDeliveryDecision(AttentionDeliveryAction.SUPPRESS, "web_presence")

    if in_quiet_hours(user, occurred_at):
        bypass = user_time_sensitive_blocked(user) and event_type in {
            "session_blocked",
            "session_needs_answer",
            "session_blocked_reminder",
        }
        if not bypass:
            queue_until = next_quiet_hours_end(user, occurred_at)
            if queue_until is not None:
                return AttentionDeliveryDecision(AttentionDeliveryAction.QUEUE, "quiet_hours", queue_until)

    return AttentionDeliveryDecision(AttentionDeliveryAction.DELIVER)
