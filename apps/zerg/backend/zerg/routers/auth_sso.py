"""Hosted SSO bridge routes for tenant auth."""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Response
from fastapi import status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from zerg.auth.session_tokens import JWT_SECRET
from zerg.auth.session_tokens import _issue_access_token
from zerg.auth.session_tokens import _set_session_cookie
from zerg.auth.strategy import _decode_jwt_fallback
from zerg.config import get_settings
from zerg.crud import create_user
from zerg.crud import get_user
from zerg.crud import get_user_by_email
from zerg.database import get_db
from zerg.schemas.schemas import TokenOut
from zerg.services import sso_keys as sso_keys_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/accept-token", response_model=TokenOut)
def accept_token(response: Response, body: dict[str, str], db: Session = Depends(get_db)) -> TokenOut:
    """Accept a JWT token from cross-subdomain auth redirect."""
    token = body.get("token")
    if not token or not isinstance(token, str):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="token must be provided",
        )

    secrets_to_try = [JWT_SECRET]
    secrets_to_try.extend(k for k in sso_keys_service.get_sso_keys() if k != JWT_SECRET)

    payload: dict[str, Any] | None = None
    for secret in secrets_to_try:
        try:
            payload = _decode_jwt_fallback(token, secret)
            break
        except Exception:
            continue

    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    token_instance = payload.get("instance")
    instance_id = os.getenv("INSTANCE_ID")
    if token_instance and instance_id and token_instance != instance_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token not intended for this instance",
        )

    sub_raw = payload.get("sub")
    has_email_claim = "email" in payload
    user = None

    if not has_email_claim:
        try:
            user_id = int(sub_raw)
            user = get_user(db, user_id)
        except (TypeError, ValueError):
            pass

    if user is None:
        email = payload.get("email") or (str(sub_raw) if sub_raw else None)
        if not email or "@" not in str(email):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token payload",
            )
        email = str(email).strip().lower()

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

    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    exp = payload.get("exp", 0)
    remaining = max(0, int(exp - time.time()))
    if remaining == 0:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has expired")

    expires_in = min(remaining, 30 * 60)
    access_token = _issue_access_token(
        user.id,
        user.email,
        display_name=getattr(user, "display_name", None),
        avatar_url=getattr(user, "avatar_url", None),
    )
    _set_session_cookie(response, access_token, expires_in)
    return TokenOut(access_token=access_token, expires_in=expires_in)


@router.get("/sso")
def sso_redirect(token: str, response: Response, db: Session = Depends(get_db)):
    """Cross-subdomain SSO: accept token via URL param, set cookie, redirect."""
    accept_token(response, {"token": token}, db)

    redirect = RedirectResponse("/timeline", status_code=302)
    for header_name, header_value in response.headers.items():
        if header_name.lower() == "set-cookie":
            redirect.headers.append("set-cookie", header_value)
    return redirect


__all__ = ["accept_token", "router", "sso_redirect"]
