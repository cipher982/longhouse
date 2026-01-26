"""
Fiche state recovery service for application startup.

This module provides robust fiche state management that prevents the stuck fiche bug
by implementing startup recovery procedures based on distributed systems principles.

Handles recovery of:
- Fiches stuck in "running" state with no active courses
- Course rows stuck in RUNNING/QUEUED/DEFERRED status
- CommisJob rows stuck in "running" status (queued jobs are resumable)
- RunnerJob rows stuck in queued/running status
"""

import logging
from datetime import datetime
from datetime import timezone
from typing import List

from sqlalchemy import text

from zerg.database import get_session_factory
from zerg.models.enums import CourseStatus
from zerg.models.models import CommisJob
from zerg.models.models import Course
from zerg.models.models import Fiche
from zerg.models.models import RunnerJob

logger = logging.getLogger(__name__)


async def perform_startup_fiche_recovery() -> List[int]:
    """
    Perform fiche state recovery on application startup.

    This implements the distributed systems principle of process recovery:
    On startup, check for any fiches that may be in inconsistent states due to
    previous process crashes and recover them to a consistent state.

    Returns:
        List of fiche IDs that were recovered
    """
    logger.info("Starting fiche state recovery process...")

    session_factory = get_session_factory()
    recovered_fiche_ids = []

    with session_factory() as db:
        try:
            # Find fiches stuck in running state with no active courses
            # This indicates a previous process crash where the lock wasn't released
            stuck_fiches = (
                db.query(Fiche)
                .outerjoin(
                    Course,
                    (Fiche.id == Course.fiche_id) & (Course.status.in_([CourseStatus.RUNNING.value, CourseStatus.QUEUED.value])),
                )
                .filter(
                    Fiche.status.in_(["running", "RUNNING"]),  # Fiche shows as running
                    Course.id.is_(None),  # But no active courses exist
                )
                .all()
            )

            if not stuck_fiches:
                logger.info("✅ No stuck fiches found during startup recovery")
                return recovered_fiche_ids

            logger.warning(f"🔧 Found {len(stuck_fiches)} fiches stuck in running state, recovering...")

            for fiche in stuck_fiches:
                try:
                    # Reset fiche to idle state with recovery message
                    db.query(Fiche).filter(Fiche.id == fiche.id).update(
                        {
                            "status": "idle",
                            "last_error": "Recovered from stuck running state during application startup",
                        }
                    )

                    recovered_fiche_ids.append(fiche.id)
                    logger.info(f"✅ Recovered fiche {fiche.id} ({fiche.name})")

                except Exception as e:
                    logger.error(f"❌ Failed to recover fiche {fiche.id}: {e}")

            # Commit all changes
            if recovered_fiche_ids:
                db.commit()
                logger.info(f"✅ Successfully recovered {len(recovered_fiche_ids)} fiches: {recovered_fiche_ids}")

        except Exception as e:
            logger.error(f"❌ Fiche recovery process failed: {e}")
            db.rollback()
            raise

    return recovered_fiche_ids


async def perform_startup_course_recovery() -> List[int]:
    """
    Recover orphaned Course rows on application startup.

    Finds courses stuck in RUNNING, QUEUED, or DEFERRED status and marks them
    as FAILED with a clear error message indicating server restart.

    Returns:
        List of course IDs that were recovered
    """
    logger.info("Starting course recovery process...")

    session_factory = get_session_factory()
    recovered_course_ids = []

    with session_factory() as db:
        try:
            # Find courses stuck in active states
            stuck_courses = (
                db.query(Course)
                .filter(
                    Course.status.in_(
                        [
                            CourseStatus.RUNNING.value,
                            CourseStatus.QUEUED.value,
                            CourseStatus.DEFERRED.value,
                        ]
                    )
                )
                .all()
            )

            if not stuck_courses:
                logger.info("✅ No stuck courses found during startup recovery")
                return recovered_course_ids

            logger.warning(f"🔧 Found {len(stuck_courses)} courses stuck in active state, recovering...")

            now = datetime.now(timezone.utc)
            for course in stuck_courses:
                try:
                    # Calculate duration if started
                    duration_ms = None
                    if course.started_at:
                        started = course.started_at
                        if started.tzinfo is None:
                            started = started.replace(tzinfo=timezone.utc)
                        duration_ms = int((now - started).total_seconds() * 1000)

                    db.query(Course).filter(Course.id == course.id).update(
                        {
                            "status": CourseStatus.FAILED.value,
                            "finished_at": now,
                            "duration_ms": duration_ms,
                            "error": "Orphaned after server restart - execution state lost",
                        }
                    )

                    recovered_course_ids.append(course.id)
                    logger.info(f"✅ Recovered course {course.id} (fiche={course.fiche_id}, was {course.status})")

                except Exception as e:
                    logger.error(f"❌ Failed to recover course {course.id}: {e}")

            if recovered_course_ids:
                db.commit()
                logger.info(f"✅ Successfully recovered {len(recovered_course_ids)} courses")

        except Exception as e:
            logger.error(f"❌ Course recovery process failed: {e}")
            db.rollback()
            raise

    return recovered_course_ids


