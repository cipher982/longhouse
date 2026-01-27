"""Tests for Oikos router endpoints."""

import pytest
from fastapi import status

from zerg.models.enums import RunStatus
from zerg.models.enums import RunTrigger
from zerg.models.models import Run
from zerg.services.oikos_service import OikosService


class TestOikosCancelEndpoint:
    """Tests for POST /api/oikos/run/{run_id}/cancel endpoint."""

    @pytest.fixture
    def oikos_components(self, db_session, test_user):
        """Create oikos fiche, thread, and run for testing."""
        service = OikosService(db_session)
        fiche = service.get_or_create_oikos_fiche(test_user.id)
        thread = service.get_or_create_oikos_thread(test_user.id, fiche)

        # Create a running oikos run
        run = Run(
            fiche_id=fiche.id,
            thread_id=thread.id,
            status=RunStatus.RUNNING,
            trigger=RunTrigger.API,
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        return {"fiche": fiche, "thread": thread, "run": run}

    def test_cancel_running_run_succeeds(self, client, db_session, test_user, oikos_components):
        """Test that cancelling a running run succeeds."""
        run = oikos_components["run"]

        response = client.post(f"/api/oikos/run/{run.id}/cancel")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["run_id"] == run.id
        assert data["status"] == "cancelled"
        assert data["message"] == "Investigation cancelled"

        # Verify database state
        db_session.refresh(run)
        assert run.status == RunStatus.CANCELLED
        assert run.finished_at is not None

    def test_cancel_already_completed_run(self, client, db_session, test_user, oikos_components):
        """Test that cancelling an already-completed run returns current status."""
        run = oikos_components["run"]

        # Mark run as already completed
        run.status = RunStatus.SUCCESS
        db_session.commit()

        response = client.post(f"/api/oikos/run/{run.id}/cancel")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["run_id"] == run.id
        assert data["status"] == "success"
        assert data["message"] == "Run already completed"

    def test_cancel_already_cancelled_run(self, client, db_session, test_user, oikos_components):
        """Test that cancelling an already-cancelled run returns current status."""
        run = oikos_components["run"]

        # Mark run as already cancelled
        run.status = RunStatus.CANCELLED
        db_session.commit()

        response = client.post(f"/api/oikos/run/{run.id}/cancel")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["run_id"] == run.id
        assert data["status"] == "cancelled"
        assert data["message"] == "Run already completed"

    def test_cancel_nonexistent_run_returns_404(self, client, db_session, test_user):
        """Test that cancelling a nonexistent run returns 404."""
        response = client.post("/api/oikos/run/999999/cancel")

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert "not found" in response.json()["detail"].lower()

    def test_cancel_other_user_run_returns_404(self, client, db_session, test_user, other_user, oikos_components):
        """Test that cancelling another user's run returns 404 (no info leak)."""
        # Create a run for the other user
        service = OikosService(db_session)
        other_agent = service.get_or_create_oikos_fiche(other_user.id)
        other_thread = service.get_or_create_oikos_thread(other_user.id, other_agent)

        other_run = Run(
            fiche_id=other_agent.id,
            thread_id=other_thread.id,
            status=RunStatus.RUNNING,
            trigger=RunTrigger.API,
        )
        db_session.add(other_run)
        db_session.commit()
        db_session.refresh(other_run)

        # Test user tries to cancel other user's run
        response = client.post(f"/api/oikos/run/{other_run.id}/cancel")

        # Should return 404 to not reveal existence
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_cancel_failed_run_returns_current_status(self, client, db_session, test_user, oikos_components):
        """Test that cancelling a failed run returns current status."""
        run = oikos_components["run"]

        # Mark run as already failed
        run.status = RunStatus.FAILED
        db_session.commit()

        response = client.post(f"/api/oikos/run/{run.id}/cancel")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["run_id"] == run.id
        assert data["status"] == "failed"
        assert data["message"] == "Run already completed"
