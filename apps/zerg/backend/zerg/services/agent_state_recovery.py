"""
Agent state recovery service for application startup.

This module provides robust agent state management that prevents the stuck agent bug
by implementing startup recovery procedures based on distributed systems principles.

Handles recovery of:
- Agents stuck in "running" state with no active runs
- AgentRun rows stuck in RUNNING/QUEUED/DEFERRED status
- WorkerJob rows stuck in "running" status (queued jobs are resumable)
- RunnerJob rows stuck in queued/running status
"""

import logging
from datetime import datetime
from datetime import timezone
from typing import List

from sqlalchemy import text

from zerg.database import get_session_factory
from zerg.models.enums import RunStatus
from zerg.models.models import Agent
from zerg.models.models import AgentRun
from zerg.models.models import RunnerJob
from zerg.models.models import WorkerJob

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


async def perform_startup_run_recovery() -> List[int]:
    """
    Recover orphaned AgentRun rows on application startup.

    Finds runs stuck in RUNNING, QUEUED, or DEFERRED status and marks them
    as FAILED with a clear error message indicating server restart.

    Returns:
        List of run IDs that were recovered
    """
    logger.info("Starting run recovery process...")

    session_factory = get_session_factory()
    recovered_run_ids = []

    with session_factory() as db:
        try:
            # Find runs stuck in active states
            stuck_runs = (
                db.query(AgentRun)
                .filter(
                    AgentRun.status.in_(
                        [
                            RunStatus.RUNNING.value,
                            RunStatus.QUEUED.value,
                            RunStatus.DEFERRED.value,
                        ]
                    )
                )
                .all()
            )

            if not stuck_runs:
                logger.info("✅ No stuck runs found during startup recovery")
                return recovered_run_ids

            logger.warning(f"🔧 Found {len(stuck_runs)} runs stuck in active state, recovering...")

            now = datetime.now(timezone.utc)
            for run in stuck_runs:
                try:
                    # Calculate duration if started
                    duration_ms = None
                    if run.started_at:
                        started = run.started_at
                        if started.tzinfo is None:
                            started = started.replace(tzinfo=timezone.utc)
                        duration_ms = int((now - started).total_seconds() * 1000)

                    db.query(AgentRun).filter(AgentRun.id == run.id).update(
                        {
                            "status": RunStatus.FAILED.value,
                            "finished_at": now,
                            "duration_ms": duration_ms,
                            "error": "Orphaned after server restart - execution state lost",
                        }
                    )

                    recovered_run_ids.append(run.id)
                    logger.info(f"✅ Recovered run {run.id} (agent={run.agent_id}, was {run.status})")

                except Exception as e:
                    logger.error(f"❌ Failed to recover run {run.id}: {e}")

            if recovered_run_ids:
                db.commit()
                logger.info(f"✅ Successfully recovered {len(recovered_run_ids)} runs")

        except Exception as e:
            logger.error(f"❌ Run recovery process failed: {e}")
            db.rollback()
            raise

    return recovered_run_ids


async def perform_startup_worker_job_recovery() -> List[int]:
    """
    Recover orphaned WorkerJob rows on application startup.

    Only recovers jobs in "running" status - these were mid-execution when
    the server crashed and their state is lost.

    Jobs in "queued" status are NOT recovered because WorkerJobProcessor
    is designed to resume them after restart (they haven't started yet).

    Returns:
        List of job IDs that were recovered
    """
    logger.info("Starting worker job recovery process...")

    session_factory = get_session_factory()
    recovered_job_ids = []

    with session_factory() as db:
        try:
            # Only recover "running" jobs - queued jobs are resumable
            stuck_jobs = db.query(WorkerJob).filter(WorkerJob.status == "running").all()

            if not stuck_jobs:
                logger.info("✅ No stuck worker jobs found during startup recovery")
                return recovered_job_ids

            logger.warning(f"🔧 Found {len(stuck_jobs)} worker jobs stuck, recovering...")

            now = datetime.now(timezone.utc)
            for job in stuck_jobs:
                try:
                    db.query(WorkerJob).filter(WorkerJob.id == job.id).update(
                        {
                            "status": "failed",
                            "finished_at": now,
                            "error": "Orphaned after server restart - execution state lost",
                        }
                    )

                    recovered_job_ids.append(job.id)
                    logger.info(f"✅ Recovered worker job {job.id} (was {job.status})")

                except Exception as e:
                    logger.error(f"❌ Failed to recover worker job {job.id}: {e}")

            if recovered_job_ids:
                db.commit()
                logger.info(f"✅ Successfully recovered {len(recovered_job_ids)} worker jobs")

        except Exception as e:
            logger.error(f"❌ Worker job recovery process failed: {e}")
            db.rollback()
            raise

    return recovered_job_ids