async def perform_startup_commis_job_recovery() -> List[int]:
    """
    Recover orphaned CommisJob rows on application startup.

    Only recovers jobs in "running" status - these were mid-execution when
    the server crashed and their state is lost.

    Jobs in "queued" status are NOT recovered because CommisJobProcessor
    is designed to resume them after restart (they haven't started yet).

    Returns:
        List of job IDs that were recovered
    """
    logger.info("Starting commis job recovery process...")

    session_factory = get_session_factory()
    recovered_job_ids = []

    with session_factory() as db:
        try:
            # Only recover "running" jobs - queued jobs are resumable
            stuck_jobs = db.query(CommisJob).filter(CommisJob.status == "running").all()

            if not stuck_jobs:
                logger.info("✅ No stuck commis jobs found during startup recovery")
                return recovered_job_ids

            logger.warning(f"🔧 Found {len(stuck_jobs)} commis jobs stuck, recovering...")

            now = datetime.now(timezone.utc)
            for job in stuck_jobs:
                try:
                    db.query(CommisJob).filter(CommisJob.id == job.id).update(
                        {
                            "status": "failed",
                            "finished_at": now,
                            "error": "Orphaned after server restart - execution state lost",
                        }
                    )

                    recovered_job_ids.append(job.id)
                    logger.info(f"✅ Recovered commis job {job.id} (was {job.status})")

                except Exception as e:
                    logger.error(f"❌ Failed to recover commis job {job.id}: {e}")

            if recovered_job_ids:
                db.commit()
                logger.info(f"✅ Successfully recovered {len(recovered_job_ids)} commis jobs")

        except Exception as e:
            logger.error(f"❌ Commis job recovery process failed: {e}")
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


async def initialize_fiche_state_system():
    """
    Initialize the fiche state management system.

    This should be called during application startup to:
    1. Perform recovery of any orphaned courses (MUST happen before fiche recovery)
    2. Perform recovery of any orphaned commis jobs
    3. Perform recovery of any orphaned runner jobs
    4. Perform recovery of any stuck fiches (after courses are cleared)
    5. Initialize the proper locking system

    IMPORTANT: Course/job recovery must happen BEFORE fiche recovery because
    fiche recovery skips fiches with active courses. If we recover fiches first,
    fiches with stuck courses won't be reset. Then when course recovery marks those
    courses as FAILED, the fiche stays stuck in "running" forever.
    """
    logger.info("Initializing fiche state management system...")

    try:
        # Step 1: Recover courses FIRST (so fiches no longer have "active courses")
        recovered_courses = await perform_startup_course_recovery()

        # Step 2: Recover commis jobs
        recovered_commis_jobs = await perform_startup_commis_job_recovery()

        # Step 3: Recover runner jobs
        recovered_runner_jobs = await perform_startup_runner_job_recovery()

        # Step 4: Recover fiches LAST (now will correctly see no active courses)
        recovered_fiches = await perform_startup_fiche_recovery()

        # Step 5: Check advisory lock support for future enhancement
        advisory_locks_available = check_postgresql_advisory_lock_support()

        # Log summary
        total_recovered = len(recovered_fiches) + len(recovered_courses) + len(recovered_commis_jobs) + len(recovered_runner_jobs)

        if total_recovered > 0:
            logger.info(
                f"🔧 Startup recovery complete: "
                f"{len(recovered_fiches)} fiches, "
                f"{len(recovered_courses)} courses, "
                f"{len(recovered_commis_jobs)} commis jobs, "
                f"{len(recovered_runner_jobs)} runner jobs"
            )

        if advisory_locks_available:
            logger.info("✅ Fiche state system initialized with PostgreSQL advisory lock support")
        else:
            logger.info("✅ Fiche state system initialized with startup recovery only")

        return {
            "recovered_fiches": recovered_fiches,
            "recovered_courses": recovered_courses,
            "recovered_commis_jobs": recovered_commis_jobs,
            "recovered_runner_jobs": recovered_runner_jobs,
            "advisory_locks_available": advisory_locks_available,
        }

    except Exception as e:
        logger.error(f"❌ Failed to initialize fiche state system: {e}")
        raise
