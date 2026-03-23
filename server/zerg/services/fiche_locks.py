"""
Fiche concurrency control using SQLite resource_locks table.

This module implements distributed locking for fiches using a status column
pattern with heartbeat-based stale detection via the resource_locks table.
"""

import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy.orm import Session

from zerg.db_utils import _generate_holder_id
from zerg.db_utils import acquire_resource_lock
from zerg.db_utils import get_active_locks
from zerg.db_utils import release_resource_lock
from zerg.db_utils import resource_lock

logger = logging.getLogger(__name__)

# Lock type constant for fiche locks
FICHE_LOCK_TYPE = "fiche"


class FicheLockManager:
    """
    Manages fiche concurrency using resource_locks table.

    Uses a status column pattern with heartbeat-based stale detection.
    Stale locks (no heartbeat for STALE_LOCK_TIMEOUT_SECONDS) can be taken over.
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
            The holder_id is needed to release the lock.
        """
        holder = holder_id or _generate_holder_id()

        try:
            acquired = acquire_resource_lock(db, FICHE_LOCK_TYPE, str(fiche_id), holder, blocking=False)
            if acquired:
                logger.debug(f"Acquired lock for fiche {fiche_id}")
            else:
                logger.debug(f"Fiche {fiche_id} is already locked")
            return acquired, holder

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
            True if lock was released, False if not held by this holder
        """
        try:
            released = release_resource_lock(db, FICHE_LOCK_TYPE, str(fiche_id), holder_id)
            if released:
                logger.debug(f"Released lock for fiche {fiche_id}")
            else:
                logger.warning(f"Fiche {fiche_id} lock not held by {holder_id}")
            return released

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

        with resource_lock(db, FICHE_LOCK_TYPE, str(fiche_id), holder) as acquired:
            yield acquired

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
            locks = get_active_locks(db, FICHE_LOCK_TYPE)
            locked_fiches = [int(lock["lock_key"]) for lock in locks]
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
