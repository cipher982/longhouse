"""Refresh token lifecycle: generate, rotate, revoke.

Implements rotating refresh tokens with reuse detection, following the
Auth0 / Supabase pattern (RFC 9700 §4.14).

Key invariants:
- Raw tokens never hit the database — only SHA-256 hashes.
- Every successful rotation marks the old token as ``used`` and creates a
  new one in the same family.
- Presenting a *used* token outside the reuse grace window revokes the
  entire family (breach signal).
- Tokens have both an absolute lifetime (hard cap) and an idle timeout
  (sliding window extended on each rotation).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from sqlalchemy import update
from sqlalchemy.orm import Session
from zerg.models.refresh_session import RefreshSession

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (override via env in a future iteration if needed)
# ---------------------------------------------------------------------------

TOKEN_BYTES = 32  # 256-bit random token
ABSOLUTE_LIFETIME = timedelta(days=90)
IDLE_LIFETIME = timedelta(days=30)
REUSE_GRACE_SECONDS = 10  # tolerate concurrent tab refreshes


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def _generate_token() -> str:
    """Return a URL-safe opaque token string."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def _hash_token(raw: str) -> str:
    """Return hex-encoded SHA-256 of the raw token."""
    return hashlib.sha256(raw.encode()).hexdigest()


def _utcnow() -> datetime:
    """Naive UTC — matches SQLite storage (no tzinfo)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RotationResult:
    """Returned by ``rotate`` on success."""

    raw_token: str
    session_id: int
    user_id: int
    family_id: str


def create(db: Session, *, user_id: int, family_id: str | None = None, parent_id: int | None = None) -> str:
    """Create a new refresh token and return the **raw** (unhashed) value.

    The caller is responsible for delivering it to the client (cookie).
    """
    raw = _generate_token()
    now = _utcnow()

    row = RefreshSession(
        token_hash=_hash_token(raw),
        user_id=user_id,
        family_id=family_id or uuid.uuid4().hex,
        parent_id=parent_id,
        created_at=now,
        absolute_expires_at=now + ABSOLUTE_LIFETIME,
        idle_expires_at=now + IDLE_LIFETIME,
    )
    db.add(row)
    db.flush()  # assigns row.id for parent_id chaining
    return raw


def rotate(db: Session, raw_token: str) -> RotationResult | None:
    """Validate ``raw_token``, rotate it, and return a fresh token.

    Returns ``None`` when the token is invalid / expired / revoked.
    Revokes the entire family if reuse is detected outside the grace window.
    """
    token_hash = _hash_token(raw_token)
    now = _utcnow()

    row: RefreshSession | None = db.query(RefreshSession).filter(RefreshSession.token_hash == token_hash).with_for_update().first()

    if row is None:
        return None

    # Already revoked (family breach or logout).
    if row.revoked_at is not None:
        return None

    # Absolute or idle expiry.
    if now > row.absolute_expires_at or now > row.idle_expires_at:
        return None

    # --- Reuse detection ---
    if row.used_at is not None:
        elapsed = (now - row.used_at).total_seconds()
        if elapsed > REUSE_GRACE_SECONDS:
            # Breach: revoke the whole family.
            _revoke_family(db, row.family_id, now)
            logger.warning(
                "Refresh token reuse detected — family %s revoked (user %d, elapsed %.1fs)",
                row.family_id,
                row.user_id,
                elapsed,
            )
            return None

        # Within grace window: return the token that already replaced this one
        # (idempotent retry from a concurrent tab).
        child: RefreshSession | None = (
            db.query(RefreshSession)
            .filter(
                RefreshSession.parent_id == row.id,
                RefreshSession.revoked_at.is_(None),
            )
            .first()
        )
        if child is None:
            return None

        # Re-derive the raw token? We can't — we only store hashes.
        # The child was already issued; the caller that triggered this
        # concurrent rotation already received the new cookie.  Return
        # None so this caller retries once more and picks up the cookie
        # from the other response.
        return None

    # --- Normal rotation ---
    row.used_at = now

    new_raw = _generate_token()
    new_row = RefreshSession(
        token_hash=_hash_token(new_raw),
        user_id=row.user_id,
        family_id=row.family_id,
        parent_id=row.id,
        created_at=now,
        absolute_expires_at=row.absolute_expires_at,  # inherit family ceiling
        idle_expires_at=now + IDLE_LIFETIME,
    )
    db.add(new_row)
    db.flush()

    return RotationResult(
        raw_token=new_raw,
        session_id=new_row.id,
        user_id=row.user_id,
        family_id=row.family_id,
    )


def revoke_family(db: Session, family_id: str) -> int:
    """Revoke all tokens in a family (logout / admin action).

    Returns the number of rows affected.
    """
    return _revoke_family(db, family_id, _utcnow())


def revoke_all_for_user(db: Session, user_id: int) -> int:
    """Revoke every refresh session for a user (password change, etc.)."""
    now = _utcnow()
    count = (
        db.execute(
            update(RefreshSession).where(RefreshSession.user_id == user_id, RefreshSession.revoked_at.is_(None)).values(revoked_at=now)
        )
    ).rowcount
    db.flush()
    return count


def cleanup_expired(db: Session, *, batch_size: int = 500) -> int:
    """Delete rows that are fully expired or revoked.

    Called periodically (e.g. daily job) to keep the table small.
    """
    cutoff = _utcnow()
    subq = (
        db.query(RefreshSession.id)
        .filter((RefreshSession.absolute_expires_at < cutoff) | (RefreshSession.revoked_at.isnot(None)))
        .limit(batch_size)
        .subquery()
    )
    count = db.query(RefreshSession).filter(RefreshSession.id.in_(subq)).delete(synchronize_session=False)
    db.flush()
    return count


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


def _revoke_family(db: Session, family_id: str, now: datetime) -> int:
    count = (
        db.execute(
            update(RefreshSession).where(RefreshSession.family_id == family_id, RefreshSession.revoked_at.is_(None)).values(revoked_at=now)
        )
    ).rowcount
    db.flush()
    return count
