"""Tests for barrier-based parallel commis coordination.

These tests verify the race condition handling in the parallel-first architecture:
- Double resume prevention (atomic SELECT FOR UPDATE)
- Fast commis race (two-phase commit)
- Timeout reaper (deadline-based expiration)
- Batch re-interrupt barrier reset (reusing barriers)
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zerg.crud import crud
from zerg.models.enums import CourseStatus, CourseTrigger
from zerg.models.models import Course, CommisJob
from zerg.models.commis_barrier import CommisBarrierJob, CommisBarrier


@pytest.fixture
def sample_barrier_setup(db_session, sample_fiche):
    """Create a barrier with multiple commis for testing."""
    # Create thread (note: owner_id comes from fiche)
    thread = crud.create_thread(
        db=db_session,
        fiche_id=sample_fiche.id,
        title="Test parallel thread",
        active=True,
    )

    # Create concierge run in WAITING status
    run = Course(
        thread_id=thread.id,
        fiche_id=sample_fiche.id,
        status=CourseStatus.WAITING,
        trigger=CourseTrigger.CHAT,
        trace_id="12345678-1234-5678-1234-567812345678",
        started_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db_session.add(run)
    db_session.flush()

    # Create 3 commis jobs (owner_id from fiche)
    jobs = []
    for i in range(3):
        job = CommisJob(
            task=f"Task {i+1}",
            status="queued",
            concierge_course_id=run.id,
            owner_id=sample_fiche.owner_id,
        )
        db_session.add(job)
        db_session.flush()
        jobs.append(job)

    # Create barrier
    barrier = CommisBarrier(
        course_id=run.id,
        expected_count=3,
        completed_count=0,
        status="waiting",
        deadline_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10),
    )
    db_session.add(barrier)
    db_session.flush()

    # Create barrier jobs
    for i, job in enumerate(jobs):
        barrier_job = CommisBarrierJob(
            barrier_id=barrier.id,
            job_id=job.id,
            tool_call_id=f"tool_call_{i+1}",
            status="queued",
        )
        db_session.add(barrier_job)

    db_session.commit()

    return {
        "thread": thread,
        "run": run,
        "jobs": jobs,
        "barrier": barrier,
    }


@pytest.mark.timeout(30)
class TestDoubleResumePrevention:
    """Test that only one commis triggers resume when multiple complete simultaneously."""

    @pytest.mark.asyncio
    async def test_atomic_barrier_check(self, db_session, sample_barrier_setup):
        """Two commis completing simultaneously should only trigger one resume."""
        from zerg.services.commis_resume import check_and_resume_if_all_complete

        run = sample_barrier_setup["run"]
        jobs = sample_barrier_setup["jobs"]

        # Complete first two commis (leaving one)
        for job in jobs[:2]:
            barrier_job = (
                db_session.query(CommisBarrierJob)
                .filter(CommisBarrierJob.job_id == job.id)
                .first()
            )
            barrier_job.status = "completed"
            barrier_job.result = f"Result for {job.task}"

        sample_barrier_setup["barrier"].completed_count = 2
        db_session.commit()

        # Simulate the last commis completing - should trigger resume
        with patch("zerg.services.commis_resume.resume_concierge_batch", new_callable=AsyncMock) as mock_resume:
            mock_resume.return_value = {"status": "success"}

            result = await check_and_resume_if_all_complete(
                db=db_session,
                course_id=run.id,
                job_id=jobs[2].id,
                result="Final result",
            )

            # Should return resume status
            assert result["status"] == "resume"
            assert "commis_results" in result
            assert len(result["commis_results"]) == 3

    @pytest.mark.asyncio
    async def test_barrier_already_resuming_returns_skipped(self, db_session, sample_barrier_setup):
        """Commis completing after resume started should be skipped."""
        from zerg.services.commis_resume import check_and_resume_if_all_complete

        run = sample_barrier_setup["run"]
        jobs = sample_barrier_setup["jobs"]

        # Set barrier to already resuming
        sample_barrier_setup["barrier"].status = "resuming"
        db_session.commit()

        result = await check_and_resume_if_all_complete(
            db=db_session,
            course_id=run.id,
            job_id=jobs[0].id,
            result="Late result",
        )

        assert result["status"] == "skipped"
        assert "not waiting" in result["reason"]


@pytest.mark.timeout(30)
class TestTwoPhaseCommit:
    """Test two-phase commit pattern for fast commis race prevention."""

    def test_commis_job_starts_with_created_status(self, db_session, sample_fiche):
        """Commis jobs should start with 'created' status for two-phase pattern.

        The two-phase commit pattern requires:
        1. Jobs created with status='created' (not immediately visible to commis)
        2. After barrier exists, flip status to 'queued' (commis can pick up)

        This test verifies the DB model allows 'created' status.
        """
        thread = crud.create_thread(
            db=db_session,
            fiche_id=sample_fiche.id,
            title="Test thread",
            active=True,
        )

        run = Course(
            thread_id=thread.id,
            fiche_id=sample_fiche.id,
            status=CourseStatus.RUNNING,
            trigger=CourseTrigger.CHAT,
        )
        db_session.add(run)
        db_session.flush()

        # Create job with 'created' status (two-phase commit initial state)
        job = CommisJob(
            task="Test task",
            status="created",  # Two-phase: starts as 'created', not 'queued'
            concierge_course_id=run.id,
            owner_id=sample_fiche.owner_id,
        )
        db_session.add(job)
        db_session.commit()

        # Verify job was created with 'created' status
        retrieved = db_session.query(CommisJob).filter(CommisJob.id == job.id).first()
        assert retrieved is not None
        assert retrieved.status == "created"

        # Commis should NOT pick up 'created' jobs (only 'queued')
        queued_jobs = (
            db_session.query(CommisJob)
            .filter(CommisJob.status == "queued")
            .all()
        )
        assert len(queued_jobs) == 0  # No queued jobs yet

        # After barrier exists, flip to 'queued'
        retrieved.status = "queued"
        db_session.commit()

        # Now commis can pick it up
        queued_jobs = (
            db_session.query(CommisJob)
            .filter(CommisJob.status == "queued")
            .all()
        )
        assert len(queued_jobs) == 1


@pytest.mark.timeout(30)
class TestTimeoutReaper:
    """Test timeout reaper for expired barriers."""

    @pytest.mark.asyncio
    async def test_reaper_finds_expired_barriers(self, db_session, sample_barrier_setup):
        """Reaper should find and handle expired barriers."""
        from zerg.services.commis_resume import reap_expired_barriers

        # Set barrier deadline to past
        barrier = sample_barrier_setup["barrier"]
        barrier.deadline_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
        db_session.commit()

        # Mock resume to avoid actual concierge execution
        with patch("zerg.services.commis_resume.resume_concierge_batch", new_callable=AsyncMock) as mock_resume:
            mock_resume.return_value = {"status": "success"}

            result = await reap_expired_barriers(db_session)

        assert result["reaped"] == 1
        assert len(result["details"]) == 1
        assert result["details"][0]["barrier_id"] == barrier.id

        # Verify incomplete jobs marked as timeout
        for barrier_job in db_session.query(CommisBarrierJob).filter(CommisBarrierJob.barrier_id == barrier.id).all():
            assert barrier_job.status == "timeout"

    @pytest.mark.asyncio
    async def test_reaper_ignores_non_expired_barriers(self, db_session, sample_barrier_setup):
        """Reaper should not touch barriers that haven't expired."""
        from zerg.services.commis_resume import reap_expired_barriers

        # Barrier deadline is in the future (from fixture)
        result = await reap_expired_barriers(db_session)

        assert result["reaped"] == 0

    @pytest.mark.asyncio
    async def test_reaper_ignores_completed_barriers(self, db_session, sample_barrier_setup):
        """Reaper should not touch completed barriers even if expired."""
        from zerg.services.commis_resume import reap_expired_barriers

        barrier = sample_barrier_setup["barrier"]
        barrier.status = "completed"
        barrier.deadline_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
        db_session.commit()

        result = await reap_expired_barriers(db_session)

        assert result["reaped"] == 0


