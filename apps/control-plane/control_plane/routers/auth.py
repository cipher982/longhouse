"""Google OAuth for control plane signup/login.

Flow:
  GET  /auth/google          → redirect to Google consent screen
  GET  /auth/google/callback  → exchange code, upsert user, set session cookie
  GET  /auth/status           → check if authenticated
  POST /auth/logout           → clear session cookie
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from control_plane.config import settings
from control_plane.db import get_db
from control_plane.models import User

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SESSION_COOKIE_NAME = "cp_session"
SESSION_COOKIE_MAX_AGE = 7 * 24 * 60 * 60  # 7 days
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


# ---------------------------------------------------------------------------
# JWT helpers (HS256, minimal — no external dep needed)
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def _b64url_decode(data: str) -> bytes:
    padded = data + "=" * (4 - len(data) % 4)
    return base64.urlsafe_b64decode(padded)


def _encode_jwt(payload: dict[str, Any], secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = header_b64 + b"." + payload_b64
    signature = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    sig_b64 = _b64url(signature)
    return (signing_input + b"." + sig_b64).decode()


def _decode_jwt(token: str, secret: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT")

    signing_input = (parts[0] + "." + parts[1]).encode()
    expected_sig = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    actual_sig = _b64url_decode(parts[2])

    if not hmac.compare_digest(expected_sig, actual_sig):
        raise ValueError("Invalid JWT signature")

    payload = json.loads(_b64url_decode(parts[1]))
    if payload.get("exp", 0) < time.time():
        raise ValueError("JWT expired")

    return payload


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def _set_session(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_COOKIE_MAX_AGE,
        path="/",
        httponly=True,
        secure=True,
        samesite="lax",
    )


def _issue_session_token(user: User) -> str:
    return _encode_jwt(
        {
            "sub": str(user.id),
            "email": user.email,
            "exp": int(time.time()) + SESSION_COOKIE_MAX_AGE,
        },
        settings.jwt_secret,
    )


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency: extract authenticated user from session cookie."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    try:
        payload = _decode_jwt(token, settings.jwt_secret)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))

    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------


def _require_oauth():
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth not configured",
        )


def _callback_url() -> str:
    return f"https://control.{settings.root_domain}/auth/google/callback"


def _exchange_code(code: str) -> dict[str, Any]:
    """Exchange authorization code for tokens."""
    data = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": _callback_url(),
            "grant_type": "authorization_code",
        }
    ).encode()

    req = urllib.request.Request(
        GOOGLE_TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        logger.error(f"Google token exchange failed: {exc}")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Google token exchange failed") from exc


def _get_userinfo(access_token: str) -> dict[str, Any]:
    """Fetch user profile from Google."""
    req = urllib.request.Request(
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        logger.error(f"Google userinfo fetch failed: {exc}")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to fetch user info") from exc


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/google")
def google_login():
    """Redirect to Google OAuth consent screen."""
    _require_oauth()

    params = urllib.parse.urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": _callback_url(),
            "response_type": "code",
            "scope": "openid email profile",
            "access_type": "online",
            "prompt": "select_account",
        }
    )
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{params}", status_code=302)


@router.get("/google/callback")
def google_callback(code: str | None = None, error: str | None = None, db: Session = Depends(get_db)):
    """Handle Google OAuth callback: exchange code, upsert user, redirect."""
    _require_oauth()

    if error:
        logger.warning(f"Google OAuth error: {error}")
        return RedirectResponse(f"https://{settings.root_domain}?auth_error={error}", status_code=302)

    if not code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing authorization code")

    # Exchange code for tokens
    token_data = _exchange_code(code)
    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="No access_token in Google response")

    # Get user profile
    userinfo = _get_userinfo(access_token)
    email = userinfo.get("email")
    if not email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google account has no email")

    email = email.strip().lower()

    # Upsert user
    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(email=email)
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info(f"Created new user: {email}")
    else:
        logger.info(f"Existing user logged in: {email}")

    # Issue session token + set cookie
    session_token = _issue_session_token(user)
    response = RedirectResponse(f"https://control.{settings.root_domain}/dashboard", status_code=302)
    _set_session(response, session_token)
    return response


@router.get("/status")
def auth_status(request: Request, db: Session = Depends(get_db)):
    """Check authentication status."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return {"authenticated": False, "user": None}

    try:
        payload = _decode_jwt(token, settings.jwt_secret)
    except ValueError:
        return {"authenticated": False, "user": None}

    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user:
        return {"authenticated": False, "user": None}

    return {
        "authenticated": True,
        "user": {
            "id": user.id,
            "email": user.email,
            "subscription_status": user.subscription_status,
            "has_instance": user.instance is not None,
        },
    }


@router.post("/logout")
def logout(response: Response):
    """Clear session cookie."""
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/", httponly=True, secure=True, samesite="lax")
    return {"ok": True}
