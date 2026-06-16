"""Internal auth endpoints used by the control plane."""

from __future__ import annotations

import os
from dataclasses import dataclass

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from zerg.auth.cp_jwks import CPTokenError
from zerg.auth.cp_jwks import verify_runtime_token
from zerg.auth.hosted import hosted_instance_id
from zerg.auth.jwt_utils import decode_jwt_with_secret_candidates
from zerg.auth.session_tokens import JWT_SECRET
from zerg.config import get_settings
from zerg.crud import create_user
from zerg.crud import get_user_by_email
from zerg.database import get_db
from zerg.dependencies.auth import require_internal_call
from zerg.routers.auth_gmail import GmailConnectResponse
from zerg.routers.auth_gmail import _store_gmail_connector

router = APIRouter(
    prefix="/internal/auth",
    tags=["internal"],
    dependencies=[Depends(require_internal_call)],
)


class HostedGmailConnectHandoffPayload(BaseModel):
    """Payload the control plane sends after Gmail OAuth succeeds."""

    refresh_token: str
    runtime_token: str | None = None
    handoff_token: str | None = None


@dataclass(frozen=True)
class _HostedGmailIdentity:
    email: str
    cp_user_id: int | None = None
    email_verified: bool = True
    display_name: str | None = None
    avatar_url: str | None = None


def _decode_legacy_handoff_token(token: str) -> _HostedGmailIdentity:
    """Validate the pre-JWKS Gmail handoff token during deploy skew."""

    payload = decode_jwt_with_secret_candidates(token, [JWT_SECRET])

    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired handoff token",
        )

    if payload.get("purpose") != "hosted_gmail_connect_handoff":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid handoff token purpose",
        )

    instance_id = os.getenv("INSTANCE_ID", "").strip()
    token_instance = str(payload.get("instance") or "").strip()
    if instance_id and token_instance and token_instance != instance_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Handoff token not intended for this instance",
        )

    email = str(payload.get("email") or payload.get("sub") or "").strip().lower()
    if "@" not in email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid handoff token payload",
        )

    return _HostedGmailIdentity(email=email)


def _decode_runtime_token(token: str) -> _HostedGmailIdentity:
    try:
        claims = verify_runtime_token(token, audience=hosted_instance_id())
    except CPTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    return _HostedGmailIdentity(
        email=claims.email,
        cp_user_id=claims.cp_user_id,
        email_verified=claims.email_verified,
        display_name=claims.display_name,
        avatar_url=claims.avatar_url,
    )


def _resolve_handoff_identity(payload: HostedGmailConnectHandoffPayload) -> _HostedGmailIdentity:
    if payload.runtime_token:
        return _decode_runtime_token(payload.runtime_token)
    if payload.handoff_token:
        return _decode_legacy_handoff_token(payload.handoff_token)
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="runtime_token must be provided")


@router.post("/google/gmail/handoff", response_model=GmailConnectResponse)
def hosted_gmail_connect_handoff(
    payload: HostedGmailConnectHandoffPayload,
    db: Session = Depends(get_db),
) -> GmailConnectResponse:
    """Persist a hosted Gmail connector after control-plane OAuth succeeds."""

    identity = _resolve_handoff_identity(payload)
    user = get_user_by_email(db, identity.email)
    if user is None:
        settings = get_settings()
        if settings.single_tenant and not settings.testing:
            from zerg.services.single_tenant import is_owner_email

            if not is_owner_email(identity.email):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="This instance is configured for a specific owner.",
                )

        user = create_user(
            db,
            email=identity.email,
            provider="control-plane",
            provider_user_id=str(identity.cp_user_id) if identity.cp_user_id is not None else f"cp:{identity.email}",
            skip_notification=True,
        )
        if identity.cp_user_id is not None:
            user.cp_user_id = identity.cp_user_id
            user.email_verified = identity.email_verified
    elif identity.cp_user_id is not None:
        if getattr(user, "cp_user_id", None) not in (None, identity.cp_user_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Control-plane account does not match this tenant user.",
            )
        user.cp_user_id = identity.cp_user_id
        user.provider = "control-plane"
        user.provider_user_id = str(identity.cp_user_id)
        if identity.email_verified:
            user.email_verified = True

    if identity.display_name:
        user.display_name = identity.display_name
    if identity.avatar_url:
        user.avatar_url = identity.avatar_url

    return _store_gmail_connector(
        db,
        owner_id=user.id,
        refresh_token=payload.refresh_token,
        callback_url=None,
    )
