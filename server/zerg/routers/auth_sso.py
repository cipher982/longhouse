"""Hosted SSO bridge routes for tenant auth."""

from __future__ import annotations

import hmac
import os
import time
from typing import Any

import httpx
from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from zerg.auth import refresh_tokens
from zerg.auth.jwt_utils import decode_jwt_with_secret_candidates
from zerg.auth.redirects import normalize_local_return_to
from zerg.auth.session_tokens import ACCESS_TOKEN_LIFETIME
from zerg.auth.session_tokens import JWT_SECRET
from zerg.auth.session_tokens import _clear_refresh_cookie
from zerg.auth.session_tokens import _issue_access_token
from zerg.auth.session_tokens import _set_refresh_cookie
from zerg.auth.session_tokens import _set_session_cookie
from zerg.config import get_settings
from zerg.crud import create_user
from zerg.crud import get_user
from zerg.crud import get_user_by_email
from zerg.database import get_db
from zerg.schemas.schemas import TokenOut
from zerg.services import sso_keys as sso_keys_service
from zerg.services.write_serializer import get_write_serializer

router = APIRouter(prefix="/auth", tags=["auth"])
TENANT_LOGIN_STATE_COOKIE = "tenant_login_state"


def _hosted_instance_id() -> str:
    instance_id = os.getenv("INSTANCE_ID", "").strip()
    if instance_id:
        return instance_id
    settings = get_settings()
    public_url = settings.app_public_url or settings.public_site_url or ""
    if public_url:
        from urllib.parse import urlparse

        host = urlparse(public_url).hostname or ""
        if host:
            return host.split(".")[0]
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="INSTANCE_ID is not configured")


def _hosted_auth_enabled() -> bool:
    return bool(getattr(get_settings(), "control_plane_url", None))


async def _accept_token(response: Response, token: str, db: Session) -> TokenOut:
    if _hosted_auth_enabled():
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Hosted accept-token is no longer supported")

    if not token:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="token must be provided",
        )

    secrets_to_try = [JWT_SECRET]
    secrets_to_try.extend(sso_keys_service.get_sso_keys())
    payload: dict[str, Any] | None = decode_jwt_with_secret_candidates(token, secrets_to_try)

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

    at_seconds = int(ACCESS_TOKEN_LIFETIME.total_seconds())
    access_token = _issue_access_token(
        user.id,
        user.email,
        display_name=getattr(user, "display_name", None),
        avatar_url=getattr(user, "avatar_url", None),
    )
    _set_session_cookie(response, access_token, at_seconds)

    # Issue refresh token so the browser session survives beyond the AT lifetime.
    ws = get_write_serializer()
    raw_rt = await ws.execute_or_direct(
        lambda wdb, _user_id=user.id: refresh_tokens.create(wdb, user_id=_user_id),
        db,
        label="refresh-session",
    )
    _set_refresh_cookie(response, raw_rt, 90 * 24 * 60 * 60)

    return TokenOut(access_token=access_token, expires_in=at_seconds)


@router.post("/accept-token", response_model=TokenOut)
async def accept_token(response: Response, body: dict[str, str], db: Session = Depends(get_db)) -> TokenOut:
    """Accept a JWT token from cross-subdomain auth redirect."""
    token = body.get("token")
    if not token or not isinstance(token, str):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="token must be provided",
        )
    return await _accept_token(response, token, db)


@router.get("/accept-token")
async def accept_token_redirect(
    token: str,
    response: Response,
    return_to: str | None = None,
    db: Session = Depends(get_db),
):
    """Accept a hosted login token, set the cookie, and continue to the app."""
    await _accept_token(response, token, db)
    redirect = RedirectResponse(normalize_local_return_to(return_to) or "/timeline", status_code=302)
    for header_name, header_value in response.headers.items():
        if header_name.lower() == "set-cookie":
            redirect.headers.append("set-cookie", header_value)
    return redirect


@router.get("/accept-handoff")
async def accept_handoff_request(
    request: Request,
    code: str,
    response: Response,
    return_to: str | None = None,
    tenant_state: str | None = None,
    db: Session = Depends(get_db),
):
    settings = get_settings()
    control_plane_url = getattr(settings, "control_plane_url", None)
    if not control_plane_url:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hosted handoff is not configured")

    expected_state = request.cookies.get(TENANT_LOGIN_STATE_COOKIE)
    if not expected_state:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing login state")
    if not tenant_state or not hmac.compare_digest(expected_state, tenant_state):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Login state mismatch")

    tenant = _hosted_instance_id()
    try:
        exchange = httpx.post(
            f"{control_plane_url.rstrip('/')}/api/identity/exchange-handoff",
            headers={"X-Internal-Token": settings.internal_api_secret},
            json={"code": code, "tenant": tenant},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Control plane handoff exchange failed",
        ) from exc

    if exchange.status_code >= 400:
        raise HTTPException(status_code=exchange.status_code, detail="Control plane rejected handoff")

    data = exchange.json()
    runtime_token = data.get("runtime_token")
    expires_in = int(data.get("expires_in") or 3600)
    if not isinstance(runtime_token, str) or not runtime_token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Control plane handoff response missing token",
        )

    from zerg.dependencies.auth import _get_strategy

    user = _get_strategy().validate_ws_token(runtime_token, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid runtime token")

    _set_session_cookie(response, runtime_token, expires_in)
    _clear_refresh_cookie(response)

    redirect = RedirectResponse(normalize_local_return_to(return_to) or "/timeline", status_code=302)
    for header_name, header_value in response.headers.items():
        if header_name.lower() == "set-cookie":
            redirect.headers.append("set-cookie", header_value)
    redirect.delete_cookie(
        TENANT_LOGIN_STATE_COOKIE,
        path="/",
        httponly=True,
        secure=not settings.auth_disabled and not settings.testing,
        samesite="lax",
    )
    return redirect


__all__ = ["accept_handoff_request", "accept_token", "accept_token_redirect", "router"]
