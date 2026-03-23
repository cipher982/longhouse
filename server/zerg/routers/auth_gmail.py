"""Gmail-connect endpoints and helpers for tenant auth."""

from __future__ import annotations

import os
import time
import urllib.parse
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Literal

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from zerg.auth.session_tokens import JWT_SECRET
from zerg.auth.session_tokens import _encode_jwt
from zerg.config import get_settings
from zerg.crud import create_connector
from zerg.crud import get_connectors
from zerg.crud import update_connector
from zerg.database import get_db
from zerg.dependencies.browser_auth import get_current_browser_user

router = APIRouter(prefix="/auth", tags=["auth"])

HOSTED_GMAIL_CONNECT_TOKEN_TTL_SECONDS = 10 * 60


class GmailWatchStateResponse(BaseModel):
    """Watch/bootstrap state returned after Gmail connector setup."""

    status: Literal["active", "failed", "not_configured"]
    method: Literal["pubsub", "legacy"] | None = None
    history_id: int | None = None
    watch_expiry: int | None = None
    error: str | None = None


class GmailConnectResponse(BaseModel):
    """Response returned after connecting a Gmail inbox."""

    status: Literal["connected"]
    connector_id: int
    mailbox_email: str | None = None
    watch: GmailWatchStateResponse


class HostedGmailConnectStartResponse(BaseModel):
    """Short-lived control-plane redirect URL for hosted Gmail connect."""

    url: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gmail_setup_state(settings: Any) -> tuple[bool, str | None]:
    """Return whether Gmail connect is actually ready and, if not, why."""
    missing: list[str] = []

    if not getattr(settings, "google_client_id", None):
        missing.append("GOOGLE_CLIENT_ID")
    if not getattr(settings, "google_client_secret", None):
        missing.append("GOOGLE_CLIENT_SECRET")
    if not getattr(settings, "gmail_pubsub_topic", None):
        missing.append("GMAIL_PUBSUB_TOPIC")

    if not missing:
        return True, None

    if getattr(settings, "control_plane_url", None):
        return (
            False,
            "Hosted Gmail is not ready on this instance yet. Reprovision it from the "
            "control plane so it picks up Google OAuth and Pub/Sub config.",
        )

    return (
        False,
        f"This instance still needs BYO Google config before anyone can connect Gmail. Missing: {', '.join(missing)}.",
    )


def _normalize_email_address(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized or None


def _gmail_watch_target(settings: Any, callback_url: str | None) -> tuple[str | None, str | None, str | None]:
    topic = getattr(settings, "gmail_pubsub_topic", None)
    if topic:
        return "pubsub", topic, None

    if settings.testing and callback_url:
        return "legacy", callback_url, None

    if callback_url:
        return None, None, "Legacy Gmail HTTPS webhooks are test-only. Configure GMAIL_PUBSUB_TOPIC for production."

    return None, None, "GMAIL_PUBSUB_TOPIC is not configured."


def _exchange_google_auth_code(auth_code: str, *, redirect_uri: str | None = None) -> dict[str, str]:
    """Exchange an authorization code for Google's token payload."""
    settings = get_settings()
    google_client_id = settings.google_client_id
    google_client_secret = settings.google_client_secret

    if google_client_id is None or google_client_secret is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Google OAuth client not configured",
        )

    import json
    import urllib.parse
    import urllib.request

    token_endpoint = "https://oauth2.googleapis.com/token"
    data = {
        "code": auth_code,
        "client_id": google_client_id,
        "client_secret": google_client_secret,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri or "postmessage",
    }

    encoded = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        token_endpoint,
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310
            payload = json.loads(resp.read().decode())
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Google token exchange failed") from exc

    if "refresh_token" not in payload:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No refresh_token in Google response")

    return payload


def _bootstrap_gmail_watch(refresh_token: str, callback_url: str | None) -> tuple[str | None, GmailWatchStateResponse]:
    from zerg.services import gmail_api

    settings = get_settings()
    watch_method, watch_target, watch_target_error = _gmail_watch_target(settings, callback_url)
    mailbox_email: str | None = None

    try:
        access_token = gmail_api.exchange_refresh_token(refresh_token)
    except Exception as exc:
        if watch_method is None:
            return mailbox_email, GmailWatchStateResponse(status="not_configured", error=watch_target_error)
        return (
            mailbox_email,
            GmailWatchStateResponse(
                status="failed",
                method=watch_method,  # type: ignore[arg-type]
                error=f"Failed to exchange Gmail refresh token: {exc}",
            ),
        )

    mailbox_email = _normalize_email_address(gmail_api.get_profile(access_token).get("emailAddress"))

    if watch_method is None:
        return mailbox_email, GmailWatchStateResponse(status="not_configured", error=watch_target_error)

    try:
        if watch_method == "pubsub":
            watch_info = gmail_api.start_watch(access_token=access_token, topic_name=watch_target)
        else:
            watch_info = gmail_api.start_watch(access_token=access_token, callback_url=watch_target)
    except Exception as exc:
        return (
            mailbox_email,
            GmailWatchStateResponse(
                status="failed",
                method=watch_method,  # type: ignore[arg-type]
                error=f"Failed to start Gmail watch: {exc}",
            ),
        )

    return (
        mailbox_email,
        GmailWatchStateResponse(
            status="active",
            method=watch_method,  # type: ignore[arg-type]
            history_id=watch_info["history_id"],
            watch_expiry=watch_info["watch_expiry"],
            error=None,
        ),
    )


