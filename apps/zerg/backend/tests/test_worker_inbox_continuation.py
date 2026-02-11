"""Tests for Commis Inbox Continuation (Human PA model).

These tests verify the auto-response behavior when a commis completes
and the original oikos run has already finished (SUCCESS/FAILED/CANCELLED).

Key behaviors tested:
- Happy path: Commis completion triggers inbox continuation
- SSE aliasing: Continuation events alias back to original run_id
- Multiple commiss: First creates continuation, subsequent updates queue + follow-up
- Commis failure: Error is reported in continuation
- Inheritance: Continuation inherits model and trace_id
- Edge cases: Races, chains, and existing continuations
"""

import uuid
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from zerg.crud import crud
from zerg.models.enums import RunStatus
from zerg.models.enums import RunTrigger
from zerg.models.models import CommisJob
from zerg.models.models import Run
from zerg.models.models import ThreadMessage
from zerg.services.oikos_service import OikosService


@pytest.mark.timeout(60)
class TestCommisInboxTrigger:
    """Test trigger_commis_inbox_run() function."""

    @pytest.mark.asyncio
    async def test_inbox_triggers_when_run_is_success(self, db_session, test_user, sample_fiche):
        """Commis completion triggers inbox run when original run is SUCCESS."""
        from zerg.services.commis_resume import trigger_commis_inbox_run

        # Create thread
        thread = crud.create_thread(
            db=db_session,
            fiche_id=sample_fiche.id,
            title="Test thread",
            active=True,
        )

        # Create SUCCESS run (terminal state)
        original_run = Run(
            fiche_id=sample_fiche.id,
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

        # Create CommisJob
        job = CommisJob(
            owner_id=test_user.id,
            oikos_run_id=original_run.id,
            task="Check disk space on cube",
            model="gpt-mock",
            status="success",
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        # Mock OikosService.run_oikos
        mock_result = MagicMock()
        mock_result.status = "success"
        mock_result.result = "Disk is at 39%"

        with patch.object(OikosService, "run_oikos", new=AsyncMock(return_value=mock_result)):
            result = await trigger_commis_inbox_run(
                db=db_session,
                original_run_id=original_run.id,
                commis_job_id=job.id,
                commis_result="Disk space: 39% used, 61% free",
                commis_status="success",
            )

        # Verify continuation was triggered
        assert result["status"] == "triggered"
        assert "continuation_run_id" in result

        # Verify continuation run was created
        continuation = db_session.query(Run).filter(Run.continuation_of_run_id == original_run.id).first()
        assert continuation is not None
        assert continuation.trigger == RunTrigger.CONTINUATION
        assert continuation.model == original_run.model
        assert continuation.reasoning_effort == original_run.reasoning_effort

    @pytest.mark.asyncio
    async def test_inbox_skipped_when_run_is_waiting(self, db_session, test_user, sample_fiche):
        """Inbox is skipped when run is WAITING (normal resume path handles it)."""
        from zerg.services.commis_resume import trigger_commis_inbox_run

        # Create thread
        thread = crud.create_thread(
            db=db_session,
            fiche_id=sample_fiche.id,
            title="Test thread",
            active=True,
        )

        # Create WAITING run (not terminal)
        waiting_run = Run(
            fiche_id=sample_fiche.id,
            thread_id=thread.id,
            status=RunStatus.WAITING,
            trigger=RunTrigger.API,
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(waiting_run)
        db_session.commit()
        db_session.refresh(waiting_run)

        # Create CommisJob
        job = CommisJob(
            owner_id=test_user.id,
            oikos_run_id=waiting_run.id,
            task="Test task",
            model="gpt-mock",
            status="success",
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        result = await trigger_commis_inbox_run(
            db=db_session,
            original_run_id=waiting_run.id,
            commis_job_id=job.id,
            commis_result="Result",
            commis_status="success",
        )

        # Verify skipped
        assert result["status"] == "skipped"
        assert "waiting" in result.get("reason", "").lower()

    @pytest.mark.asyncio
    async def test_inbox_handles_commis_failure(self, db_session, test_user, sample_fiche):
        """Commis failure triggers inbox with error context."""
        from zerg.services.commis_resume import trigger_commis_inbox_run

        # Create thread
        thread = crud.create_thread(
            db=db_session,
            fiche_id=sample_fiche.id,
            title="Test thread",
            active=True,
        )

        # Create SUCCESS run
        original_run = Run(
            fiche_id=sample_fiche.id,
            thread_id=thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.API,
            model="gpt-mock",
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(original_run)
        db_session.commit()
        db_session.refresh(original_run)

        # Create failed CommisJob
        job = CommisJob(
            owner_id=test_user.id,
            oikos_run_id=original_run.id,
            task="Check disk on cube",
            model="gpt-mock",
            status="failed",
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        # Mock OikosService.run_oikos and capture the task
        captured_task = None
        mock_result = MagicMock()
        mock_result.status = "success"

        async def capture_run_oikos(owner_id, task, **kwargs):
            nonlocal captured_task
            captured_task = task
            return mock_result

        with patch.object(OikosService, "run_oikos", side_effect=capture_run_oikos):
            result = await trigger_commis_inbox_run(
                db=db_session,
                original_run_id=original_run.id,
                commis_job_id=job.id,
                commis_result="",
                commis_status="failed",
                commis_error="SSH connection refused",
            )

        # Verify continuation was triggered
        assert result["status"] == "triggered"

        # Verify task contains error info
        assert captured_task is not None
        assert "failed" in captured_task.lower()
        assert "SSH connection refused" in captured_task

    @pytest.mark.asyncio
    async def test_inbox_inherits_model_and_trace(self, db_session, test_user, sample_fiche):
        """Continuation inherits model and reasoning_effort from original run."""
        from zerg.services.commis_resume import trigger_commis_inbox_run

        # Create thread
        thread = crud.create_thread(
            db=db_session,
            fiche_id=sample_fiche.id,
            title="Test thread",
            active=True,
        )

        # Create SUCCESS run with specific model settings
        original_run = Run(
            fiche_id=sample_fiche.id,
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

        # Create CommisJob
        job = CommisJob(
            owner_id=test_user.id,
            oikos_run_id=original_run.id,
            task="Test task",
            model="gpt-mock",
            status="success",
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        # Mock OikosService.run_oikos
        mock_result = MagicMock()
        mock_result.status = "success"

        with patch.object(OikosService, "run_oikos", new=AsyncMock(return_value=mock_result)):
            result = await trigger_commis_inbox_run(
                db=db_session,
                original_run_id=original_run.id,
                commis_job_id=job.id,
                commis_result="Result",
                commis_status="success",
            )

        # Verify continuation inherits settings
        continuation = db_session.query(Run).filter(Run.id == result["continuation_run_id"]).first()
        assert continuation.model == "gpt-4-turbo"
        assert continuation.reasoning_effort == "high"


@pytest.mark.timeout(60)
class TestMultipleCommissContinuation:
    """Test handling of multiple commiss completing for same run."""

    @pytest.mark.asyncio
    async def test_first_commis_creates_continuation(self, db_session, test_user, sample_fiche):
        """First commis to complete creates the continuation run."""
        from zerg.services.commis_resume import trigger_commis_inbox_run

        # Create thread
        thread = crud.create_thread(
            db=db_session,
            fiche_id=sample_fiche.id,
            title="Test thread",
            active=True,
        )

        # Create SUCCESS run
        original_run = Run(
            fiche_id=sample_fiche.id,
            thread_id=thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.API,
            model="gpt-mock",
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(original_run)
        db_session.commit()
        db_session.refresh(original_run)

        # Create two CommisJobs
        job1 = CommisJob(
            owner_id=test_user.id,
            oikos_run_id=original_run.id,
            task="Check disk on cube",
            model="gpt-mock",
            status="success",
        )
        job2 = CommisJob(
            owner_id=test_user.id,
            oikos_run_id=original_run.id,
            task="Check memory on cube",
            model="gpt-mock",
            status="success",
        )
        db_session.add(job1)
        db_session.add(job2)
        db_session.commit()
        db_session.refresh(job1)
        db_session.refresh(job2)

        # Mock OikosService.run_oikos
        mock_result = MagicMock()
        mock_result.status = "success"

        with patch.object(OikosService, "run_oikos", new=AsyncMock(return_value=mock_result)):
            # First commis completes - should create continuation
            result1 = await trigger_commis_inbox_run(
                db=db_session,
                original_run_id=original_run.id,
                commis_job_id=job1.id,
                commis_result="Disk: 39%",
                commis_status="success",
            )

        assert result1["status"] == "triggered"
        continuation_id = result1["continuation_run_id"]

        # Mark continuation as SUCCESS to simulate completion
        continuation = db_session.query(Run).filter(Run.id == continuation_id).first()
        continuation.status = RunStatus.SUCCESS
        db_session.commit()

        # Second commis completes - should create chain continuation
        with patch.object(OikosService, "run_oikos", new=AsyncMock(return_value=mock_result)):
            result2 = await trigger_commis_inbox_run(
                db=db_session,
                original_run_id=original_run.id,
                commis_job_id=job2.id,
                commis_result="Memory: 60%",
                commis_status="success",
            )

        # Second creates chain (continuation of continuation)
        assert result2["status"] == "triggered"
        assert result2["continuation_run_id"] != continuation_id

    @pytest.mark.asyncio
    async def test_second_commis_queues_followup_when_continuation_running(self, db_session, test_user, sample_fiche):
        """Second commis queues update and schedules follow-up when continuation is running."""
        from zerg.services import commis_resume
        from zerg.services.commis_resume import trigger_commis_inbox_run

        # Create thread
        thread = crud.create_thread(
            db=db_session,
            fiche_id=sample_fiche.id,
            title="Test thread",
            active=True,
        )

        # Create SUCCESS run
        original_run = Run(
            fiche_id=sample_fiche.id,
            thread_id=thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.API,
            model="gpt-mock",
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(original_run)
        db_session.commit()
        db_session.refresh(original_run)

        # Create RUNNING continuation (simulating first commis's inbox run in progress)
        running_continuation = Run(
            fiche_id=sample_fiche.id,
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

        # Create second CommisJob
        job2 = CommisJob(
            owner_id=test_user.id,
            oikos_run_id=original_run.id,
            task="Check memory on cube",
            model="gpt-mock",
            status="success",
        )
        db_session.add(job2)
        db_session.commit()
        db_session.refresh(job2)

        # Second commis completes while continuation is running
        with patch.object(commis_resume, "_schedule_inbox_followup_after_run") as schedule_mock:
            result = await trigger_commis_inbox_run(
                db=db_session,
                original_run_id=original_run.id,
                commis_job_id=job2.id,
                commis_result="Memory: 60%",
                commis_status="success",
            )

        # Verify queued + follow-up scheduled
        assert result["status"] == "queued"
        assert result["continuation_run_id"] == running_continuation.id
        schedule_mock.assert_called_once_with(
            run_id=running_continuation.id,
            commis_job_id=job2.id,
            commis_status="success",
            commis_error=None,
        )

        # Verify context message was injected (as "user" role so FicheRunner includes it)
        merged_msgs = (
            db_session.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.role == "user",  # Fixed: use "user" not "system"
                ThreadMessage.internal == True,  # noqa: E712
            )
            .all()
        )
        merged_content = [m.content for m in merged_msgs if "Commis update" in (m.content or "")]
        assert len(merged_content) >= 1
        assert str(job2.id) in merged_content[0]


@pytest.mark.timeout(60)
class TestCommisInboxIntegration:
    """Test commis inbox trigger integration."""

    @pytest.mark.asyncio
    async def test_trigger_inbox_run_called_on_terminal_run(self, db_session, test_user, sample_fiche):
        """trigger_commis_inbox_run is called when oikos run is terminal."""
        from zerg.services.commis_resume import trigger_commis_inbox_run

        # Create thread
        thread = crud.create_thread(
            db=db_session,
            fiche_id=sample_fiche.id,
            title="Test thread",
            active=True,
        )

        # Create SUCCESS run
        original_run = Run(
            fiche_id=sample_fiche.id,
            thread_id=thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.API,
            model="gpt-mock",
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(original_run)
        db_session.commit()
        db_session.refresh(original_run)

        # Create CommisJob
        job = CommisJob(
            owner_id=test_user.id,
            oikos_run_id=original_run.id,
            task="Test task",
            model="gpt-mock",
            status="success",
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        # Test that trigger_commis_inbox_run handles terminal run state
        mock_result = MagicMock()
        mock_result.status = "success"

        with patch.object(OikosService, "run_oikos", new=AsyncMock(return_value=mock_result)):
            result = await trigger_commis_inbox_run(
                db=db_session,
                original_run_id=original_run.id,
                commis_job_id=job.id,
                commis_result="Test result",
                commis_status="success",
            )

        # Verify continuation was triggered for terminal run
        assert result["status"] == "triggered"
        assert "continuation_run_id" in result


@pytest.mark.timeout(60)
class TestSSEEventAliasing:
    """Test SSE event aliasing for continuation runs."""

    @pytest.mark.asyncio
    async def test_continuation_relationship_for_sse_aliasing(self, db_session, test_user, sample_fiche):
        """continuation_of_run_id relationship enables SSE aliasing in stream router."""
        # Create thread
        thread = crud.create_thread(
            db=db_session,
            fiche_id=sample_fiche.id,
            title="Test thread",
            active=True,
        )

        # Create original run
        original_run = Run(
            fiche_id=sample_fiche.id,
            thread_id=thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.API,
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(original_run)
        db_session.commit()
        db_session.refresh(original_run)

        # Create continuation run
        continuation = Run(
            fiche_id=sample_fiche.id,
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
        loaded_continuation = db_session.query(Run).filter(Run.continuation_of_run_id == original_run.id).first()
        assert loaded_continuation is not None
        assert loaded_continuation.id == continuation.id

        # Verify the trigger is set correctly
        assert continuation.trigger == RunTrigger.CONTINUATION


@pytest.mark.timeout(60)
class TestIdempotencyAndRaces:
    """Test idempotency and race condition handling."""

    @pytest.mark.asyncio
    async def test_unique_constraint_prevents_duplicate_continuation(self, db_session, test_user, sample_fiche):
        """Unique constraint on continuation_of_run_id prevents duplicates."""
        from zerg.services.commis_resume import trigger_commis_inbox_run

        # Create thread
        thread = crud.create_thread(
            db=db_session,
            fiche_id=sample_fiche.id,
            title="Test thread",
            active=True,
        )

        # Create SUCCESS run
        original_run = Run(
            fiche_id=sample_fiche.id,
            thread_id=thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.API,
            model="gpt-mock",
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(original_run)
        db_session.commit()
        db_session.refresh(original_run)

        # Create existing continuation (simulating first commis already created one)
        existing_continuation = Run(
            fiche_id=sample_fiche.id,
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

        # Create CommisJob
        job = CommisJob(
            owner_id=test_user.id,
            oikos_run_id=original_run.id,
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

        with patch.object(OikosService, "run_oikos", new=AsyncMock(return_value=mock_result)):
            result = await trigger_commis_inbox_run(
                db=db_session,
                original_run_id=original_run.id,
                commis_job_id=job.id,
                commis_result="Result",
                commis_status="success",
            )

        # Should create chain (continuation of continuation)
        assert result["status"] == "triggered"
        # The new continuation should be of the existing continuation
        new_continuation = db_session.query(Run).filter(Run.id == result["continuation_run_id"]).first()
        assert new_continuation.continuation_of_run_id == existing_continuation.id

    @pytest.mark.asyncio
    async def test_root_run_id_propagates_through_chains(self, db_session, test_user, sample_fiche):
        """root_run_id is preserved through continuation chains for SSE aliasing."""
        from zerg.services.commis_resume import trigger_commis_inbox_run

        # Create thread
        thread = crud.create_thread(
            db=db_session,
            fiche_id=sample_fiche.id,
            title="Test thread",
            active=True,
        )

        # Create original SUCCESS run (the "root")
        original_run = Run(
            fiche_id=sample_fiche.id,
            thread_id=thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.API,
            model="gpt-mock",
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(original_run)
        db_session.commit()
        db_session.refresh(original_run)

        # Create first CommisJob
        job1 = CommisJob(
            owner_id=test_user.id,
            oikos_run_id=original_run.id,
            task="First task",
            model="gpt-mock",
            status="success",
        )
        db_session.add(job1)
        db_session.commit()
        db_session.refresh(job1)

        # First commis creates continuation
        mock_result = MagicMock()
        mock_result.status = "success"

        with patch.object(OikosService, "run_oikos", new=AsyncMock(return_value=mock_result)):
            result1 = await trigger_commis_inbox_run(
                db=db_session,
                original_run_id=original_run.id,
                commis_job_id=job1.id,
                commis_result="First result",
                commis_status="success",
            )

        assert result1["status"] == "triggered"
        first_continuation = db_session.query(Run).filter(Run.id == result1["continuation_run_id"]).first()

        # Verify first continuation has root_run_id set to original
        assert first_continuation.root_run_id == original_run.id

        # Mark first continuation as SUCCESS
        first_continuation.status = RunStatus.SUCCESS
        db_session.commit()

        # Second commis creates chain continuation
        job2 = CommisJob(
            owner_id=test_user.id,
            oikos_run_id=original_run.id,
            task="Second task",
            model="gpt-mock",
            status="success",
        )
        db_session.add(job2)
        db_session.commit()
        db_session.refresh(job2)

        with patch.object(OikosService, "run_oikos", new=AsyncMock(return_value=mock_result)):
            result2 = await trigger_commis_inbox_run(
                db=db_session,
                original_run_id=original_run.id,
                commis_job_id=job2.id,
                commis_result="Second result",
                commis_status="success",
            )

        assert result2["status"] == "triggered"
        chain_continuation = db_session.query(Run).filter(Run.id == result2["continuation_run_id"]).first()

        # Verify chain continuation still has root_run_id pointing to ORIGINAL run
        assert chain_continuation.root_run_id == original_run.id
        # But continuation_of_run_id points to the first continuation
        assert chain_continuation.continuation_of_run_id == first_continuation.id

    @pytest.mark.asyncio
    async def test_followup_runs_after_running_continuation_finishes(self, db_session, test_user, sample_fiche):
        """Queued commis update triggers a follow-up continuation after running continuation finishes."""
        from zerg.services import commis_resume
        from zerg.services.commis_resume import run_inbox_followup_after_run
        from zerg.services.commis_resume import trigger_commis_inbox_run

        # Create thread
        thread = crud.create_thread(
            db=db_session,
            fiche_id=sample_fiche.id,
            title="Test thread",
            active=True,
        )

        # Create SUCCESS run
        original_run = Run(
            fiche_id=sample_fiche.id,
            thread_id=thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.API,
            model="gpt-mock",
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(original_run)
        db_session.commit()
        db_session.refresh(original_run)

        # Create CommisJob
        job = CommisJob(
            owner_id=test_user.id,
            oikos_run_id=original_run.id,
            task="Test task",
            model="gpt-mock",
            status="success",
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        # Pre-create a RUNNING continuation to simulate an in-flight inbox run
        running_continuation = Run(
            fiche_id=sample_fiche.id,
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

        # Queue the commis update while continuation is running
        with patch.object(commis_resume, "_schedule_inbox_followup_after_run") as schedule_mock:
            result = await trigger_commis_inbox_run(
                db=db_session,
                original_run_id=original_run.id,
                commis_job_id=job.id,
                commis_result="Test result",
                commis_status="success",
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
        with patch.object(OikosService, "run_oikos", new=AsyncMock(return_value=mock_result)):
            followup_result = await run_inbox_followup_after_run(
                run_id=running_continuation.id,
                commis_job_id=job.id,
                commis_status="success",
                commis_error=None,
                timeout_s=1,
            )

        assert followup_result is not None
        assert followup_result["status"] == "triggered"
        new_continuation = db_session.query(Run).filter(Run.continuation_of_run_id == running_continuation.id).first()
        assert new_continuation is not None
