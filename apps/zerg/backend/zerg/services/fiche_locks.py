"""
PostgreSQL advisory lock-based fiche concurrency control.

This module implements proper distributed locking using PostgreSQL advisory locks,
which automatically release when the database session terminates, eliminating the
stuck fiche bug entirely.

This is the proper architectural solution based on distributed systems research.
"""

import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class FicheLockManager:
    """
    Manages fiche concurrency using PostgreSQL advisory locks.

    Advisory locks are session-scoped and automatically released when:
    1. The session/connection terminates (normal shutdown or crash)
    2. The connection is lost

    Note: session-level advisory locks are NOT released on transaction rollback.
    To keep a simple boolean contract we also guard against re-entrancy within
    the same DB session (second acquire returns False).
    """

    @staticmethod
    def acquire_fiche_lock(db: Session, fiche_id: int) -> bool:
        """
        Acquire an advisory lock for a fiche.

        Uses PostgreSQL pg_try_advisory_lock which:
        - Returns immediately (non-blocking)
        - Returns True if lock acquired, False if already held
        - Automatically releases on session termination

        Args:
            db: Database session
            fiche_id: ID of the fiche to lock

        Returns:
            True if lock was acquired, False if already held by another session
        """
        try:
            # Guard against re-entrancy within the same DB session. PostgreSQL
            # allows session-level advisory locks to be acquired multiple times
            # by the same session; to keep a simple boolean contract we treat
            # a second acquisition attempt from the same session as "not acquired".
            already_held = db.execute(
                text(
                    """
                    SELECT 1
                    FROM pg_locks
                    WHERE locktype = 'advisory'
                      AND granted = true
                      AND pid = pg_backend_pid()
                      AND ((classid::bigint << 32) | objid::bigint) = :fiche_id
                    LIMIT 1
                    """
                ),
                {"fiche_id": int(fiche_id)},
            ).scalar()

            if already_held:
                logger.debug(f"⚠️ Advisory lock for fiche {fiche_id} already held by this session")
                return False

            result = db.execute(
                text("SELECT pg_try_advisory_lock(:fiche_id)"),
                {"fiche_id": int(fiche_id)},
            )
            acquired = result.scalar()

            if acquired:
                logger.debug(f"✅ Acquired advisory lock for fiche {fiche_id}")
            else:
                logger.debug(f"⚠️ Fiche {fiche_id} is already locked by another session")

            return bool(acquired)

        except Exception as e:
            logger.error(f"❌ Failed to acquire advisory lock for fiche {fiche_id}: {e}")
            return False

    @staticmethod
    def release_fiche_lock(db: Session, fiche_id: int) -> bool:
        """
        Release an advisory lock for a fiche.

        Args:
            db: Database session
            fiche_id: ID of the fiche to unlock

        Returns:
            True if lock was released, False if not held by this session
        """
        try:
            result = db.execute(text("SELECT pg_advisory_unlock(:fiche_id)"), {"fiche_id": fiche_id})
            released = result.scalar()

            if released:
                logger.debug(f"✅ Released advisory lock for fiche {fiche_id}")
            else:
                logger.warning(f"⚠️ Attempted to release advisory lock for fiche {fiche_id} but it wasn't held by this session")

            return bool(released)

        except Exception as e:
            logger.error(f"❌ Failed to release advisory lock for fiche {fiche_id}: {e}")
            return False

    @staticmethod
    @contextmanager
    def fiche_lock(db: Session, fiche_id: int) -> Generator[bool, None, None]:
        """
        Context manager for fiche advisory locks.

        Usage:
            with FicheLockManager.fiche_lock(db, fiche_id) as acquired:
                if acquired:
                    # Do work with exclusive access to fiche
                    pass
                else:
                    # Fiche is already running
                    raise ValueError("Fiche already running")

        The lock is automatically released when the context exits,
        even if an exception occurs.

        Args:
            db: Database session
            fiche_id: ID of the fiche to lock

        Yields:
            bool: True if lock was acquired, False otherwise
        """
        acquired = FicheLockManager.acquire_fiche_lock(db, fiche_id)

        try:
            yield acquired
        finally:
            if acquired:
                FicheLockManager.release_fiche_lock(db, fiche_id)

    @staticmethod
    def get_locked_fiches(db: Session) -> list[int]:
        """
        Get list of currently locked fiche IDs.

        This queries pg_locks to see which advisory locks are currently held.

        Args:
            db: Database session

        Returns:
            List of fiche IDs that are currently locked
        """
        try:
            # For pg_try_advisory_lock(bigint) the lock key is split across
            # classid (high 32 bits) and objid (low 32 bits). Reconstruct the
            # original bigint to match the fiche_id we lock against.
            result = db.execute(
                text(
                    """
                    SELECT ((classid::bigint << 32) | objid::bigint) AS fiche_id
                    FROM pg_locks
                    WHERE locktype = 'advisory'
                      AND granted = true
                    ORDER BY fiche_id
                    """
                )
            )

            locked_fiches = [int(row[0]) for row in result.fetchall()]
            logger.debug(f"Currently locked fiches: {locked_fiches}")

            return locked_fiches

        except Exception as e:
            logger.error(f"❌ Failed to get locked fiches: {e}")
            return []


# Convenience helpers for fiche lock usage


def acquire_fiche_lock_advisory(db, fiche_id: int) -> bool:
    """Acquire a fiche advisory lock (thin wrapper for FicheLockManager)."""
    return FicheLockManager.acquire_fiche_lock(db, fiche_id)


def release_fiche_lock_advisory(db, fiche_id: int) -> bool:
    """Release a fiche advisory lock (thin wrapper for FicheLockManager)."""
    return FicheLockManager.release_fiche_lock(db, fiche_id)
