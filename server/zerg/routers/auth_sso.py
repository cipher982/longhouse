"""Hosted SSO bridge routes for tenant auth."""

import asyncio
import hashlib
import hmac
import logging
import time
import urllib.parse
from collections import OrderedDict
from collections import deque
from datetime import datetime
from datetime import timezone
from threading import Lock

import httpx
from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from pydantic import Field

from zerg.auth.client_ip import get_client_ip
from zerg.auth.hosted import MAX_TENANT_LOGIN_STATE_LENGTH
from zerg.auth.hosted import hosted_instance_id
from zerg.auth.hosted import tenant_cookie_secure
from zerg.auth.hosted import tenant_handoff_attempt_cookie_name
from zerg.auth.hosted import tenant_login_cookie_name
from zerg.auth.hosted import tenant_login_cookie_secret
from zerg.auth.redirects import normalize_local_return_to
from zerg.auth.session_tokens import _set_refresh_cookie
from zerg.auth.session_tokens import _set_session_cookie
from zerg.config import get_settings
from zerg.dependencies.form_post_origin import reject_cross_origin_form_post

router = APIRouter(prefix="/auth", tags=["auth"])
_HOSTED_REFRESH_COOKIE_MAX_AGE = 30 * 24 * 60 * 60
logger = logging.getLogger(__name__)

_HANDOFF_RATE_WINDOW_SECONDS = 60
_HANDOFF_RATE_MAX_ATTEMPTS = 20
_NATIVE_HANDOFF_IP_MAX_ATTEMPTS = 60
_NATIVE_HANDOFF_TENANT_MAX_ATTEMPTS = 300
_HANDOFF_RATE_MAX_KEYS = 2048
_HANDOFF_RATE_BUCKETS: OrderedDict[str, deque[float]] = OrderedDict()
_HANDOFF_RATE_LOCK = Lock()

_MAX_NATIVE_REFRESH_TOKEN_LENGTH = 512


def _rate_limit_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _enforce_handoff_rate_limit(
    *,
    tenant: str,
    surface: str,
    attempt_id: str,
    client_ip: str | None = None,
) -> None:
    """Bound browser attempts and native credential abuse independently."""
    checks = [(f"{surface}:{tenant}:{_rate_limit_digest(attempt_id)}", _HANDOFF_RATE_MAX_ATTEMPTS)]
    if surface.startswith("native"):
        ip_key = (client_ip or "unknown").strip() or "unknown"
        checks.extend(
            [
                (f"native-ip:{tenant}:{_rate_limit_digest(ip_key)}", _NATIVE_HANDOFF_IP_MAX_ATTEMPTS),
                (f"native-tenant:{tenant}", _NATIVE_HANDOFF_TENANT_MAX_ATTEMPTS),
            ]
        )

    now = time.monotonic()
    window_start = now - _HANDOFF_RATE_WINDOW_SECONDS
    with _HANDOFF_RATE_LOCK:
        for stale_key, bucket in list(_HANDOFF_RATE_BUCKETS.items()):
            while bucket and bucket[0] < window_start:
                bucket.popleft()
            if not bucket:
                del _HANDOFF_RATE_BUCKETS[stale_key]

        for key, max_attempts in checks:
            bucket = _HANDOFF_RATE_BUCKETS.get(key)
            if bucket is not None and len(bucket) >= max_attempts:
                retry_after = max(1, int(_HANDOFF_RATE_WINDOW_SECONDS - (now - bucket[0])) + 1)
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many handoff attempts. Try again later.",
                    headers={"Retry-After": str(retry_after)},
                )

        for key, _max_attempts in checks:
            bucket = _HANDOFF_RATE_BUCKETS.setdefault(key, deque())
            bucket.append(now)
            _HANDOFF_RATE_BUCKETS.move_to_end(key)
        while len(_HANDOFF_RATE_BUCKETS) > _HANDOFF_RATE_MAX_KEYS:
            _HANDOFF_RATE_BUCKETS.popitem(last=False)


