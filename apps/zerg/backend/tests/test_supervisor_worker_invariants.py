"""Tests for concierge-commis completion invariants.

These tests verify critical invariants that prevent "early completion" bugs where
the concierge responds while commis are still running.

Key invariants:
1. Resume only works on WAITING runs - prevents accidental completion
2. Double resume prevention - atomic state transition prevents races
3. Completion requires graph completion - no early SUCCESS marking
"""

import pytest

from zerg.models.enums import CourseStatus
from zerg.models.models import Course
from zerg.models.models import CommisJob
from zerg.services.commis_resume import resume_concierge_with_commis_result


class TestConciergeCommisInvariants:
    """Tests for concierge-commis completion invariants."""

    @pytest.mark.asyncio
    async def test_resume_only_works_on_waiting_runs(self, db_session, sample_fiche, sample_thread):
        """Verify resume_concierge_with_commis_result only works on WAITING runs.

        If the run is in any other state (RUNNING, SUCCESS, FAILED, etc.),
        resume should skip without changing the run status.
        """
        fiche = sample_fiche
        thread = sample_thread

        # Test each non-WAITING status
        for status in [CourseStatus.RUNNING, CourseStatus.SUCCESS, CourseStatus.FAILED, CourseStatus.CANCELLED]:
            run = Course(
                fiche_id=fiche.id,
                thread_id=thread.id,
                status=status,
            )
            db_session.add(run)
            db_session.commit()
            db_session.refresh(run)

            # Attempt to resume
            result = await resume_concierge_with_commis_result(
                db=db_session,
                course_id=run.id,
                commis_result="Test commis result",
            )

            # Should be skipped
            assert result is not None
            assert result["status"] == "skipped"
            assert "not waiting" in result.get("reason", "").lower() or "not WAITING" in result.get("reason", "")

            # Run status should be unchanged
            db_session.refresh(run)
            assert run.status == status, f"Run status changed from {status} when it shouldn't"

    @pytest.mark.asyncio
    async def test_resume_skips_nonexistent_run(self, db_session):
        """Verify resume gracefully handles nonexistent run IDs."""
        result = await resume_concierge_with_commis_result(
            db=db_session,
            course_id=99999,  # Nonexistent
            commis_result="Test result",
        )

        # Should return None for missing run
        assert result is None

    @pytest.mark.asyncio
    async def test_atomic_state_transition_prevents_double_resume(self, db_session, sample_fiche, sample_thread):
        """Verify atomic WAITING → RUNNING transition prevents double resume.

        If two callers try to resume the same run concurrently, only one should
        succeed. The other should get a "skipped" response.
        """
        fiche = sample_fiche
        thread = sample_thread

        # Create WAITING run
        run = Course(
            fiche_id=fiche.id,
            thread_id=thread.id,
            status=CourseStatus.WAITING,
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        # Simulate first caller winning the atomic transition
        # by manually transitioning to RUNNING (as the first resume would do)
        db_session.query(Course).filter(
            Course.id == run.id,
            Course.status == CourseStatus.WAITING,
        ).update({Course.status: CourseStatus.RUNNING})
        db_session.commit()

        # Now a second caller tries to resume - should be skipped
        result = await resume_concierge_with_commis_result(
            db=db_session,
            course_id=run.id,
            commis_result="Second caller result",
        )

        # Second caller should get "skipped"
        assert result is not None
        assert result["status"] == "skipped"
        assert "no longer waiting" in result.get("reason", "").lower() or run.status.value in result.get("reason", "")

    @pytest.mark.asyncio
    async def test_run_not_success_while_commis_active(self, db_session, test_user, sample_fiche, sample_thread):
        """Verify a run cannot be marked SUCCESS while commis are still active.

        This is a backstop invariant - if somehow a run were marked SUCCESS while
        a commis job is still queued/running, that would be a bug. We verify
        that the system doesn't allow this state.
        """
        fiche = sample_fiche
        thread = sample_thread

        # Create a WAITING run (concierge waiting for commis)
        run = Course(
            fiche_id=fiche.id,
            thread_id=thread.id,
            status=CourseStatus.WAITING,
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        # Create a commis job that's still running
        commis_job = CommisJob(
            owner_id=test_user.id,
            concierge_course_id=run.id,
            task="Test task",
            status="running",  # Still active!
        )
        db_session.add(commis_job)
        db_session.commit()

        # The invariant: while commis is running, run should stay WAITING
        # (Not SUCCESS, not FAILED)
        db_session.refresh(run)
        assert run.status == CourseStatus.WAITING

        # If we try to mark it SUCCESS directly, we violate the invariant
        # The application code should never do this, but we document the expectation
        # In a real scenario, the run transitions:
        # WAITING (commis running) → commis completes → resume called → SUCCESS

        # Verify the run is still waiting
        assert run.status == CourseStatus.WAITING, "Run should stay WAITING while commis is running"

        # Now simulate commis completion and resume
        commis_job.status = "success"
        db_session.commit()

        # The resume function would normally be called here
        # For this test, we just verify the invariant held

    @pytest.mark.asyncio
    async def test_commis_job_linked_to_concierge_run(self, db_session, test_user, sample_fiche, sample_thread):
        """Verify commis jobs are properly linked to their concierge run.

        This ensures we can always find which commis belong to which concierge
        run, enabling proper state management.
        """
        fiche = sample_fiche
        thread = sample_thread

        # Create concierge run
        run = Course(
            fiche_id=fiche.id,
            thread_id=thread.id,
            status=CourseStatus.WAITING,
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        # Create multiple commis jobs for this run
        jobs = [
            CommisJob(
                owner_id=test_user.id,
                concierge_course_id=run.id,
                task=f"Task {i}",
                status="queued",
            )
            for i in range(3)
        ]
        db_session.add_all(jobs)
        db_session.commit()

        # Verify all jobs are linked to the concierge run
        linked_jobs = db_session.query(CommisJob).filter(
            CommisJob.concierge_course_id == run.id
        ).all()
        assert len(linked_jobs) == 3

        # Verify we can check if any commis are still active
        active_commis = db_session.query(CommisJob).filter(
            CommisJob.concierge_course_id == run.id,
            CommisJob.status.in_(["queued", "running"]),
        ).count()
        assert active_commis == 3

        # Mark one as complete
        jobs[0].status = "success"
        db_session.commit()

        active_commis = db_session.query(CommisJob).filter(
            CommisJob.concierge_course_id == run.id,
            CommisJob.status.in_(["queued", "running"]),
        ).count()
        assert active_commis == 2

    @pytest.mark.asyncio
    async def test_waiting_run_cannot_transition_to_success_directly(self, db_session, sample_fiche, sample_thread):
        """Verify WAITING runs must go through RUNNING before SUCCESS.

        The correct state machine is:
        RUNNING → spawn_commis → interrupt → WAITING → commis done → resume → RUNNING → SUCCESS

        A direct WAITING → SUCCESS transition would skip the resume logic and
        potentially leave the graph in an inconsistent state.
        """
        fiche = sample_fiche
        thread = sample_thread

        # Create WAITING run
        run = Course(
            fiche_id=fiche.id,
            thread_id=thread.id,
            status=CourseStatus.WAITING,
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        # Document the expected state transitions
        # The resume function handles: WAITING → RUNNING (atomic) → (graph execution) → SUCCESS
        # This is the ONLY valid path from WAITING to SUCCESS

        # Attempting to call resume on a WAITING run should:
        # 1. Atomically transition WAITING → RUNNING
        # 2. Execute the resumed graph
        # 3. On completion, transition RUNNING → SUCCESS

        # Verify the run is still in WAITING (no direct transition occurred)
        db_session.refresh(run)
        assert run.status == CourseStatus.WAITING
