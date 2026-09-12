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
from zerg.auth.cp_jwks import CPAuthorityUnavailable
from zerg.auth.cp_jwks import CPTokenError
from zerg.auth.cp_jwks import verify_runtime_token
from zerg.auth.hosted import MAX_TENANT_LOGIN_STATE_LENGTH
from zerg.auth.hosted import hosted_cookie_origin_is_secure
from zerg.auth.hosted import hosted_instance_id
from zerg.auth.hosted import tenant_cookie_secure
from zerg.auth.hosted import tenant_handoff_attempt_cookie_name
from zerg.auth.hosted import tenant_login_attempt_cookie_name
from zerg.auth.hosted import tenant_login_cookie_name
from zerg.auth.hosted import tenant_login_cookie_secret
from zerg.auth.hosted import tenant_login_ready_cookie_name
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
_NATIVE_REFRESH_IP_MAX_ATTEMPTS = 120
_NATIVE_REVOKE_IP_MAX_ATTEMPTS = 300
_HANDOFF_RATE_MAX_KEYS = 2048
_HANDOFF_RATE_BUCKETS: OrderedDict[str, deque[float]] = OrderedDict()
_HANDOFF_RATE_LOCK = Lock()


def _set_no_store(response: Response) -> None:
    response.headers["cache-control"] = "no-store"
    response.headers["pragma"] = "no-cache"


_MAX_NATIVE_REFRESH_TOKEN_LENGTH = 512


def _rate_limit_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _enforce_handoff_rate_limit(
    *,
    tenant: str,
    surface: str,
    attempt_id: str | None,
    client_ip: str | None = None,
) -> None:
    """Bound browser attempts and native credential abuse independently."""
    checks: list[tuple[str, int]] = []
    if attempt_id:
        checks.append((f"{surface}:{tenant}:{_rate_limit_digest(attempt_id)}", _HANDOFF_RATE_MAX_ATTEMPTS))
    if surface == "native":
        ip_key = (client_ip or "unknown").strip() or "unknown"
        checks.extend(
            [
                (f"native-ip:{tenant}:{_rate_limit_digest(ip_key)}", _NATIVE_HANDOFF_IP_MAX_ATTEMPTS),
                (f"native-tenant:{tenant}", _NATIVE_HANDOFF_TENANT_MAX_ATTEMPTS),
            ]
        )
    elif surface == "native-refresh":
        ip_key = (client_ip or "unknown").strip() or "unknown"
        checks.append((f"native-refresh-ip:{tenant}:{_rate_limit_digest(ip_key)}", _NATIVE_REFRESH_IP_MAX_ATTEMPTS))
    elif surface == "native-revoke":
        ip_key = (client_ip or "unknown").strip() or "unknown"
        checks.append((f"native-revoke-ip:{tenant}:{_rate_limit_digest(ip_key)}", _NATIVE_REVOKE_IP_MAX_ATTEMPTS))

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
    code_verifier: str = Field(min_length=43, max_length=128)


class NativeRefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=_MAX_NATIVE_REFRESH_TOKEN_LENGTH)


class NativeRevokeRequest(BaseModel):
    # A refresh token is the caller's proof of ownership. Session IDs are
    # identifiers carried in access tokens, not revocation credentials.
    refresh_token: str = Field(min_length=1, max_length=_MAX_NATIVE_REFRESH_TOKEN_LENGTH)
    revoke_authority: bool = False
    orphan_cleanup: bool = False


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
    if not isinstance(runtime_token, str) or not runtime_token or len(runtime_token) > 16_384:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Control plane response missing token",
        )
    refresh_token = data.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token or len(refresh_token) > _MAX_NATIVE_REFRESH_TOKEN_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Control plane response missing refresh token",
        )
    refresh_expires_at = data.get("refresh_token_expires_at")
    if not isinstance(refresh_expires_at, str) or not refresh_expires_at or len(refresh_expires_at) > 128:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Control plane response missing refresh expiry",
        )
    try:
        parsed_refresh_expiry = datetime.fromisoformat(refresh_expires_at)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Control plane response has an invalid refresh expiry",
        ) from exc
    if parsed_refresh_expiry.tzinfo is None:
        parsed_refresh_expiry = parsed_refresh_expiry.replace(tzinfo=timezone.utc)
    if parsed_refresh_expiry <= datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Control plane response has an elapsed refresh expiry",
        )
    token_type = data.get("token_type", "bearer")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Control plane response has an invalid token type",
        )
    payload = {
        "runtime_token": runtime_token,
        "expires_in": expires_in,
        "refresh_token": refresh_token,
        "refresh_token_expires_at": refresh_expires_at,
    }
    device_session_id = data.get("device_session_id")
    if isinstance(device_session_id, str) and device_session_id:
        payload["device_session_id"] = device_session_id
    return payload