def _hosted_refresh_cookie_max_age(payload: dict) -> int:
    raw_expiry = payload.get("refresh_token_expires_at")
    if not isinstance(raw_expiry, str) or not raw_expiry:
        logger.error("control_plane_refresh_expiry_missing")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "cp_unavailable", "message": "Control plane returned no refresh expiry"},
        )
    try:
        expires_at = datetime.fromisoformat(raw_expiry)
    except ValueError as exc:
        logger.error("control_plane_refresh_expiry_invalid")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "cp_unavailable", "message": "Control plane returned an invalid refresh expiry"},
        ) from exc
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    remaining = int((expires_at - datetime.now(timezone.utc)).total_seconds())
    if remaining <= 0:
        logger.error("control_plane_refresh_expiry_elapsed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "cp_unavailable", "message": "Control plane returned an elapsed refresh expiry"},
        )
    if remaining > _HOSTED_REFRESH_COOKIE_MAX_AGE:
        logger.warning("control_plane_refresh_expiry_clamped")
    return min(_HOSTED_REFRESH_COOKIE_MAX_AGE, remaining)


class NativeHandoffRequest(BaseModel):
    code: str = Field(min_length=1, max_length=256)
    tenant_state: str = Field(min_length=1, max_length=MAX_TENANT_LOGIN_STATE_LENGTH)


class NativeRefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=_MAX_NATIVE_REFRESH_TOKEN_LENGTH)


class NativeRevokeRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=_MAX_NATIVE_REFRESH_TOKEN_LENGTH)
    revoke_authority: bool = False


def _runtime_payload(data: dict) -> dict:
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Control plane response was not a JSON object",
        )
    raw_expires_in = data.get("expires_in")
    if isinstance(raw_expires_in, bool) or raw_expires_in is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Control plane response missing expiry",
        )
    try:
        expires_in = int(raw_expires_in)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Control plane response has an invalid expiry",
        ) from exc
    if expires_in <= 0:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Control plane response has an invalid expiry",
        )
    runtime_token = data.get("runtime_token")
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


def _json_object(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Control plane response was not valid JSON",
        ) from exc
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Control plane response was not a JSON object",
        )
    return data


def _raise_control_plane_error(response: httpx.Response, *, default: str) -> None:
    try:
        body = response.json()
    except (TypeError, ValueError):
        body = None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict) and detail.get("code") == "tenant_internal_auth_failed":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "cp_unavailable", "message": "Control plane authentication is unavailable."},
        )
    raise HTTPException(
        status_code=response.status_code,
        detail=detail or default,
    )


def _exchange_handoff_code(
    *,
    control_plane_url: str,
    internal_api_secret: str,
    code: str,
    tenant: str,
    tenant_state: str | None = None,
    client: str | None = None,
) -> dict:
    if not code or len(code) > 256 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in code):
        raise HTTPException(status_code=400, detail="Invalid handoff code")
    payload = {"code": code, "tenant": tenant}
    if tenant_state:
        payload["tenant_state"] = tenant_state
    if client:
        payload["client"] = client
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
        _raise_control_plane_error(exchange, default="Control plane rejected handoff")

    raw_payload = _json_object(exchange)
    try:
        return _runtime_payload(raw_payload)
    except HTTPException:
        refresh_token = raw_payload.get("refresh_token")
        if isinstance(refresh_token, str) and refresh_token:
            try:
                _revoke_native_session_payload(
                    settings=get_settings(),
                    refresh_token=refresh_token,
                    strict=False,
                )
            except Exception:
                logger.warning("tenant_handoff_orphan_revoke_failed", exc_info=True)
        raise


def _refresh_native_session_payload(*, settings, refresh_token: str) -> dict:
    control_plane_url = getattr(settings, "control_plane_url", None)
    if not control_plane_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Hosted native session refresh is not configured",
        )
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
        _raise_control_plane_error(exchange, default="Control plane rejected native refresh")
    return _runtime_payload(_json_object(exchange))


