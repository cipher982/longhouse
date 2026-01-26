"""Tests for Worker Inbox Continuation (Human PA model).

These tests verify the auto-response behavior when a worker completes
and the original supervisor run has already finished (SUCCESS/FAILED/CANCELLED).

Key behaviors tested:
- Happy path: Worker completion triggers inbox continuation
- SSE aliasing: Continuation events alias back to original run_id
- Multiple workers: First creates continuation, subsequent updates queue + follow-up
- Worker failure: Error is reported in continuation
- Inheritance: Continuation inherits model and trace_id
- Edge cases: Races, chains, and existing continuations
"""

import asyncio
import uuid
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage

from zerg.crud import crud
from zerg.models.enums import RunStatus
from zerg.models.enums import RunTrigger
from zerg.models.models import AgentRun
from zerg.models.models import ThreadMessage
from zerg.models.models import WorkerJob
from zerg.services.supervisor_service import SupervisorService


@pytest.mark.timeout(60)
class TestWorkerInboxTrigger:
    """Test trigger_worker_inbox_run() function."""

    @pytest.mark.asyncio
    async def test_inbox_triggers_when_run_is_success(self, db_session, test_user, sample_agent):
        """Worker completion triggers inbox run when original run is SUCCESS."""
        from zerg.services.worker_resume import trigger_worker_inbox_run

        # Create thread
        thread = crud.create_thread(
            db=db_session,
            agent_id=sample_agent.id,
            title="Test thread",
            active=True,
        )

        # Create SUCCESS run (terminal state)
        original_run = AgentRun(
            agent_id=sample_agent.id,
            thread_id=thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.API,
            model="gpt-mock",
            reasoning_effort="medium",
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(original_run)
        db_session.commit()
        db_session.refresh(original_run)

        # Create WorkerJob
        job = WorkerJob(
            owner_id=test_user.id,
            supervisor_run_id=original_run.id,
            task="Check disk space on cube",
            model="gpt-mock",
            status="success",
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        # Mock SupervisorService.run_supervisor
        mock_result = MagicMock()
        mock_result.status = "success"
        mock_result.result = "Disk is at 39%"

        with patch.object(
            SupervisorService, "run_supervisor", new=AsyncMock(return_value=mock_result)
        ):
            result = await trigger_worker_inbox_run(
                db=db_session,
                original_run_id=original_run.id,
                worker_job_id=job.id,
                worker_result="Disk space: 39% used, 61% free",
                worker_status="success",
            )

        # Verify continuation was triggered
        assert result["status"] == "triggered"
        assert "continuation_run_id" in result

        # Verify continuation run was created
        continuation = (
            db_session.query(AgentRun)
            .filter(AgentRun.continuation_of_run_id == original_run.id)
            .first()
        )
        assert continuation is not None
        assert continuation.trigger == RunTrigger.CONTINUATION
        assert continuation.model == original_run.model
        assert continuation.reasoning_effort == original_run.reasoning_effort

    @pytest.mark.asyncio
    async def test_inbox_skipped_when_run_is_waiting(self, db_session, test_user, sample_agent):
        """Inbox is skipped when run is WAITING (normal resume path handles it)."""
        from zerg.services.worker_resume import trigger_worker_inbox_run

        # Create thread
        thread = crud.create_thread(
            db=db_session,
            agent_id=sample_agent.id,
            title="Test thread",
            active=True,
        )

        # Create WAITING run (not terminal)
        waiting_run = AgentRun(
            agent_id=sample_agent.id,
            thread_id=thread.id,
            status=RunStatus.WAITING,
            trigger=RunTrigger.API,
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(waiting_run)
        db_session.commit()
        db_session.refresh(waiting_run)

        # Create WorkerJob
        job = WorkerJob(
            owner_id=test_user.id,
            supervisor_run_id=waiting_run.id,
            task="Test task",
            model="gpt-mock",
            status="success",
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        result = await trigger_worker_inbox_run(
            db=db_session,
            original_run_id=waiting_run.id,
            worker_job_id=job.id,
            worker_result="Result",
            worker_status="success",
        )

        # Verify skipped
        assert result["status"] == "skipped"
        assert "waiting" in result.get("reason", "").lower()

    @pytest.mark.asyncio
    async def test_inbox_handles_worker_failure(self, db_session, test_user, sample_agent):
        """Worker failure triggers inbox with error context."""
        from zerg.services.worker_resume import trigger_worker_inbox_run

        # Create thread
        thread = crud.create_thread(
            db=db_session,
            agent_id=sample_agent.id,
            title="Test thread",
            active=True,
        )

        # Create SUCCESS run
        original_run = AgentRun(
            agent_id=sample_agent.id,
            thread_id=thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.API,
            model="gpt-mock",
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(original_run)
        db_session.commit()
        db_session.refresh(original_run)

        # Create failed WorkerJob
        job = WorkerJob(
            owner_id=test_user.id,
            supervisor_run_id=original_run.id,
            task="Check disk on cube",
            model="gpt-mock",
            status="failed",
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        # Mock SupervisorService.run_supervisor and capture the task
        captured_task = None
        mock_result = MagicMock()
        mock_result.status = "success"

        async def capture_run_supervisor(owner_id, task, **kwargs):
            nonlocal captured_task
            captured_task = task
            return mock_result

        with patch.object(
            SupervisorService, "run_supervisor", side_effect=capture_run_supervisor
        ):
            result = await trigger_worker_inbox_run(
                db=db_session,
                original_run_id=original_run.id,
                worker_job_id=job.id,
                worker_result="",
                worker_status="failed",
                worker_error="SSH connection refused",
            )

        # Verify continuation was triggered
        assert result["status"] == "triggered"

        # Verify task contains error info
        assert captured_task is not None
        assert "failed" in captured_task.lower()
        assert "SSH connection refused" in captured_task

    @pytest.mark.asyncio
    async def test_inbox_inherits_model_and_trace(self, db_session, test_user, sample_agent):
        """Continuation inherits model and reasoning_effort from original run."""
        from zerg.services.worker_resume import trigger_worker_inbox_run

        # Create thread
        thread = crud.create_thread(
            db=db_session,
            agent_id=sample_agent.id,
            title="Test thread",
            active=True,
        )

        # Create SUCCESS run with specific model settings
        original_run = AgentRun(
            agent_id=sample_agent.id,
            thread_id=thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.API,
            model="gpt-4-turbo",
            reasoning_effort="high",
            trace_id=uuid.uuid4(),
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(original_run)
        db_session.commit()
        db_session.refresh(original_run)

        # Create WorkerJob
        job = WorkerJob(
            owner_id=test_user.id,
            supervisor_run_id=original_run.id,
            task="Test task",
            model="gpt-mock",
            status="success",
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        # Mock SupervisorService.run_supervisor
        mock_result = MagicMock()
        mock_result.status = "success"

        with patch.object(
            SupervisorService, "run_supervisor", new=AsyncMock(return_value=mock_result)
        ):
            result = await trigger_worker_inbox_run(
                db=db_session,
                original_run_id=original_run.id,
                worker_job_id=job.id,
                worker_result="Result",
                worker_status="success",
            )

        # Verify continuation inherits settings
        continuation = db_session.query(AgentRun).filter(AgentRun.id == result["continuation_run_id"]).first()
        assert continuation.model == "gpt-4-turbo"
        assert continuation.reasoning_effort == "high"


@pytest.mark.timeout(60)
class TestMultipleWorkersContinuation:
    """Test handling of multiple workers completing for same run."""

    @pytest.mark.asyncio
    async def test_first_worker_creates_continuation(self, db_session, test_user, sample_agent):
        """First worker to complete creates the continuation run."""
        from zerg.services.worker_resume import trigger_worker_inbox_run

        # Create thread
        thread = crud.create_thread(
            db=db_session,
            agent_id=sample_agent.id,
            title="Test thread",
            active=True,
        )

        # Create SUCCESS run
        original_run = AgentRun(
            agent_id=sample_agent.id,
            thread_id=thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.API,
            model="gpt-mock",
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(original_run)
        db_session.commit()
        db_session.refresh(original_run)

        # Create two WorkerJobs
        job1 = WorkerJob(
            owner_id=test_user.id,
            supervisor_run_id=original_run.id,
            task="Check disk on cube",
            model="gpt-mock",
            status="success",
        )
        job2 = WorkerJob(
            owner_id=test_user.id,
            supervisor_run_id=original_run.id,
            task="Check memory on cube",
            model="gpt-mock",
            status="success",
        )
        db_session.add(job1)
        db_session.add(job2)
        db_session.commit()
        db_session.refresh(job1)
        db_session.refresh(job2)

        # Mock SupervisorService.run_supervisor
        mock_result = MagicMock()
        mock_result.status = "success"

        with patch.object(
            SupervisorService, "run_supervisor", new=AsyncMock(return_value=mock_result)
        ):
            # First worker completes - should create continuation
            result1 = await trigger_worker_inbox_run(
                db=db_session,
                original_run_id=original_run.id,
                worker_job_id=job1.id,
                worker_result="Disk: 39%",
                worker_status="success",
            )

        assert result1["status"] == "triggered"
        continuation_id = result1["continuation_run_id"]

        # Mark continuation as SUCCESS to simulate completion
        continuation = db_session.query(AgentRun).filter(AgentRun.id == continuation_id).first()
        continuation.status = RunStatus.SUCCESS
        db_session.commit()

        # Second worker completes - should create chain continuation
        with patch.object(
            SupervisorService, "run_supervisor", new=AsyncMock(return_value=mock_result)
        ):
            result2 = await trigger_worker_inbox_run(
                db=db_session,
                original_run_id=original_run.id,
                worker_job_id=job2.id,
                worker_result="Memory: 60%",
                worker_status="success",
            )

        # Second creates chain (continuation of continuation)
        assert result2["status"] == "triggered"
        assert result2["continuation_run_id"] != continuation_id

    @pytest.mark.asyncio
    async def test_second_worker_queues_followup_when_continuation_running(self, db_session, test_user, sample_agent):
        """Second worker queues update and schedules follow-up when continuation is running."""
        from zerg.services import worker_resume
        from zerg.services.worker_resume import trigger_worker_inbox_run

        # Create thread
        thread = crud.create_thread(
            db=db_session,
            agent_id=sample_agent.id,
            title="Test thread",
            active=True,
        )

        # Create SUCCESS run
        original_run = AgentRun(
            agent_id=sample_agent.id,
            thread_id=thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.API,
            model="gpt-mock",
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(original_run)
        db_session.commit()
        db_session.refresh(original_run)

        # Create RUNNING continuation (simulating first worker's inbox run in progress)
        running_continuation = AgentRun(
            agent_id=sample_agent.id,
            thread_id=thread.id,
            continuation_of_run_id=original_run.id,
            status=RunStatus.RUNNING,
            trigger=RunTrigger.CONTINUATION,
            model="gpt-mock",
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(running_continuation)
        db_session.commit()
        db_session.refresh(running_continuation)

        # Create second WorkerJob
        job2 = WorkerJob(
            owner_id=test_user.id,
            supervisor_run_id=original_run.id,
            task="Check memory on cube",
            model="gpt-mock",
            status="success",
        )
        db_session.add(job2)
        db_session.commit()
        db_session.refresh(job2)

        # Second worker completes while continuation is running
        with patch.object(worker_resume, "_schedule_inbox_followup_after_run") as schedule_mock:
            result = await trigger_worker_inbox_run(
                db=db_session,
                original_run_id=original_run.id,
                worker_job_id=job2.id,
                worker_result="Memory: 60%",
                worker_status="success",
            )

        # Verify queued + follow-up scheduled
        assert result["status"] == "queued"
        assert result["continuation_run_id"] == running_continuation.id
        schedule_mock.assert_called_once_with(
            run_id=running_continuation.id,
            worker_job_id=job2.id,
            worker_status="success",
            worker_error=None,
        )

        # Verify context message was injected (as "user" role so AgentRunner includes it)
        merged_msgs = (
            db_session.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.role == "user",  # Fixed: use "user" not "system"
                ThreadMessage.internal == True,  # noqa: E712
            )
            .all()
        )
        merged_content = [m.content for m in merged_msgs if "Worker update" in (m.content or "")]
        assert len(merged_content) >= 1
        assert str(job2.id) in merged_content[0]


@pytest.mark.timeout(60)
class TestWorkerRunnerIntegration:
    """Test worker_runner.py integration with inbox trigger."""

    @pytest.mark.asyncio
    async def test_trigger_inbox_run_called_on_terminal_run(self, db_session, test_user, sample_agent):
        """trigger_worker_inbox_run is called when supervisor run is terminal."""
        from zerg.services.worker_resume import trigger_worker_inbox_run

        # Create thread
        thread = crud.create_thread(
            db=db_session,
            agent_id=sample_agent.id,
            title="Test thread",
            active=True,
        )

        # Create SUCCESS run
        original_run = AgentRun(
            agent_id=sample_agent.id,
            thread_id=thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.API,
            model="gpt-mock",
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(original_run)
        db_session.commit()
        db_session.refresh(original_run)

        # Create WorkerJob
        job = WorkerJob(
            owner_id=test_user.id,
            supervisor_run_id=original_run.id,
            task="Test task",
            model="gpt-mock",
            status="success",
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        # Test that trigger_worker_inbox_run handles terminal run state
        mock_result = MagicMock()
        mock_result.status = "success"

        with patch.object(
            SupervisorService, "run_supervisor", new=AsyncMock(return_value=mock_result)
        ):
            result = await trigger_worker_inbox_run(
                db=db_session,
                original_run_id=original_run.id,
                worker_job_id=job.id,
                worker_result="Test result",
                worker_status="success",
            )

        # Verify continuation was triggered for terminal run
        assert result["status"] == "triggered"
        assert "continuation_run_id" in result


@pytest.mark.timeout(60)
class TestSSEEventAliasing:
    """Test SSE event aliasing for continuation runs."""

    @pytest.mark.asyncio
    async def test_continuation_relationship_for_sse_aliasing(self, db_session, test_user, sample_agent):
        """continuation_of_run_id relationship enables SSE aliasing in stream router."""
        # Create thread
        thread = crud.create_thread(
            db=db_session,
            agent_id=sample_agent.id,
            title="Test thread",
            active=True,
        )

        # Create original run
        original_run = AgentRun(
            agent_id=sample_agent.id,
            thread_id=thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.API,
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(original_run)
        db_session.commit()
        db_session.refresh(original_run)

        # Create continuation run
        continuation = AgentRun(
            agent_id=sample_agent.id,
            thread_id=thread.id,
            continuation_of_run_id=original_run.id,
            status=RunStatus.RUNNING,
            trigger=RunTrigger.CONTINUATION,
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(continuation)
        db_session.commit()
        db_session.refresh(continuation)

        # Verify relationship for SSE aliasing (stream.py:154-191)
        # The SSE stream uses this relationship to alias continuation events
        # back to the original run_id for UI stability
        assert continuation.continuation_of_run_id == original_run.id

        # Verify the lookup that SSE aliasing performs
        loaded_continuation = (
            db_session.query(AgentRun)
            .filter(AgentRun.continuation_of_run_id == original_run.id)
            .first()
        )
        assert loaded_continuation is not None
        assert loaded_continuation.id == continuation.id

        # Verify the trigger is set correctly
        assert continuation.trigger == RunTrigger.CONTINUATION


@pytest.mark.timeout(60)
class TestIdempotencyAndRaces:
    """Test idempotency and race condition handling."""

    @pytest.mark.asyncio
    async def test_unique_constraint_prevents_duplicate_continuation(self, db_session, test_user, sample_agent):
        """Unique constraint on continuation_of_run_id prevents duplicates."""
        from zerg.services.worker_resume import trigger_worker_inbox_run

        # Create thread
        thread = crud.create_thread(
            db=db_session,
            agent_id=sample_agent.id,
            title="Test thread",
            active=True,
        )

        # Create SUCCESS run
        original_run = AgentRun(
            agent_id=sample_agent.id,
            thread_id=thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.API,
            model="gpt-mock",
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(original_run)
        db_session.commit()
        db_session.refresh(original_run)

        # Create existing continuation (simulating first worker already created one)
        existing_continuation = AgentRun(
            agent_id=sample_agent.id,
            thread_id=thread.id,
            continuation_of_run_id=original_run.id,
            status=RunStatus.SUCCESS,  # Already completed
            trigger=RunTrigger.CONTINUATION,
            model="gpt-mock",
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(existing_continuation)
        db_session.commit()
        db_session.refresh(existing_continuation)

        # Create WorkerJob
        job = WorkerJob(
            owner_id=test_user.id,
            supervisor_run_id=original_run.id,
            task="Test task",
            model="gpt-mock",
            status="success",
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        # Second attempt should create chain continuation
        mock_result = MagicMock()
        mock_result.status = "success"

        with patch.object(
            SupervisorService, "run_supervisor", new=AsyncMock(return_value=mock_result)
        ):
            result = await trigger_worker_inbox_run(
                db=db_session,
                original_run_id=original_run.id,
                worker_job_id=job.id,
                worker_result="Result",
                worker_status="success",
            )

        # Should create chain (continuation of continuation)
        assert result["status"] == "triggered"
        # The new continuation should be of the existing continuation
        new_continuation = db_session.query(AgentRun).filter(AgentRun.id == result["continuation_run_id"]).first()
        assert new_continuation.continuation_of_run_id == existing_continuation.id

    @pytest.mark.asyncio
    async def test_root_run_id_propagates_through_chains(self, db_session, test_user, sample_agent):
        """root_run_id is preserved through continuation chains for SSE aliasing."""
        from zerg.services.worker_resume import trigger_worker_inbox_run

        # Create thread
        thread = crud.create_thread(
            db=db_session,
            agent_id=sample_agent.id,
            title="Test thread",
            active=True,
        )

        # Create original SUCCESS run (the "root")
        original_run = AgentRun(
            agent_id=sample_agent.id,
            thread_id=thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.API,
            model="gpt-mock",
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(original_run)
        db_session.commit()
        db_session.refresh(original_run)

        # Create first WorkerJob
        job1 = WorkerJob(
            owner_id=test_user.id,
            supervisor_run_id=original_run.id,
            task="First task",
            model="gpt-mock",
            status="success",
        )
        db_session.add(job1)
        db_session.commit()
        db_session.refresh(job1)

        # First worker creates continuation
        mock_result = MagicMock()
        mock_result.status = "success"

        with patch.object(
            SupervisorService, "run_supervisor", new=AsyncMock(return_value=mock_result)
        ):
            result1 = await trigger_worker_inbox_run(
                db=db_session,
                original_run_id=original_run.id,
                worker_job_id=job1.id,
                worker_result="First result",
                worker_status="success",
            )

        assert result1["status"] == "triggered"
        first_continuation = db_session.query(AgentRun).filter(AgentRun.id == result1["continuation_run_id"]).first()

        # Verify first continuation has root_run_id set to original
        assert first_continuation.root_run_id == original_run.id

        # Mark first continuation as SUCCESS
        first_continuation.status = RunStatus.SUCCESS
        db_session.commit()

        # Second worker creates chain continuation
        job2 = WorkerJob(
            owner_id=test_user.id,
            supervisor_run_id=original_run.id,
            task="Second task",
            model="gpt-mock",
            status="success",
        )
        db_session.add(job2)
        db_session.commit()
        db_session.refresh(job2)

        with patch.object(
            SupervisorService, "run_supervisor", new=AsyncMock(return_value=mock_result)
        ):
            result2 = await trigger_worker_inbox_run(
                db=db_session,
                original_run_id=original_run.id,
                worker_job_id=job2.id,
                worker_result="Second result",
                worker_status="success",
            )

        assert result2["status"] == "triggered"
        chain_continuation = db_session.query(AgentRun).filter(AgentRun.id == result2["continuation_run_id"]).first()

        # Verify chain continuation still has root_run_id pointing to ORIGINAL run
        assert chain_continuation.root_run_id == original_run.id
        # But continuation_of_run_id points to the first continuation
        assert chain_continuation.continuation_of_run_id == first_continuation.id

    @pytest.mark.asyncio
    async def test_followup_runs_after_running_continuation_finishes(self, db_session, test_user, sample_agent):
        """Queued worker update triggers a follow-up continuation after running continuation finishes."""
        from zerg.services import worker_resume
        from zerg.services.worker_resume import run_inbox_followup_after_run
        from zerg.services.worker_resume import trigger_worker_inbox_run

        # Create thread
        thread = crud.create_thread(
            db=db_session,
            agent_id=sample_agent.id,
            title="Test thread",
            active=True,
        )

        # Create SUCCESS run
        original_run = AgentRun(
            agent_id=sample_agent.id,
            thread_id=thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.API,
            model="gpt-mock",
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(original_run)
        db_session.commit()
        db_session.refresh(original_run)

        # Create WorkerJob
        job = WorkerJob(
            owner_id=test_user.id,
            supervisor_run_id=original_run.id,
            task="Test task",
            model="gpt-mock",
            status="success",
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        # Pre-create a RUNNING continuation to simulate an in-flight inbox run
        running_continuation = AgentRun(
            agent_id=sample_agent.id,
            thread_id=thread.id,
            continuation_of_run_id=original_run.id,
            root_run_id=original_run.id,
            status=RunStatus.RUNNING,  # Still running
            trigger=RunTrigger.CONTINUATION,
            model="gpt-mock",
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(running_continuation)
        db_session.commit()
        db_session.refresh(running_continuation)

        # Queue the worker update while continuation is running
        with patch.object(worker_resume, "_schedule_inbox_followup_after_run") as schedule_mock:
            result = await trigger_worker_inbox_run(
                db=db_session,
                original_run_id=original_run.id,
                worker_job_id=job.id,
                worker_result="Test result",
                worker_status="success",
            )

        assert result["status"] == "queued"
        assert result["continuation_run_id"] == running_continuation.id
        schedule_mock.assert_called_once()

        # Mark running continuation as finished
        running_continuation.status = RunStatus.SUCCESS
        db_session.commit()

        # Follow-up should create a new continuation
        mock_result = MagicMock()
        mock_result.status = "success"
        with patch.object(SupervisorService, "run_supervisor", new=AsyncMock(return_value=mock_result)):
            followup_result = await run_inbox_followup_after_run(
                run_id=running_continuation.id,
                worker_job_id=job.id,
                worker_status="success",
                worker_error=None,
                timeout_s=1,
            )

        assert followup_result is not None
        assert followup_result["status"] == "triggered"
        new_continuation = (
            db_session.query(AgentRun)
            .filter(AgentRun.continuation_of_run_id == running_continuation.id)
            .first()
        )
        assert new_continuation is not None
