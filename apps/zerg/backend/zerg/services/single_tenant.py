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
from zerg.crud import crud

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Default user email for OSS instances (no config needed)
OSS_DEFAULT_EMAIL = "local@zerg"


class SingleTenantViolation(Exception):
    """Raised when single-tenant invariant is violated (>1 user exists)."""


def validate_single_tenant(db: Session) -> None:
    """Validate that at most one user exists in the database.

    Raises SingleTenantViolation if >1 user exists.
    Called during startup to enforce single-tenant invariant.
    """
    settings = get_settings()
    if not settings.single_tenant:
        return  # Multi-tenant mode, skip validation

    user_count = crud.count_users(db)
    if user_count > 1:
        raise SingleTenantViolation(
            f"Single-tenant violation: {user_count} users exist (expected 0 or 1). "
            f"This Zerg instance is configured for single-tenant mode. "
            f"Delete extra users or disable SINGLE_TENANT to allow multiple users."
        )


def get_owner_email() -> str:
    """Return the email address for the instance owner.

    Priority:
    1. OWNER_EMAIL env var (set by control plane for hosted instances)
    2. ADMIN_EMAILS first entry (legacy/migration support)
    3. OSS_DEFAULT_EMAIL ('local@zerg') for OSS instances
    """
    # Check explicit OWNER_EMAIL first (control plane sets this)
    owner_email = os.getenv("OWNER_EMAIL", "").strip()
    if owner_email:
        return owner_email

    # Fall back to ADMIN_EMAILS first entry (legacy support)
    settings = get_settings()
    if settings.admin_emails:
        first_admin = settings.admin_emails.split(",")[0].strip()
        if first_admin:
            return first_admin

    # Default for OSS instances
    return OSS_DEFAULT_EMAIL


def bootstrap_owner_user(db: Session) -> None:
    """Create the instance owner user if no users exist.

    Called during startup to ensure zero-friction onboarding.
    Does nothing if a user already exists.
    """
    settings = get_settings()
    if not settings.single_tenant:
        return  # Multi-tenant mode, no auto-bootstrap

    user_count = crud.count_users(db)
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
        user = crud.create_user(
            db,
            email=owner_email,
            provider=provider,
            provider_user_id=provider_user_id,
            role="ADMIN",  # Owner is always admin
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
    Always returns True for OSS mode (AUTH_DISABLED or no OWNER_EMAIL set).
    """
    settings = get_settings()

    # In dev/test mode, accept any email
    if settings.auth_disabled or settings.testing:
        return True

    # Check if OWNER_EMAIL is configured
    owner_email = os.getenv("OWNER_EMAIL", "").strip()
    if not owner_email:
        # No owner email configured - accept any email
        # This supports legacy deployments without explicit owner binding
        return True

    # Compare case-insensitively
    return email.strip().lower() == owner_email.lower()


def can_create_user(db: Session) -> bool:
    """Check if a new user can be created (single-tenant enforcement).

    Returns True if:
    - Single-tenant mode is disabled, OR
    - No users exist yet
    """
    settings = get_settings()
    if not settings.single_tenant:
        return True

    return crud.count_users(db) == 0


__all__ = [
    "SingleTenantViolation",
    "validate_single_tenant",
    "get_owner_email",
    "bootstrap_owner_user",
    "is_owner_email",
    "can_create_user",
    "OSS_DEFAULT_EMAIL",
]
