from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from threading import Event
from threading import Lock
from typing import Any

import httpx
import jwt
from jwt import PyJWTError
from jwt.algorithms import RSAAlgorithm
from zerg.config import get_settings

JWKS_CACHE_TTL_SECONDS = 300
JWKS_STALE_IF_ERROR_SECONDS = 24 * 60 * 60
JWKS_CACHE_MAX_ENTRIES = 8
JWKS_UNKNOWN_KID_BACKOFF_SECONDS = 5
JWKS_UNKNOWN_KID_MAX_ENTRIES = 256
logger = logging.getLogger(__name__)


class CPTokenError(ValueError):
    pass


class CPAuthorityUnavailable(CPTokenError):
    """The issuer could not be reached or did not return usable keys."""


@dataclass(frozen=True)
class CPTokenClaims:
    cp_user_id: int
    email: str
    email_verified: bool
    display_name: str | None
    avatar_url: str | None
    audience: str
    issuer: str
    expires_at: int
    token_id: str
    device_session_id: str


_jwks_cache: OrderedDict[str, tuple[float, dict[str, dict[str, Any]]]] = OrderedDict()
_jwks_cache_lock = Lock()
_jwks_fetch_events: dict[str, Event] = {}
_jwks_retry_after: dict[str, float] = {}
_jwks_unknown_kid_retry_after: dict[str, float] = {}
JWKS_FETCH_WAIT_SECONDS = 1.0
JWKS_RETRY_BACKOFF_SECONDS = 5.0


def _control_plane_url() -> str:
    settings = get_settings()
    if not settings.control_plane_url:
        raise CPAuthorityUnavailable("CONTROL_PLANE_URL is not configured")
    return settings.control_plane_url.rstrip("/")


def _fetch_jwks(*, force: bool = False) -> dict[str, dict[str, Any]]:
    base = _control_plane_url()
    allow_stale = not force

    while True:
        with _jwks_cache_lock:
            cached = _jwks_cache.get(base)
            now = time.time()
            if not force and cached and cached[0] > now:
                _jwks_cache.move_to_end(base)
                return cached[1]

            retry_after = _jwks_retry_after.get(base, 0.0)
            inflight = _jwks_fetch_events.get(base)
            if retry_after > now:
                if allow_stale and cached and cached[0] + JWKS_STALE_IF_ERROR_SECONDS > now:
                    _jwks_cache.move_to_end(base)
                    return cached[1]
                raise CPAuthorityUnavailable("Unable to refresh CP JWKS")
            if inflight is None:
                inflight = Event()
                _jwks_fetch_events[base] = inflight
                break

        if not inflight.wait(timeout=JWKS_FETCH_WAIT_SECONDS):
            with _jwks_cache_lock:
                cached = _jwks_cache.get(base)
                now = time.time()
                if allow_stale and cached and cached[0] + JWKS_STALE_IF_ERROR_SECONDS > now:
                    return cached[1]
            raise CPAuthorityUnavailable("CP JWKS refresh is still in progress")
        force = False
        allow_stale = True

    try:
        response = httpx.get(f"{base}/api/identity/jwks.json", timeout=5.0)
        response.raise_for_status()
        payload = response.json()
        keys = payload.get("keys")
        by_kid: dict[str, dict[str, Any]] = {}
        for key in keys:
            if not isinstance(key, dict) or not key.get("kid") or key.get("kty") != "RSA":
                continue
            try:
                RSAAlgorithm.from_jwk(json.dumps(key))
            except (TypeError, ValueError, PyJWTError):
                logger.warning("ignoring malformed CP JWKS key kid=%s", key.get("kid"))
                continue
            by_kid[str(key["kid"])] = key
        if not by_kid:
            raise ValueError("CP JWKS has no usable signing keys")
    except Exception as exc:
        with _jwks_cache_lock:
            _jwks_retry_after[base] = time.time() + JWKS_RETRY_BACKOFF_SECONDS
            _jwks_fetch_events.pop(base, None)
            inflight.set()
            cached = _jwks_cache.get(base)
            now = time.time()
            if allow_stale and cached and cached[0] + JWKS_STALE_IF_ERROR_SECONDS > now:
                logger.warning("using stale CP JWKS while control plane is unavailable")
                _jwks_cache.move_to_end(base)
                return cached[1]
        raise CPAuthorityUnavailable("Unable to fetch CP JWKS") from exc

    with _jwks_cache_lock:
        _jwks_cache[base] = (time.time() + JWKS_CACHE_TTL_SECONDS, by_kid)
        _jwks_cache.move_to_end(base)
        _jwks_retry_after.pop(base, None)
        _jwks_fetch_events.pop(base, None)
        inflight.set()
        while len(_jwks_cache) > JWKS_CACHE_MAX_ENTRIES:
            _jwks_cache.popitem(last=False)
    return by_kid


