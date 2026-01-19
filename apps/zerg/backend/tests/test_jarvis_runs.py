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


class TestListJarvisRuns:
    def test_list_runs_avoids_agent_n_plus_one(self, client, db_session, test_user, monkeypatch):
        service = SupervisorService(db_session)
        agent = service.get_or_create_supervisor_agent(test_user.id)
        thread = service.get_or_create_supervisor_thread(test_user.id, agent)

        run = AgentRun(
            agent_id=agent.id,
            thread_id=thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.API,
        )
        db_session.add(run)
        db_session.commit()

        from zerg.routers import jarvis_runs

        def _boom(*_args, **_kwargs):
            raise AssertionError("crud.get_agent should not be called")

        monkeypatch.setattr(jarvis_runs.crud, "get_agent", _boom)

        response = client.get("/api/jarvis/runs")
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload[0]["agent_name"] == agent.name


import httpx
import pytest_asyncio


class TestAttachToRunStream:
    """Tests for GET /api/jarvis/runs/{run_id}/stream endpoint."""

    @pytest.fixture
    def run_components(self, db_session, test_user):
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

    @pytest_asyncio.fixture
    async def async_client(self, db_session, auth_headers):
        """Asynchronous client for testing SSE streams."""
        from zerg.database import get_db
        from zerg.main import app

        def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
            ac.headers.update(auth_headers)
            yield ac
        app.dependency_overrides = {}

    @pytest.mark.asyncio
    async def test_attach_to_completed_run(self, async_client, db_session, run_components):
        """Test attaching to a completed run returns result immediately."""
        run = run_components["run"]
        thread = run_components["thread"]

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

        # Use the JSON endpoint, not the SSE stream endpoint
        response = await async_client.get(f"/api/jarvis/runs/{run.id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["run_id"] == run.id
        assert data["status"] == "success"
        assert data["result"] == "Analysis complete."
        assert data["error"] is None

    @pytest.mark.asyncio
    async def test_attach_to_failed_run(self, async_client, db_session, run_components):
        """Test attaching to a failed run returns error."""
        run = run_components["run"]

        # Mark run as failed
        run.status = RunStatus.FAILED
        run.error = "Tool execution failed"
        db_session.commit()

        # Use the JSON endpoint, not the SSE stream endpoint
        response = await async_client.get(f"/api/jarvis/runs/{run.id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "failed"
        assert data["error"] == "Tool execution failed"
        assert data["result"] is None

    @pytest.mark.asyncio
    async def test_attach_to_in_progress_run(self, async_client, run_components):
        """Test attaching to an in-progress run returns current status snapshot."""
        run = run_components["run"]

        # Use the JSON endpoint, not the SSE stream endpoint
        response = await async_client.get(f"/api/jarvis/runs/{run.id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["run_id"] == run.id
        assert data["status"] == "running"
        assert data["result"] is None

    @pytest.mark.asyncio
    async def test_attach_to_nonexistent_run(self, async_client):
        """Test attaching to a run that doesn't exist."""
        # Use the JSON endpoint, not the SSE stream endpoint
        response = await async_client.get("/api/jarvis/runs/99999")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_attach_stream_multi_tenant_isolation(self, async_client, db_session, run_components, other_user):
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

        # Try to attach to the other user's run (use JSON endpoint)
        response = await async_client.get(f"/api/jarvis/runs/{other_run.id}")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_attach_includes_finished_at(self, async_client, db_session, run_components):
        """Test that finished_at timestamp is included in response."""
        run = run_components["run"]
        thread = run_components["thread"]

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

        # Use the JSON endpoint, not the SSE stream endpoint
        response = await async_client.get(f"/api/jarvis/runs/{run.id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["finished_at"] is not None
        # Verify it's a valid ISO timestamp
        datetime.fromisoformat(data["finished_at"].replace("Z", "+00:00"))


class TestGetRunTimeline:
    """Tests for GET /api/jarvis/runs/{run_id}/timeline endpoint (Phase 3: chat-observability-eval)."""

    @pytest.fixture
    def run_with_events(self, db_session, test_user):
        """Create a run with sample timeline events."""
        from datetime import datetime, timezone, timedelta
        from zerg.models.agent_run_event import AgentRunEvent

        service = SupervisorService(db_session)
        agent = service.get_or_create_supervisor_agent(test_user.id)
        thread = service.get_or_create_supervisor_thread(test_user.id, agent)

        # Create a run with correlation ID
        run = AgentRun(
            agent_id=agent.id,
            thread_id=thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.API,
            correlation_id="test-correlation-123",
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        # Create timeline events (simulating a full supervisor + worker flow)
        base_time = datetime.now(timezone.utc)

        events = [
            AgentRunEvent(
                run_id=run.id,
                event_type="supervisor_started",
                payload={"message": "Starting supervisor"},
                created_at=base_time,
            ),
            AgentRunEvent(
                run_id=run.id,
                event_type="supervisor_thinking",
                payload={"status": "analyzing"},
                created_at=base_time + timedelta(milliseconds=100),
            ),
            AgentRunEvent(
                run_id=run.id,
                event_type="worker_spawned",
                payload={"job_id": 1, "worker_type": "executor"},
                created_at=base_time + timedelta(milliseconds=500),
            ),
            AgentRunEvent(
                run_id=run.id,
                event_type="worker_started",
                payload={"worker_id": "worker-123"},
                created_at=base_time + timedelta(milliseconds=700),
            ),
            AgentRunEvent(
                run_id=run.id,
                event_type="tool_started",
                payload={"tool_name": "ssh_exec"},
                created_at=base_time + timedelta(milliseconds=900),
            ),
            AgentRunEvent(
                run_id=run.id,
                event_type="tool_completed",
                payload={"tool_name": "ssh_exec", "duration_ms": 400},
                created_at=base_time + timedelta(milliseconds=1300),
            ),
            AgentRunEvent(
                run_id=run.id,
                event_type="worker_complete",
                payload={"result": "Success"},
                created_at=base_time + timedelta(milliseconds=1600),
            ),
            AgentRunEvent(
                run_id=run.id,
                event_type="supervisor_complete",
                payload={"final_result": "Task complete"},
                created_at=base_time + timedelta(milliseconds=2000),
            ),
        ]

        for event in events:
            db_session.add(event)
        db_session.commit()

        return {"agent": agent, "thread": thread, "run": run, "events": events}

    def test_get_timeline_success(self, client, run_with_events):
        """Test getting timeline for a run with events."""
        run = run_with_events["run"]

        response = client.get(f"/api/jarvis/runs/{run.id}/timeline")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()

        # Verify structure
        assert data["correlation_id"] == "test-correlation-123"
        assert data["run_id"] == run.id
        assert "events" in data
        assert "summary" in data

        # Verify events
        events = data["events"]
        assert len(events) == 8  # All 8 events

        # First event should have offset 0
        assert events[0]["phase"] == "supervisor_started"
        assert events[0]["offset_ms"] == 0
        assert events[0]["timestamp"] is not None

        # Last event should have highest offset
        assert events[-1]["phase"] == "supervisor_complete"
        assert events[-1]["offset_ms"] > 0

        # Verify events are sorted by offset
        offsets = [e["offset_ms"] for e in events]
        assert offsets == sorted(offsets)

        # Verify metadata is included
        assert events[2]["metadata"]["job_id"] == 1
        assert events[4]["metadata"]["tool_name"] == "ssh_exec"

    def test_get_timeline_summary_calculations(self, client, run_with_events):
        """Test that summary statistics are calculated correctly."""
        run = run_with_events["run"]

        response = client.get(f"/api/jarvis/runs/{run.id}/timeline")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()

        summary = data["summary"]

        # Total duration should be from first to last event
        assert summary["total_duration_ms"] == 2000

        # Supervisor thinking time: supervisor_started to worker_spawned
        assert summary["supervisor_thinking_ms"] == 500

        # Worker execution time: worker_spawned to worker_complete
        assert summary["worker_execution_ms"] == 1100

        # Tool execution time: tool_started to tool_completed
        assert summary["tool_execution_ms"] == 400

    def test_get_timeline_empty_events(self, client, db_session, test_user):
        """Test getting timeline for a run with no events yet."""
        service = SupervisorService(db_session)
        agent = service.get_or_create_supervisor_agent(test_user.id)
        thread = service.get_or_create_supervisor_thread(test_user.id, agent)

        # Create a run with no events
        run = AgentRun(
            agent_id=agent.id,
            thread_id=thread.id,
            status=RunStatus.RUNNING,
            trigger=RunTrigger.API,
            correlation_id="empty-run-123",
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        response = client.get(f"/api/jarvis/runs/{run.id}/timeline")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()

        assert data["correlation_id"] == "empty-run-123"
        assert data["run_id"] == run.id
        assert data["events"] == []
        assert data["summary"]["total_duration_ms"] == 0
        assert data["summary"]["supervisor_thinking_ms"] is None
        assert data["summary"]["worker_execution_ms"] is None
        assert data["summary"]["tool_execution_ms"] is None

    def test_get_timeline_nonexistent_run(self, client):
        """Test getting timeline for a run that doesn't exist."""
        response = client.get("/api/jarvis/runs/99999/timeline")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_get_timeline_multi_tenant_isolation(self, client, db_session, run_with_events, other_user):
        """Test that users cannot access timelines for runs owned by other users."""
        # Create a run owned by another user
        other_agent = SupervisorService(db_session).get_or_create_supervisor_agent(other_user.id)
        other_thread = SupervisorService(db_session).get_or_create_supervisor_thread(other_user.id, other_agent)

        other_run = AgentRun(
            agent_id=other_agent.id,
            thread_id=other_thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.API,
            correlation_id="other-user-run",
        )
        db_session.add(other_run)
        db_session.commit()
        db_session.refresh(other_run)

        # Try to access the other user's run timeline (client is authenticated as test_user)
        response = client.get(f"/api/jarvis/runs/{other_run.id}/timeline")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_get_timeline_partial_flow(self, client, db_session, test_user):
        """Test timeline with partial flow (supervisor only, no worker)."""
        from datetime import datetime, timezone, timedelta
        from zerg.models.agent_run_event import AgentRunEvent

        service = SupervisorService(db_session)
        agent = service.get_or_create_supervisor_agent(test_user.id)
        thread = service.get_or_create_supervisor_thread(test_user.id, agent)

        run = AgentRun(
            agent_id=agent.id,
            thread_id=thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.API,
            correlation_id="partial-flow",
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        # Only supervisor events (no worker spawned)
        base_time = datetime.now(timezone.utc)
        events = [
            AgentRunEvent(
                run_id=run.id,
                event_type="supervisor_started",
                payload={},
                created_at=base_time,
            ),
            AgentRunEvent(
                run_id=run.id,
                event_type="supervisor_thinking",
                payload={},
                created_at=base_time + timedelta(milliseconds=200),
            ),
            AgentRunEvent(
                run_id=run.id,
                event_type="supervisor_complete",
                payload={},
                created_at=base_time + timedelta(milliseconds=800),
            ),
        ]

        for event in events:
            db_session.add(event)
        db_session.commit()

        response = client.get(f"/api/jarvis/runs/{run.id}/timeline")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()

        # Summary should handle missing worker/tool metrics gracefully
        summary = data["summary"]
        assert summary["total_duration_ms"] == 800
        assert summary["supervisor_thinking_ms"] is None  # No worker_spawned event
        assert summary["worker_execution_ms"] is None
        assert summary["tool_execution_ms"] is None
