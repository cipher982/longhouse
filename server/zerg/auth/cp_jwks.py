from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt import PyJWTError
from jwt.algorithms import RSAAlgorithm
from zerg.config import get_settings

JWKS_CACHE_TTL_SECONDS = 300


class CPTokenError(ValueError):
    pass


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


_jwks_cache: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}


def _control_plane_url() -> str:
    settings = get_settings()
    if not settings.control_plane_url:
        raise CPTokenError("CONTROL_PLANE_URL is not configured")
    return settings.control_plane_url.rstrip("/")


def _fetch_jwks(*, force: bool = False) -> dict[str, dict[str, Any]]:
    base = _control_plane_url()
    cached = _jwks_cache.get(base)
    now = time.time()
    if not force and cached and cached[0] > now:
        return cached[1]

    response = httpx.get(f"{base}/api/identity/jwks.json", timeout=5.0)
    response.raise_for_status()
    payload = response.json()
    keys = payload.get("keys")
    if not isinstance(keys, list):
        raise CPTokenError("CP JWKS payload missing keys")
    by_kid: dict[str, dict[str, Any]] = {}
    for key in keys:
        if isinstance(key, dict) and key.get("kid"):
            by_kid[str(key["kid"])] = key
    if not by_kid:
        raise CPTokenError("CP JWKS has no usable keys")
    _jwks_cache[base] = (now + JWKS_CACHE_TTL_SECONDS, by_kid)
    return by_kid


def clear_jwks_cache() -> None:
    _jwks_cache.clear()


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
        keys = _fetch_jwks(force=True)
        jwk = keys.get(kid)
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
            options={"require": ["iss", "aud", "sub", "email", "email_verified", "iat", "exp"]},
        )
    except PyJWTError as exc:
        raise CPTokenError("Invalid CP runtime token") from exc

    sub = str(payload.get("sub") or "")
    if not sub.isdecimal():
        raise CPTokenError("CP token sub must be a decimal user id")
    email = str(payload.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise CPTokenError("CP token email is invalid")
    email_verified = payload.get("email_verified")
    if not isinstance(email_verified, bool):
        raise CPTokenError("CP token email_verified must be boolean")

    return CPTokenClaims(
        cp_user_id=int(sub),
        email=email,
        email_verified=email_verified,
        display_name=payload.get("display_name"),
        avatar_url=payload.get("avatar_url"),
        audience=str(payload["aud"]),
        issuer=str(payload["iss"]),
        expires_at=int(payload["exp"]),
    )