def _validate_runtime_payload(payload: dict, *, audience: str) -> dict:
    """Verify a CP replacement before it reaches a browser or native client."""
    runtime_token = payload["runtime_token"]
    try:
        claims = verify_runtime_token(runtime_token, audience=audience)
    except CPAuthorityUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "cp_unavailable", "message": "Control plane authentication is temporarily unavailable."},
        ) from exc
    except CPTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Control plane returned an invalid runtime token",
        ) from exc

    expected_session_id = payload.get("device_session_id")
    if expected_session_id is not None and expected_session_id != claims.device_session_id:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Control plane runtime session binding changed",
        )
    remaining = claims.expires_at - int(time.time())
    if remaining <= 0 or payload["expires_in"] <= 0:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Control plane returned an expired runtime token",
        )
    # ``expires_in`` was calculated before the CP response crossed the
    # network. Transport and JWKS verification can consume seconds, so the
    # signed JWT expiry is authoritative; never reject or revoke a valid
    # rotated refresh family solely because that relative value is stale.
    payload["expires_in"] = min(payload["expires_in"], remaining)
    payload["device_session_id"] = claims.device_session_id
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
    code_verifier: str | None = None,
    client: str | None = None,
) -> dict:
    if not code or len(code) > 256 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in code):
        raise HTTPException(status_code=400, detail="Invalid handoff code")
    payload = {"code": code, "tenant": tenant}
    if tenant_state:
        payload["tenant_state"] = tenant_state
    if code_verifier:
        payload["code_verifier"] = code_verifier
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
    # The CP response is a cross-service contract. A 200 response with an
    # unverifiable token is an operational/contract failure, not proof that the
    # newly committed family is stolen. Preserve it for CP-side expiry and
    # retry after the issuer/JWKS contract recovers.
    payload = _runtime_payload(raw_payload)
    return _validate_runtime_payload(payload, audience=tenant)


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
    raw_payload = _json_object(exchange)
    payload = _runtime_payload(raw_payload)
    return _validate_runtime_payload(payload, audience=hosted_instance_id())


def _revoke_native_session_payload(
    *,
    settings,
    refresh_token: str | None = None,
    revoke_authority: bool = False,
    orphan_cleanup: bool = False,
    strict: bool = True,
) -> bool:
    control_plane_url = getattr(settings, "control_plane_url", None)
    if not control_plane_url or not refresh_token:
        if strict and control_plane_url is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "cp_unavailable"},
            )
        return False
    payload = {"tenant": hosted_instance_id(), "refresh_token": refresh_token}
    if orphan_cleanup:
        payload["orphan_cleanup"] = True
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
        return False

    if response.status_code != status.HTTP_200_OK:
        logger.warning(
            "control_plane_native_session_revoke_rejected",
            extra={"status_code": response.status_code},
        )
        if strict and response.status_code == status.HTTP_409_CONFLICT:
            try:
                body = response.json()
            except (TypeError, ValueError):
                body = None
            detail = body.get("detail") if isinstance(body, dict) else None
            if isinstance(detail, dict) and detail.get("code") == "revocation_not_authorized":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
        if strict:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "cp_unavailable"},
            )
        return False

    try:
        body = response.json()
    except (TypeError, ValueError):
        body = None
    if not isinstance(body, dict) or body.get("status") != "ok":
        logger.warning("control_plane_native_session_revoke_invalid_response")
        if strict:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "cp_unavailable"},
            )
        return False
    return True


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
    # Failed attempts must not ratchet the browser into the cookie-loop
    # breaker. The next explicit login starts with a fresh bounded attempt.
    redirect.delete_cookie(
        tenant_handoff_attempt_cookie_name(secure=cookie_secure),
        path="/",
        httponly=True,
        secure=cookie_secure,
        samesite="lax",
    )
    return redirect


