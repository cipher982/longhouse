"""APNs registration helpers and attention-push fan-out."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Literal

import httpx
import jwt
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.models.agents import AgentSession
from zerg.models.apns_device_registration import APNSDeviceRegistration
from zerg.models.user import User

logger = logging.getLogger(__name__)

ATTENTION_PUSH_STATES = {"needs_user", "blocked"}
ATTENTION_PUSH_DEBOUNCE = timedelta(seconds=30)
_APNS_PROVIDER_TOKEN_TTL = timedelta(minutes=50)

_cached_provider_token: str | None = None
_cached_provider_token_expires_at: datetime | None = None


@dataclass(frozen=True)
class APNSDeviceTarget:
    device_token: str
    push_environment: Literal["sandbox", "production"]


@dataclass(frozen=True)
class SessionAttentionPush:
    session_id: str
    state: Literal["needs_user", "blocked"]
    title: str
    summary: str
    alert_title: str
    alert_body: str
    targets: tuple[APNSDeviceTarget, ...]


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


def prepare_session_attention_push(
    db: Session,
    *,
    owner_id: int | None,
    session_id,
    previous_state: str | None,
    current_state: str | None,
    occurred_at: datetime,
) -> SessionAttentionPush | None:
    if owner_id is None or current_state not in ATTENTION_PUSH_STATES or previous_state == current_state:
        return None

    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if session is None:
        return None

    last_attention_push_at = session.last_attention_push_at
    if last_attention_push_at is not None and last_attention_push_at.tzinfo is None:
        last_attention_push_at = last_attention_push_at.replace(tzinfo=timezone.utc)
    if last_attention_push_at is not None and (occurred_at - last_attention_push_at) < ATTENTION_PUSH_DEBOUNCE:
        return None

    try:
        user = db.query(User).filter(User.id == owner_id).first()
    except OperationalError as exc:
        if _is_missing_optional_table(exc):
            logger.debug("Skipping APNs attention push; users table is unavailable", exc_info=exc)
            return None
        raise
    if not user_apns_enabled(user):
        return None

    try:
        registrations = (
            db.query(APNSDeviceRegistration)
            .filter(
                APNSDeviceRegistration.owner_id == owner_id,
                APNSDeviceRegistration.platform == "ios",
                APNSDeviceRegistration.revoked_at.is_(None),
            )
            .order_by(APNSDeviceRegistration.last_seen_at.desc(), APNSDeviceRegistration.created_at.desc())
            .all()
        )
    except OperationalError as exc:
        if _is_missing_optional_table(exc):
            logger.debug("Skipping APNs attention push; registration table is unavailable", exc_info=exc)
            return None
        raise
    if not registrations:
        return None

    session.last_attention_push_at = occurred_at
    session.last_attention_push_state = current_state

    title = (
        str(getattr(session, "summary_title", "") or "").strip()
        or str(getattr(session, "managed_session_name", "") or "").strip()
        or str(getattr(session, "project", "") or "").strip()
        or str(getattr(session, "provider", "") or "").strip()
        or "Longhouse session"
    )
    summary = str(getattr(session, "summary", "") or "").strip() or title
    alert_title = "Needs you" if current_state == "needs_user" else "Blocked"
    alert_body = _trim_alert_text(summary if summary else title)

    targets = tuple(
        APNSDeviceTarget(
            device_token=registration.device_token,
            push_environment="production" if registration.push_environment == "production" else "sandbox",
        )
        for registration in registrations
    )
    if not targets:
        return None

    return SessionAttentionPush(
        session_id=str(session.id),
        state=current_state,
        title=title,
        summary=summary,
        alert_title=alert_title,
        alert_body=alert_body,
        targets=targets,
    )


async def send_session_attention_push(notification: SessionAttentionPush) -> None:
    settings = get_settings()
    if settings.testing or not settings.apns_enabled:
        return

    provider_token = _provider_token()
    topic = str(settings.apns_topic or "ai.longhouse.ios").strip() or "ai.longhouse.ios"
    payload = {
        "aps": {
            "alert": {
                "title": notification.alert_title,
                "body": notification.alert_body,
            },
            "sound": "default",
        },
        "session_id": notification.session_id,
        "title": notification.title,
        "summary": notification.summary,
        "state": notification.state,
    }

    async with httpx.AsyncClient(http2=True, timeout=10.0) as client:
        for target in notification.targets:
            host = _apns_host(target.push_environment)
            headers = {
                "authorization": f"bearer {provider_token}",
                "apns-topic": topic,
                "apns-push-type": "alert",
                "apns-priority": "10",
            }
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


def _trim_alert_text(value: str, limit: int = 180) -> str:
    compact = " ".join(str(value or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


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
    if _cached_provider_token is not None and _cached_provider_token_expires_at is not None and now < _cached_provider_token_expires_at:
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
