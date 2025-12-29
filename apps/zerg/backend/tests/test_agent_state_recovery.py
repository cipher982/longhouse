"""
Tests for the agent state recovery system.

Tests the startup recovery mechanism that prevents stuck agents, runs, and jobs.
"""

from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from zerg.crud.crud import create_agent
from zerg.models.enums import RunStatus
from zerg.models.models import Agent
from zerg.models.models import AgentRun
from zerg.models.models import RunnerJob
from zerg.models.models import Thread
from zerg.models.models import WorkerJob
from zerg.services.agent_state_recovery import check_postgresql_advisory_lock_support
from zerg.services.agent_state_recovery import initialize_agent_state_system
from zerg.services.agent_state_recovery import perform_startup_agent_recovery
from zerg.services.agent_state_recovery import perform_startup_run_recovery
from zerg.services.agent_state_recovery import perform_startup_runner_job_recovery
from zerg.services.agent_state_recovery import perform_startup_worker_job_recovery


class TestAgentStateRecovery:
    """Test agent state recovery functionality."""

    @pytest.mark.asyncio
    async def test_startup_recovery_no_stuck_agents(self, db_session: Session):
        """Test startup recovery when no agents are stuck."""
        # Create some normal agents
        agent1 = create_agent(
            db_session,
            owner_id=1,
            name="Normal Agent 1",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-mock",
        )

        agent2 = create_agent(
            db_session,
            owner_id=1,
            name="Normal Agent 2",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-mock",
        )

        # Both should be idle by default
        assert agent1.status == "idle"
        assert agent2.status == "idle"

        # Recovery should find no stuck agents
        with patch("zerg.services.agent_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered = await perform_startup_agent_recovery()

        assert recovered == []

    @pytest.mark.asyncio
    async def test_startup_recovery_with_stuck_agents(self, db_session: Session):
        """Test startup recovery finds and fixes stuck agents."""
        # Create agents
        agent1 = create_agent(
            db_session,
            owner_id=1,
            name="Stuck Agent 1",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-mock",
        )

        agent2 = create_agent(
            db_session,
            owner_id=1,
            name="Normal Agent",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-mock",
        )

        # Manually set agent1 to running status (simulating stuck state)
        db_session.query(Agent).filter(Agent.id == agent1.id).update({"status": "running"})
        db_session.commit()

        # Verify setup
        stuck_agent = db_session.query(Agent).filter(Agent.id == agent1.id).first()
        assert stuck_agent.status == "running"

        # Run recovery
        with patch("zerg.services.agent_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered = await perform_startup_agent_recovery()

        # Should have recovered the stuck agent
        assert agent1.id in recovered
        assert agent2.id not in recovered

        # Verify agent1 was fixed
        recovered_agent = db_session.query(Agent).filter(Agent.id == agent1.id).first()
        assert recovered_agent.status == "idle"
        assert "Recovered from stuck running state" in recovered_agent.last_error

        # Verify agent2 was untouched
        normal_agent = db_session.query(Agent).filter(Agent.id == agent2.id).first()
        assert normal_agent.status == "idle"
        assert normal_agent.last_error is None

    @pytest.mark.asyncio
    async def test_startup_recovery_with_active_runs(self, db_session: Session):
        """Test that agents with active runs are NOT recovered."""
        # Create agent and thread
        agent = create_agent(
            db_session,
            owner_id=1,
            name="Agent with Active Run",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-mock",
        )

        thread = Thread(agent_id=agent.id, title="Test Thread", thread_type="manual")
        db_session.add(thread)
        db_session.flush()

        # Set agent to running status
        db_session.query(Agent).filter(Agent.id == agent.id).update({"status": "running"})

        # Create an active run for this agent
        run = AgentRun(agent_id=agent.id, thread_id=thread.id, status="running", trigger="manual")
        db_session.add(run)
        db_session.commit()

        # Run recovery
        with patch("zerg.services.agent_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered = await perform_startup_agent_recovery()

        # Should NOT recover this agent because it has an active run
        assert agent.id not in recovered

        # Agent should still be running
        agent_after = db_session.query(Agent).filter(Agent.id == agent.id).first()
        assert agent_after.status == "running"

    @pytest.mark.asyncio
    async def test_startup_recovery_uppercase_status(self, db_session: Session):
        """Test recovery handles uppercase RUNNING status."""
        # Create agent
        agent = create_agent(
            db_session,
            owner_id=1,
            name="Uppercase Stuck Agent",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-mock",
        )

        # Set to uppercase RUNNING status
        db_session.query(Agent).filter(Agent.id == agent.id).update({"status": "RUNNING"})
        db_session.commit()

        # Run recovery
        with patch("zerg.services.agent_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered = await perform_startup_agent_recovery()

        # Should recover the uppercase status agent
        assert agent.id in recovered

        # Verify it was fixed
        recovered_agent = db_session.query(Agent).filter(Agent.id == agent.id).first()
        assert recovered_agent.status == "idle"

    def test_postgresql_advisory_lock_support(self, db_session: Session):
        """Test PostgreSQL advisory lock support detection."""
        with patch("zerg.services.agent_state_recovery.get_session_factory", return_value=lambda: db_session):
            supported = check_postgresql_advisory_lock_support()

        # Should return True for PostgreSQL (which our test uses)
        assert isinstance(supported, bool)
        # We can't guarantee the specific result as it depends on the test database

    @pytest.mark.asyncio
    async def test_initialize_agent_state_system(self, db_session: Session):
        """Test full initialization of the agent state system."""
        # Create a stuck agent
        agent = create_agent(
            db_session,
            owner_id=1,
            name="Initialization Test Agent",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-mock",
        )

        # Set to running status
        db_session.query(Agent).filter(Agent.id == agent.id).update({"status": "running"})
        db_session.commit()

        # Initialize the system
        with patch("zerg.services.agent_state_recovery.get_session_factory", return_value=lambda: db_session):
            result = await initialize_agent_state_system()

        # Should have results
        assert "recovered_agents" in result
        assert "advisory_locks_available" in result
        assert agent.id in result["recovered_agents"]
        assert isinstance(result["advisory_locks_available"], bool)

        # Agent should be recovered
        recovered_agent = db_session.query(Agent).filter(Agent.id == agent.id).first()
        assert recovered_agent.status == "idle"

        # Check new recovery result keys exist
        assert "recovered_runs" in result
        assert "recovered_worker_jobs" in result
        assert "recovered_runner_jobs" in result


class TestRunRecovery:
    """Test AgentRun recovery functionality."""

    @pytest.mark.asyncio
    async def test_run_recovery_no_stuck_runs(self, db_session: Session):
        """Test run recovery when no runs are stuck."""
        # Create an agent and thread for the run
        agent = create_agent(
            db_session,
            owner_id=1,
            name="Test Agent",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-mock",
        )
        thread = Thread(agent_id=agent.id, title="Test Thread", thread_type="manual")
        db_session.add(thread)
        db_session.flush()

        # Create a completed run
        run = AgentRun(agent_id=agent.id, thread_id=thread.id, status="SUCCESS", trigger="manual")
        db_session.add(run)
        db_session.commit()

        # Recovery should find no stuck runs
        with patch("zerg.services.agent_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered = await perform_startup_run_recovery()

        assert recovered == []

    @pytest.mark.asyncio
    async def test_run_recovery_with_stuck_runs(self, db_session: Session):
        """Test run recovery finds and fixes stuck runs."""
        # Create an agent and thread
        agent = create_agent(
            db_session,
            owner_id=1,
            name="Test Agent",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-mock",
        )
        thread = Thread(agent_id=agent.id, title="Test Thread", thread_type="manual")
        db_session.add(thread)
        db_session.flush()

        # Create a stuck run
        stuck_run = AgentRun(agent_id=agent.id, thread_id=thread.id, status="RUNNING", trigger="manual")
        db_session.add(stuck_run)
        db_session.flush()
        stuck_run_id = stuck_run.id

        # Create a completed run (should not be affected)
        completed_run = AgentRun(agent_id=agent.id, thread_id=thread.id, status="SUCCESS", trigger="manual")
        db_session.add(completed_run)
        db_session.commit()

        # Run recovery
        with patch("zerg.services.agent_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered = await perform_startup_run_recovery()

        # Should have recovered the stuck run
        assert stuck_run_id in recovered
        assert completed_run.id not in recovered

        # Verify stuck run was fixed
        db_session.expire_all()
        fixed_run = db_session.query(AgentRun).filter(AgentRun.id == stuck_run_id).first()
        assert fixed_run.status == RunStatus.FAILED.value
        assert "Orphaned after server restart" in fixed_run.error

    @pytest.mark.asyncio
    async def test_run_recovery_handles_queued_and_deferred(self, db_session: Session):
        """Test run recovery handles QUEUED and DEFERRED statuses."""
        # Create an agent and thread
        agent = create_agent(
            db_session,
            owner_id=1,
            name="Test Agent",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-mock",
        )
        thread = Thread(agent_id=agent.id, title="Test Thread", thread_type="manual")
        db_session.add(thread)
        db_session.flush()

        # Create runs in different stuck states
        queued_run = AgentRun(agent_id=agent.id, thread_id=thread.id, status="QUEUED", trigger="manual")
        deferred_run = AgentRun(agent_id=agent.id, thread_id=thread.id, status="DEFERRED", trigger="manual")
        db_session.add(queued_run)
        db_session.add(deferred_run)
        db_session.commit()

        # Run recovery
        with patch("zerg.services.agent_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered = await perform_startup_run_recovery()

        # Both should be recovered
        assert queued_run.id in recovered
        assert deferred_run.id in recovered


class TestWorkerJobRecovery:
    """Test WorkerJob recovery functionality."""

    @pytest.mark.asyncio
    async def test_worker_job_recovery_no_stuck_jobs(self, db_session: Session, test_user):
        """Test worker job recovery when no jobs are stuck."""
        # Create a completed worker job
        job = WorkerJob(owner_id=test_user.id, task="Test task", status="success")
        db_session.add(job)
        db_session.commit()

        # Recovery should find no stuck jobs
        with patch("zerg.services.agent_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered = await perform_startup_worker_job_recovery()

        assert recovered == []

    @pytest.mark.asyncio
    async def test_worker_job_recovery_with_stuck_jobs(self, db_session: Session, test_user):
        """Test worker job recovery finds and fixes stuck jobs."""
        # Create a stuck worker job
        stuck_job = WorkerJob(owner_id=test_user.id, task="Stuck task", status="running")
        db_session.add(stuck_job)
        db_session.flush()
        stuck_job_id = stuck_job.id

        # Create a queued job (also stuck)
        queued_job = WorkerJob(owner_id=test_user.id, task="Queued task", status="queued")
        db_session.add(queued_job)
        db_session.flush()
        queued_job_id = queued_job.id

        # Create a completed job (should not be affected)
        completed_job = WorkerJob(owner_id=test_user.id, task="Completed task", status="success")
        db_session.add(completed_job)
        db_session.commit()

        # Run recovery
        with patch("zerg.services.agent_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered = await perform_startup_worker_job_recovery()

        # Should have recovered both stuck jobs
        assert stuck_job_id in recovered
        assert queued_job_id in recovered
        assert completed_job.id not in recovered

        # Verify stuck jobs were fixed
        db_session.expire_all()
        fixed_running = db_session.query(WorkerJob).filter(WorkerJob.id == stuck_job_id).first()
        fixed_queued = db_session.query(WorkerJob).filter(WorkerJob.id == queued_job_id).first()

        assert fixed_running.status == "failed"
        assert "Orphaned after server restart" in fixed_running.error
        assert fixed_queued.status == "failed"
        assert "Orphaned after server restart" in fixed_queued.error


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
        with patch("zerg.services.agent_state_recovery.get_session_factory", return_value=lambda: db_session):
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
        with patch("zerg.services.agent_state_recovery.get_session_factory", return_value=lambda: db_session):
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
        # Create an agent and thread
        agent = create_agent(
            db_session,
            owner_id=1,
            name="Test Agent",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-mock",
        )
        thread = Thread(agent_id=agent.id, title="Test Thread", thread_type="manual")
        db_session.add(thread)
        db_session.flush()

        # Create a stuck run
        stuck_run = AgentRun(agent_id=agent.id, thread_id=thread.id, status="RUNNING", trigger="manual")
        db_session.add(stuck_run)
        db_session.commit()
        stuck_run_id = stuck_run.id

        # Run recovery twice
        with patch("zerg.services.agent_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered1 = await perform_startup_run_recovery()
            recovered2 = await perform_startup_run_recovery()

        # First call should recover, second should find nothing
        assert stuck_run_id in recovered1
        assert recovered2 == []

    @pytest.mark.asyncio
    async def test_worker_job_recovery_idempotent(self, db_session: Session, test_user):
        """Test worker job recovery can be run multiple times safely."""
        stuck_job = WorkerJob(owner_id=test_user.id, task="Stuck task", status="running")
        db_session.add(stuck_job)
        db_session.commit()
        stuck_job_id = stuck_job.id

        # Run recovery twice
        with patch("zerg.services.agent_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered1 = await perform_startup_worker_job_recovery()
            recovered2 = await perform_startup_worker_job_recovery()

        # First call should recover, second should find nothing
        assert stuck_job_id in recovered1
        assert recovered2 == []
