"""Explicit signed share-link service for session detail pages."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

import zerg.dependencies.auth as auth_deps
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionInput
from zerg.models.session_share import SessionShare
from zerg.models.session_share import SessionShareEvent
from zerg.models.user import User
from zerg.services.session_views import SessionSharerResponse
from zerg.utils.time import normalize_utc

TOKEN_PREFIX = "lhshr"
TOKEN_VERSION = "v1"
DEFAULT_SHARE_TTL_DAYS = 30


class SessionShareError(ValueError):
    """Base exception for share-link validation failures."""

    status_code = 400
    detail = "Invalid share link"


class SessionShareNotFound(SessionShareError):
    status_code = 404
    detail = "Share link not found"


class SessionShareExpired(SessionShareError):
    status_code = 410
    detail = "Share link expired"


class SessionShareRevoked(SessionShareError):
    status_code = 410
    detail = "Share link revoked"


class SessionShareMisconfigured(SessionShareError):
    status_code = 500
    detail = "Share links are not configured"


@dataclass(frozen=True)
class ResolvedSessionShare:
    share: SessionShare
    session: AgentSession
    sharer: SessionSharerResponse | None


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _signing_secret() -> bytes:
    secret = str(getattr(auth_deps, "JWT_SECRET", "") or "")
    if secret.strip() in {"", "dev-secret"} or len(secret) < 16:
        raise SessionShareMisconfigured()
    return secret.encode("utf-8")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _token_signature(payload: str) -> str:
    digest = hmac.new(_signing_secret(), payload.encode("utf-8"), hashlib.sha256).digest()
    return _b64url_encode(digest)


def _build_token(share_id: int, nonce: str) -> str:
    payload = _b64url_encode(f"{TOKEN_VERSION}.{share_id}.{nonce}".encode("utf-8"))
    signature = _token_signature(payload)
    return f"{TOKEN_PREFIX}_{payload}.{signature}"


def parse_share_token(token: str) -> int:
    """Return the share id embedded in a signed share token."""
    if not token or not token.startswith(f"{TOKEN_PREFIX}_"):
        raise SessionShareNotFound()
    try:
        payload, signature = token[len(TOKEN_PREFIX) + 1 :].split(".", 1)
    except ValueError as exc:
        raise SessionShareNotFound() from exc

    expected = _token_signature(payload)
    if not hmac.compare_digest(signature, expected):
        raise SessionShareNotFound()

    try:
        version, raw_share_id, nonce = _b64url_decode(payload).decode("utf-8").split(".", 2)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SessionShareNotFound() from exc

    if version != TOKEN_VERSION or not nonce:
        raise SessionShareNotFound()
    try:
        share_id = int(raw_share_id)
    except ValueError as exc:
        raise SessionShareNotFound() from exc
    if share_id < 1:
        raise SessionShareNotFound()
    return share_id


def _user_display_name(user: User | None) -> str | None:
    if user is None:
        return None
    display_name = (getattr(user, "display_name", None) or "").strip()
    if display_name:
        return display_name
    email = (getattr(user, "email", None) or "").strip()
    if "@" in email:
        return email.split("@", 1)[0] or None
    return None


def _project_sharer(user: User | None) -> SessionSharerResponse | None:
    if user is None:
        return None
    return SessionSharerResponse(id=int(user.id), display_name=_user_display_name(user))


def _session_input_owner_ids(db: Session, *, session_id: UUID) -> set[int]:
    rows = db.query(SessionInput.owner_id).filter(SessionInput.session_id == session_id, SessionInput.owner_id.isnot(None)).distinct().all()
    owner_ids: set[int] = set()
    for (owner_id,) in rows:
        try:
            owner_ids.add(int(owner_id))
        except (TypeError, ValueError):
            continue
    return owner_ids


def _record_event(
    db: Session,
    *,
    share: SessionShare,
    event_type: Literal["created", "resolved", "revoked"],
    actor_user_id: int | None,
    metadata: dict[str, object] | None = None,
) -> None:
    db.add(
        SessionShareEvent(
            share_id=int(share.id),
            session_id=share.session_id,
            actor_user_id=actor_user_id,
            event_type=event_type,
            metadata_json=metadata or None,
        )
    )


def _validate_active(share: SessionShare, *, now: datetime | None = None) -> None:
    if share.revoked_at is not None:
        raise SessionShareRevoked()
    now = now or datetime.now(timezone.utc)
    expires_at = normalize_utc(share.expires_at)
    if expires_at is not None and expires_at <= now:
        raise SessionShareExpired()


def create_session_share(
    db: Session,
    *,
    session_id: UUID,
    created_by_user_id: int,
    expires_in_days: int | None = DEFAULT_SHARE_TTL_DAYS,
    note: str | None = None,
) -> tuple[SessionShare, str]:
    """Create a new explicit share link for a session and return its token."""
    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if session is None:
        raise SessionShareNotFound()
    input_owner_ids = _session_input_owner_ids(db, session_id=session.id)
    if input_owner_ids and int(created_by_user_id) not in input_owner_ids:
        raise SessionShareNotFound()

    cleaned_note = (note or "").strip() or None
    if cleaned_note is not None and len(cleaned_note) > 280:
        cleaned_note = cleaned_note[:280]

    expires_at = None
    if expires_in_days is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(days=expires_in_days)

    share = SessionShare(
        session_id=session.id,
        created_by_user_id=created_by_user_id,
        token_hash=_token_hash(f"pending:{secrets.token_urlsafe(32)}"),
        note=cleaned_note,
        expires_at=expires_at,
    )
    db.add(share)
    db.flush()

    nonce = secrets.token_urlsafe(24)
    token = _build_token(int(share.id), nonce)
    share.token_hash = _token_hash(token)
    _record_event(
        db,
        share=share,
        event_type="created",
        actor_user_id=created_by_user_id,
        metadata={"expires_in_days": expires_in_days},
    )
    db.commit()
    db.refresh(share)
    return share, token


def resolve_session_share(
    db: Session,
    *,
    token: str,
    actor_user_id: int | None = None,
    expected_session_id: UUID | None = None,
    record_access: bool = False,
) -> ResolvedSessionShare:
    """Validate a share token and return the session plus public sharer data."""
    share_id = parse_share_token(token)
    share = db.query(SessionShare).filter(SessionShare.id == share_id).first()
    if share is None or not hmac.compare_digest(str(share.token_hash), _token_hash(token)):
        raise SessionShareNotFound()
    _validate_active(share)
    if expected_session_id is not None and str(share.session_id) != str(expected_session_id):
        raise SessionShareNotFound()

    session = db.query(AgentSession).filter(AgentSession.id == share.session_id).first()
    if session is None:
        raise SessionShareNotFound()
    user = db.query(User).filter(User.id == share.created_by_user_id).first()
    sharer = _project_sharer(user)

    if record_access:
        share.access_count = int(share.access_count or 0) + 1
        share.last_accessed_at = datetime.now(timezone.utc)
        _record_event(
            db,
            share=share,
            event_type="resolved",
            actor_user_id=actor_user_id,
            metadata={"self": actor_user_id == share.created_by_user_id},
        )
        db.commit()
        db.refresh(share)

    return ResolvedSessionShare(share=share, session=session, sharer=sharer)


def revoke_session_share(
    db: Session,
    *,
    share_id: int,
    actor_user_id: int,
) -> SessionShare:
    share = db.query(SessionShare).filter(SessionShare.id == share_id).first()
    if share is None:
        raise SessionShareNotFound()
    if int(share.created_by_user_id) != int(actor_user_id):
        raise SessionShareNotFound()
    if share.revoked_at is None:
        share.revoked_at = datetime.now(timezone.utc)
        share.revoked_by_user_id = actor_user_id
        _record_event(db, share=share, event_type="revoked", actor_user_id=actor_user_id)
        db.commit()
        db.refresh(share)
    return share
