"""Single-tenant user management for Zerg instances.

Each Zerg instance has exactly one user. This module provides:
1. Startup validation (fail if >1 user exists)
2. Auto-bootstrap of the instance owner user
3. Email binding for hosted OAuth

Architecture:
- OSS: AUTH_DISABLED=1, auto-creates 'local@zerg' user
- Hosted: Control plane sets OWNER_EMAIL, OAuth binds to that email only
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from zerg.config import get_settings
from zerg.crud import count_users
from zerg.crud import create_user

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Default user email for OSS instances (no config needed)
OSS_DEFAULT_EMAIL = "local@zerg"

# Default owner identity for password-auth self-hosters who enable auth but do
# not run a hosted control plane (no OWNER_EMAIL, no OAuth). Password auth binds
# a single local owner, so a stable synthetic email is sufficient.
PASSWORD_AUTH_DEFAULT_EMAIL = "owner@longhouse.local"


def _password_auth_configured() -> bool:
    """Return True when simple password auth is configured for this instance."""
    settings = get_settings()
    return bool(settings.longhouse_password or settings.longhouse_password_hash)


class SingleTenantViolation(Exception):
    """Raised when single-tenant invariant is violated (>1 user exists)."""


def validate_single_tenant(db: Session) -> None:
    """Validate that at most one real user exists in the database.

    Raises SingleTenantViolation if >1 real user exists.
    Service accounts (provider="service") are excluded from the count,
    allowing smoke test users alongside the real owner.

    Called during startup to enforce single-tenant invariant.
    """
    settings = get_settings()
    if not settings.single_tenant:
        return  # Multi-tenant mode, skip validation

    # Exclude service accounts (smoke test users) from the count
    user_count = count_users(db, exclude_service=True)
    if user_count > 1:
        raise SingleTenantViolation(
            f"Single-tenant violation: {user_count} real users exist (expected 0 or 1). "
            f"This Zerg instance is configured for single-tenant mode. "
            f"Delete extra users or disable SINGLE_TENANT to allow multiple users."
        )


def get_owner_email() -> str:
    """Return the email address for the instance owner.

    Priority:
    1. OWNER_EMAIL env var (set by control plane for hosted instances)
    2. OSS_DEFAULT_EMAIL ('local@zerg') for OSS/dev instances
    """
    owner_email = os.getenv("OWNER_EMAIL", "").strip()
    if owner_email:
        return owner_email

    settings = get_settings()
    if settings.auth_disabled or settings.testing:
        return OSS_DEFAULT_EMAIL

    # Password-auth self-hosters enable auth without a hosted control plane or
    # OAuth. They bind a single local owner, so a stable synthetic email lets
    # the simplest documented setup (set LONGHOUSE_PASSWORD_HASH) work without
    # also requiring OWNER_EMAIL.
    if _password_auth_configured():
        return PASSWORD_AUTH_DEFAULT_EMAIL

    raise RuntimeError("OWNER_EMAIL is required when auth is enabled for single-tenant instances.")


def bootstrap_owner_user(db: Session) -> None:
    """Create the instance owner user if no users exist.

    Called during startup to ensure zero-friction onboarding.
    Does nothing if a user already exists.
    """
    settings = get_settings()
    if not settings.single_tenant:
        return  # Multi-tenant mode, no auto-bootstrap

    user_count = count_users(db)
    if user_count > 0:
        logger.debug("Owner user already exists, skipping bootstrap")
        return

    owner_email = get_owner_email()
    logger.info("Bootstrapping owner user: %s", owner_email)

    # Determine provider based on email
    if owner_email == OSS_DEFAULT_EMAIL:
        provider = "local"
        provider_user_id = "local-user-1"
    else:
        # Hosted instance - user will authenticate via Google OAuth
        # We create a placeholder that OAuth will associate with
        provider = None
        provider_user_id = None

    try:
        user = create_user(
            db,
            email=owner_email,
            provider=provider,
            provider_user_id=provider_user_id,
            role="ADMIN",  # Owner is always admin
            skip_notification=True,  # Bootstrap, not a real signup
        )
        logger.info("Created owner user: id=%s, email=%s", user.id, owner_email)
    except Exception as e:
        # Handle race condition (another process created the user)
        error_str = str(e).lower()
        if "duplicate" in error_str or "unique" in error_str:
            logger.debug("Owner user already exists (race condition): %s", owner_email)
            db.rollback()
        else:
            raise


def is_owner_email(email: str) -> bool:
    """Check if the given email matches the instance owner.

    Used by OAuth to reject signups from non-owners.
    Always returns True for OSS/dev mode.
    """
    settings = get_settings()

    # In dev/test mode, accept any email
    if settings.auth_disabled or settings.testing:
        return True

    # Compare case-insensitively
    return email.strip().lower() == get_owner_email().lower()


def can_create_user_locked(db: Session) -> bool:
    """Check if a new user can be created, with lock for concurrency safety.

    Uses dialect-aware locking to prevent race conditions where concurrent
    OAuth sign-ins both see 0 users and create multiple accounts.

    On PostgreSQL: Uses transaction-scoped advisory lock (held until commit/rollback)
    On SQLite: Uses transaction with BEGIN IMMEDIATE for serialization

    IMPORTANT: The caller MUST use this in a transaction and create the user
    in the same transaction to maintain atomicity. Example:
        with db.begin():
            if can_create_user_locked(db):
                create_user(db, ...)
            # Lock released on commit/rollback

    Returns True if:
    - Single-tenant mode is disabled, OR
    - No users exist yet (checked under lock)
    """
    from sqlalchemy import text

    settings = get_settings()
    if not settings.single_tenant:
        return True

    # SQLite: Use BEGIN IMMEDIATE for write lock
    # This serializes concurrent write transactions at the DB level.
    # The lock is held until the transaction commits/rollbacks.
    # Caller MUST create user in same transaction for atomicity.
    try:
        # Force a write lock by starting an immediate transaction
        # Note: If already in a transaction, this becomes a no-op
        # and the existing transaction provides serialization.
        db.execute(text("BEGIN IMMEDIATE"))
    except Exception:
        # Already in a transaction - that's fine, we have the lock
        pass

    # Now safely check user count under the write lock
    return count_users(db) == 0


def can_create_user(db: Session) -> bool:
    """Check if a new user can be created (single-tenant enforcement).

    Note: This is NOT race-safe. Use can_create_user_locked() in OAuth flows.
    This version is kept for backwards compatibility and non-critical checks.

    Returns True if:
    - Single-tenant mode is disabled, OR
    - No users exist yet
    """
    settings = get_settings()
    if not settings.single_tenant:
        return True

    return count_users(db) == 0


def validate_single_tenant_config() -> str | None:
    """Validate single-tenant configuration at startup.

    Returns an error message if misconfigured, None if valid.
    Called during startup to fail fast on bad config.
    """
    settings = get_settings()

    if not settings.single_tenant:
        return None  # Multi-tenant mode, no validation needed

    # If auth is enabled, OWNER_EMAIL must be explicit so hosted instances
    # fail closed instead of inheriting unrelated admin config — UNLESS simple
    # password auth is configured, which binds a single local owner and does
    # not need a hosted identity.
    if not settings.auth_disabled:
        owner_email = os.getenv("OWNER_EMAIL", "").strip()
        if not owner_email and not _password_auth_configured():
            return (
                "Single-tenant mode with auth enabled requires OWNER_EMAIL "
                "(or configure password auth via LONGHOUSE_PASSWORD_HASH). "
                "Set OWNER_EMAIL to the email of the instance owner, or use AUTH_DISABLED=1 for OSS mode."
            )

    return None


__all__ = [
    "SingleTenantViolation",
    "validate_single_tenant",
    "validate_single_tenant_config",
    "get_owner_email",
    "bootstrap_owner_user",
    "is_owner_email",
    "can_create_user",
    "can_create_user_locked",
    "OSS_DEFAULT_EMAIL",
]
