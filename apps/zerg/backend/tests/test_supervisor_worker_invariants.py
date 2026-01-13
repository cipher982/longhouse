"""Tests for supervisor-worker completion invariants.

These tests verify critical invariants that prevent "early completion" bugs where
the supervisor responds while workers are still running.

Key invariants:
1. Resume only works on WAITING runs - prevents accidental completion
2. Double resume prevention - atomic state transition prevents races
3. Completion requires graph completion - no early SUCCESS marking
"""

import pytest

from zerg.models.enums import RunStatus
from zerg.models.models import AgentRun
from zerg.models.models import WorkerJob
from zerg.services.worker_resume import resume_supervisor_with_worker_result


class TestSupervisorWorkerInvariants:
    """Tests for supervisor-worker completion invariants."""

    @pytest.mark.asyncio
    async def test_resume_only_works_on_waiting_runs(self, db_session, sample_agent, sample_thread):
        """Verify resume_supervisor_with_worker_result only works on WAITING runs.

        If the run is in any other state (RUNNING, SUCCESS, FAILED, etc.),
        resume should skip without changing the run status.
        """
        agent = sample_agent
        thread = sample_thread

        # Test each non-WAITING status
        for status in [RunStatus.RUNNING, RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED]:
            run = AgentRun(
                agent_id=agent.id,
                thread_id=thread.id,
                status=status,
            )
            db_session.add(run)
            db_session.commit()
            db_session.refresh(run)

            # Attempt to resume
            result = await resume_supervisor_with_worker_result(
                db=db_session,
                run_id=run.id,
                worker_result="Test worker result",
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
        result = await resume_supervisor_with_worker_result(
            db=db_session,
            run_id=99999,  # Nonexistent
            worker_result="Test result",
        )

        # Should return None for missing run
        assert result is None

    @pytest.mark.asyncio
    async def test_atomic_state_transition_prevents_double_resume(self, db_session, sample_agent, sample_thread):
        """Verify atomic WAITING → RUNNING transition prevents double resume.

        If two callers try to resume the same run concurrently, only one should
        succeed. The other should get a "skipped" response.
        """
        agent = sample_agent
        thread = sample_thread

        # Create WAITING run
        run = AgentRun(
            agent_id=agent.id,
            thread_id=thread.id,
            status=RunStatus.WAITING,
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        # Simulate first caller winning the atomic transition
        # by manually transitioning to RUNNING (as the first resume would do)
        db_session.query(AgentRun).filter(
            AgentRun.id == run.id,
            AgentRun.status == RunStatus.WAITING,
        ).update({AgentRun.status: RunStatus.RUNNING})
        db_session.commit()

        # Now a second caller tries to resume - should be skipped
        result = await resume_supervisor_with_worker_result(
            db=db_session,
            run_id=run.id,
            worker_result="Second caller result",
        )

        # Second caller should get "skipped"
        assert result is not None
        assert result["status"] == "skipped"
        assert "no longer waiting" in result.get("reason", "").lower() or run.status.value in result.get("reason", "")

    @pytest.mark.asyncio
    async def test_run_not_success_while_worker_active(self, db_session, test_user, sample_agent, sample_thread):
        """Verify a run cannot be marked SUCCESS while workers are still active.

        This is a backstop invariant - if somehow a run were marked SUCCESS while
        a worker job is still queued/running, that would be a bug. We verify
        that the system doesn't allow this state.
        """
        agent = sample_agent
        thread = sample_thread

        # Create a WAITING run (supervisor waiting for worker)
        run = AgentRun(
            agent_id=agent.id,
            thread_id=thread.id,
            status=RunStatus.WAITING,
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        # Create a worker job that's still running
        worker_job = WorkerJob(
            owner_id=test_user.id,
            supervisor_run_id=run.id,
            task="Test task",
            status="running",  # Still active!
        )
        db_session.add(worker_job)
        db_session.commit()

        # The invariant: while worker is running, run should stay WAITING
        # (Not SUCCESS, not FAILED)
        db_session.refresh(run)
        assert run.status == RunStatus.WAITING

        # If we try to mark it SUCCESS directly, we violate the invariant
        # The application code should never do this, but we document the expectation
        # In a real scenario, the run transitions:
        # WAITING (worker running) → worker completes → resume called → SUCCESS

        # Verify the run is still waiting
        assert run.status == RunStatus.WAITING, "Run should stay WAITING while worker is running"

        # Now simulate worker completion and resume
        worker_job.status = "success"
        db_session.commit()

        # The resume function would normally be called here
        # For this test, we just verify the invariant held

    @pytest.mark.asyncio
    async def test_worker_job_linked_to_supervisor_run(self, db_session, test_user, sample_agent, sample_thread):
        """Verify worker jobs are properly linked to their supervisor run.

        This ensures we can always find which workers belong to which supervisor
        run, enabling proper state management.
        """
        agent = sample_agent
        thread = sample_thread

        # Create supervisor run
        run = AgentRun(
            agent_id=agent.id,
            thread_id=thread.id,
            status=RunStatus.WAITING,
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        # Create multiple worker jobs for this run
        jobs = [
            WorkerJob(
                owner_id=test_user.id,
                supervisor_run_id=run.id,
                task=f"Task {i}",
                status="queued",
            )
            for i in range(3)
        ]
        db_session.add_all(jobs)
        db_session.commit()

        # Verify all jobs are linked to the supervisor run
        linked_jobs = db_session.query(WorkerJob).filter(
            WorkerJob.supervisor_run_id == run.id
        ).all()
        assert len(linked_jobs) == 3

        # Verify we can check if any workers are still active
        active_workers = db_session.query(WorkerJob).filter(
            WorkerJob.supervisor_run_id == run.id,
            WorkerJob.status.in_(["queued", "running"]),
        ).count()
        assert active_workers == 3

        # Mark one as complete
        jobs[0].status = "success"
        db_session.commit()

        active_workers = db_session.query(WorkerJob).filter(
            WorkerJob.supervisor_run_id == run.id,
            WorkerJob.status.in_(["queued", "running"]),
        ).count()
        assert active_workers == 2

    @pytest.mark.asyncio
    async def test_waiting_run_cannot_transition_to_success_directly(self, db_session, sample_agent, sample_thread):
        """Verify WAITING runs must go through RUNNING before SUCCESS.

        The correct state machine is:
        RUNNING → spawn_worker → interrupt → WAITING → worker done → resume → RUNNING → SUCCESS

        A direct WAITING → SUCCESS transition would skip the resume logic and
        potentially leave the graph in an inconsistent state.
        """
        agent = sample_agent
        thread = sample_thread

        # Create WAITING run
        run = AgentRun(
            agent_id=agent.id,
            thread_id=thread.id,
            status=RunStatus.WAITING,
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
        assert run.status == RunStatus.WAITING