@pytest.mark.timeout(30)
class TestBarrierErrorHandling:
    """Test barrier state management on error paths."""

    @pytest.mark.asyncio
    async def test_barrier_marked_failed_on_error(self, db_session, sample_barrier_setup):
        """Barrier should be marked as failed when resume fails."""
        from zerg.services.commis_resume import resume_concierge_batch

        run = sample_barrier_setup["run"]
        barrier = sample_barrier_setup["barrier"]

        # Complete all barrier jobs
        for bj in db_session.query(CommisBarrierJob).filter(CommisBarrierJob.barrier_id == barrier.id).all():
            bj.status = "completed"
            bj.result = "Result"
        barrier.completed_count = 3
        barrier.status = "resuming"  # Simulating resume in progress
        db_session.commit()

        commis_results = [
            {"tool_call_id": f"tool_call_{i+1}", "result": f"Result {i+1}", "error": None, "status": "completed"}
            for i in range(3)
        ]

        # Mock to force an exception
        with patch("zerg.managers.fiche_runner.FicheRunner") as mock_runner:
            mock_runner.return_value.run_batch_continuation = AsyncMock(
                side_effect=Exception("Simulated failure")
            )

            result = await resume_concierge_batch(
                db=db_session,
                course_id=run.id,
                commis_results=commis_results,
            )

        assert result["status"] == "error"

        # Verify barrier marked as failed
        db_session.refresh(barrier)
        assert barrier.status == "failed"


