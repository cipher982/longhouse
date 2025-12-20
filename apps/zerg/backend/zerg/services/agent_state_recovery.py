"""
Agent state recovery service for application startup.

This module provides robust agent state management that prevents the stuck agent bug
by implementing startup recovery procedures based on distributed systems principles.
"""

import logging
from typing import List

from sqlalchemy import text

from zerg.database import get_session_factory
from zerg.models.enums import RunStatus
from zerg.models.models import Agent
from zerg.models.models import AgentRun

logger = logging.getLogger(__name__)


async def perform_startup_agent_recovery() -> List[int]:
    """
    Perform agent state recovery on application startup.

    This implements the distributed systems principle of process recovery:
    On startup, check for any agents that may be in inconsistent states due to
    previous process crashes and recover them to a consistent state.

    Returns:
        List of agent IDs that were recovered
    """
    logger.info("Starting agent state recovery process...")

    session_factory = get_session_factory()
    recovered_agent_ids = []

    with session_factory() as db:
        try:
            # Find agents stuck in running state with no active runs
            # This indicates a previous process crash where the lock wasn't released
            stuck_agents = (
                db.query(Agent)
                .outerjoin(
                    AgentRun,
                    (Agent.id == AgentRun.agent_id) & (AgentRun.status.in_([RunStatus.RUNNING.value, RunStatus.QUEUED.value])),
                )
                .filter(
                    Agent.status.in_(["running", "RUNNING"]),  # Agent shows as running
                    AgentRun.id.is_(None),  # But no active runs exist
                )
                .all()
            )

            if not stuck_agents:
                logger.info("✅ No stuck agents found during startup recovery")
                return recovered_agent_ids

            logger.warning(f"🔧 Found {len(stuck_agents)} agents stuck in running state, recovering...")

            for agent in stuck_agents:
                try:
                    # Reset agent to idle state with recovery message
                    db.query(Agent).filter(Agent.id == agent.id).update(
                        {
                            "status": "idle",
                            "last_error": "Recovered from stuck running state during application startup",
                        }
                    )

                    recovered_agent_ids.append(agent.id)
                    logger.info(f"✅ Recovered agent {agent.id} ({agent.name})")

                except Exception as e:
                    logger.error(f"❌ Failed to recover agent {agent.id}: {e}")

            # Commit all changes
            if recovered_agent_ids:
                db.commit()
                logger.info(f"✅ Successfully recovered {len(recovered_agent_ids)} agents: {recovered_agent_ids}")

        except Exception as e:
            logger.error(f"❌ Agent recovery process failed: {e}")
            db.rollback()
            raise

    return recovered_agent_ids


def check_postgresql_advisory_lock_support() -> bool:
    """
    Check if PostgreSQL advisory locks are available.

    Advisory locks are the proper solution for distributed locking because:
    1. They automatically release when the session terminates
    2. They don't rely on persistent data state
    3. They're designed for exactly this use case

    Returns:
        True if advisory locks are supported, False otherwise
    """
    session_factory = get_session_factory()

    with session_factory() as db:
        try:
            # Test advisory lock functionality
            result = db.execute(text("SELECT pg_try_advisory_lock(999999)"))
            acquired = result.scalar()

            if acquired:
                # Release the test lock
                db.execute(text("SELECT pg_advisory_unlock(999999)"))
                logger.info("✅ PostgreSQL advisory locks are available")
                return True
            else:
                logger.warning("⚠️ PostgreSQL advisory locks test failed")
                return False

        except Exception as e:
            logger.warning(f"⚠️ PostgreSQL advisory locks not available: {e}")
            return False


async def initialize_agent_state_system():
    """
    Initialize the agent state management system.

    This should be called during application startup to:
    1. Perform recovery of any stuck agents
    2. Initialize the proper locking system
    """
    logger.info("Initializing agent state management system...")

    try:
        # Step 1: Perform startup recovery
        recovered_agents = await perform_startup_agent_recovery()

        # Step 2: Check advisory lock support for future enhancement
        advisory_locks_available = check_postgresql_advisory_lock_support()

        if advisory_locks_available:
            logger.info("✅ Agent state system initialized with PostgreSQL advisory lock support")
        else:
            logger.info("✅ Agent state system initialized with startup recovery only")

        return {"recovered_agents": recovered_agents, "advisory_locks_available": advisory_locks_available}

    except Exception as e:
        logger.error(f"❌ Failed to initialize agent state system: {e}")
        raise