def _revoke_native_session_payload(
    *,
    settings,
    refresh_token: str,
    revoke_authority: bool = False,
    strict: bool = True,
) -> None:
    control_plane_url = getattr(settings, "control_plane_url", None)
    if not control_plane_url or not refresh_token:
        if strict and control_plane_url is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "cp_unavailable"},
            )
        return
    payload = {"refresh_token": refresh_token}
    if revoke_authority:
        payload["revoke_authority"] = True
    try:
        response = httpx.post(
            f"{control_plane_url.rstrip('/')}/api/identity/revoke-native-session",
            headers={"X-Internal-Token": settings.internal_api_secret},
            json=payload,
            timeout=5.0,
        )
    except httpx.HTTPError as exc:
        logger.warning("control_plane_native_session_revoke_failed", exc_info=True)
        if strict:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "cp_unavailable"},
            ) from exc
        return
    if response.status_code >= 400:
        logger.warning(
            "control_plane_native_session_revoke_rejected",
            extra={"status_code": response.status_code},
        )
        if strict:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "cp_unavailable"},
            )


async def _best_effort_revoke_native_session(settings, refresh_token: object) -> None:
    """Revoke an exchanged native session before returning a validation error."""
    if not isinstance(refresh_token, str) or not refresh_token:
        return
    try:
        await asyncio.to_thread(
            _revoke_native_session_payload,
            settings=settings,
            refresh_token=refresh_token,
            strict=False,
        )
    except Exception:
        logger.warning("tenant_handoff_orphan_revoke_failed", exc_info=True)


def _refresh_runtime_token_payload(*, control_plane_url: str, token: str) -> dict:
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
    return _runtime_payload(_json_object(exchange))


def _handoff_failure_redirect(
    *,
    settings,
    return_to: str | None,
    error: str,
    clear_login_state: bool = False,
    tenant_state: str | None = None,
) -> RedirectResponse:
    safe_return_to = normalize_local_return_to(return_to) or "/timeline"
    redirect = RedirectResponse(
        f"/login?return_to={urllib.parse.quote(safe_return_to, safe='')}&auth_error={urllib.parse.quote(error, safe='')}",
        status_code=303,
    )
    redirect.headers["cache-control"] = "no-store"
    redirect.headers["content-security-policy"] = "default-src 'none'; frame-ancestors 'none'"
    redirect.headers["referrer-policy"] = "no-referrer"
    cookie_secure = tenant_cookie_secure(settings)
    if clear_login_state:
        cookie_name = tenant_login_cookie_name(tenant_state, secure=cookie_secure)
        if cookie_name is not None:
            redirect.delete_cookie(
                cookie_name,
                path="/",
                httponly=True,
                secure=cookie_secure,
                samesite="lax",
            )
    return redirect


