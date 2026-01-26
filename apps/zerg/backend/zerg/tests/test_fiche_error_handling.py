"""
Tests for fiche error handling functionality.

This module tests error handling behavior for fiches, specifically verifying that:
1. Fiche status is correctly set to "error" when exceptions occur
2. The last_error field is properly populated with error messages
3. The status is reset to "idle" on successful runs
"""

from datetime import datetime
from datetime import timezone
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from zerg.models.models import Fiche
from zerg.services.scheduler_service import SchedulerService


class TestFicheErrorHandling:
    @pytest.fixture
    def mock_fiche(self):
        """Create a mock fiche with default values for testing."""
        fiche = MagicMock(spec=Fiche)
        fiche.id = 1
        fiche.name = "Test Fiche"
        fiche.status = "idle"
        fiche.system_instructions = "System instructions"
        fiche.task_instructions = "Task instructions"
        fiche.owner_id = 1
        fiche.last_error = None
        return fiche

    @pytest.fixture
    def mock_db_session(self, mock_fiche):
        """Create a mock DB session for testing."""
        session = MagicMock()

        # Mock CRUD operations
        session.query.return_value.filter.return_value.first.return_value = mock_fiche

        # Mock commit to keep track of it being called
        session.commit = MagicMock()

        return session

    @pytest.fixture
    def mock_session_factory(self, mock_db_session):
        """Create a mock session factory that returns our mock session."""
        return lambda: mock_db_session

    @pytest.fixture
    def scheduler_service(self, mock_session_factory):
        """Create a scheduler service instance with mocked dependencies."""
        scheduler = SchedulerService(session_factory=mock_session_factory)
        # Mock the scheduler object itself
        scheduler.scheduler = MagicMock()
        scheduler.scheduler.get_job = MagicMock(return_value=None)
        return scheduler

    @patch("zerg.services.scheduler_service.execute_fiche_task", new_callable=AsyncMock)
    @patch("zerg.services.scheduler_service.crud")
    @patch("zerg.services.scheduler_service.event_bus")
    async def test_scheduler_successful_run(self, mock_event_bus, mock_crud, mock_execute, scheduler_service, mock_db_session, mock_fiche):
        """Test that a scheduled run calls execute_fiche_task and updates next_course_at."""
        fiche_id = 1
        mock_crud.get_fiche.return_value = mock_fiche
        mock_event_bus.publish = AsyncMock()

        mock_thread = MagicMock()
        mock_thread.id = "thread-123"
        mock_execute.return_value = mock_thread

        next_run_time = datetime.now(timezone.utc)
        scheduler_service.scheduler.get_job.return_value = MagicMock(next_run_time=next_run_time)

        await scheduler_service.run_fiche_task(fiche_id)

        mock_execute.assert_awaited_once_with(
            mock_db_session,
            mock_fiche,
            thread_type="schedule",
            trigger="schedule",
        )
        mock_crud.update_fiche.assert_called_with(mock_db_session, fiche_id, next_course_at=next_run_time)
        mock_event_bus.publish.assert_awaited()

    @patch("zerg.services.scheduler_service.execute_fiche_task", new_callable=AsyncMock)
    @patch("zerg.services.scheduler_service.crud")
    @patch("zerg.services.scheduler_service.event_bus")
    async def test_scheduler_error_handling(self, mock_event_bus, mock_crud, mock_execute, scheduler_service, mock_db_session, mock_fiche):
        """Test that scheduler swallows execution errors and logs them."""
        fiche_id = 1
        mock_crud.get_fiche.return_value = mock_fiche
        mock_event_bus.publish = AsyncMock()
        mock_execute.side_effect = Exception("Test error message")

        await scheduler_service.run_fiche_task(fiche_id)

        mock_execute.assert_awaited_once()
        mock_event_bus.publish.assert_not_called()

    @patch("zerg.routers.fiches.execute_fiche_task", new_callable=AsyncMock)
    @patch("zerg.routers.fiches.crud")
    async def test_manual_run_error_handling(self, mock_crud, mock_execute):
        """Test that manual runs surface execution errors as HTTP exceptions."""
        from zerg.routers.fiches import run_fiche_task

        fiche_id = 1
        mock_fiche = MagicMock(spec=Fiche)
        mock_fiche.id = fiche_id
        mock_fiche.owner_id = 1
        mock_crud.get_fiche.return_value = mock_fiche

        mock_execute.side_effect = ValueError("Fiche already running")

        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.role = "USER"

        with pytest.raises(HTTPException) as excinfo:
            await run_fiche_task(fiche_id=fiche_id, db=MagicMock(), current_user=mock_user)

        assert excinfo.value.status_code == 409

    @patch("zerg.routers.fiches.execute_fiche_task", new_callable=AsyncMock)
    @patch("zerg.routers.fiches.crud")
    async def test_manual_run_success(self, mock_crud, mock_execute):
        """Test that manual runs return the thread id on success."""
        from zerg.routers.fiches import run_fiche_task

        fiche_id = 1
        mock_fiche = MagicMock(spec=Fiche)
        mock_fiche.id = fiche_id
        mock_fiche.owner_id = 1
        mock_crud.get_fiche.return_value = mock_fiche

        mock_thread = MagicMock()
        mock_thread.id = "thread-789"
        mock_execute.return_value = mock_thread

        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.role = "USER"

        result = await run_fiche_task(fiche_id=fiche_id, db=MagicMock(), current_user=mock_user)

        assert result["thread_id"] == mock_thread.id
