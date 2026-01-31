"""
Dialect-aware fiche concurrency control.

This module implements distributed locking for fiches that works on both
PostgreSQL (using advisory locks) and SQLite (using status column pattern).

PostgreSQL advisory locks automatically release when the session terminates.
SQLite uses a resource_locks table with heartbeat-based stale detection.
"""

import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import text
from sqlalchemy.orm import Session

from zerg.db_utils import _generate_holder_id
from zerg.db_utils import acquire_resource_lock
from zerg.db_utils import get_active_locks_sqlite
from zerg.db_utils import is_sqlite_session
from zerg.db_utils import release_resource_lock
from zerg.db_utils import resource_lock

logger = logging.getLogger(__name__)

# Lock type constant for fiche locks
FICHE_LOCK_TYPE = "fiche"


class FicheLockManager:
    """
    Manages fiche concurrency using dialect-aware locking.

    On PostgreSQL: Uses advisory locks (session-scoped, auto-released on disconnect)
    On SQLite: Uses resource_locks table with heartbeat-based stale detection

    Note: For PostgreSQL, session-level advisory locks are NOT released on
    transaction rollback. To keep a simple boolean contract we also guard
    against re-entrancy within the same DB session (second acquire returns False).
    """

    @staticmethod
    def acquire_fiche_lock(db: Session, fiche_id: int, holder_id: str | None = None) -> tuple[bool, str]:
        """
        Acquire a lock for a fiche.

        Args:
            db: Database session
            fiche_id: ID of the fiche to lock
            holder_id: Optional holder identifier (defaults to auto-generated unique ID)

        Returns:
            Tuple of (acquired: bool, holder_id: str).
            The holder_id is needed to release SQLite locks.
        """
        holder = holder_id or _generate_holder_id()

        try:
            if is_sqlite_session(db):
                # SQLite: use resource_lock table
                acquired = acquire_resource_lock(db, FICHE_LOCK_TYPE, str(fiche_id), holder, blocking=False)
                if acquired:
                    logger.debug(f"Acquired SQLite lock for fiche {fiche_id}")
                else:
                    logger.debug(f"Fiche {fiche_id} is already locked (SQLite)")
                return acquired, holder

            # PostgreSQL: use advisory locks with re-entrancy guard
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
                logger.debug(f"Advisory lock for fiche {fiche_id} already held by this session")
                return False, holder

            result = db.execute(
                text("SELECT pg_try_advisory_lock(:fiche_id)"),
                {"fiche_id": int(fiche_id)},
            )
            acquired = result.scalar()

            if acquired:
                logger.debug(f"Acquired advisory lock for fiche {fiche_id}")
            else:
                logger.debug(f"Fiche {fiche_id} is already locked by another session")

            return bool(acquired), holder

        except Exception as e:
            logger.error(f"Failed to acquire lock for fiche {fiche_id}: {e}")
            return False, holder

    @staticmethod
    def release_fiche_lock(db: Session, fiche_id: int, holder_id: str) -> bool:
        """
        Release a lock for a fiche.

        Args:
            db: Database session
            fiche_id: ID of the fiche to unlock
            holder_id: The holder identifier returned from acquire_fiche_lock()

        Returns:
            True if lock was released, False if not held by this session
        """
        holder = holder_id

        try:
            if is_sqlite_session(db):
                # SQLite: delete from resource_locks table
                released = release_resource_lock(db, FICHE_LOCK_TYPE, str(fiche_id), holder)
                if released:
                    logger.debug(f"Released SQLite lock for fiche {fiche_id}")
                else:
                    logger.warning(f"Fiche {fiche_id} lock not held by {holder} (SQLite)")
                return released

            # PostgreSQL: release advisory lock
            result = db.execute(
                text("SELECT pg_advisory_unlock(:fiche_id)"),
                {"fiche_id": fiche_id},
            )
            released = result.scalar()

            if released:
                logger.debug(f"Released advisory lock for fiche {fiche_id}")
            else:
                logger.warning(f"Advisory lock for fiche {fiche_id} not held by this session")

            return bool(released)

        except Exception as e:
            logger.error(f"Failed to release lock for fiche {fiche_id}: {e}")
            return False

    @staticmethod
    @contextmanager
    def fiche_lock(db: Session, fiche_id: int, holder_id: str | None = None) -> Generator[bool, None, None]:
        """
        Context manager for fiche locks.

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
            holder_id: Optional holder identifier (auto-generated if not provided)

        Yields:
            bool: True if lock was acquired, False otherwise
        """
        # Generate holder_id once upfront for consistent acquire/release
        holder = holder_id or _generate_holder_id()

        if is_sqlite_session(db):
            # Use the unified resource_lock context manager for SQLite
            with resource_lock(db, FICHE_LOCK_TYPE, str(fiche_id), holder) as acquired:
                yield acquired
        else:
            # PostgreSQL: use direct acquire/release for advisory locks
            acquired, actual_holder = FicheLockManager.acquire_fiche_lock(db, fiche_id, holder)
            try:
                yield acquired
            finally:
                if acquired:
                    FicheLockManager.release_fiche_lock(db, fiche_id, actual_holder)

    @staticmethod
    def get_locked_fiches(db: Session) -> list[int]:
        """
        Get list of currently locked fiche IDs.

        Args:
            db: Database session

        Returns:
            List of fiche IDs that are currently locked
        """
        try:
            if is_sqlite_session(db):
                # SQLite: query resource_locks table
                locks = get_active_locks_sqlite(db, FICHE_LOCK_TYPE)
                locked_fiches = [int(lock["lock_key"]) for lock in locks]
                logger.debug(f"Currently locked fiches (SQLite): {locked_fiches}")
                return locked_fiches

            # PostgreSQL: query pg_locks for advisory locks
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
            logger.error(f"Failed to get locked fiches: {e}")
            return []


# Convenience helpers for fiche lock usage


def acquire_fiche_lock_advisory(db: Session, fiche_id: int) -> tuple[bool, str]:
    """Acquire a fiche lock (thin wrapper for FicheLockManager).

    Returns:
        Tuple of (acquired: bool, holder_id: str).
        Keep holder_id to pass to release_fiche_lock_advisory().
    """
    return FicheLockManager.acquire_fiche_lock(db, fiche_id)


def release_fiche_lock_advisory(db: Session, fiche_id: int, holder_id: str) -> bool:
    """Release a fiche lock (thin wrapper for FicheLockManager).

    Args:
        db: Database session
        fiche_id: ID of the fiche to unlock
        holder_id: The holder_id returned from acquire_fiche_lock_advisory()
    """
    return FicheLockManager.release_fiche_lock(db, fiche_id, holder_id)