@router.get("/accept-handoff")
async def accept_handoff_request(
    request: Request,
    code: str,
    return_to: str | None = None,
    tenant_state: str | None = None,
):
    settings = get_settings()
    control_plane_url = getattr(settings, "control_plane_url", None)
    if not control_plane_url:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hosted handoff is not configured")

    # Browser prefetchers must never spend a one-use login code. A real
    # top-level navigation is ``document``; other fetch destinations cannot
    # complete the browser handoff safely.
    purpose = f"{request.headers.get('purpose', '')},{request.headers.get('sec-purpose', '')}".lower()
    fetch_dest = request.headers.get("sec-fetch-dest", "").strip().lower()
    if "prefetch" in purpose or (fetch_dest and fetch_dest != "document"):
        return Response(
            status_code=status.HTTP_204_NO_CONTENT,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    cookie_secure = tenant_cookie_secure(settings)
    if not tenant_state or len(tenant_state) > MAX_TENANT_LOGIN_STATE_LENGTH:
        logger.warning("tenant_login_state_query_missing")
        return _handoff_failure_redirect(settings=settings, return_to=return_to, error="login_state_missing")
    login_cookie_name = tenant_login_cookie_name(tenant_state, secure=cookie_secure)
    expected_secret = tenant_login_cookie_secret(tenant_state)
    if login_cookie_name is None or expected_secret is None:
        logger.warning("tenant_login_state_invalid")
        return _handoff_failure_redirect(
            settings=settings,
            return_to=return_to,
            error="login_state_missing",
        )
    actual_secret = request.cookies.get(login_cookie_name)
    if not actual_secret:
        logger.warning("tenant_login_cookie_missing")
        return _handoff_failure_redirect(
            settings=settings,
            return_to=return_to,
            error="login_state_missing",
        )
    if not hmac.compare_digest(expected_secret.encode("utf-8"), actual_secret.encode("utf-8")):
        logger.warning("tenant_login_state_mismatch")
        return _handoff_failure_redirect(
            settings=settings,
            return_to=return_to,
            error="login_state_mismatch",
        )

    tenant = hosted_instance_id()
    _enforce_handoff_rate_limit(tenant=tenant, surface="web", attempt_id=tenant_state)
    try:
        payload = await asyncio.to_thread(
            _exchange_handoff_code,
            control_plane_url=control_plane_url,
            internal_api_secret=settings.internal_api_secret,
            code=code,
            tenant=tenant,
            tenant_state=tenant_state,
            client="web",
        )
    except HTTPException as exc:
        if exc.status_code in {404, 410}:
            error = "handoff_expired"
        elif exc.status_code == 403:
            error = "login_state_mismatch"
        elif exc.status_code >= 500:
            error = "cp_unavailable"
        else:
            error = "handoff_failed"
        logger.warning("tenant_handoff_exchange_failed error=%s status=%s", error, exc.status_code)
        return _handoff_failure_redirect(
            settings=settings,
            return_to=return_to,
            error=error,
            clear_login_state=True,
            tenant_state=tenant_state,
        )

    runtime_token = payload["runtime_token"]
    expires_in = payload["expires_in"]
    refresh_token = payload.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        logger.error("tenant_handoff_exchange_missing_refresh")
        return _handoff_failure_redirect(
            settings=settings,
            return_to=return_to,
            error="cp_unavailable",
            clear_login_state=True,
            tenant_state=tenant_state,
        )

    try:
        refresh_cookie_max_age = _hosted_refresh_cookie_max_age(payload)
    except HTTPException:
        logger.error("tenant_handoff_exchange_invalid_refresh_expiry")
        try:
            await asyncio.to_thread(
                _revoke_native_session_payload,
                settings=settings,
                refresh_token=refresh_token,
                strict=False,
            )
        except Exception:
            logger.warning("tenant_handoff_orphan_revoke_failed", exc_info=True)
        return _handoff_failure_redirect(
            settings=settings,
            return_to=return_to,
            error="cp_unavailable",
            clear_login_state=True,
            tenant_state=tenant_state,
        )

    from zerg.dependencies.auth import _get_strategy

    validation_error = "handoff_failed"
    try:
        user = await asyncio.to_thread(_get_strategy().validate_ws_token, runtime_token)
    except HTTPException as exc:
        user = None
        if exc.status_code >= 500:
            validation_error = "catalog_unavailable"
        logger.warning("tenant_handoff_runtime_validation_failed error=%s", validation_error)
    if user is None:
        logger.warning("tenant_handoff_runtime_validation_failed")
        try:
            await asyncio.to_thread(
                _revoke_native_session_payload,
                settings=settings,
                refresh_token=refresh_token,
                strict=False,
            )
        except Exception:
            logger.warning("tenant_handoff_orphan_revoke_failed", exc_info=True)
        return _handoff_failure_redirect(
            settings=settings,
            return_to=return_to,
            error=validation_error,
            clear_login_state=True,
            tenant_state=tenant_state,
        )
    redirect = RedirectResponse(normalize_local_return_to(return_to) or "/timeline", status_code=303)
    _set_session_cookie(redirect, runtime_token, expires_in)
    _set_refresh_cookie(redirect, refresh_token, refresh_cookie_max_age)
    redirect.delete_cookie(
        login_cookie_name,
        path="/",
        httponly=True,
        secure=cookie_secure,
        samesite="lax",
    )
    redirect.headers["cache-control"] = "no-store"
    redirect.headers["referrer-policy"] = "no-referrer"
    redirect.delete_cookie(
        tenant_handoff_attempt_cookie_name(secure=cookie_secure),
        path="/",
        httponly=True,
        secure=cookie_secure,
        samesite="lax",
    )
    return redirect


@router.post("/accept-native-handoff")
async def accept_native_handoff(request: Request, body: NativeHandoffRequest):
    reject_cross_origin_form_post(request)
    settings = get_settings()
    control_plane_url = getattr(settings, "control_plane_url", None)
    if not control_plane_url:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hosted handoff is not configured")

    tenant = hosted_instance_id()
    _enforce_handoff_rate_limit(
        tenant=tenant,
        surface="native",
        attempt_id=body.tenant_state,
        client_ip=get_client_ip(request),
    )
    payload = await asyncio.to_thread(
        _exchange_handoff_code,
        control_plane_url=control_plane_url,
        internal_api_secret=settings.internal_api_secret,
        code=body.code,
        tenant=tenant,
        tenant_state=body.tenant_state,
        client="ios",
    )
    try:
        normalized_payload = _runtime_payload(payload)
    except HTTPException:
        refresh_token = payload.get("refresh_token") if isinstance(payload, dict) else None
        await _best_effort_revoke_native_session(settings, refresh_token)
        raise

    runtime_token = normalized_payload["runtime_token"]
    refresh_token = normalized_payload.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        await _best_effort_revoke_native_session(settings, refresh_token)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Control plane response missing native refresh token",
        )

    from zerg.dependencies.auth import _get_strategy

    try:
        user = await asyncio.to_thread(_get_strategy().validate_ws_token, runtime_token)
    except Exception:
        await _best_effort_revoke_native_session(settings, refresh_token)
        raise
    if user is None:
        await _best_effort_revoke_native_session(settings, refresh_token)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid runtime token")

    return normalized_payload


