"""Tests for Jarvis run status and stream endpoints (Phase 4 of durable-runs-v2.2)."""

import pytest
from fastapi import status

from zerg.models.enums import RunStatus
from zerg.models.enums import RunTrigger
from zerg.models.models import AgentRun
from zerg.models.models import Thread
from zerg.models.models import ThreadMessage
from zerg.services.supervisor_service import SupervisorService


class TestGetRunStatus:
    """Tests for GET /api/jarvis/runs/{run_id} endpoint."""

    @pytest.fixture
    def run_components(self, db_session, test_user):
        """Create supervisor agent, thread, run, and messages for testing."""
        service = SupervisorService(db_session)
        agent = service.get_or_create_supervisor_agent(test_user.id)
        thread = service.get_or_create_supervisor_thread(test_user.id, agent)

        # Create a run
        run = AgentRun(
            agent_id=agent.id,
            thread_id=thread.id,
            status=RunStatus.RUNNING,
            trigger=RunTrigger.API,
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        return {"agent": agent, "thread": thread, "run": run}

    def test_get_running_run_status(self, client, run_components):
        """Test getting status of a running run."""
        run = run_components["run"]

        response = client.get(f"/api/jarvis/runs/{run.id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["run_id"] == run.id
        assert data["status"] == "running"
        assert data["created_at"] is not None
        assert data["finished_at"] is None
        assert data["error"] is None
        assert data["result"] is None  # No result for running runs

    def test_get_successful_run_with_result(self, client, db_session, run_components):
        """Test getting status of a completed run with result."""
        run = run_components["run"]
        thread = run_components["thread"]

        # Add an assistant message to the thread
        message = ThreadMessage(
            thread_id=thread.id,
            role="assistant",
            content="Task completed successfully. Here is your result.",
        )
        db_session.add(message)

        # Mark run as successful
        run.status = RunStatus.SUCCESS
        db_session.commit()

        response = client.get(f"/api/jarvis/runs/{run.id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["run_id"] == run.id
        assert data["status"] == "success"
        assert data["result"] == "Task completed successfully. Here is your result."
        assert data["error"] is None

    def test_get_failed_run_with_error(self, client, db_session, run_components):
        """Test getting status of a failed run."""
        run = run_components["run"]

        # Mark run as failed with error
        run.status = RunStatus.FAILED
        run.error = "Connection timeout after 30s"
        db_session.commit()

        response = client.get(f"/api/jarvis/runs/{run.id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["run_id"] == run.id
        assert data["status"] == "failed"
        assert data["error"] == "Connection timeout after 30s"
        assert data["result"] is None  # No result for failed runs

    def test_get_nonexistent_run(self, client):
        """Test getting status of a run that doesn't exist."""
        response = client.get("/api/jarvis/runs/99999")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_get_run_multi_tenant_isolation(self, client, db_session, run_components, other_user):
        """Test that users cannot access runs owned by other users."""
        run = run_components["run"]

        # Create a run owned by another user
        other_agent = SupervisorService(db_session).get_or_create_supervisor_agent(other_user.id)
        other_thread = SupervisorService(db_session).get_or_create_supervisor_thread(other_user.id, other_agent)

        other_run = AgentRun(
            agent_id=other_agent.id,
            thread_id=other_thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.API,
        )
        db_session.add(other_run)
        db_session.commit()
        db_session.refresh(other_run)

        # Try to access the other user's run (client is authenticated as test_user)
        response = client.get(f"/api/jarvis/runs/{other_run.id}")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_get_run_with_structured_content(self, client, db_session, run_components):
        """Test handling of structured message content (JSON-encoded list of blocks)."""
        import json

        run = run_components["run"]
        thread = run_components["thread"]

        # Add an assistant message with structured content (stored as JSON string)
        structured_content = [
            {"type": "text", "text": "First part. "},
            {"type": "text", "text": "Second part."},
        ]
        message = ThreadMessage(
            thread_id=thread.id,
            role="assistant",
            content=json.dumps(structured_content),  # Store as JSON string
        )
        db_session.add(message)

        # Mark run as successful
        run.status = RunStatus.SUCCESS
        db_session.commit()

        response = client.get(f"/api/jarvis/runs/{run.id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        # Helper should parse JSON and extract text blocks
        assert data["result"] == "First part.  Second part."

    def test_get_run_with_multiple_messages(self, client, db_session, run_components):
        """Test that only the LAST assistant message is returned as result."""
        run = run_components["run"]
        thread = run_components["thread"]

        # Add multiple assistant messages
        for i in range(3):
            message = ThreadMessage(
                thread_id=thread.id,
                role="assistant",
                content=f"Message {i + 1}",
            )
            db_session.add(message)

        # Mark run as successful
        run.status = RunStatus.SUCCESS
        db_session.commit()

        response = client.get(f"/api/jarvis/runs/{run.id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        # Should return the LAST message (Message 3)
        assert data["result"] == "Message 3"


class TestAttachToRunStream:
    """Tests for GET /api/jarvis/runs/{run_id}/stream endpoint."""

    @pytest.fixture
    def stream_components(self, db_session, test_user):
        """Create supervisor agent, thread, and run for stream testing."""
        service = SupervisorService(db_session)
        agent = service.get_or_create_supervisor_agent(test_user.id)
        thread = service.get_or_create_supervisor_thread(test_user.id, agent)

        run = AgentRun(
            agent_id=agent.id,
            thread_id=thread.id,
            status=RunStatus.RUNNING,
            trigger=RunTrigger.API,
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        return {"agent": agent, "thread": thread, "run": run}

    def test_attach_to_completed_run(self, client, db_session, stream_components):
        """Test attaching to a completed run returns result immediately."""
        run = stream_components["run"]
        thread = stream_components["thread"]

        # Add result message
        message = ThreadMessage(
            thread_id=thread.id,
            role="assistant",
            content="Analysis complete.",
        )
        db_session.add(message)

        # Mark run as complete
        run.status = RunStatus.SUCCESS
        db_session.commit()

        response = client.get(f"/api/jarvis/runs/{run.id}/stream")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["run_id"] == run.id
        assert data["status"] == "success"
        assert data["result"] == "Analysis complete."
        assert data["error"] is None

    def test_attach_to_failed_run(self, client, db_session, stream_components):
        """Test attaching to a failed run returns error."""
        run = stream_components["run"]

        # Mark run as failed
        run.status = RunStatus.FAILED
        run.error = "Tool execution failed"
        db_session.commit()

        response = client.get(f"/api/jarvis/runs/{run.id}/stream")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "failed"
        assert data["error"] == "Tool execution failed"
        assert data["result"] is None

    def test_attach_to_in_progress_run(self, client, stream_components):
        """Test attaching to an in-progress run returns current status (MVP)."""
        run = stream_components["run"]

        response = client.get(f"/api/jarvis/runs/{run.id}/stream")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["run_id"] == run.id
        assert data["status"] == "running"
        # For MVP, no streaming - just current status
        assert data["result"] is None

    def test_attach_to_nonexistent_run(self, client):
        """Test attaching to a run that doesn't exist."""
        response = client.get("/api/jarvis/runs/99999/stream")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_attach_stream_multi_tenant_isolation(self, client, db_session, stream_components, other_user):
        """Test that users cannot attach to runs owned by other users."""
        # Create a run owned by another user
        other_agent = SupervisorService(db_session).get_or_create_supervisor_agent(other_user.id)
        other_thread = SupervisorService(db_session).get_or_create_supervisor_thread(other_user.id, other_agent)

        other_run = AgentRun(
            agent_id=other_agent.id,
            thread_id=other_thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.API,
        )
        db_session.add(other_run)
        db_session.commit()
        db_session.refresh(other_run)

        # Try to attach to the other user's run
        response = client.get(f"/api/jarvis/runs/{other_run.id}/stream")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_attach_includes_finished_at(self, client, db_session, stream_components):
        """Test that finished_at timestamp is included in response."""
        run = stream_components["run"]
        thread = stream_components["thread"]

        # Add result message
        message = ThreadMessage(
            thread_id=thread.id,
            role="assistant",
            content="Done.",
        )
        db_session.add(message)

        # Mark run as complete
        run.status = RunStatus.SUCCESS
        from datetime import datetime, timezone

        run.finished_at = datetime.now(timezone.utc)
        db_session.commit()

        response = client.get(f"/api/jarvis/runs/{run.id}/stream")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["finished_at"] is not None
        # Verify it's a valid ISO timestamp
        datetime.fromisoformat(data["finished_at"].replace("Z", "+00:00"))