@router.get("/accept-handoff", include_in_schema=False)
async def accept_handoff_request(
    request: Request,
    code: str | None = None,
    return_to: str | None = None,
    tenant_state: str | None = None,
):
    settings = get_settings()
    control_plane_url = getattr(settings, "control_plane_url", None)
    if not control_plane_url:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hosted handoff is not configured")
    if not code:
        logger.warning("tenant_handoff_code_missing")
        return _handoff_failure_redirect(settings=settings, return_to=return_to, error="handoff_missing")
    if not hosted_cookie_origin_is_secure(settings):
        logger.error("hosted_auth_public_origin_invalid")
        return _handoff_failure_redirect(settings=settings, return_to=return_to, error="auth_misconfigured")

    # Browser prefetchers must never spend a one-use login code. A real
    # top-level navigation is ``document``; other fetch destinations cannot
    # complete the browser handoff safely.
    purpose = f"{request.headers.get('purpose', '')},{request.headers.get('sec-purpose', '')}".lower()
    fetch_dest = request.headers.get("sec-fetch-dest", "").strip().lower()
    if "prefetch" in purpose or (fetch_dest and fetch_dest != "document"):
        logger.info("tenant_handoff_prefetch_ignored", extra={"fetch_dest": fetch_dest or None})
        return Response(
            status_code=status.HTTP_204_NO_CONTENT,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    cookie_secure = tenant_cookie_secure(settings)
    if not tenant_state:
        logger.warning("tenant_login_state_query_missing")
        return _handoff_failure_redirect(settings=settings, return_to=return_to, error="login_state_not_returned")
    if len(tenant_state) > MAX_TENANT_LOGIN_STATE_LENGTH:
        logger.warning("tenant_login_state_oversized", extra={"state_length": len(tenant_state)})
        return _handoff_failure_redirect(settings=settings, return_to=return_to, error="login_state_malformed")
    login_cookie_name = tenant_login_cookie_name(tenant_state, secure=cookie_secure)
    expected_secret = tenant_login_cookie_secret(tenant_state)
    if login_cookie_name is None or expected_secret is None:
        logger.warning(
            "tenant_login_state_invalid",
            extra={"state_length": len(tenant_state)},
        )
        return _handoff_failure_redirect(
            settings=settings,
            return_to=return_to,
            error="login_state_malformed",
        )
    actual_secret = request.cookies.get(login_cookie_name)
    if not actual_secret:
        logger.warning("tenant_login_cookie_missing", extra={"state_length": len(tenant_state)})
        return _handoff_failure_redirect(
            settings=settings,
            return_to=return_to,
            error="login_cookie_absent",
            clear_login_state=True,
            tenant_state=tenant_state,
        )
    if not hmac.compare_digest(expected_secret.encode("utf-8"), actual_secret.encode("utf-8")):
        logger.warning("tenant_login_state_mismatch", extra={"state_length": len(tenant_state)})
        return _handoff_failure_redirect(
            settings=settings,
            return_to=return_to,
            error="login_cookie_mismatch",
            clear_login_state=True,
            tenant_state=tenant_state,
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
    login_attempt_marker = request.cookies.get(tenant_login_attempt_cookie_name(secure=cookie_secure)) or "1"
    redirect.set_cookie(
        tenant_login_ready_cookie_name(secure=cookie_secure),
        login_attempt_marker,
        max_age=30,
        path="/",
        httponly=False,
        secure=cookie_secure,
        samesite="lax",
    )
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
async def accept_native_handoff(request: Request, response: Response, body: NativeHandoffRequest):
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
        code_verifier=body.code_verifier,
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
    except HTTPException as exc:
        if exc.status_code < 500:
            await _best_effort_revoke_native_session(settings, refresh_token)
        raise
    if user is None:
        await _best_effort_revoke_native_session(settings, refresh_token)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid runtime token")

    _set_no_store(response)
    return normalized_payload


@router.post("/refresh-native-session")
async def refresh_native_session(request: Request, response: Response, body: NativeRefreshRequest):
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
        attempt_id=refresh_token,
        client_ip=get_client_ip(request),
    )
    payload = await asyncio.to_thread(
        _refresh_native_session_payload,
        settings=settings,
        refresh_token=refresh_token,
    )
    _set_no_store(response)
    return payload


@router.post("/revoke-native-session")
async def revoke_native_session(request: Request, response: Response, body: NativeRevokeRequest):
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
        attempt_id=refresh_token,
        client_ip=get_client_ip(request),
    )
    await asyncio.to_thread(
        _revoke_native_session_payload,
        settings=settings,
        refresh_token=refresh_token,
        revoke_authority=body.revoke_authority,
        orphan_cleanup=body.orphan_cleanup,
        strict=True,
    )
    _set_no_store(response)
    return {"status": "ok"}


__all__ = [
    "NativeHandoffRequest",
    "NativeRefreshRequest",
    "NativeRevokeRequest",
    "accept_handoff_request",
    "accept_native_handoff",
    "refresh_native_session",
    "revoke_native_session",
    "router",
]
