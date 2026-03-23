"""Shared database utilities.

This module contains low-level database helpers that need to be importable
without triggering circular dependencies. Specifically, these functions
are used by both config/__init__.py and database.py during module load.

Also provides locking helpers using SQLite resource_locks table pattern.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import TYPE_CHECKING
from typing import Generator

from sqlalchemy import text
from sqlalchemy.engine.url import make_url

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _generate_holder_id() -> str:
    """Generate a globally unique holder ID.

    Combines hostname, PID, thread ID, and UUID to ensure uniqueness across:
    - Different hosts/containers
    - Different processes on same host
    - Different threads in same process
    - Multiple acquisitions in same context (UUID disambiguates)
    """
    import socket

    hostname = socket.gethostname()[:20]  # Truncate to keep reasonable length
    pid = os.getpid()
    thread_id = threading.current_thread().ident or 0
    unique = uuid.uuid4().hex[:8]

    return f"{hostname}:{pid}:{thread_id}:{unique}"


def is_sqlite_url(url: str) -> bool:
    """Check if a database URL is SQLite, handling quoted URLs.

    Uses SQLAlchemy's make_url() for proper parsing instead of string matching.
    This handles cases where the URL has surrounding quotes from .env files.

    Args:
        url: Database URL string (possibly with surrounding quotes)

    Returns:
        True if the URL is a SQLite database
    """
    url = (url or "").strip()
    if not url:
        return False

    # Strip surrounding quotes (common from .env files)
    if (url.startswith('"') and url.endswith('"')) or (url.startswith("'") and url.endswith("'")):
        url = url[1:-1].strip()

    if not url:
        return False

    try:
        parsed = make_url(url)
        return parsed.drivername.startswith("sqlite")
    except Exception:
        # Fallback to string matching if parsing fails
        return url.startswith("sqlite")


# =============================================================================
# SQLite Advisory Locking via resource_locks table
# =============================================================================
#
# SQLite doesn't have native advisory locks, so we use a status column pattern
# with timestamps for atomicity.
#
# Uses:
# - A `resource_locks` table with (lock_type, lock_key, holder_id, acquired_at, heartbeat_at)
# - UPDATE ... WHERE with timestamp checks for stale lock detection
#
# =============================================================================


# Stale lock timeout: locks without heartbeat for this long can be taken over
STALE_LOCK_TIMEOUT_SECONDS = int(os.getenv("STALE_LOCK_TIMEOUT_SECONDS", "120"))


def acquire_lock(
    db: "Session",
    lock_type: str,
    lock_key: str,
    holder_id: str,
    blocking: bool = False,
) -> bool:
    """Acquire a lock using the resource_locks table pattern.

    Uses INSERT ... ON CONFLICT DO UPDATE for atomic lock acquisition.
    Stale locks (no heartbeat update for STALE_LOCK_TIMEOUT_SECONDS) can be taken over.

    Args:
        db: SQLAlchemy Session
        lock_type: Type of resource being locked (e.g., 'fiche', 'user_creation')
        lock_key: Unique key within the lock type (e.g., fiche_id as string)
        holder_id: Identifier for this lock holder (e.g., process ID, worker ID)
        blocking: Unused, for API compatibility only

    Returns:
        True if lock acquired, False if held by another active holder
    """
    now = datetime.now(timezone.utc)
    stale_threshold = now - timedelta(seconds=STALE_LOCK_TIMEOUT_SECONDS)

    # Ensure resource_locks table exists
    _ensure_resource_locks_table(db)

    # Try to insert a new lock row or update a stale one
    # SQLite uses UPSERT (INSERT ... ON CONFLICT) for atomicity
    result = db.execute(
        text("""
            INSERT INTO resource_locks (lock_type, lock_key, holder_id, acquired_at, heartbeat_at)
            VALUES (:lock_type, :lock_key, :holder_id, :now, :now)
            ON CONFLICT (lock_type, lock_key) DO UPDATE SET
                holder_id = :holder_id,
                acquired_at = :now,
                heartbeat_at = :now
            WHERE resource_locks.holder_id = :holder_id
               OR resource_locks.heartbeat_at < :stale_threshold
            RETURNING lock_type
        """),
        {
            "lock_type": lock_type,
            "lock_key": lock_key,
            "holder_id": holder_id,
            "now": now,
            "stale_threshold": stale_threshold,
        },
    )

    # RETURNING only returns a row if we actually inserted/updated
    acquired = result.fetchone() is not None
    db.commit()

    if acquired:
        logger.debug(f"Acquired lock: {lock_type}/{lock_key} (holder={holder_id})")
    else:
        # Check who holds the lock
        existing = db.execute(
            text("""
                SELECT holder_id, heartbeat_at FROM resource_locks
                WHERE lock_type = :lock_type AND lock_key = :lock_key
            """),
            {"lock_type": lock_type, "lock_key": lock_key},
        ).fetchone()
        if existing:
            logger.debug(f"Lock {lock_type}/{lock_key} held by {existing[0]} (last heartbeat: {existing[1]})")

    return acquired


def release_lock(
    db: "Session",
    lock_type: str,
    lock_key: str,
    holder_id: str,
) -> bool:
    """Release a lock.

    Args:
        db: SQLAlchemy Session
        lock_type: Type of resource being locked
        lock_key: Unique key within the lock type
        holder_id: Identifier for this lock holder

    Returns:
        True if lock was held by this holder and released, False otherwise
    """
    result = db.execute(
        text("""
            DELETE FROM resource_locks
            WHERE lock_type = :lock_type
              AND lock_key = :lock_key
              AND holder_id = :holder_id
        """),
        {
            "lock_type": lock_type,
            "lock_key": lock_key,
            "holder_id": holder_id,
        },
    )
    db.commit()

    released = result.rowcount > 0
    if released:
        logger.debug(f"Released lock: {lock_type}/{lock_key} (holder={holder_id})")
    else:
        logger.debug(f"Lock {lock_type}/{lock_key} not held by {holder_id}")

    return released


def update_lock_heartbeat(
    db: "Session",
    lock_type: str,
    lock_key: str,
    holder_id: str,
) -> bool:
    """Update the heartbeat timestamp for a held lock.

    Should be called periodically (e.g., every 30s) to prevent stale lock takeover.

    Args:
        db: SQLAlchemy Session
        lock_type: Type of resource being locked
        lock_key: Unique key within the lock type
        holder_id: Identifier for this lock holder

    Returns:
        True if lock was held by this holder and heartbeat updated
    """
    now = datetime.now(timezone.utc)
    result = db.execute(
        text("""
            UPDATE resource_locks
            SET heartbeat_at = :now
            WHERE lock_type = :lock_type
              AND lock_key = :lock_key
              AND holder_id = :holder_id
        """),
        {
            "now": now,
            "lock_type": lock_type,
            "lock_key": lock_key,
            "holder_id": holder_id,
        },
    )
    db.commit()
    return result.rowcount > 0


def _ensure_resource_locks_table(db: "Session") -> None:
    """Ensure the resource_locks table exists.

    Creates the table on-demand for SQLite.
    """
    db.execute(
        text("""
            CREATE TABLE IF NOT EXISTS resource_locks (
                lock_type TEXT NOT NULL,
                lock_key TEXT NOT NULL,
                holder_id TEXT NOT NULL,
                acquired_at TIMESTAMP NOT NULL,
                heartbeat_at TIMESTAMP NOT NULL,
                PRIMARY KEY (lock_type, lock_key)
            )
        """)
    )
    db.commit()


# =============================================================================
# Unified Locking Interface (for compatibility)
# =============================================================================


def acquire_resource_lock(
    db: "Session",
    lock_type: str,
    lock_key: str | int,
    holder_id: str,
    blocking: bool = False,
) -> bool:
    """Acquire a resource lock.

    Args:
        db: SQLAlchemy Session
        lock_type: Type of resource (e.g., 'fiche', 'user_creation')
        lock_key: Unique key
        holder_id: Identifier for this lock holder
        blocking: Unused, for API compatibility

    Returns:
        True if lock acquired
    """
    return acquire_lock(db, lock_type, str(lock_key), holder_id, blocking)


def release_resource_lock(
    db: "Session",
    lock_type: str,
    lock_key: str | int,
    holder_id: str,
) -> bool:
    """Release a resource lock.

    Args:
        db: SQLAlchemy Session
        lock_type: Type of resource
        lock_key: Unique key
        holder_id: Identifier for this lock holder

    Returns:
        True if lock was held and released
    """
    return release_lock(db, lock_type, str(lock_key), holder_id)


@contextmanager
def resource_lock(
    db: "Session",
    lock_type: str,
    lock_key: str | int,
    holder_id: str,
    blocking: bool = False,
) -> Generator[bool, None, None]:
    """Context manager for resource locking.

    Usage:
        with resource_lock(db, "fiche", fiche_id, worker_id) as acquired:
            if acquired:
                # Do work with exclusive access
                pass
            else:
                raise ValueError("Resource is locked")

    Args:
        db: SQLAlchemy Session
        lock_type: Type of resource being locked
        lock_key: Unique key for the resource
        holder_id: Identifier for this lock holder
        blocking: Unused, for API compatibility

    Yields:
        bool: True if lock was acquired
    """
    acquired = acquire_resource_lock(db, lock_type, lock_key, holder_id, blocking)
    try:
        yield acquired
    finally:
        if acquired:
            release_resource_lock(db, lock_type, lock_key, holder_id)


def get_active_locks(db: "Session", lock_type: str | None = None) -> list[dict]:
    """Get list of active locks.

    Args:
        db: SQLAlchemy Session
        lock_type: Optional filter by lock type

    Returns:
        List of dicts with lock_type, lock_key, holder_id, acquired_at, heartbeat_at
    """
    _ensure_resource_locks_table(db)

    if lock_type:
        result = db.execute(
            text("""
                SELECT lock_type, lock_key, holder_id, acquired_at, heartbeat_at
                FROM resource_locks
                WHERE lock_type = :lock_type
                ORDER BY acquired_at
            """),
            {"lock_type": lock_type},
        )
    else:
        result = db.execute(
            text("""
                SELECT lock_type, lock_key, holder_id, acquired_at, heartbeat_at
                FROM resource_locks
                ORDER BY lock_type, acquired_at
            """)
        )

    return [
        {
            "lock_type": row[0],
            "lock_key": row[1],
            "holder_id": row[2],
            "acquired_at": row[3],
            "heartbeat_at": row[4],
        }
        for row in result.fetchall()
    ]