@pytest.mark.timeout(30)
class TestBatchReinterrupt:
    """Test barrier reset when batch continuation spawns more commis."""

    @pytest.mark.asyncio
    async def test_barrier_reused_on_reinterrupt(self, db_session, sample_barrier_setup):
        """Existing barrier should be reused when batch continuation spawns more commis."""
        from zerg.managers.fiche_runner import CourseInterrupted
        from zerg.services.commis_resume import resume_concierge_batch

        run = sample_barrier_setup["run"]
        barrier = sample_barrier_setup["barrier"]
        original_barrier_id = barrier.id

        # Complete all current barrier jobs
        for bj in db_session.query(CommisBarrierJob).filter(CommisBarrierJob.barrier_id == barrier.id).all():
            bj.status = "completed"
            bj.result = "Result"
        barrier.completed_count = 3
        barrier.status = "resuming"
        db_session.commit()

        commis_results = [
            {"tool_call_id": f"tool_call_{i+1}", "result": f"Result {i+1}", "error": None, "status": "completed"}
            for i in range(3)
        ]

        # Create new commis jobs for re-interrupt (get owner_id from fiche)
        new_jobs = []
        for i in range(2):
            job = CommisJob(
                task=f"New task {i+1}",
                status="created",
                concierge_course_id=run.id,
                owner_id=run.fiche.owner_id,
            )
            db_session.add(job)
            db_session.flush()
            new_jobs.append(job)
        db_session.commit()

        # Mock batch continuation to raise CourseInterrupted with new commis
        interrupt_value = {
            "type": "commis_pending",
            "job_ids": [j.id for j in new_jobs],
            "created_jobs": [{"job": j, "tool_call_id": f"new_tool_{i}"} for i, j in enumerate(new_jobs)],
        }

        with patch("zerg.managers.fiche_runner.FicheRunner") as mock_runner:
            mock_instance = MagicMock()
            mock_instance.run_batch_continuation = AsyncMock(
                side_effect=CourseInterrupted(interrupt_value)
            )
            mock_instance.usage_total_tokens = 100
            mock_runner.return_value = mock_instance

            result = await resume_concierge_batch(
                db=db_session,
                course_id=run.id,
                commis_results=commis_results,
            )

        assert result["status"] == "waiting"

        # Verify barrier was REUSED (same ID) and reset
        db_session.refresh(barrier)
        assert barrier.id == original_barrier_id
        assert barrier.status == "waiting"
        assert barrier.expected_count == 2
        assert barrier.completed_count == 0

        # Verify new BarrierJobs created
        new_commis_barrier_jobs = (
            db_session.query(CommisBarrierJob)
            .filter(CommisBarrierJob.barrier_id == barrier.id, CommisBarrierJob.status == "queued")
            .all()
        )
        # Should have 2 new jobs (the old completed ones are still there)
        assert len(new_commis_barrier_jobs) >= 2
