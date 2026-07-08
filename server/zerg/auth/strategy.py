"""Authentication strategy abstraction for the Longhouse backend.

This module formalises the authentication flow behind a small *strategy*
interface so that the actual logic can be swapped depending on the runtime
configuration (development bypass vs. production JWT validation).

The previous implementation in ``zerg.dependencies.auth`` mixed two modes in
conditionals which complicated unit-testing and violated *single-responsibility*.
By extracting **DevAuthStrategy** and **JWTAuthStrategy** into discrete
classes we can now:

• Decide once at *startup* which branch to use – no per-request branching.
• Monkey-patch :pydata:`zerg.dependencies.auth._strategy` in tests to inject
  custom behaviour.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from abc import ABC
from abc import abstractmethod
from typing import Any
from typing import Optional
from urllib.parse import urlparse

from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session
from zerg.auth.cp_jwks import CPTokenClaims
from zerg.auth.cp_jwks import CPTokenError
from zerg.auth.cp_jwks import verify_runtime_token
from zerg.config import get_settings
from zerg.crud import count_users
from zerg.crud import create_user
from zerg.crud import get_user
from zerg.crud import get_user_by_email
from zerg.utils.time import utc_now
from zerg.utils.time import utc_now_naive

# Cookie name for browser-based auth (must match routers/auth.py)
SESSION_COOKIE_NAME = "longhouse_session"
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Minimal HS256 JWT decoding fallback (keeps CI lightweight)
# ---------------------------------------------------------------------------


def _b64url_decode(data: str) -> bytes:  # pragma: no cover – helper
    """Decode *URL-safe* base64, adding padding if required."""

    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _decode_jwt_fallback(token: str, secret: str) -> dict[str, Any]:  # pragma: no cover
    """Very small HS256 validator used when *python-jose* is unavailable."""

    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
    except ValueError as exc:  # noqa: BLE001 – malformed token
        raise ValueError("Invalid JWT structure") from exc

    signing_input = f"{header_b64}.{payload_b64}".encode()
    signature = _b64url_decode(signature_b64)
    expected_sig = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()

    if not hmac.compare_digest(signature, expected_sig):
        raise ValueError("Invalid signature")

    try:
        payload: dict[str, Any] = json.loads(_b64url_decode(payload_b64))
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid payload JSON") from exc

    exp_ts_raw: Optional[float] = None
    if isinstance(payload.get("exp"), (int, float)):
        exp_ts_raw = float(payload["exp"])

    if exp_ts_raw is not None and utc_now().timestamp() > exp_ts_raw:
        raise ValueError("Token expired")

    return payload


# ---------------------------------------------------------------------------
# Strategy base-class
# ---------------------------------------------------------------------------


class AuthStrategy(ABC):
    """Pluggable authentication backend (strategy pattern)."""

    @abstractmethod
    def get_current_user(self, request: Request, db: Session):  # noqa: D401 – abstract
        """Return the authenticated user or raise **401**."""

    @abstractmethod
    def validate_ws_token(self, token: str | None, db: Session):  # noqa: D401 – abstract
        """Return user for valid token, *None* otherwise (WS handshake)."""


# ---------------------------------------------------------------------------
# Development-mode bypass
# ---------------------------------------------------------------------------


class DevAuthStrategy(AuthStrategy):
    """Bypass all checks – used when *AUTH_DISABLED* is true or in tests."""

    DEV_EMAIL = "dev@local"

    def __init__(self):
        self._settings = get_settings()

    # Internal helpers --------------------------------------------------

    def _get_or_create_dev_user(self, db: Session):
        import os

        from zerg.models.models import User

        # Skip database operations in unit test mode (NODE_ENV=test WITHOUT E2E)
        # E2E tests need real database operations for full integration testing
        env = os.getenv("ENVIRONMENT", "")
        is_e2e_env = "e2e" in env.lower() or os.getenv("E2E_DB_DIR") or os.getenv("E2E_DEFAULT_MODEL") or os.getenv("E2E_HATCH_PATH")
        is_unit_test = os.getenv("NODE_ENV") == "test" and not is_e2e_env
        if is_unit_test:
            # Return a mock user for unit tests to avoid database issues

            mock_user = User()
            mock_user.id = 1
            mock_user.email = self.DEV_EMAIL
            mock_user.role = "ADMIN" if self._settings.dev_admin else "USER"
            mock_user.is_active = True
            mock_user.provider = "dev"
            mock_user.provider_user_id = "test-user-1"
            mock_user.display_name = "Test User"
            mock_user.avatar_url = None
            mock_user.prefs = {}
            mock_user.last_login = None
            mock_user.created_at = utc_now_naive()
            mock_user.updated_at = utc_now_naive()
            return mock_user

        desired_role = "ADMIN" if self._settings.dev_admin else "USER"

        # Single-tenant mode: use existing owner user if one exists
        if self._settings.single_tenant:
            from zerg.services.single_tenant import OSS_DEFAULT_EMAIL
            from zerg.services.single_tenant import get_owner_email

            owner_email = get_owner_email()

            # First check if any user exists
            user_count = count_users(db)
            if user_count > 0:
                # Try to get the owner user
                user = get_user_by_email(db, owner_email)
                if user is not None:
                    return user
                # Fall back to first user if owner email doesn't match
                # (handles migration scenarios)
                first_user = db.query(User).first()
                if first_user is not None:
                    return first_user

            # No users exist - create the owner user
            try:
                return create_user(
                    db,
                    email=owner_email,
                    provider="local" if owner_email == OSS_DEFAULT_EMAIL else None,
                    provider_user_id="local-user-1" if owner_email == OSS_DEFAULT_EMAIL else None,
                    role=desired_role,
                    skip_notification=True,
                )
            except Exception as e:  # noqa: BLE001 – catch IntegrityError from any DB driver
                error_str = str(e).lower()
                if "duplicate" in error_str or "unique" in error_str:
                    db.rollback()
                    user = get_user_by_email(db, owner_email)
                    if user is not None:
                        return user
                raise

        # Legacy multi-tenant dev mode: use dev@local user
        user = get_user_by_email(db, self.DEV_EMAIL)
        if user is not None:
            if getattr(user, "role", "USER") != desired_role:
                user.role = desired_role  # type: ignore[attr-defined]
                db.commit()
                db.refresh(user)
            return user

        # Handle race condition: another process may create the user between our
        # get_user_by_email() check and create_user() call. Catch the integrity
        # error and re-fetch.
        try:
            return create_user(db, email=self.DEV_EMAIL, provider=None, role=desired_role, skip_notification=True)
        except Exception as e:  # noqa: BLE001 – catch IntegrityError from any DB driver
            # Check if it's a duplicate key error (concurrent creation race)
            error_str = str(e).lower()
            if "duplicate" in error_str or "unique" in error_str:
                db.rollback()
                user = get_user_by_email(db, self.DEV_EMAIL)
                if user is not None:
                    return user
            raise

    # Public API --------------------------------------------------------

    def get_current_user(self, request: Request, db: Session):  # noqa: D401 – impl
        auth_header = request.headers.get("Authorization")

        # If *no* header provided we still allow access for almost all paths.
        if not auth_header:
            if "/mcp-servers" in request.url.path:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
            return self._get_or_create_dev_user(db)

        # Header present → always return dev user regardless of its content.
        return self._get_or_create_dev_user(db)

    def validate_ws_token(self, token: str | None, db: Session):  # noqa: D401 – impl
        return self._get_or_create_dev_user(db)


# ---------------------------------------------------------------------------
# HS256 JWT validation (production)
# ---------------------------------------------------------------------------


class JWTAuthStrategy(AuthStrategy):
    """Production strategy that validates HS256 tokens."""

    def __init__(self):
        self._secret = get_settings().jwt_secret

    # Internal ----------------------------------------------------------

    def _decode(self, token: str) -> dict[str, Any]:  # noqa: D401 – helper
        try:
            from jose import jwt  # type: ignore

            return jwt.decode(token, self._secret, algorithms=["HS256"])
        except ModuleNotFoundError:
            return _decode_jwt_fallback(token, self._secret)

    # Internal ----------------------------------------------------------

    def _extract_token(self, request: Request) -> str | None:
        """Extract JWT from request: prefer bearer header, fall back to cookie.

        Order:
        1. Authorization: Bearer <token> header (for API clients)
        2. longhouse_session cookie (for browser auth)
        """
        # 1. Check Authorization header first
        auth_header: str | None = request.headers.get("Authorization")
        if auth_header and auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
            if token:
                return token

        # 2. Fall back to session cookie (browser auth)
        return request.cookies.get(SESSION_COOKIE_NAME)

    # Public API --------------------------------------------------------

    def get_current_user(self, request: Request, db: Session):  # noqa: D401 – impl
        token = self._extract_token(request)
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing bearer token or session cookie",
            )

        if token.startswith("zdt_"):
            user = _resolve_device_token_user(token, db)
            if user is None:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or revoked device token")
            return user

        try:
            payload = self._decode(token)
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

        try:
            user_id_int = int(payload.get("sub"))
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token subject")

        user = get_user(db, user_id_int)
        if user is None or not getattr(user, "is_active", True):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")

        if getattr(user, "last_login", None) is None:
            user.last_login = utc_now_naive()  # type: ignore[attr-defined]
            db.commit()

        return user

    def validate_ws_token(self, token: str | None, db: Session):  # noqa: D401 – impl
        if not token:
            return None

        if token.startswith("zdt_"):
            return _resolve_device_token_user(token, db)

        try:
            payload = self._decode(token)
        except Exception:  # noqa: BLE001
            return None

        try:
            user_id_int = int(payload.get("sub"))
        except Exception:  # noqa: BLE001
            return None

        user = get_user(db, user_id_int)
        if user is None or not getattr(user, "is_active", True):
            return None

        return user


class HostedCPAuthStrategy(AuthStrategy):
    """Hosted strategy that validates CP-issued RS256 runtime tokens."""

    def __init__(self):
        self._settings = get_settings()
        self._audience = _hosted_audience(self._settings)

    def _extract_token(self, request: Request) -> str | None:
        auth_header: str | None = request.headers.get("Authorization")
        if auth_header and auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
            if token:
                return token
        return request.cookies.get(SESSION_COOKIE_NAME)

    def _resolve_claims_user(self, db: Session, claims: CPTokenClaims):
        from zerg.models.models import User

        changed = False
        user = db.query(User).filter(User.cp_user_id == claims.cp_user_id).first()
        if user is None:
            existing = get_user_by_email(db, claims.email)
            if existing is not None:
                if not claims.email_verified:
                    logger.warning(
                        "Refusing to link unverified CP user %s to existing tenant email %s",
                        claims.cp_user_id,
                        claims.email,
                    )
                    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Email must be verified")
                if getattr(existing, "cp_user_id", None) not in (None, claims.cp_user_id):
                    logger.warning(
                        "account_link_conflict: CP user %s email %s maps to local user %s already linked to CP %s",
                        claims.cp_user_id,
                        claims.email,
                        existing.id,
                        existing.cp_user_id,
                    )
                    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account link conflict")
                user = existing
            else:
                user = create_user(
                    db,
                    email=claims.email,
                    provider="control-plane",
                    provider_user_id=f"cp:{claims.cp_user_id}",
                    skip_notification=True,
                )

            if user.cp_user_id != claims.cp_user_id:
                user.cp_user_id = claims.cp_user_id
                changed = True
            if user.provider != "control-plane":
                user.provider = "control-plane"
                changed = True
            provider_user_id = f"cp:{claims.cp_user_id}"
            if user.provider_user_id != provider_user_id:
                user.provider_user_id = provider_user_id
                changed = True

        if user.email != claims.email:
            other = get_user_by_email(db, claims.email)
            if other is not None and int(other.id) != int(user.id):
                logger.warning(
                    "account_link_conflict: CP user %s email update %s collides with local user %s",
                    claims.cp_user_id,
                    claims.email,
                    other.id,
                )
            else:
                user.email = claims.email
                changed = True

        display_name = claims.display_name or user.display_name
        if user.display_name != display_name:
            user.display_name = display_name
            changed = True
        if user.avatar_url != claims.avatar_url:
            user.avatar_url = claims.avatar_url
            changed = True
        if user.email_verified != claims.email_verified:
            user.email_verified = claims.email_verified
            changed = True
        if user.is_active is not True:
            user.is_active = True
            changed = True
        if getattr(user, "last_login", None) is None:
            user.last_login = utc_now_naive()
            changed = True
        if changed:
            db.commit()
            db.refresh(user)
        return user

    def _user_from_token(self, token: str, db: Session):
        if token.startswith("zdt_"):
            user = _resolve_device_token_user(token, db)
            if user is None:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or revoked device token")
            return user
        try:
            claims = verify_runtime_token(token, audience=self._audience)
        except CPTokenError:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
        return self._resolve_claims_user(db, claims)

    def get_current_user(self, request: Request, db: Session):  # noqa: D401 – impl
        token = self._extract_token(request)
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing bearer token or session cookie",
            )
        return self._user_from_token(token, db)

    def validate_ws_token(self, token: str | None, db: Session):  # noqa: D401 – impl
        if not token:
            return None
        try:
            return self._user_from_token(token, db)
        except HTTPException:
            return None


def _resolve_device_token_user(token: str, db: Session):
    """Resolve a `zdt_...` device token to its owner User row, or None.

    Lets the local dev proxy (and any other tooling that already holds a
    device token) authenticate browser API routes as the token's owner.
    """
    from zerg.routers.device_tokens import validate_device_token

    device_token = validate_device_token(token, db)
    if device_token is None:
        return None
    user = get_user(db, int(device_token.owner_id))
    if user is None or not getattr(user, "is_active", True):
        return None
    return user


def _hosted_audience(settings) -> str:
    instance_id = os.getenv("INSTANCE_ID", "").strip()
    if instance_id:
        return instance_id
    public_url = settings.app_public_url or settings.public_site_url or ""
    host = urlparse(public_url).hostname or ""
    if host:
        return host.split(".")[0]
    raise RuntimeError("Hosted auth requires INSTANCE_ID or APP_PUBLIC_URL")


# Public re-exports ---------------------------------------------------------


__all__ = [
    "AuthStrategy",
    "DevAuthStrategy",
    "HostedCPAuthStrategy",
    "JWTAuthStrategy",
]
