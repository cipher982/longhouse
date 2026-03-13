"""Auth for control plane: email+password and Google OAuth.

Flow:
  POST /auth/signup            → create user (unverified), send verification email, redirect to /verify-email
  POST /auth/login             → verify email+password, set session cookie
  GET  /auth/google            → redirect to Google consent screen
  GET  /auth/google/callback   → exchange code, upsert user (auto-verified), set session cookie
  GET  /auth/verify            → verify email via token, log in, redirect to dashboard
  POST /auth/resend-verification → resend verification email for logged-in unverified user
  POST /auth/reset-password-request → send password reset email (always 200)
  POST /auth/reset-password    → validate token + update password
  GET  /auth/status            → check if authenticated
  POST /auth/logout            → clear session cookie
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from typing import Any

import httpx
from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Form
from fastapi import Response
from fastapi import status
from fastapi.responses import RedirectResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from control_plane.config import settings
from control_plane.db import get_db
from control_plane.models import Instance
from control_plane.models import User
from control_plane.services.gmail_pubsub import HostedGmailPubSubError
from control_plane.services.gmail_pubsub import ensure_instance_gmail_subscription

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)
limiter = Limiter(key_func=get_remote_address)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SESSION_COOKIE_NAME = "cp_session"
SESSION_COOKIE_MAX_AGE = 7 * 24 * 60 * 60  # 7 days
VERIFY_TOKEN_MAX_AGE = 24 * 60 * 60  # 24 hours
RESET_TOKEN_MAX_AGE = 60 * 60  # 1 hour
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
GMAIL_CONNECT_SCOPES = "https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/gmail.send"
HOSTED_GMAIL_STATE_MAX_AGE = 10 * 60


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

    # Reject non-session tokens (e.g. password_reset, email_verify)
    purpose = payload.get("purpose")
    if purpose is not None and purpose != "session":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token purpose")

    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def get_current_user_or_none(request: Request, db: Session = Depends(get_db)) -> User | None:
    """Dependency: return authenticated user or None (no exception)."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    try:
        payload = _decode_jwt(token, settings.jwt_secret)
    except ValueError:
        return None
    # Reject non-session tokens (e.g. password_reset, email_verify)
    purpose = payload.get("purpose")
    if purpose is not None and purpose != "session":
        return None
    return db.query(User).filter(User.id == int(payload["sub"])).first()


# ---------------------------------------------------------------------------
# Email verification helpers
# ---------------------------------------------------------------------------


def _issue_verify_token(user: User) -> str:
    """Issue a short-lived JWT for email verification."""
    return _encode_jwt(
        {
            "sub": str(user.id),
            "purpose": "email_verify",
            "exp": int(time.time()) + VERIFY_TOKEN_MAX_AGE,
        },
        settings.jwt_secret,
    )


def _send_verification(user: User) -> bool:
    """Send verification email. Returns True on success, False on failure."""
    token = _issue_verify_token(user)
    verify_url = f"https://control.{settings.root_domain}/auth/verify?token={token}"
    try:
        from control_plane.services.email import send_verification_email

        send_verification_email(user.email, verify_url)
        return True
    except Exception:
        logger.exception(f"Could not send verification email to {user.email}")
        return False


# ---------------------------------------------------------------------------
# Password hashing (PBKDF2 — same pattern as provisioner.py)
# ---------------------------------------------------------------------------


