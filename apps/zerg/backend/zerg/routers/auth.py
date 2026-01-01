"""Authentication routes (Stage 2 – Google Sign-In)."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from typing import Any
from typing import Optional

from dotenv import load_dotenv
from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.crud import crud
from zerg.database import get_db
from zerg.dependencies.auth import get_current_user
from zerg.schemas.schemas import TokenOut

# Use override=True to ensure proper quote stripping even if vars are inherited from parent process
load_dotenv(override=True)

router = APIRouter(prefix="/auth", tags=["auth"])

# Unified settings ---------------------------------------------------------

_settings = get_settings()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Snapshot of critical secrets – access once at import time.  We purposefully
# *copy* the values rather than referencing the Settings object throughout so
# that test fixtures which mutates settings after import need to patch
# *this* module explicitly (mirrors old behaviour).

GOOGLE_CLIENT_ID = _settings.google_client_id
JWT_SECRET = _settings.jwt_secret

# Client secret is required for *server-side* OAuth code exchange when users
# connect their Gmail account to enable *email triggers*.  The variable is
# intentionally **optional** so that the regular Google Sign-In flow (which
# only needs the *client ID* for ID-token verification) keeps working in
# existing development setups.

GOOGLE_CLIENT_SECRET = _settings.google_client_secret

# Cookie configuration for browser auth
SESSION_COOKIE_NAME = "swarmlet_session"
SESSION_COOKIE_PATH = "/"
# Secure=True only in production (HTTPS); False in dev for http://localhost
SESSION_COOKIE_SECURE = not _settings.auth_disabled and not _settings.testing


def _set_session_cookie(response: Response, token: str, max_age: int) -> None:
    """Set the session cookie with proper security flags.

    Args:
        response: FastAPI Response object to set cookie on
        token: JWT access token to store in cookie
        max_age: Cookie lifetime in seconds
    """
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
    """Clear the session cookie."""
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path=SESSION_COOKIE_PATH,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
    )


def _verify_google_id_token(id_token_str: str) -> dict[str, Any]:
    """Validate the JWT issued by Google and return the decoded claims.

    Raises HTTPException(401) if the token is invalid or the *aud* claim does
    not match our configured client ID.
    """

    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="GOOGLE_CLIENT_ID not set")

    try:
        # Import google-auth lazily to keep optional.
        from google.auth.transport import requests as google_requests  # type: ignore
        from google.oauth2 import id_token  # type: ignore

        # verify_oauth2_token does signature, expiration, issuer & audience.
        request = google_requests.Request()  # Re-usable HTTP transport
        idinfo = id_token.verify_oauth2_token(id_token_str, request, GOOGLE_CLIENT_ID)
        return idinfo
    except Exception as exc:  # broad but we map to 401 below
        import logging

        logger = logging.getLogger(__name__)
        logger.error(f"Google token validation failed: {exc}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid Google token: {str(exc)}") from exc


def _issue_access_token(
    user_id: int,
    email: str,
    *,
    display_name: Optional[str] = None,
    avatar_url: Optional[str] = None,
    expires_delta: timedelta = timedelta(minutes=30),
) -> str:
    """Return signed HS256 access token including *optional* profile fields.

    The token now embeds ``display_name`` and ``avatar_url`` so that the
    frontend can show basic user information immediately after login without
    an additional round-trip to ``/api/users/me``.  These claims are **optional**
    and omitted if the corresponding values are ``None``.
    """

    from datetime import timezone

    expiry = datetime.now(timezone.utc) + expires_delta
    # Import python-jose lazily to avoid hard dependency during unit tests.
    try:
        from jose import jwt  # type: ignore
    except ModuleNotFoundError:  # pragma: no cover – fallback minimal signer
        import base64
        import hashlib
        import hmac
        import json

        class _MiniJWT:
            @staticmethod
            def _b64(data: bytes) -> bytes:
                return base64.urlsafe_b64encode(data).rstrip(b"=")

            @classmethod
            def encode(cls, payload_: dict[str, Any], secret_: str, algorithm: str = "HS256") -> str:  # noqa: D401 – purpose
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

    # ``exp`` must be an **integer** UNIX timestamp so that json.dumps (used
    # by the lightweight fallback encoder below) can serialise the payload
    # without hitting "Object of type datetime is not JSON serialisable".

    payload = {
        "sub": str(user_id),
        "email": email,
        "exp": int(expiry.timestamp()),
    }

    if display_name is not None:
        payload["display_name"] = display_name

    if avatar_url is not None:
        payload["avatar_url"] = avatar_url
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


# ---------------------------------------------------------------------------
# Gmail *offline_access* helper – Phase-2 Email Triggers
# ---------------------------------------------------------------------------


def _exchange_google_auth_code(auth_code: str, *, redirect_uri: Optional[str] = None) -> dict[str, str]:
    """Exchange an *authorization code* for tokens via Google's OAuth endpoint.

    The function returns the full token response as a dictionary.  We are
    primarily interested in the ``refresh_token`` which we persist so that the
    forthcoming *Email Trigger Service* can fetch short-lived access tokens on
    demand.

    The call requires a **client secret** which must be provided via the
    ``GOOGLE_CLIENT_SECRET`` environment variable.  In unit-tests the function
    is usually monkey-patched so no external HTTP call is performed.
    """

    if GOOGLE_CLIENT_ID is None or GOOGLE_CLIENT_SECRET is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Google OAuth client not configured",
        )

    import json
    import urllib.parse
    import urllib.request

    token_endpoint = "https://oauth2.googleapis.com/token"

    data = {
        "code": auth_code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "grant_type": "authorization_code",
        # The recommended value for SPA → backend hand-off is ``postmessage``.
        # Callers can override by passing an explicit redirect_uri.
        "redirect_uri": redirect_uri or "postmessage",
    }

    # Encode as application/x-www-form-urlencoded
    encoded = urllib.parse.urlencode(data).encode()

    req = urllib.request.Request(
        token_endpoint,
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 – trusted URL
            payload = json.loads(resp.read().decode())
    except Exception as exc:  # pragma: no cover – network failure mapped to 502
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Google token exchange failed") from exc

    if "refresh_token" not in payload:
        # The most common reason is that *offline* access was not requested or
        # the user previously granted access.  We surface a descriptive error
        # so the frontend can re-prompt with `prompt=consent`.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No refresh_token in Google response")

    return payload  # Contains refresh_token, access_token, expires_in, scope, …


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/dev-login", response_model=TokenOut)
def dev_login(response: Response, db: Session = Depends(get_db)) -> TokenOut:
    """Development-only login endpoint that bypasses Google OAuth.

    Only works when AUTH_DISABLED=1 is set in environment.
    Creates/returns a token for dev@local admin user.
    Also sets swarmlet_session cookie for browser auth.
    """
    if not _settings.auth_disabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Dev login only available when AUTH_DISABLED=1")

    # Get or create dev@local user
    user = crud.get_user_by_email(db, "dev@local")
    if not user:
        user = crud.create_user(db, email="dev@local", provider="dev", provider_user_id="dev-user-1", role="ADMIN")

    # Issue platform JWT
    expires_in = 30 * 60  # 30 minutes
    access_token = _issue_access_token(
        user.id,
        user.email,
        display_name=user.display_name or "Dev User",
        avatar_url=user.avatar_url,
    )

    # Set session cookie for browser auth
    _set_session_cookie(response, access_token, expires_in)

    return TokenOut(access_token=access_token, expires_in=expires_in)


@router.post("/google", response_model=TokenOut)
def google_sign_in(response: Response, body: dict[str, str], db: Session = Depends(get_db)) -> TokenOut:  # noqa: D401 – simple name
    """Exchange a Google ID token for a platform access token.

    Expected JSON body: `{ "id_token": "<JWT from Google>" }`.
    Also sets swarmlet_session cookie for browser auth.
    """

    raw_token = body.get("id_token")
    if not raw_token or not isinstance(raw_token, str):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="id_token must be provided")

    # 1. Validate & decode Google token
    claims = _verify_google_id_token(raw_token)
    # Enforce verified emails to protect admin allowlist semantics
    if claims.get("email_verified") is False:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Google email not verified")

    # 2. Upsert (or fetch) user record
    email: str = claims.get("email")  # type: ignore[assignment]
    sub: str = claims.get("sub")  # stable Google user id

    if not email:
        # Should never happen for verified google accounts
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google token missing email claim")

    user = crud.get_user_by_email(db, email)
    settings = get_settings()
    admin_emails = {e.strip().lower() for e in (settings.admin_emails or "").split(",") if e.strip()}
    is_admin = email.lower() in admin_emails

    if not user:
        # Enforce simple signup cap with admin exemption
        # When not testing and not admin, stop creating new users once MAX_USERS reached
        if not settings.testing and not is_admin:
            try:
                total = crud.count_users(db)
            except Exception:  # pragma: no cover – extremely unlikely
                total = 0
            if settings.max_users and total >= settings.max_users:
                # Explicit 403 so frontend can show a friendly message
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sign-ups disabled: user limit reached")

        # Create user; grant ADMIN role if email is in admin list
        role = "ADMIN" if is_admin else "USER"
        user = crud.create_user(db, email=email, provider="google", provider_user_id=sub, role=role)
    else:
        # Existing user – promote to ADMIN if configured and not already admin
        if is_admin and getattr(user, "role", None) != "ADMIN":
            try:
                _ = crud.update_user(db, user.id, display_name=user.display_name)
                # direct SQLAlchemy update for role (update_user doesn't expose role)
                user.role = "ADMIN"  # type: ignore[assignment]
                db.commit()
                db.refresh(user)
            except Exception:  # pragma: no cover – best-effort promotion
                pass

    # 3. Issue platform JWT
    expires_in = 30 * 60  # 30 minutes
    access_token = _issue_access_token(
        user.id,
        user.email,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
    )

    # 4. Set session cookie for browser auth
    _set_session_cookie(response, access_token, expires_in)

    # 5. Return response (JSON for backwards compatibility; browsers use cookie)
    return TokenOut(access_token=access_token, expires_in=expires_in)


@router.get("/verify", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def verify_session(request: Request, db: Session = Depends(get_db)):
    """Fast auth check for nginx auth_request.

    Validates the session from cookie (preferred) or Authorization header.
    Returns 204 if valid, 401 if missing/invalid/expired/user-inactive.

    In development mode (AUTH_DISABLED=1), always returns 204.
    """
    if _settings.auth_disabled:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    from zerg.auth.strategy import _decode_jwt_fallback

    # Try to extract token: prefer cookie, fall back to bearer
    token: str | None = None

    # 1. Check cookie first (browser auth)
    token = request.cookies.get(SESSION_COOKIE_NAME)

    # 2. Fall back to Authorization header (API clients)
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No session")

    # Validate the token (checks signature and expiry)
    try:
        payload = _decode_jwt_fallback(token, JWT_SECRET)
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")

    # Extract user_id and verify user exists and is active
    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")

    user = crud.get_user(db, user_id)
    if user is None or not getattr(user, "is_active", True):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")

    # Valid token and user - 204 response handled by status_code


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def logout(response: Response):
    """Clear the session cookie.

    Returns 204 on success. Safe to call even if not logged in.
    """
    _clear_session_cookie(response)


# ---------------------------------------------------------------------------
# Gmail connection endpoint (Phase-2 Email Triggers)
# ---------------------------------------------------------------------------


@router.post("/google/gmail", status_code=status.HTTP_200_OK)
def connect_gmail(
    body: dict[str, str],
    db: Session = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> dict[str, str | int]:
    """Connect Gmail via OAuth and create/update a Gmail connector.

    Expected body: { "auth_code": "...", "callback_url": "https://.../api/email/webhook/google" }

    - Stores the encrypted refresh token in a Connector (type="email", provider="gmail").
    - Optionally attempts to register a Gmail watch if ``callback_url`` is provided.
    - Returns the ``connector_id``.
    """

    auth_code = body.get("auth_code")
    if not auth_code:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="auth_code missing")

    callback_url = body.get("callback_url")

    # Exchange code for tokens (patched to stub out in unit-tests)
    token_payload = _exchange_google_auth_code(auth_code)
    refresh_token: str = token_payload["refresh_token"]

    # Create or update connector for this user
    from zerg.utils import crypto  # lazy import

    enc = crypto.encrypt(refresh_token)

    # Try to find an existing Gmail connector for this owner
    existing = crud.get_connectors(db, owner_id=current_user.id, type="email", provider="gmail")
    if existing:
        conn = existing[0]
        cfg = dict(conn.config or {})
        cfg["refresh_token"] = enc
        # Clear watch meta; will be re-initialized below when possible
        cfg.pop("history_id", None)
        cfg.pop("watch_expiry", None)
        conn = crud.update_connector(db, conn.id, config=cfg)  # type: ignore[assignment]
        connector_id = conn.id if conn else None
    else:
        try:
            conn = crud.create_connector(
                db,
                owner_id=current_user.id,
                type="email",
                provider="gmail",
                config={"refresh_token": enc},
            )
            connector_id = conn.id
        except Exception:
            # Handle potential uniqueness race: fetch existing and reuse
            existing = crud.get_connectors(db, owner_id=current_user.id, type="email", provider="gmail")
            if not existing:
                raise
            conn = existing[0]
            connector_id = conn.id

    # Optionally start a Gmail watch immediately (best effort)
    try:
        # Derive/validate callback URL server-side for security
        final_callback: str | None = None
        settings = get_settings()
        if settings.app_public_url:
            base = str(settings.app_public_url).rstrip("/")
            final_callback = f"{base}/api/email/webhook/google"
        elif callback_url:
            # In testing we allow arbitrary callback to keep unit-tests simple
            if settings.testing:
                final_callback = callback_url
            else:
                # Minimal validation when APP_PUBLIC_URL is not set – only accept https
                # and the expected webhook path.
                from urllib.parse import urlparse

                try:
                    parsed = urlparse(callback_url)
                    if parsed.scheme == "https" and parsed.path.endswith("/api/email/webhook/google"):
                        final_callback = callback_url
                except Exception:  # pragma: no cover – defensive parsing guard
                    final_callback = None

        if final_callback:
            # Exchange refresh->access token and start watch
            from zerg.services import gmail_api

            access_token = gmail_api.exchange_refresh_token(refresh_token)

            # Get user's email address for Pub/Sub mapping
            try:
                import httpx

                headers = {"Authorization": f"Bearer {access_token}"}
                resp = httpx.get(
                    "https://gmail.googleapis.com/gmail/v1/users/me/profile",
                    headers=headers,
                )
                if resp.status_code == 200:
                    email_address = resp.json().get("emailAddress")
                else:
                    email_address = None
            except Exception:
                email_address = None

            # Prefer Pub/Sub topic if configured; otherwise fall back to legacy
            # callback param for local dev/tests (some tests patch start_watch).
            topic = getattr(settings, "gmail_pubsub_topic", None)
            if topic:
                watch_info = gmail_api.start_watch(access_token=access_token, topic_name=topic)
            else:
                watch_info = gmail_api.start_watch(access_token=access_token, callback_url=final_callback)

            cfg = dict(conn.config or {})
            cfg.update(
                {
                    "history_id": watch_info["history_id"],
                    "watch_expiry": watch_info["watch_expiry"],
                    "emailAddress": email_address,  # Store for Pub/Sub mapping
                }
            )
            crud.update_connector(db, conn.id, config=cfg)
    except Exception:  # pragma: no cover – best-effort; skip network failures
        pass

    return {"status": "connected", "connector_id": int(connector_id)}
