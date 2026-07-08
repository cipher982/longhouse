"""Hosted SSO bridge routes for tenant auth."""

from __future__ import annotations

import hmac

import httpx
from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from zerg.auth.hosted import TENANT_LOGIN_STATE_COOKIE
from zerg.auth.hosted import hosted_instance_id
from zerg.auth.redirects import normalize_local_return_to
from zerg.auth.session_tokens import _clear_refresh_cookie
from zerg.auth.session_tokens import _set_session_cookie
from zerg.config import get_settings
from zerg.database import get_db

router = APIRouter(prefix="/auth", tags=["auth"])


class NativeHandoffRequest(BaseModel):
    code: str
    tenant_state: str


class NativeRefreshRequest(BaseModel):
    refresh_token: str


class NativeRevokeRequest(BaseModel):
    refresh_token: str


def _runtime_payload(data: dict) -> dict:
    runtime_token = data.get("runtime_token")
    expires_in = int(data.get("expires_in") or 3600)
    if not isinstance(runtime_token, str) or not runtime_token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Control plane response missing token",
        )
    payload = {"runtime_token": runtime_token, "expires_in": expires_in}
    for key in ("refresh_token", "refresh_token_expires_at", "device_session_id"):
        value = data.get(key)
        if isinstance(value, str) and value:
            payload[key] = value
    return payload


def _exchange_handoff_code(
    *,
    control_plane_url: str,
    internal_api_secret: str,
    code: str,
    tenant: str,
    tenant_state: str | None = None,
) -> dict:
    payload = {"code": code, "tenant": tenant}
    if tenant_state:
        payload["tenant_state"] = tenant_state
    try:
        exchange = httpx.post(
            f"{control_plane_url.rstrip('/')}/api/identity/exchange-handoff",
            headers={"X-Internal-Token": internal_api_secret},
            json=payload,
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Control plane handoff exchange failed",
        ) from exc

    if exchange.status_code >= 400:
        raise HTTPException(status_code=exchange.status_code, detail="Control plane rejected handoff")

    return _runtime_payload(exchange.json())


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

    if tenant_state:
        expected_state = request.cookies.get(TENANT_LOGIN_STATE_COOKIE)
        if not expected_state:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing login state")
        if not hmac.compare_digest(expected_state, tenant_state):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Login state mismatch")

    tenant = hosted_instance_id()
    payload = _exchange_handoff_code(
        control_plane_url=control_plane_url,
        internal_api_secret=settings.internal_api_secret,
        code=code,
        tenant=tenant,
        tenant_state=tenant_state,
    )
    runtime_token = payload["runtime_token"]
    expires_in = payload["expires_in"]

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


@router.post("/accept-native-handoff")
async def accept_native_handoff(body: NativeHandoffRequest, db: Session = Depends(get_db)):
    settings = get_settings()
    control_plane_url = getattr(settings, "control_plane_url", None)
    if not control_plane_url:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hosted handoff is not configured")

    tenant = hosted_instance_id()
    payload = _exchange_handoff_code(
        control_plane_url=control_plane_url,
        internal_api_secret=settings.internal_api_secret,
        code=body.code,
        tenant=tenant,
        tenant_state=body.tenant_state,
    )
    runtime_token = payload["runtime_token"]

    from zerg.dependencies.auth import _get_strategy

    user = _get_strategy().validate_ws_token(runtime_token, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid runtime token")

    return payload


@router.post("/refresh-native-session")
async def refresh_native_session(body: NativeRefreshRequest):
    settings = get_settings()
    control_plane_url = getattr(settings, "control_plane_url", None)
    if not control_plane_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Hosted native session refresh is not configured",
        )

    refresh_token = body.refresh_token.strip()
    if not refresh_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing refresh token")

    try:
        exchange = httpx.post(
            f"{control_plane_url.rstrip('/')}/api/identity/refresh-native-session",
            headers={"X-Internal-Token": settings.internal_api_secret},
            json={"refresh_token": refresh_token, "tenant": hosted_instance_id()},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Control plane native session refresh failed",
        ) from exc

    if exchange.status_code >= 400:
        raise HTTPException(status_code=exchange.status_code, detail="Control plane rejected native refresh")

    return _runtime_payload(exchange.json())


@router.post("/revoke-native-session")
async def revoke_native_session(body: NativeRevokeRequest):
    settings = get_settings()
    control_plane_url = getattr(settings, "control_plane_url", None)
    if not control_plane_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Hosted native session revoke is not configured",
        )

    refresh_token = body.refresh_token.strip()
    if not refresh_token:
        return {"status": "ok"}

    try:
        exchange = httpx.post(
            f"{control_plane_url.rstrip('/')}/api/identity/revoke-native-session",
            headers={"X-Internal-Token": settings.internal_api_secret},
            json={"refresh_token": refresh_token},
            timeout=5.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Control plane native session revoke failed",
        ) from exc

    if exchange.status_code >= 500:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Control plane revoke failed")
    return {"status": "ok"}


@router.post("/refresh-runtime-token")
async def refresh_runtime_token(request: Request):
    """Proxy a CP runtime token refresh for iOS/hosted native clients.

    iOS stores the CP-issued bearer in keychain and sends it on every request.
    Active runtime tokens have a short lifetime, so the client proactively
    refreshes before expiry and retries with refresh on a 401. This route
    forwards the current bearer to the CP's
    /api/identity/refresh-runtime-token and returns the re-minted token. No
    local validation — the CP is the issuer and is the authority on token
    validity, including the long native-app refresh window.
    """
    settings = get_settings()
    control_plane_url = getattr(settings, "control_plane_url", None)
    if not control_plane_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Hosted runtime token refresh is not configured",
        )

    auth_header = request.headers.get("Authorization") or request.headers.get("authorization")
    if not auth_header or not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    token = auth_header.split(" ", 1)[1].strip()
    if not token or token.startswith("zdt_"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Runtime token required")

    try:
        exchange = httpx.post(
            f"{control_plane_url.rstrip('/')}/api/identity/refresh-runtime-token",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Control plane runtime token refresh failed",
        ) from exc

    if exchange.status_code >= 400:
        raise HTTPException(status_code=exchange.status_code, detail="Control plane rejected refresh")

    return _runtime_payload(exchange.json())


__all__ = [
    "NativeHandoffRequest",
    "NativeRefreshRequest",
    "NativeRevokeRequest",
    "accept_handoff_request",
    "accept_native_handoff",
    "refresh_native_session",
    "refresh_runtime_token",
    "revoke_native_session",
    "router",
]