async def perform_startup_runner_job_recovery() -> List[str]:
    """
    Recover orphaned RunnerJob rows on application startup.

    Finds runner jobs stuck in queued/running status and marks them
    as failed with a clear error message indicating server restart.

    Returns:
        List of job IDs (UUIDs as strings) that were recovered
    """
    logger.info("Starting runner job recovery process...")

    session_factory = get_session_factory()
    recovered_job_ids = []

    with session_factory() as db:
        try:
            stuck_jobs = db.query(RunnerJob).filter(RunnerJob.status.in_(["queued", "running"])).all()

            if not stuck_jobs:
                logger.info("✅ No stuck runner jobs found during startup recovery")
                return recovered_job_ids

            logger.warning(f"🔧 Found {len(stuck_jobs)} runner jobs stuck, recovering...")

            now = datetime.now(timezone.utc)
            for job in stuck_jobs:
                try:
                    db.query(RunnerJob).filter(RunnerJob.id == job.id).update(
                        {
                            "status": "failed",
                            "finished_at": now,
                            "error": "Orphaned after server restart - execution state lost",
                        }
                    )

                    recovered_job_ids.append(job.id)
                    logger.info(f"✅ Recovered runner job {job.id} (was {job.status})")

                except Exception as e:
                    logger.error(f"❌ Failed to recover runner job {job.id}: {e}")

            if recovered_job_ids:
                db.commit()
                logger.info(f"✅ Successfully recovered {len(recovered_job_ids)} runner jobs")

        except Exception as e:
            logger.error(f"❌ Runner job recovery process failed: {e}")
            db.rollback()
            raise

    return recovered_job_ids


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
    1. Perform recovery of any orphaned runs (MUST happen before agent recovery)
    2. Perform recovery of any orphaned worker jobs
    3. Perform recovery of any orphaned runner jobs
    4. Perform recovery of any stuck agents (after runs are cleared)
    5. Initialize the proper locking system

    IMPORTANT: Run/job recovery must happen BEFORE agent recovery because
    agent recovery skips agents with active runs. If we recover agents first,
    agents with stuck runs won't be reset. Then when run recovery marks those
    runs as FAILED, the agent stays stuck in "running" forever.
    """
    logger.info("Initializing agent state management system...")

    try:
        # Step 1: Recover runs FIRST (so agents no longer have "active runs")
        recovered_runs = await perform_startup_run_recovery()

        # Step 2: Recover worker jobs
        recovered_worker_jobs = await perform_startup_worker_job_recovery()

        # Step 3: Recover runner jobs
        recovered_runner_jobs = await perform_startup_runner_job_recovery()

        # Step 4: Recover agents LAST (now will correctly see no active runs)
        recovered_agents = await perform_startup_agent_recovery()

        # Step 5: Check advisory lock support for future enhancement
        advisory_locks_available = check_postgresql_advisory_lock_support()

        # Log summary
        total_recovered = len(recovered_agents) + len(recovered_runs) + len(recovered_worker_jobs) + len(recovered_runner_jobs)

        if total_recovered > 0:
            logger.info(
                f"🔧 Startup recovery complete: "
                f"{len(recovered_agents)} agents, "
                f"{len(recovered_runs)} runs, "
                f"{len(recovered_worker_jobs)} worker jobs, "
                f"{len(recovered_runner_jobs)} runner jobs"
            )

        if advisory_locks_available:
            logger.info("✅ Agent state system initialized with PostgreSQL advisory lock support")
        else:
            logger.info("✅ Agent state system initialized with startup recovery only")

        return {
            "recovered_agents": recovered_agents,
            "recovered_runs": recovered_runs,
            "recovered_worker_jobs": recovered_worker_jobs,
            "recovered_runner_jobs": recovered_runner_jobs,
            "advisory_locks_available": advisory_locks_available,
        }

    except Exception as e:
        logger.error(f"❌ Failed to initialize agent state system: {e}")
        raise