def clear_jwks_cache() -> None:
    with _jwks_cache_lock:
        _jwks_cache.clear()
        _jwks_retry_after.clear()
        _jwks_unknown_kid_retry_after.clear()


def verify_runtime_token(token: str, *, audience: str) -> CPTokenClaims:
    try:
        header = jwt.get_unverified_header(token)
    except PyJWTError as exc:
        raise CPTokenError("Invalid CP token header") from exc
    kid = str(header.get("kid") or "")
    if not kid:
        raise CPTokenError("CP token missing kid")

    keys = _fetch_jwks()
    jwk = keys.get(kid)
    if jwk is None:
        base = _control_plane_url()
        now = time.time()
        with _jwks_cache_lock:
            retry_after = _jwks_unknown_kid_retry_after.get(base, 0.0)
        if retry_after > now:
            raise CPTokenError("Unknown CP token kid")

        # Fetch before setting the issuer-wide backoff. A caller may present
        # an unknown key id immediately before a legitimate CP key rotation;
        # setting the backoff first would make that newly published key fail
        # for the whole window. If the forced fetch still has no key, the
        # issuer-wide backoff prevents arbitrary attacker-controlled kids from
        # turning into one CP request each.
        keys = _fetch_jwks(force=True)
        jwk = keys.get(kid)
        if jwk is None:
            with _jwks_cache_lock:
                _jwks_unknown_kid_retry_after[base] = now + JWKS_UNKNOWN_KID_BACKOFF_SECONDS
        else:
            with _jwks_cache_lock:
                _jwks_unknown_kid_retry_after.pop(base, None)
    if jwk is None:
        raise CPTokenError("Unknown CP token kid")

    issuer = _control_plane_url()
    try:
        public_key = RSAAlgorithm.from_jwk(json.dumps(jwk))
        payload = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            options={
                "require": [
                    "iss",
                    "aud",
                    "sub",
                    "email",
                    "email_verified",
                    "iat",
                    "exp",
                    "jti",
                    "sid",
                    "typ",
                ]
            },
        )
    except (PyJWTError, TypeError, ValueError) as exc:
        raise CPTokenError("Invalid CP runtime token") from exc
    if payload.get("aud") != audience:
        raise CPTokenError("CP token audience is not exact")
    if payload.get("typ") != "access":
        raise CPTokenError("CP token type is not access")

    sub = str(payload.get("sub") or "")
    if not sub.isdecimal():
        raise CPTokenError("CP token sub must be a decimal user id")
    email = str(payload.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise CPTokenError("CP token email is invalid")
    email_verified = payload.get("email_verified")
    if not isinstance(email_verified, bool):
        raise CPTokenError("CP token email_verified must be boolean")
    token_id = payload.get("jti")
    if not isinstance(token_id, str) or not token_id:
        raise CPTokenError("CP token jti is invalid")
    device_session_id = payload.get("sid")
    if not isinstance(device_session_id, str) or not device_session_id:
        raise CPTokenError("CP token sid is invalid")

    return CPTokenClaims(
        cp_user_id=int(sub),
        email=email,
        email_verified=email_verified,
        display_name=payload.get("display_name"),
        avatar_url=payload.get("avatar_url"),
        audience=str(payload["aud"]),
        issuer=str(payload["iss"]),
        expires_at=int(payload["exp"]),
        token_id=token_id,
        device_session_id=device_session_id,
    )