def _hash_password(password: str) -> str:
    """Hash a password with PBKDF2-SHA256 and a random salt."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
    return f"pbkdf2:sha256:600000${salt.hex()}${dk.hex()}"


def _verify_password(password: str, hash_string: str) -> bool:
    """Verify a password against a PBKDF2 hash string."""
    try:
        _, _, rest = hash_string.partition("pbkdf2:sha256:600000$")
        salt_hex, _, dk_hex = rest.partition("$")
        salt = bytes.fromhex(salt_hex)
        expected_dk = bytes.fromhex(dk_hex)
        actual_dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
        return hmac.compare_digest(expected_dk, actual_dk)
    except (ValueError, AttributeError):
        return False


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


def _gmail_callback_url() -> str:
    return f"https://control.{settings.root_domain}/auth/google/gmail/callback"


def _exchange_code_with_redirect_uri(code: str, redirect_uri: str) -> dict[str, Any]:
    """Exchange authorization code for tokens."""
    data = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": redirect_uri,
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


def _exchange_code(code: str) -> dict[str, Any]:
    return _exchange_code_with_redirect_uri(code, _callback_url())


def _issue_hosted_gmail_state(*, email: str, instance: str) -> str:
    return _encode_jwt(
        {
            "sub": email,
            "email": email,
            "instance": instance,
            "purpose": "hosted_gmail_connect_callback",
            "exp": int(time.time()) + HOSTED_GMAIL_STATE_MAX_AGE,
        },
        settings.jwt_secret,
    )


def _issue_hosted_gmail_handoff_token(*, email: str, instance: str) -> str:
    return _encode_jwt(
        {
            "sub": email,
            "email": email,
            "instance": instance,
            "purpose": "hosted_gmail_connect_handoff",
            "exp": int(time.time()) + HOSTED_GMAIL_STATE_MAX_AGE,
        },
        settings.instance_jwt_secret,
    )


def _tenant_conversations_url(subdomain: str, *, error: str | None = None) -> str:
    base = f"https://{subdomain}.{settings.root_domain}/conversations"
    if not error:
        return base
    encoded = urllib.parse.quote(error[:300], safe="")
    return f"{base}?gmail_error={encoded}"


def _load_hosted_gmail_instance(
    db: Session,
    *,
    subdomain: str,
    email: str,
) -> tuple[Instance, User]:
    row = (
        db.query(Instance, User).join(User, Instance.user_id == User.id).filter(Instance.subdomain == subdomain).first()
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Instance not found")

    instance, user = row
    if user.email.strip().lower() != email.strip().lower():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Instance does not belong to this user")
    return instance, user


def _post_hosted_gmail_handoff(*, subdomain: str, refresh_token: str, handoff_token: str) -> None:
    url = f"https://{subdomain}.{settings.root_domain}/api/internal/auth/google/gmail/handoff"
    response = httpx.post(
        url,
        headers={"X-Internal-Token": settings.instance_internal_api_secret},
        json={
            "refresh_token": refresh_token,
            "handoff_token": handoff_token,
        },
        timeout=20.0,
    )
    response.raise_for_status()


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


@router.post("/signup")
@limiter.limit("5/minute")
def email_signup(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    """Create a new user with email + password."""
    email = email.strip().lower()

    if len(password) < 8:
        from fastapi.responses import RedirectResponse as _RR

        return _RR("/signup?error=Password+must+be+at+least+8+characters", status_code=303)

    if password != password_confirm:
        from fastapi.responses import RedirectResponse as _RR

        return _RR("/signup?error=Passwords+do+not+match", status_code=303)

    existing = db.query(User).filter(User.email == email).first()
    if existing:
        from fastapi.responses import RedirectResponse as _RR

        return _RR("/signup?error=An+account+with+this+email+already+exists", status_code=303)

    user = User(email=email, password_hash=_hash_password(password), email_verified=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info(f"Created new user via email signup: {email}")

    # Send verification email
    _send_verification(user)

    # Log the user in but redirect to verify-email page (not dashboard)
    session_token = _issue_session_token(user)
    response = RedirectResponse(f"https://control.{settings.root_domain}/verify-email", status_code=303)
    _set_session(response, session_token)
    return response


@router.post("/login")
@limiter.limit("10/minute")
def email_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    """Authenticate with email + password."""
    email = email.strip().lower()

    user = db.query(User).filter(User.email == email).first()
    if not user or not user.password_hash:
        from fastapi.responses import RedirectResponse as _RR

        return _RR("/?error=Invalid+email+or+password", status_code=303)

    if not _verify_password(password, user.password_hash):
        from fastapi.responses import RedirectResponse as _RR

        return _RR("/?error=Invalid+email+or+password", status_code=303)

    logger.info(f"User logged in via email: {email}")

    session_token = _issue_session_token(user)
    response = RedirectResponse(f"https://control.{settings.root_domain}/dashboard", status_code=303)
    _set_session(response, session_token)
    return response


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

    # Upsert user (handle concurrent callbacks for same email)
    # Google OAuth users are auto-verified (Google already verified their email)
    user = db.query(User).filter(User.email == email).first()
    if not user:
        from sqlalchemy.exc import IntegrityError

        try:
            user = User(email=email, email_verified=True)
            db.add(user)
            db.commit()
            db.refresh(user)
            logger.info(f"Created new user (Google, verified): {email}")
        except IntegrityError:
            db.rollback()
            user = db.query(User).filter(User.email == email).first()
            logger.info(f"Concurrent signup resolved for: {email}")
    else:
        logger.info(f"Existing user logged in: {email}")

    # Ensure Google users are always marked verified
    if user and not user.email_verified:
        user.email_verified = True
        db.commit()

    # Issue session token + set cookie
    session_token = _issue_session_token(user)
    response = RedirectResponse(f"https://control.{settings.root_domain}/dashboard", status_code=302)
    _set_session(response, session_token)
    return response


@router.get("/google/gmail/start")
def google_gmail_start(token: str | None = None, db: Session = Depends(get_db)):
    """Start hosted Gmail OAuth using a tenant-issued one-shot token."""

    _require_oauth()
    if not token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing connect token")

    try:
        payload = _decode_jwt(token, settings.instance_jwt_secret)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    if payload.get("purpose") != "hosted_gmail_connect_start":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid connect token purpose")

    email = str(payload.get("email") or payload.get("sub") or "").strip().lower()
    subdomain = str(payload.get("instance") or "").strip().lower()
    if not email or not subdomain:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid connect token payload")

    _load_hosted_gmail_instance(db, subdomain=subdomain, email=email)
    state = _issue_hosted_gmail_state(email=email, instance=subdomain)

    params = urllib.parse.urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": _gmail_callback_url(),
            "response_type": "code",
            "scope": GMAIL_CONNECT_SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
    )
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{params}", status_code=302)


@router.get("/google/gmail/callback")
def google_gmail_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    """Complete hosted Gmail OAuth and hand the refresh token to the tenant."""

    _require_oauth()
    if not state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing state")

    try:
        state_payload = _decode_jwt(state, settings.jwt_secret)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    if state_payload.get("purpose") != "hosted_gmail_connect_callback":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid state purpose")

    email = str(state_payload.get("email") or state_payload.get("sub") or "").strip().lower()
    subdomain = str(state_payload.get("instance") or "").strip().lower()
    if not email or not subdomain:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid state payload")

    _load_hosted_gmail_instance(db, subdomain=subdomain, email=email)

    if error:
        return RedirectResponse(_tenant_conversations_url(subdomain, error=error), status_code=302)

    if not code:
        return RedirectResponse(
            _tenant_conversations_url(subdomain, error="Missing authorization code."),
            status_code=302,
        )

    token_data = _exchange_code_with_redirect_uri(code, _gmail_callback_url())
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        return RedirectResponse(
            _tenant_conversations_url(
                subdomain,
                error="Google did not return a refresh token. Remove Longhouse from Google permissions, then try again.",
            ),
            status_code=302,
        )

    try:
        ensure_instance_gmail_subscription(subdomain=subdomain)
    except HostedGmailPubSubError as exc:
        logger.error("Hosted Gmail Pub/Sub provisioning failed for %s: %s", subdomain, exc)
        return RedirectResponse(
            _tenant_conversations_url(subdomain, error="Could not configure Gmail notifications for this instance."),
            status_code=302,
        )

    handoff_token = _issue_hosted_gmail_handoff_token(email=email, instance=subdomain)
    try:
        _post_hosted_gmail_handoff(
            subdomain=subdomain,
            refresh_token=str(refresh_token),
            handoff_token=handoff_token,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Hosted Gmail handoff failed for %s: %s", subdomain, exc)
        return RedirectResponse(
            _tenant_conversations_url(subdomain, error="Could not finish Gmail connection on the instance."),
            status_code=302,
        )

    return RedirectResponse(_tenant_conversations_url(subdomain), status_code=302)


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
            "email_verified": user.email_verified,
            "subscription_status": user.subscription_status,
            "has_instance": user.instance is not None,
        },
    }


@router.get("/verify")
def verify_email(token: str, db: Session = Depends(get_db)):
    """Verify email via token link. Marks user verified, logs them in, redirects to dashboard."""
    try:
        payload = _decode_jwt(token, settings.jwt_secret)
    except ValueError as exc:
        error_msg = "expired" if "expired" in str(exc).lower() else "invalid"
        return RedirectResponse(f"/verify-email?error=Verification+link+{error_msg}", status_code=302)

    if payload.get("purpose") != "email_verify":
        return RedirectResponse("/verify-email?error=Invalid+verification+link", status_code=302)

    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user:
        return RedirectResponse("/verify-email?error=User+not+found", status_code=302)

    user.email_verified = True
    db.commit()
    logger.info(f"Email verified for: {user.email}")

    # Log the user in and redirect to dashboard
    session_token = _issue_session_token(user)
    response = RedirectResponse(f"https://control.{settings.root_domain}/dashboard", status_code=302)
    _set_session(response, session_token)
    return response


@router.post("/resend-verification")
@limiter.limit("3/minute")
def resend_verification(
    request: Request,
    user: User = Depends(get_current_user),
):
    """Resend verification email for the current logged-in user."""
    if user.email_verified:
        return RedirectResponse("/dashboard", status_code=303)

    if _send_verification(user):
        return RedirectResponse("/verify-email?resent=1", status_code=303)
    return RedirectResponse("/verify-email?error=Could+not+send+email.+Please+try+again+later.", status_code=303)


@router.post("/logout")
def logout(response: Response):
    """Clear session cookie."""
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/", httponly=True, secure=True, samesite="lax")
    return {"ok": True}


@router.get("/logout")
def logout_redirect(return_to: str | None = None):
    """Clear session cookie and redirect (safe allowlist)."""
    target = "/"
    if return_to:
        parsed = urllib.parse.urlparse(return_to)
        if not parsed.scheme and not parsed.netloc and return_to.startswith("/"):
            target = return_to
        elif parsed.scheme in ("http", "https") and parsed.netloc:
            host = parsed.netloc.split(":")[0].lower()
            root = settings.root_domain.lower()
            if host == root or host.endswith(f".{root}"):
                target = return_to

    response = RedirectResponse(target, status_code=302)
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/", httponly=True, secure=True, samesite="lax")
    return response


# ---------------------------------------------------------------------------
# Password reset helpers
# ---------------------------------------------------------------------------


def _issue_reset_token(user: User) -> str:
    """Issue a short-lived JWT for password reset (1 hour).

    Includes a hash prefix (phash) so the token is invalidated once the
    password is changed — prevents replay after use.
    """
    return _encode_jwt(
        {
            "sub": str(user.id),
            "purpose": "password_reset",
            "phash": user.password_hash[:8] if user.password_hash else "",
            "exp": int(time.time()) + RESET_TOKEN_MAX_AGE,
        },
        settings.jwt_secret,
    )


def _send_password_reset(user: User) -> bool:
    """Send password reset email. Returns True on success, False on failure."""
    token = _issue_reset_token(user)
    reset_url = f"https://control.{settings.root_domain}/reset-password?token={token}"
    try:
        from control_plane.services.email import send_password_reset_email

        send_password_reset_email(user.email, reset_url)
        return True
    except Exception:
        logger.exception(f"Could not send password reset email to {user.email}")
        return False


# ---------------------------------------------------------------------------
# Password reset routes
# ---------------------------------------------------------------------------


@router.post("/reset-password-request")
@limiter.limit("3/minute")
def reset_password_request(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    """Request a password reset link. Always returns 200 to prevent email enumeration."""
    email = email.strip().lower()

    user = db.query(User).filter(User.email == email).first()
    if user and user.password_hash:
        _send_password_reset(user)

    # Always redirect with the same message regardless of whether user exists
    return RedirectResponse("/forgot-password?sent=1", status_code=303)


@router.post("/reset-password")
def reset_password(
    token: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    """Reset password using a valid JWT token."""
    if len(password) < 8:
        return RedirectResponse(
            f"/reset-password?token={token}&error=Password+must+be+at+least+8+characters",
            status_code=303,
        )

    if password != password_confirm:
        return RedirectResponse(
            f"/reset-password?token={token}&error=Passwords+do+not+match",
            status_code=303,
        )

    try:
        payload = _decode_jwt(token, settings.jwt_secret)
    except ValueError as exc:
        error_msg = "expired" if "expired" in str(exc).lower() else "invalid"
        return RedirectResponse(f"/reset-password?error=Reset+link+{error_msg}", status_code=303)

    if payload.get("purpose") != "password_reset":
        return RedirectResponse("/reset-password?error=Invalid+reset+link", status_code=303)

    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user:
        return RedirectResponse("/reset-password?error=User+not+found", status_code=303)

    # Reject replayed tokens: phash must match current password hash prefix
    expected_phash = user.password_hash[:8] if user.password_hash else ""
    if payload.get("phash", "") != expected_phash:
        return RedirectResponse("/reset-password?error=Reset+link+already+used", status_code=303)
    user.password_hash = _hash_password(password)
    db.commit()
    logger.info(f"Password reset for: {user.email}")

    # Log the user in and redirect to dashboard
    session_token = _issue_session_token(user)
    response = RedirectResponse(f"https://control.{settings.root_domain}/dashboard", status_code=303)
    _set_session(response, session_token)
    return response
