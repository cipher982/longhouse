"""Tests for Jarvis router endpoints."""

import pytest
from fastapi import status

from zerg.models.enums import RunStatus, RunTrigger
from zerg.models.models import AgentRun
from zerg.services.supervisor_service import SupervisorService


class TestSupervisorCancelEndpoint:
    """Tests for POST /api/jarvis/supervisor/{run_id}/cancel endpoint."""

    @pytest.fixture
    def supervisor_components(self, db_session, test_user):
        """Create supervisor agent, thread, and run for testing."""
        service = SupervisorService(db_session)
        agent = service.get_or_create_supervisor_agent(test_user.id)
        thread = service.get_or_create_supervisor_thread(test_user.id, agent)

        # Create a running supervisor run
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

    def test_cancel_running_run_succeeds(
        self, client, db_session, test_user, supervisor_components
    ):
        """Test that cancelling a running run succeeds."""
        run = supervisor_components["run"]

        response = client.post(f"/api/jarvis/supervisor/{run.id}/cancel")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["run_id"] == run.id
        assert data["status"] == "cancelled"
        assert data["message"] == "Investigation cancelled"

        # Verify database state
        db_session.refresh(run)
        assert run.status == RunStatus.CANCELLED
        assert run.finished_at is not None

    def test_cancel_already_completed_run(
        self, client, db_session, test_user, supervisor_components
    ):
        """Test that cancelling an already-completed run returns current status."""
        run = supervisor_components["run"]

        # Mark run as already completed
        run.status = RunStatus.SUCCESS
        db_session.commit()

        response = client.post(f"/api/jarvis/supervisor/{run.id}/cancel")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["run_id"] == run.id
        assert data["status"] == "success"
        assert data["message"] == "Run already completed"

    def test_cancel_already_cancelled_run(
        self, client, db_session, test_user, supervisor_components
    ):
        """Test that cancelling an already-cancelled run returns current status."""
        run = supervisor_components["run"]

        # Mark run as already cancelled
        run.status = RunStatus.CANCELLED
        db_session.commit()

        response = client.post(f"/api/jarvis/supervisor/{run.id}/cancel")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["run_id"] == run.id
        assert data["status"] == "cancelled"
        assert data["message"] == "Run already completed"

    def test_cancel_nonexistent_run_returns_404(self, client, db_session, test_user):
        """Test that cancelling a nonexistent run returns 404."""
        response = client.post("/api/jarvis/supervisor/999999/cancel")

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert "not found" in response.json()["detail"].lower()

    def test_cancel_other_user_run_returns_404(
        self, client, db_session, test_user, other_user, supervisor_components
    ):
        """Test that cancelling another user's run returns 404 (no info leak)."""
        # Create a run for the other user
        service = SupervisorService(db_session)
        other_agent = service.get_or_create_supervisor_agent(other_user.id)
        other_thread = service.get_or_create_supervisor_thread(other_user.id, other_agent)

        other_run = AgentRun(
            agent_id=other_agent.id,
            thread_id=other_thread.id,
            status=RunStatus.RUNNING,
            trigger=RunTrigger.API,
        )
        db_session.add(other_run)
        db_session.commit()
        db_session.refresh(other_run)

        # Test user tries to cancel other user's run
        response = client.post(f"/api/jarvis/supervisor/{other_run.id}/cancel")

        # Should return 404 to not reveal existence
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_cancel_failed_run_returns_current_status(
        self, client, db_session, test_user, supervisor_components
    ):
        """Test that cancelling a failed run returns current status."""
        run = supervisor_components["run"]

        # Mark run as already failed
        run.status = RunStatus.FAILED
        db_session.commit()

        response = client.post(f"/api/jarvis/supervisor/{run.id}/cancel")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["run_id"] == run.id
        assert data["status"] == "failed"
        assert data["message"] == "Run already completed"
