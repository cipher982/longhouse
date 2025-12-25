"""Tests for durable runs v2.2 Phase 4 - Continuation webhooks.

Tests the auto-continuation flow when workers complete and trigger
supervisor continuation for deferred runs.
"""

import pytest
from datetime import datetime, timezone
from fastapi import status
from unittest.mock import AsyncMock, patch

from zerg.models.enums import RunStatus, RunTrigger
from zerg.models.models import AgentRun, ThreadMessage
from zerg.services.supervisor_service import SupervisorService


class TestContinuationEndpoint:
    """Tests for POST /api/jarvis/internal/runs/{run_id}/continue endpoint."""

    @pytest.fixture
    def deferred_run(self, db_session, test_user):
        """Create a deferred supervisor run for testing continuation."""
        service = SupervisorService(db_session)
        agent = service.get_or_create_supervisor_agent(test_user.id)
        thread = service.get_or_create_supervisor_thread(test_user.id, agent)

        # Create a DEFERRED run (simulating timeout migration)
        run = AgentRun(
            agent_id=agent.id,
            thread_id=thread.id,
            status=RunStatus.DEFERRED,  # Key: run is deferred
            trigger=RunTrigger.API,
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        return {"agent": agent, "thread": thread, "run": run}

    @pytest.fixture
    def running_run(self, db_session, test_user):
        """Create a running (non-deferred) supervisor run."""
        service = SupervisorService(db_session)
        agent = service.get_or_create_supervisor_agent(test_user.id)
        thread = service.get_or_create_supervisor_thread(test_user.id, agent)

        run = AgentRun(
            agent_id=agent.id,
            thread_id=thread.id,
            status=RunStatus.RUNNING,  # Still running, not deferred
            trigger=RunTrigger.API,
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        return {"agent": agent, "thread": thread, "run": run}

    def test_continuation_triggered_for_deferred_run(self, client, deferred_run):
        """Test that continuation is triggered for a DEFERRED run.

        Note: We don't mock here - we let the endpoint do its work.
        The endpoint spawns a background task and returns immediately.
        We just verify the response indicates the continuation was triggered.
        """
        run = deferred_run["run"]

        payload = {
            "trigger": "worker_complete",
            "job_id": 123,
            "worker_id": "worker-123-abc",
            "status": "success",
            "result_summary": "Disk check complete. /dev/sda1 is 45% full.",
        }

        response = client.post(
            f"/api/jarvis/internal/runs/{run.id}/continue", json=payload
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "continuation_triggered"
        assert data["original_run_id"] == run.id

    def test_continuation_skipped_for_running_run(self, client, running_run):
        """Test that continuation is skipped for non-DEFERRED runs."""
        run = running_run["run"]

        payload = {
            "trigger": "worker_complete",
            "job_id": 123,
            "worker_id": "worker-123-abc",
            "status": "success",
            "result_summary": "Task completed.",
        }

        response = client.post(
            f"/api/jarvis/internal/runs/{run.id}/continue", json=payload
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "skipped"
        assert "not DEFERRED" in data["message"]

    def test_continuation_returns_404_for_nonexistent_run(self, client):
        """Test that 404 is returned for runs that don't exist."""
        payload = {
            "trigger": "worker_complete",
            "job_id": 123,
            "worker_id": "worker-123-abc",
            "status": "success",
            "result_summary": "Task completed.",
        }

        response = client.post("/api/jarvis/internal/runs/99999/continue", json=payload)

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestRunContinuationMethod:
    """Tests for SupervisorService.run_continuation() method."""

    @pytest.fixture
    def deferred_run_setup(self, db_session, test_user):
        """Create a complete setup for testing run_continuation."""
        service = SupervisorService(db_session)
        agent = service.get_or_create_supervisor_agent(test_user.id)
        thread = service.get_or_create_supervisor_thread(test_user.id, agent)

        # Create original run (deferred)
        original_run = AgentRun(
            agent_id=agent.id,
            thread_id=thread.id,
            status=RunStatus.DEFERRED,
            trigger=RunTrigger.API,
        )
        db_session.add(original_run)
        db_session.commit()
        db_session.refresh(original_run)

        return {
            "service": service,
            "agent": agent,
            "thread": thread,
            "original_run": original_run,
        }

    @pytest.mark.asyncio
    async def test_continuation_creates_new_run(self, db_session, deferred_run_setup):
        """Test that run_continuation creates a new run linked to original."""
        setup = deferred_run_setup
        original_run = setup["original_run"]

        # Mock the actual LLM execution
        with patch.object(
            SupervisorService,
            "run_supervisor",
            new_callable=AsyncMock,
        ) as mock_run:
            from zerg.services.supervisor_service import SupervisorRunResult

            mock_run.return_value = SupervisorRunResult(
                run_id=original_run.id + 1,  # Would be the continuation run
                thread_id=setup["thread"].id,
                status="success",
                result="Worker completed the disk check.",
            )

            service = SupervisorService(db_session)
            result = await service.run_continuation(
                original_run_id=original_run.id,
                job_id=123,
                worker_id="worker-123-abc",
                result_summary="Disk check complete. /dev/sda1 is 45% full.",
            )

            # Verify continuation was attempted
            assert mock_run.called

            # Check that run_supervisor was called with continuation task
            call_kwargs = mock_run.call_args.kwargs
            assert "[CONTINUATION]" in call_kwargs.get("task", "")

    @pytest.mark.asyncio
    async def test_continuation_injects_tool_message(
        self, db_session, deferred_run_setup
    ):
        """Test that run_continuation injects worker result as tool message."""
        setup = deferred_run_setup
        original_run = setup["original_run"]
        thread = setup["thread"]

        # Count messages before
        before_count = (
            db_session.query(ThreadMessage)
            .filter(ThreadMessage.thread_id == thread.id)
            .count()
        )

        # Mock run_supervisor to prevent actual LLM call
        with patch.object(
            SupervisorService,
            "run_supervisor",
            new_callable=AsyncMock,
        ) as mock_run:
            from zerg.services.supervisor_service import SupervisorRunResult

            mock_run.return_value = SupervisorRunResult(
                run_id=original_run.id + 1,
                thread_id=thread.id,
                status="success",
                result="Done.",
            )

            service = SupervisorService(db_session)
            await service.run_continuation(
                original_run_id=original_run.id,
                job_id=123,
                worker_id="worker-123-abc",
                result_summary="Disk check complete.",
            )

        # Verify tool message was injected
        after_count = (
            db_session.query(ThreadMessage)
            .filter(ThreadMessage.thread_id == thread.id)
            .count()
        )

        # Should have at least 1 new message (the tool result)
        # Note: run_supervisor mock won't add more messages
        assert after_count >= before_count + 1

        # Check the tool message content
        tool_msg = (
            db_session.query(ThreadMessage)
            .filter(ThreadMessage.thread_id == thread.id)
            .filter(ThreadMessage.role == "tool")
            .order_by(ThreadMessage.id.desc())
            .first()
        )
        assert tool_msg is not None
        assert "Worker job 123 completed" in tool_msg.content
        assert "Disk check complete." in tool_msg.content

    @pytest.mark.asyncio
    async def test_continuation_raises_for_nonexistent_run(self, db_session, test_user):
        """Test that run_continuation raises error for nonexistent runs."""
        service = SupervisorService(db_session)

        with pytest.raises(ValueError, match="not found"):
            await service.run_continuation(
                original_run_id=99999,
                job_id=123,
                worker_id="worker-123-abc",
                result_summary="Disk check complete.",
            )


class TestContinuationOfRunId:
    """Tests for the continuation_of_run_id model field."""

    def test_continuation_of_run_id_is_nullable(self, db_session, test_user):
        """Test that continuation_of_run_id can be null (normal runs)."""
        service = SupervisorService(db_session)
        agent = service.get_or_create_supervisor_agent(test_user.id)
        thread = service.get_or_create_supervisor_thread(test_user.id, agent)

        run = AgentRun(
            agent_id=agent.id,
            thread_id=thread.id,
            status=RunStatus.RUNNING,
            trigger=RunTrigger.API,
            # continuation_of_run_id not set - should be None
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        assert run.continuation_of_run_id is None

    def test_continuation_of_run_id_links_runs(self, db_session, test_user):
        """Test that continuation_of_run_id correctly links continuation to original."""
        service = SupervisorService(db_session)
        agent = service.get_or_create_supervisor_agent(test_user.id)
        thread = service.get_or_create_supervisor_thread(test_user.id, agent)

        # Create original run
        original_run = AgentRun(
            agent_id=agent.id,
            thread_id=thread.id,
            status=RunStatus.DEFERRED,
            trigger=RunTrigger.API,
        )
        db_session.add(original_run)
        db_session.commit()
        db_session.refresh(original_run)

        # Create continuation run
        continuation_run = AgentRun(
            agent_id=agent.id,
            thread_id=thread.id,
            status=RunStatus.RUNNING,
            trigger=RunTrigger.CONTINUATION,
            continuation_of_run_id=original_run.id,
        )
        db_session.add(continuation_run)
        db_session.commit()
        db_session.refresh(continuation_run)

        assert continuation_run.continuation_of_run_id == original_run.id
        assert continuation_run.trigger == RunTrigger.CONTINUATION

    def test_continued_from_relationship(self, db_session, test_user):
        """Test the continued_from ORM relationship."""
        service = SupervisorService(db_session)
        agent = service.get_or_create_supervisor_agent(test_user.id)
        thread = service.get_or_create_supervisor_thread(test_user.id, agent)

        # Create original run
        original_run = AgentRun(
            agent_id=agent.id,
            thread_id=thread.id,
            status=RunStatus.DEFERRED,
            trigger=RunTrigger.API,
        )
        db_session.add(original_run)
        db_session.commit()
        db_session.refresh(original_run)

        # Create continuation run
        continuation_run = AgentRun(
            agent_id=agent.id,
            thread_id=thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.CONTINUATION,
            continuation_of_run_id=original_run.id,
        )
        db_session.add(continuation_run)
        db_session.commit()

        # Refresh both to load relationships
        db_session.refresh(original_run)
        db_session.refresh(continuation_run)

        # Test relationship from continuation to original
        assert continuation_run.continued_from == original_run

        # Test reverse relationship (original.continuations)
        assert continuation_run in original_run.continuations


class TestRunTriggerContinuation:
    """Tests for the CONTINUATION trigger type."""

    def test_continuation_trigger_exists(self):
        """Test that CONTINUATION trigger type exists in enum."""
        assert hasattr(RunTrigger, "CONTINUATION")
        assert RunTrigger.CONTINUATION.value == "continuation"

    def test_can_create_run_with_continuation_trigger(self, db_session, test_user):
        """Test that runs can be created with CONTINUATION trigger."""
        service = SupervisorService(db_session)
        agent = service.get_or_create_supervisor_agent(test_user.id)
        thread = service.get_or_create_supervisor_thread(test_user.id, agent)

        run = AgentRun(
            agent_id=agent.id,
            thread_id=thread.id,
            status=RunStatus.RUNNING,
            trigger=RunTrigger.CONTINUATION,
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        assert run.trigger == RunTrigger.CONTINUATION
        assert run.trigger.value == "continuation"
