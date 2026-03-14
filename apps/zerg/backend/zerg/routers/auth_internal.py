"""Internal auth endpoints used by the control plane."""

from __future__ import annotations

import os

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from zerg.auth.strategy import _decode_jwt_fallback
from zerg.config import get_settings
from zerg.crud import create_user
from zerg.crud import get_user_by_email
from zerg.database import get_db
from zerg.dependencies.auth import require_internal_call
from zerg.routers.auth import JWT_SECRET
from zerg.routers.auth import GmailConnectResponse
from zerg.routers.auth import _store_gmail_connector
from zerg.services.sso_keys import get_sso_keys

router = APIRouter(
    prefix="/internal/auth",
    tags=["internal"],
    dependencies=[Depends(require_internal_call)],
)


class HostedGmailConnectHandoffPayload(BaseModel):
    """Payload the control plane sends after Gmail OAuth succeeds."""

    handoff_token: str
    refresh_token: str


def _decode_handoff_token(token: str) -> str:
    """Validate the control-plane handoff token and return the target user email."""

    secrets_to_try = [JWT_SECRET]
    try:
        secrets_to_try.extend(secret for secret in get_sso_keys() if secret != JWT_SECRET)
    except Exception:
        # Hosted handoff tokens are normally signed with the local instance JWT
        # secret; CP key fetch is only a best-effort fallback during rotations.
        pass

    payload = None
    for secret in secrets_to_try:
        try:
            payload = _decode_jwt_fallback(token, secret)
            break
        except Exception:
            continue

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

    return email


@router.post("/google/gmail/handoff", response_model=GmailConnectResponse)
def hosted_gmail_connect_handoff(
    payload: HostedGmailConnectHandoffPayload,
    db: Session = Depends(get_db),
) -> GmailConnectResponse:
    """Persist a hosted Gmail connector after control-plane OAuth succeeds."""

    email = _decode_handoff_token(payload.handoff_token)
    user = get_user_by_email(db, email)
    if user is None:
        settings = get_settings()
        if settings.single_tenant and not settings.testing:
            from zerg.services.single_tenant import is_owner_email

            if not is_owner_email(email):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="This instance is configured for a specific owner.",
                )

        user = create_user(
            db,
            email=email,
            provider="control-plane",
            provider_user_id=f"cp:{email}",
            skip_notification=True,
        )

    return _store_gmail_connector(
        db,
        owner_id=user.id,
        refresh_token=payload.refresh_token,
        callback_url=None,
    )
