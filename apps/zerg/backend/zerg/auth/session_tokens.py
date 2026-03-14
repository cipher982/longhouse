"""Shared tenant JWT + browser session cookie helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Optional

from fastapi import Response

from zerg.auth.strategy import SESSION_COOKIE_NAME
from zerg.config import get_settings

_settings = get_settings()

JWT_SECRET = _settings.jwt_secret
SESSION_COOKIE_PATH = "/"
SESSION_COOKIE_SECURE = not _settings.auth_disabled and not _settings.testing


def _set_session_cookie(response: Response, token: str, max_age: int) -> None:
    """Set the browser session cookie with the standard Longhouse flags."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=max_age,
        path=SESSION_COOKIE_PATH,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
    )


def _clear_session_cookie(response: Response) -> None:
    """Clear the browser session cookie."""
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path=SESSION_COOKIE_PATH,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
    )


def _issue_access_token(
    user_id: int,
    email: str,
    *,
    display_name: Optional[str] = None,
    avatar_url: Optional[str] = None,
    expires_delta: timedelta = timedelta(minutes=30),
) -> str:
    """Return signed HS256 access token including optional profile fields."""
    expiry = datetime.now(timezone.utc) + expires_delta
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "email": email,
        "exp": int(expiry.timestamp()),
    }

    if display_name is not None:
        payload["display_name"] = display_name

    if avatar_url is not None:
        payload["avatar_url"] = avatar_url

    return _encode_jwt(payload, JWT_SECRET)


def _encode_jwt(payload: dict[str, Any], secret: str) -> str:
    """Encode a compact HS256 JWT with a lightweight fallback for tests."""
    try:
        from jose import jwt  # type: ignore
    except ModuleNotFoundError:  # pragma: no cover
        import json

        class _MiniJWT:
            @staticmethod
            def _b64(data: bytes) -> bytes:
                return base64.urlsafe_b64encode(data).rstrip(b"=")

            @classmethod
            def encode(cls, payload_: dict[str, Any], secret_: str, algorithm: str = "HS256") -> str:
                if algorithm != "HS256":
                    raise ValueError("Only HS256 supported in fallback")

                header = {"alg": algorithm, "typ": "JWT"}
                header_b64 = cls._b64(json.dumps(header, separators=(",", ":")).encode())
                payload_b64 = cls._b64(json.dumps(payload_, separators=(",", ":")).encode())
                signing_input = header_b64 + b"." + payload_b64
                signature = hmac.new(secret_.encode(), signing_input, hashlib.sha256).digest()
                sig_b64 = cls._b64(signature)
                return (signing_input + b"." + sig_b64).decode()

        jwt = _MiniJWT  # type: ignore

    return jwt.encode(payload, secret, algorithm="HS256")


__all__ = [
    "JWT_SECRET",
    "SESSION_COOKIE_NAME",
    "_clear_session_cookie",
    "_encode_jwt",
    "_issue_access_token",
    "_set_session_cookie",
]
