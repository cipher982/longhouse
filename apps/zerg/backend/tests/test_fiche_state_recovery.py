"""
Tests for the fiche state recovery system.

Tests the startup recovery mechanism that prevents stuck fiches, runs, and jobs.
"""

from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from zerg.crud.crud import create_fiche
from zerg.models.enums import CourseStatus
from zerg.models.models import Fiche
from zerg.models.models import Course
from zerg.models.models import RunnerJob
from zerg.models.models import Thread
from zerg.models.models import CommisJob
from zerg.services.fiche_state_recovery import check_postgresql_advisory_lock_support
from zerg.services.fiche_state_recovery import initialize_fiche_state_system
from zerg.services.fiche_state_recovery import perform_startup_fiche_recovery
from zerg.services.fiche_state_recovery import perform_startup_course_recovery
from zerg.services.fiche_state_recovery import perform_startup_runner_job_recovery
from zerg.services.fiche_state_recovery import perform_startup_commis_job_recovery


class TestFicheStateRecovery:
    """Test fiche state recovery functionality."""

    @pytest.mark.asyncio
    async def test_startup_recovery_no_stuck_fiches(self, db_session: Session):
        """Test startup recovery when no fiches are stuck."""
        # Create some normal fiches
        fiche1 = create_fiche(
            db_session,
            owner_id=1,
            name="Normal Fiche 1",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-mock",
        )

        fiche2 = create_fiche(
            db_session,
            owner_id=1,
            name="Normal Fiche 2",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-mock",
        )

        # Both should be idle by default
        assert fiche1.status == "idle"
        assert fiche2.status == "idle"

        # Recovery should find no stuck fiches
        with patch("zerg.services.fiche_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered = await perform_startup_fiche_recovery()

        assert recovered == []

    @pytest.mark.asyncio
    async def test_startup_recovery_with_stuck_fiches(self, db_session: Session):
        """Test startup recovery finds and fixes stuck fiches."""
        # Create fiches
        fiche1 = create_fiche(
            db_session,
            owner_id=1,
            name="Stuck Fiche 1",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-mock",
        )

        fiche2 = create_fiche(
            db_session,
            owner_id=1,
            name="Normal Fiche",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-mock",
        )

        # Manually set fiche1 to running status (simulating stuck state)
        db_session.query(Fiche).filter(Fiche.id == fiche1.id).update({"status": "running"})
        db_session.commit()

        # Verify setup
        stuck_fiche = db_session.query(Fiche).filter(Fiche.id == fiche1.id).first()
        assert stuck_fiche.status == "running"

        # Run recovery
        with patch("zerg.services.fiche_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered = await perform_startup_fiche_recovery()

        # Should have recovered the stuck fiche
        assert fiche1.id in recovered
        assert fiche2.id not in recovered

        # Verify fiche1 was fixed
        recovered_fiche = db_session.query(Fiche).filter(Fiche.id == fiche1.id).first()
        assert recovered_fiche.status == "idle"
        assert "Recovered from stuck coursening state" in recovered_fiche.last_error

        # Verify fiche2 was untouched
        normal_fiche = db_session.query(Fiche).filter(Fiche.id == fiche2.id).first()
        assert normal_fiche.status == "idle"
        assert normal_fiche.last_error is None

    @pytest.mark.asyncio
    async def test_startup_recovery_with_active_runs(self, db_session: Session):
        """Test that fiches with active runs are NOT recovered."""
        # Create fiche and thread
        fiche = create_fiche(
            db_session,
            owner_id=1,
            name="Fiche with Active Run",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-mock",
        )

        thread = Thread(fiche_id=fiche.id, title="Test Thread", thread_type="manual")
        db_session.add(thread)
        db_session.flush()

        # Set fiche to running status
        db_session.query(Fiche).filter(Fiche.id == fiche.id).update({"status": "running"})

        # Create an active run for this fiche
        run = Course(fiche_id=fiche.id, thread_id=thread.id, status="running", trigger="manual")
        db_session.add(run)
        db_session.commit()

        # Run recovery
        with patch("zerg.services.fiche_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered = await perform_startup_fiche_recovery()

        # Should NOT recover this fiche because it has an active run
        assert fiche.id not in recovered

        # Fiche should still be running
        fiche_after = db_session.query(Fiche).filter(Fiche.id == fiche.id).first()
        assert fiche_after.status == "running"

    @pytest.mark.asyncio
    async def test_startup_recovery_uppercase_status(self, db_session: Session):
        """Test recovery handles uppercase RUNNING status."""
        # Create fiche
        fiche = create_fiche(
            db_session,
            owner_id=1,
            name="Uppercase Stuck Fiche",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-mock",
        )

        # Set to uppercase RUNNING status
        db_session.query(Fiche).filter(Fiche.id == fiche.id).update({"status": "RUNNING"})
        db_session.commit()

        # Run recovery
        with patch("zerg.services.fiche_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered = await perform_startup_fiche_recovery()

        # Should recover the uppercase status fiche
        assert fiche.id in recovered

        # Verify it was fixed
        recovered_fiche = db_session.query(Fiche).filter(Fiche.id == fiche.id).first()
        assert recovered_fiche.status == "idle"

    def test_postgresql_advisory_lock_support(self, db_session: Session):
        """Test PostgreSQL advisory lock support detection."""
        with patch("zerg.services.fiche_state_recovery.get_session_factory", return_value=lambda: db_session):
            supported = check_postgresql_advisory_lock_support()

        # Should return True for PostgreSQL (which our test uses)
        assert isinstance(supported, bool)
        # We can't guarantee the specific result as it depends on the test database

    @pytest.mark.asyncio
    async def test_initialize_fiche_state_system(self, db_session: Session):
        """Test full initialization of the fiche state system."""
        # Create a stuck fiche
        fiche = create_fiche(
            db_session,
            owner_id=1,
            name="Initialization Test Fiche",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-mock",
        )

        # Set to running status
        db_session.query(Fiche).filter(Fiche.id == fiche.id).update({"status": "running"})
        db_session.commit()

        # Initialize the system
        with patch("zerg.services.fiche_state_recovery.get_session_factory", return_value=lambda: db_session):
            result = await initialize_fiche_state_system()

        # Should have results
        assert "recovered_fiches" in result
        assert "advisory_locks_available" in result
        assert fiche.id in result["recovered_fiches"]
        assert isinstance(result["advisory_locks_available"], bool)

        # Fiche should be recovered
        recovered_fiche = db_session.query(Fiche).filter(Fiche.id == fiche.id).first()
        assert recovered_fiche.status == "idle"

        # Check new recovery result keys exist
        assert "recovered_runs" in result
        assert "recovered_commis_jobs" in result
        assert "recovered_runner_jobs" in result

    @pytest.mark.asyncio
    async def test_ordering_fiche_with_stuck_course_is_recovered(self, db_session: Session):
        """Test that fiches with stuck courses are properly recovered.

        This tests the critical ordering requirement: run recovery must happen
        BEFORE fiche recovery, otherwise fiches with stuck courses stay in "running"
        forever.

        Scenario:
        1. Fiche status = "running"
        2. Fiche has a run with status = "RUNNING"
        3. After initialize_fiche_state_system():
           - Run should be FAILED
           - Fiche should be idle
        """
        # Create fiche in running state
        fiche = create_fiche(
            db_session,
            owner_id=1,
            name="Fiche With Stuck Run",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-mock",
        )
        db_session.query(Fiche).filter(Fiche.id == fiche.id).update({"status": "running"})

        # Create a stuck course for this fiche
        thread = Thread(fiche_id=fiche.id, title="Test Thread", thread_type="manual")
        db_session.add(thread)
        db_session.flush()

        stuck_run = Course(fiche_id=fiche.id, thread_id=thread.id, status="RUNNING", trigger="manual")
        db_session.add(stuck_run)
        db_session.commit()

        # Verify initial state
        assert db_session.query(Fiche).filter(Fiche.id == fiche.id).first().status == "running"
        assert db_session.query(Course).filter(Course.id == stuck_run.id).first().status == "RUNNING"

        # Run full initialization (the ordering is what we're testing)
        with patch("zerg.services.fiche_state_recovery.get_session_factory", return_value=lambda: db_session):
            result = await initialize_fiche_state_system()

        # Verify results
        db_session.expire_all()

        # Run should be recovered to FAILED
        recovered_run = db_session.query(Course).filter(Course.id == stuck_run.id).first()
        assert recovered_run.status == CourseStatus.FAILED.value
        assert stuck_run.id in result["recovered_runs"]

        # Fiche should be recovered to idle (this only works if run recovery happened first!)
        recovered_fiche = db_session.query(Fiche).filter(Fiche.id == fiche.id).first()
        assert recovered_fiche.status == "idle"
        assert fiche.id in result["recovered_fiches"]


class TestRunRecovery:
    """Test Course recovery functionality."""

    @pytest.mark.asyncio
    async def test_run_recovery_no_stuck_runs(self, db_session: Session):
        """Test run recovery when no runs are stuck."""
        # Create an fiche and thread for the run
        fiche = create_fiche(
            db_session,
            owner_id=1,
            name="Test Fiche",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-mock",
        )
        thread = Thread(fiche_id=fiche.id, title="Test Thread", thread_type="manual")
        db_session.add(thread)
        db_session.flush()

        # Create a completed run
        run = Course(fiche_id=fiche.id, thread_id=thread.id, status="SUCCESS", trigger="manual")
        db_session.add(run)
        db_session.commit()

        # Recovery should find no stuck courses
        with patch("zerg.services.fiche_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered = await perform_startup_course_recovery()

        assert recovered == []

    @pytest.mark.asyncio
    async def test_run_recovery_with_stuck_runs(self, db_session: Session):
        """Test run recovery finds and fixes stuck courses."""
        # Create an fiche and thread
        fiche = create_fiche(
            db_session,
            owner_id=1,
            name="Test Fiche",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-mock",
        )
        thread = Thread(fiche_id=fiche.id, title="Test Thread", thread_type="manual")
        db_session.add(thread)
        db_session.flush()

        # Create a stuck course
        stuck_run = Course(fiche_id=fiche.id, thread_id=thread.id, status="RUNNING", trigger="manual")
        db_session.add(stuck_run)
        db_session.flush()
        stuck_course_id = stuck_run.id

        # Create a completed run (should not be affected)
        completed_run = Course(fiche_id=fiche.id, thread_id=thread.id, status="SUCCESS", trigger="manual")
        db_session.add(completed_run)
        db_session.commit()

        # Run recovery
        with patch("zerg.services.fiche_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered = await perform_startup_course_recovery()

        # Should have recovered the stuck course
        assert stuck_course_id in recovered
        assert completed_run.id not in recovered

        # Verify stuck course was fixed
        db_session.expire_all()
        fixed_run = db_session.query(Course).filter(Course.id == stuck_course_id).first()
        assert fixed_run.status == CourseStatus.FAILED.value
        assert "Orphaned after server restart" in fixed_run.error

    @pytest.mark.asyncio
    async def test_run_recovery_handles_queued_and_deferred(self, db_session: Session):
        """Test run recovery handles QUEUED and DEFERRED statuses."""
        # Create an fiche and thread
        fiche = create_fiche(
            db_session,
            owner_id=1,
            name="Test Fiche",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-mock",
        )
        thread = Thread(fiche_id=fiche.id, title="Test Thread", thread_type="manual")
        db_session.add(thread)
        db_session.flush()

        # Create runs in different stuck states
        queued_run = Course(fiche_id=fiche.id, thread_id=thread.id, status="QUEUED", trigger="manual")
        deferred_run = Course(fiche_id=fiche.id, thread_id=thread.id, status="DEFERRED", trigger="manual")
        db_session.add(queued_run)
        db_session.add(deferred_run)
        db_session.commit()

        # Run recovery
        with patch("zerg.services.fiche_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered = await perform_startup_course_recovery()

        # Both should be recovered
        assert queued_run.id in recovered
        assert deferred_run.id in recovered


class TestCommisJobRecovery:
    """Test CommisJob recovery functionality."""

    @pytest.mark.asyncio
    async def test_commis_job_recovery_no_stuck_jobs(self, db_session: Session, test_user):
        """Test commis job recovery when no jobs are stuck."""
        # Create a completed commis job
        job = CommisJob(owner_id=test_user.id, task="Test task", status="success")
        db_session.add(job)
        db_session.commit()

        # Recovery should find no stuck jobs
        with patch("zerg.services.fiche_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered = await perform_startup_commis_job_recovery()

        assert recovered == []

    @pytest.mark.asyncio
    async def test_commis_job_recovery_with_stuck_jobs(self, db_session: Session, test_user):
        """Test commis job recovery finds and fixes stuck coursening jobs.

        Note: Only "running" jobs are recovered. "Queued" jobs are NOT recovered
        because CommisJobProcessor is designed to resume them after restart.
        """
        # Create a stuck coursening job (should be recovered)
        stuck_job = CommisJob(owner_id=test_user.id, task="Stuck task", status="running")
        db_session.add(stuck_job)
        db_session.flush()
        stuck_job_id = stuck_job.id

        # Create a queued job (should NOT be recovered - it's resumable)
        queued_job = CommisJob(owner_id=test_user.id, task="Queued task", status="queued")
        db_session.add(queued_job)
        db_session.flush()
        queued_job_id = queued_job.id

        # Create a completed job (should not be affected)
        completed_job = CommisJob(owner_id=test_user.id, task="Completed task", status="success")
        db_session.add(completed_job)
        db_session.commit()

        # Run recovery
        with patch("zerg.services.fiche_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered = await perform_startup_commis_job_recovery()

        # Should only recover running jobs, not queued
        assert stuck_job_id in recovered
        assert queued_job_id not in recovered  # Queued jobs are resumable
        assert completed_job.id not in recovered

        # Verify running job was failed
        db_session.expire_all()
        fixed_running = db_session.query(CommisJob).filter(CommisJob.id == stuck_job_id).first()
        assert fixed_running.status == "failed"
        assert "Orphaned after server restart" in fixed_running.error

        # Verify queued job was NOT changed
        still_queued = db_session.query(CommisJob).filter(CommisJob.id == queued_job_id).first()
        assert still_queued.status == "queued"


class TestRunnerJobRecovery:
    """Test RunnerJob recovery functionality."""

    @pytest.mark.asyncio
    async def test_runner_job_recovery_no_stuck_jobs(self, db_session: Session, test_user):
        """Test runner job recovery when no jobs are stuck."""
        # Create a completed runner job (need a runner first)
        from zerg.crud import runner_crud

        runner = runner_crud.create_runner(
            db_session,
            owner_id=test_user.id,
            name="test-runner",
            auth_secret="fake-secret",
            capabilities=["exec.readonly"],
        )

        # Use CRUD function which handles ID generation
        job = runner_crud.create_runner_job(
            db_session,
            owner_id=test_user.id,
            runner_id=runner.id,
            command="echo test",
            timeout_secs=30,
        )
        # Mark as completed (create_runner_job creates with queued status)
        db_session.query(RunnerJob).filter(RunnerJob.id == job.id).update({"status": "completed"})
        db_session.commit()

        # Recovery should find no stuck jobs
        with patch("zerg.services.fiche_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered = await perform_startup_runner_job_recovery()

        assert recovered == []

    @pytest.mark.asyncio
    async def test_runner_job_recovery_with_stuck_jobs(self, db_session: Session, test_user):
        """Test runner job recovery finds and fixes stuck jobs."""
        from zerg.crud import runner_crud

        runner = runner_crud.create_runner(
            db_session,
            owner_id=test_user.id,
            name="test-runner",
            auth_secret="fake-secret",
            capabilities=["exec.readonly"],
        )

        # Create jobs using CRUD function (handles ID generation)
        stuck_job = runner_crud.create_runner_job(
            db_session,
            owner_id=test_user.id,
            runner_id=runner.id,
            command="echo stuck",
            timeout_secs=30,
        )
        # Set to running status
        db_session.query(RunnerJob).filter(RunnerJob.id == stuck_job.id).update({"status": "running"})
        stuck_job_id = stuck_job.id

        queued_job = runner_crud.create_runner_job(
            db_session,
            owner_id=test_user.id,
            runner_id=runner.id,
            command="echo queued",
            timeout_secs=30,
        )
        # Keep as queued (default status)
        queued_job_id = queued_job.id

        completed_job = runner_crud.create_runner_job(
            db_session,
            owner_id=test_user.id,
            runner_id=runner.id,
            command="echo done",
            timeout_secs=30,
        )
        # Mark as completed
        db_session.query(RunnerJob).filter(RunnerJob.id == completed_job.id).update({"status": "completed"})
        db_session.commit()

        # Run recovery
        with patch("zerg.services.fiche_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered = await perform_startup_runner_job_recovery()

        # Should have recovered both stuck jobs
        assert stuck_job_id in recovered
        assert queued_job_id in recovered
        assert completed_job.id not in recovered

        # Verify stuck jobs were fixed
        db_session.expire_all()
        fixed_running = db_session.query(RunnerJob).filter(RunnerJob.id == stuck_job_id).first()
        fixed_queued = db_session.query(RunnerJob).filter(RunnerJob.id == queued_job_id).first()

        assert fixed_running.status == "failed"
        assert "Orphaned after server restart" in fixed_running.error
        assert fixed_queued.status == "failed"
        assert "Orphaned after server restart" in fixed_queued.error


class TestRecoveryIdempotency:
    """Test that recovery functions are idempotent (safe to run multiple times)."""

    @pytest.mark.asyncio
    async def test_run_recovery_idempotent(self, db_session: Session):
        """Test run recovery can be run multiple times safely."""
        # Create an fiche and thread
        fiche = create_fiche(
            db_session,
            owner_id=1,
            name="Test Fiche",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-mock",
        )
        thread = Thread(fiche_id=fiche.id, title="Test Thread", thread_type="manual")
        db_session.add(thread)
        db_session.flush()

        # Create a stuck course
        stuck_run = Course(fiche_id=fiche.id, thread_id=thread.id, status="RUNNING", trigger="manual")
        db_session.add(stuck_run)
        db_session.commit()
        stuck_course_id = stuck_run.id

        # Run recovery twice
        with patch("zerg.services.fiche_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered1 = await perform_startup_course_recovery()
            recovered2 = await perform_startup_course_recovery()

        # First call should recover, second should find nothing
        assert stuck_course_id in recovered1
        assert recovered2 == []

    @pytest.mark.asyncio
    async def test_commis_job_recovery_idempotent(self, db_session: Session, test_user):
        """Test commis job recovery can be run multiple times safely."""
        stuck_job = CommisJob(owner_id=test_user.id, task="Stuck task", status="running")
        db_session.add(stuck_job)
        db_session.commit()
        stuck_job_id = stuck_job.id

        # Run recovery twice
        with patch("zerg.services.fiche_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered1 = await perform_startup_commis_job_recovery()
            recovered2 = await perform_startup_commis_job_recovery()

        # First call should recover, second should find nothing
        assert stuck_job_id in recovered1
        assert recovered2 == []