@router.post("/refresh-native-session")
async def refresh_native_session(request: Request, body: NativeRefreshRequest):
    reject_cross_origin_form_post(request)
    refresh_token = body.refresh_token.strip()
    if not refresh_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing refresh token")
    settings = get_settings()
    if not getattr(settings, "control_plane_url", None):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hosted native session refresh is not configured")
    tenant = hosted_instance_id()
    _enforce_handoff_rate_limit(
        tenant=tenant,
        surface="native-refresh",
        attempt_id="credential",
        client_ip=get_client_ip(request),
    )
    return await asyncio.to_thread(
        _refresh_native_session_payload,
        settings=settings,
        refresh_token=refresh_token,
    )


@router.post("/revoke-native-session")
async def revoke_native_session(request: Request, body: NativeRevokeRequest):
    reject_cross_origin_form_post(request)
    refresh_token = body.refresh_token.strip()
    if not refresh_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing refresh token")
    settings = get_settings()
    if not getattr(settings, "control_plane_url", None):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hosted native session revoke is not configured")
    tenant = hosted_instance_id()
    _enforce_handoff_rate_limit(
        tenant=tenant,
        surface="native-revoke",
        attempt_id="credential",
        client_ip=get_client_ip(request),
    )
    await asyncio.to_thread(
        _revoke_native_session_payload,
        settings=settings,
        refresh_token=refresh_token,
        revoke_authority=body.revoke_authority,
        strict=False,
    )
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

    return await asyncio.to_thread(
        _refresh_runtime_token_payload,
        control_plane_url=control_plane_url,
        token=token,
    )


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
