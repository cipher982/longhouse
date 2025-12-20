"""
PostgreSQL advisory lock-based agent concurrency control.

This module implements proper distributed locking using PostgreSQL advisory locks,
which automatically release when the database session terminates, eliminating the
stuck agent bug entirely.

This is the proper architectural solution based on distributed systems research.
"""

import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class AgentLockManager:
    """
    Manages agent concurrency using PostgreSQL advisory locks.

    Advisory locks are session-scoped and automatically released when:
    1. The session/connection terminates (normal shutdown or crash)
    2. The connection is lost

    Note: session-level advisory locks are NOT released on transaction rollback.
    To keep a simple boolean contract we also guard against re-entrancy within
    the same DB session (second acquire returns False).
    """

    @staticmethod
    def acquire_agent_lock(db: Session, agent_id: int) -> bool:
        """
        Acquire an advisory lock for an agent.

        Uses PostgreSQL pg_try_advisory_lock which:
        - Returns immediately (non-blocking)
        - Returns True if lock acquired, False if already held
        - Automatically releases on session termination

        Args:
            db: Database session
            agent_id: ID of the agent to lock

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
                      AND ((classid::bigint << 32) | objid::bigint) = :agent_id
                    LIMIT 1
                    """
                ),
                {"agent_id": int(agent_id)},
            ).scalar()

            if already_held:
                logger.debug(f"⚠️ Advisory lock for agent {agent_id} already held by this session")
                return False

            result = db.execute(
                text("SELECT pg_try_advisory_lock(:agent_id)"),
                {"agent_id": int(agent_id)},
            )
            acquired = result.scalar()

            if acquired:
                logger.debug(f"✅ Acquired advisory lock for agent {agent_id}")
            else:
                logger.debug(f"⚠️ Agent {agent_id} is already locked by another session")

            return bool(acquired)

        except Exception as e:
            logger.error(f"❌ Failed to acquire advisory lock for agent {agent_id}: {e}")
            return False

    @staticmethod
    def release_agent_lock(db: Session, agent_id: int) -> bool:
        """
        Release an advisory lock for an agent.

        Args:
            db: Database session
            agent_id: ID of the agent to unlock

        Returns:
            True if lock was released, False if not held by this session
        """
        try:
            result = db.execute(text("SELECT pg_advisory_unlock(:agent_id)"), {"agent_id": agent_id})
            released = result.scalar()

            if released:
                logger.debug(f"✅ Released advisory lock for agent {agent_id}")
            else:
                logger.warning(f"⚠️ Attempted to release advisory lock for agent {agent_id} but it wasn't held by this session")

            return bool(released)

        except Exception as e:
            logger.error(f"❌ Failed to release advisory lock for agent {agent_id}: {e}")
            return False

    @staticmethod
    @contextmanager
    def agent_lock(db: Session, agent_id: int) -> Generator[bool, None, None]:
        """
        Context manager for agent advisory locks.

        Usage:
            with AgentLockManager.agent_lock(db, agent_id) as acquired:
                if acquired:
                    # Do work with exclusive access to agent
                    pass
                else:
                    # Agent is already running
                    raise ValueError("Agent already running")

        The lock is automatically released when the context exits,
        even if an exception occurs.

        Args:
            db: Database session
            agent_id: ID of the agent to lock

        Yields:
            bool: True if lock was acquired, False otherwise
        """
        acquired = AgentLockManager.acquire_agent_lock(db, agent_id)

        try:
            yield acquired
        finally:
            if acquired:
                AgentLockManager.release_agent_lock(db, agent_id)

    @staticmethod
    def get_locked_agents(db: Session) -> list[int]:
        """
        Get list of currently locked agent IDs.

        This queries pg_locks to see which advisory locks are currently held.

        Args:
            db: Database session

        Returns:
            List of agent IDs that are currently locked
        """
        try:
            # For pg_try_advisory_lock(bigint) the lock key is split across
            # classid (high 32 bits) and objid (low 32 bits). Reconstruct the
            # original bigint to match the agent_id we lock against.
            result = db.execute(
                text(
                    """
                    SELECT ((classid::bigint << 32) | objid::bigint) AS agent_id
                    FROM pg_locks
                    WHERE locktype = 'advisory'
                      AND granted = true
                    ORDER BY agent_id
                    """
                )
            )

            locked_agents = [int(row[0]) for row in result.fetchall()]
            logger.debug(f"Currently locked agents: {locked_agents}")

            return locked_agents

        except Exception as e:
            logger.error(f"❌ Failed to get locked agents: {e}")
            return []


# Backward compatibility wrapper for existing code
def acquire_run_lock_advisory(db: Session, agent_id: int) -> bool:
    """
    Drop-in replacement for the old acquire_run_lock function.

    This uses advisory locks instead of persistent status updates,
    eliminating the stuck agent bug entirely.

    Args:
        db: Database session
        agent_id: ID of the agent

    Returns:
        True if lock acquired, False if already running
    """
    return AgentLockManager.acquire_agent_lock(db, agent_id)


def release_run_lock_advisory(db: Session, agent_id: int) -> bool:
    """
    Release function that was missing from the original design.

    Args:
        db: Database session
        agent_id: ID of the agent

    Returns:
        True if lock was released
    """
    return AgentLockManager.release_agent_lock(db, agent_id)