def _store_gmail_connector(
    db: Session,
    *,
    owner_id: int,
    refresh_token: str,
    callback_url: str | None,
) -> GmailConnectResponse:
    """Create/update a Gmail connector and persist the latest watch state."""
    from zerg.utils import crypto

    enc = crypto.encrypt(refresh_token)
    existing = get_connectors(db, owner_id=owner_id, type="email", provider="gmail")
    if existing:
        conn = existing[0]
        cfg = dict(conn.config or {})
        cfg["refresh_token"] = enc
        conn = update_connector(db, conn.id, config=cfg)  # type: ignore[assignment]
    else:
        try:
            conn = create_connector(
                db,
                owner_id=owner_id,
                type="email",
                provider="gmail",
                config={"refresh_token": enc},
            )
        except Exception:
            existing = get_connectors(db, owner_id=owner_id, type="email", provider="gmail")
            if not existing:
                raise
            conn = existing[0]
            cfg = dict(conn.config or {})
            cfg["refresh_token"] = enc
            conn = update_connector(db, conn.id, config=cfg)  # type: ignore[assignment]

    mailbox_email, watch_state = _bootstrap_gmail_watch(refresh_token, callback_url)

    cfg = dict(conn.config or {})
    previous_mailbox = _normalize_email_address(cfg.get("emailAddress"))
    effective_mailbox = mailbox_email or previous_mailbox
    mailbox_changed = bool(mailbox_email and previous_mailbox and mailbox_email != previous_mailbox)

    if mailbox_changed:
        cfg.pop("history_id", None)
        cfg.pop("watch_expiry", None)
        cfg.pop("last_notified_history_id", None)

    if effective_mailbox:
        cfg["emailAddress"] = effective_mailbox

    if watch_state.status == "active" and watch_state.method == "pubsub" and not effective_mailbox:
        watch_state = GmailWatchStateResponse(
            status="failed",
            method="pubsub",
            error="Started Gmail watch but could not resolve mailbox email for Pub/Sub routing.",
        )

    cfg["watch_status"] = watch_state.status
    cfg["watch_method"] = watch_state.method
    cfg["watch_error"] = watch_state.error
    cfg["watch_checked_at"] = _utc_now_iso()

    if watch_state.status == "active":
        cfg["history_id"] = watch_state.history_id
        cfg["watch_expiry"] = watch_state.watch_expiry
        cfg.pop("last_notified_history_id", None)

    update_connector(db, conn.id, config=cfg)

    return GmailConnectResponse(
        status="connected",
        connector_id=int(conn.id),
        mailbox_email=effective_mailbox,
        watch=watch_state,
    )


def _issue_hosted_gmail_connect_token(email: str) -> str:
    instance_id = os.getenv("INSTANCE_ID", "").strip()
    if not instance_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Hosted Gmail connect is unavailable because INSTANCE_ID is missing.",
        )

    expires_at = int(time.time()) + HOSTED_GMAIL_CONNECT_TOKEN_TTL_SECONDS
    return _encode_jwt(
        {
            "sub": email,
            "email": email,
            "instance": instance_id,
            "purpose": "hosted_gmail_connect_start",
            "exp": expires_at,
        },
        JWT_SECRET,
    )


@router.post("/google/gmail/start", response_model=HostedGmailConnectStartResponse)
def start_hosted_gmail_connect(
    current_user: Any = Depends(get_current_browser_user),
) -> HostedGmailConnectStartResponse:
    """Return the control-plane Gmail connect URL for hosted instances."""
    settings = get_settings()
    if not settings.control_plane_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Hosted Gmail connect is not available on this instance.",
        )

    token = _issue_hosted_gmail_connect_token(str(getattr(current_user, "email", "")).strip().lower())
    encoded = urllib.parse.quote(token, safe="")
    url = f"{settings.control_plane_url.rstrip('/')}/auth/google/gmail/start?token={encoded}"
    return HostedGmailConnectStartResponse(url=url)


@router.post("/google/gmail", status_code=status.HTTP_200_OK, response_model=GmailConnectResponse)
def connect_gmail(
    body: dict[str, str],
    db: Session = Depends(get_db),
    current_user: Any = Depends(get_current_browser_user),
) -> GmailConnectResponse:
    """Connect Gmail via OAuth and create/update a Gmail connector."""
    auth_code = body.get("auth_code")
    if not auth_code:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="auth_code missing")

    settings = get_settings()
    if getattr(settings, "control_plane_url", None) and not settings.testing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Hosted Gmail connect must start on the control plane.",
        )

    callback_url = body.get("callback_url")
    token_payload = _exchange_google_auth_code(auth_code)
    refresh_token: str = token_payload["refresh_token"]
    return _store_gmail_connector(
        db,
        owner_id=current_user.id,
        refresh_token=refresh_token,
        callback_url=callback_url,
    )


__all__ = [
    "GmailConnectResponse",
    "GmailWatchStateResponse",
    "HostedGmailConnectStartResponse",
    "_exchange_google_auth_code",
    "_gmail_setup_state",
    "_normalize_email_address",
    "_store_gmail_connector",
    "router",
]
