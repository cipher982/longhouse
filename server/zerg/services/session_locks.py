"""In-memory async locks for live session input delivery."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class SessionLock:
    """Information about a held session lock."""

    session_id: str
    holder: str
    acquired_at: float
    ttl_seconds: int = 300

    @property
    def is_expired(self) -> bool:
        """Check if this lock has expired."""
        return time.time() > (self.acquired_at + self.ttl_seconds)

    @property
    def time_remaining(self) -> float:
        """Seconds remaining on this lock."""
        remaining = (self.acquired_at + self.ttl_seconds) - time.time()
        return max(0, remaining)


class SessionLockManager:
    """Manages per-session async locks to prevent concurrent input delivery."""

    def __init__(self) -> None:
        self._locks: dict[str, SessionLock] = {}
        self._mutex = asyncio.Lock()

    async def acquire(
        self,
        session_id: str,
        holder: str = "web-chat",
        ttl_seconds: int = 300,
    ) -> SessionLock | None:
        """Try to acquire a lock for a session."""
        async with self._mutex:
            self._cleanup_expired_unlocked()

            existing = self._locks.get(session_id)
            if existing and not existing.is_expired:
                return None

            lock = SessionLock(
                session_id=session_id,
                holder=holder,
                acquired_at=time.time(),
                ttl_seconds=ttl_seconds,
            )
            self._locks[session_id] = lock
            logger.debug("Acquired session lock: %s by %s", session_id, holder)
            return lock

    async def release(self, session_id: str, holder: str | None = None) -> bool:
        """Release a session lock."""
        async with self._mutex:
            existing = self._locks.get(session_id)
            if not existing:
                return False

            if holder and existing.holder != holder:
                logger.warning("Lock release rejected: %s held by %s, not %s", session_id, existing.holder, holder)
                return False

            del self._locks[session_id]
            logger.debug("Released session lock: %s", session_id)
            return True

    async def get_lock_info(self, session_id: str) -> SessionLock | None:
        """Get information about a lock if it exists and is not expired."""
        async with self._mutex:
            self._cleanup_expired_unlocked()

            existing = self._locks.get(session_id)
            if existing and not existing.is_expired:
                return existing
            return None

    async def is_locked(self, session_id: str) -> bool:
        """Check if a session is currently locked."""
        lock = await self.get_lock_info(session_id)
        return lock is not None

    def _cleanup_expired_unlocked(self) -> int:
        """Remove expired locks without acquiring mutex. Must be called with mutex held."""
        expired = [sid for sid, lock in self._locks.items() if lock.is_expired]
        for sid in expired:
            del self._locks[sid]
        if expired:
            logger.debug("Cleaned up %s expired session locks", len(expired))
        return len(expired)

    async def cleanup_expired(self) -> int:
        """Remove expired locks. Returns count of cleaned up locks."""
        async with self._mutex:
            return self._cleanup_expired_unlocked()


session_lock_manager = SessionLockManager()
