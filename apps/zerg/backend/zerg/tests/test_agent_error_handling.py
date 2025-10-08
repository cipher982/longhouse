"""
Tests for agent error handling functionality.

This module tests error handling behavior for agents, specifically verifying that:
1. Agent status is correctly set to "error" when exceptions occur
2. The last_error field is properly populated with error messages
3. The status is reset to "idle" on successful runs
"""

from unittest.mock import ANY
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from zerg.events import EventType
from zerg.models.models import Agent
from zerg.services.scheduler_service import SchedulerService


class TestAgentErrorHandling:
    @pytest.fixture
    def mock_agent(self):
        """Create a mock agent with default values for testing."""
        agent = MagicMock(spec=Agent)
        agent.id = 1
        agent.name = "Test Agent"
        agent.status = "idle"
        agent.system_instructions = "System instructions"
        agent.task_instructions = "Task instructions"
        agent.last_error = None
        return agent

    @pytest.fixture
    def mock_db_session(self, mock_agent):
        """Create a mock DB session for testing."""
        session = MagicMock()

        # Mock CRUD operations
        session.query.return_value.filter.return_value.first.return_value = mock_agent

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

    @patch("zerg.services.scheduler_service.crud")
    @patch("zerg.services.scheduler_service.AgentManager")
    @patch("zerg.services.scheduler_service.event_bus")
    async def test_scheduler_successful_run(
        self, mock_event_bus, mock_agent_manager, mock_crud, scheduler_service, mock_db_session, mock_agent
    ):
        """Test that successful scheduled run properly updates agent status and clears errors."""
        # Setup
        agent_id = 1
        mock_crud.get_agent.return_value = mock_agent

        # Mock agent manager to not actually run any task
        mock_agent_manager_instance = AsyncMock()
        mock_agent_manager.return_value = mock_agent_manager_instance

        # Mock the thread
        mock_thread = MagicMock()
        mock_thread.id = "thread-123"
        mock_agent_manager_instance.get_or_create_thread.return_value = (mock_thread, True)

        # Run the tested function
        await scheduler_service.run_agent_task(agent_id)

        # Assertions
        assert mock_agent.status == "idle"
        assert mock_agent.last_error is None
        mock_db_session.commit.assert_called()

        # Check that event bus published idle status with no error
        mock_event_bus.publish.assert_called()
        _, kwargs = mock_event_bus.publish.call_args_list[-1]
        assert kwargs["args"][0] == EventType.AGENT_UPDATED
        assert "idle" in str(kwargs["args"][1]["status"])
        assert kwargs["args"][1]["last_error"] is None

    @patch("zerg.services.scheduler_service.crud")
    @patch("zerg.services.scheduler_service.AgentManager")
    @patch("zerg.services.scheduler_service.event_bus")
    async def test_scheduler_error_handling(
        self, mock_event_bus, mock_agent_manager, mock_crud, scheduler_service, mock_db_session, mock_agent
    ):
        """Test that errors during scheduled runs properly update agent status and error message."""
        # Setup
        agent_id = 1
        error_message = "Test error message"
        mock_crud.get_agent.return_value = mock_agent

        # Mock agent manager to raise an exception
        mock_agent_manager_instance = AsyncMock()
        mock_agent_manager.return_value = mock_agent_manager_instance

        # Mock the thread
        mock_thread = MagicMock()
        mock_thread.id = "thread-123"
        mock_agent_manager_instance.get_or_create_thread.return_value = (mock_thread, True)

        # Simulate error during processing
        mock_agent_manager_instance.process_message.side_effect = Exception(error_message)

        # Run the tested function
        await scheduler_service.run_agent_task(agent_id)

        # Assertions for error handling
        mock_crud.update_agent.assert_called_with(mock_db_session, agent_id, status="error", last_error=error_message)
        mock_db_session.commit.assert_called()

        # Check that event bus published error status with error message
        mock_event_bus.publish.assert_called()
        _, kwargs = mock_event_bus.publish.call_args_list[-1]
        assert kwargs["args"][0] == EventType.AGENT_UPDATED
        assert "error" in str(kwargs["args"][1]["status"])
        assert error_message in str(kwargs["args"][1]["last_error"])

    @patch("zerg.routers.agents.crud")
    @patch("zerg.routers.agents.AgentManager")
    @patch("zerg.routers.agents.event_bus")
    async def test_manual_run_error_handling(self, mock_event_bus, mock_agent_manager, mock_crud):
        """Test that errors during manual runs properly update agent status and error message."""
        # Import locally to avoid circular imports
        from zerg.routers.agents import run_agent_task

        # Setup
        agent_id = 1
        error_message = "Test error message in manual run"

        # Mock DB session and agent
        mock_db = MagicMock()
        mock_agent = MagicMock(spec=Agent)
        mock_agent.id = agent_id
        mock_agent.task_instructions = "Task instructions"

        # Configure mocks
        mock_crud.get_agent.return_value = mock_agent

        # Mock agent manager
        mock_agent_manager_instance = MagicMock()
        mock_agent_manager.return_value = mock_agent_manager_instance

        # Mock thread creation
        mock_thread = MagicMock()
        mock_thread.id = "thread-456"
        mock_agent_manager_instance.get_or_create_thread.return_value = (mock_thread, True)

        # Simulate error during processing
        mock_agent_manager_instance.process_message.return_value = iter([])
        mock_agent_manager_instance.process_message.side_effect = Exception(error_message)

        # Run the tested function and expect an exception
        with pytest.raises(Exception):
            await run_agent_task(agent_id=agent_id, db=mock_db)

        # Assertions for error handling
        mock_crud.update_agent.assert_called_with(mock_db, agent_id, status="error", last_error=error_message)
        mock_db.commit.assert_called()

        # Check that event bus published error status with error message
        mock_event_bus.publish.assert_called()
        for call_args in mock_event_bus.publish.call_args_list:
            args, _ = call_args
            event_data = args[1]
            # Look for the final error status update
            if "status" in event_data and event_data["status"] == "error":
                assert event_data["last_error"] == error_message
                break
        else:
            pytest.fail("No error status update was published to the event bus")

    @patch("zerg.routers.agents.crud")
    @patch("zerg.routers.agents.AgentManager")
    @patch("zerg.routers.agents.event_bus")
    async def test_manual_run_success(self, mock_event_bus, mock_agent_manager, mock_crud):
        """Test that successful manual runs properly update agent status and clear errors."""
        # Import locally to avoid circular imports
        from zerg.routers.agents import run_agent_task

        # Setup
        agent_id = 1

        # Mock DB session and agent
        mock_db = MagicMock()
        mock_agent = MagicMock(spec=Agent)
        mock_agent.id = agent_id
        mock_agent.task_instructions = "Task instructions"

        # Configure mocks
        mock_crud.get_agent.return_value = mock_agent

        # Mock agent manager
        mock_agent_manager_instance = MagicMock()
        mock_agent_manager.return_value = mock_agent_manager_instance

        # Mock thread creation
        mock_thread = MagicMock()
        mock_thread.id = "thread-789"
        mock_agent_manager_instance.get_or_create_thread.return_value = (mock_thread, True)

        # Mock successful processing
        mock_agent_manager_instance.process_message.return_value = iter(["Success"])

        # Run the tested function
        result = await run_agent_task(agent_id=agent_id, db=mock_db)

        # Assertions for success handling
        assert result["thread_id"] == mock_thread.id

        # Verify status was set to idle and last_error cleared
        mock_crud.update_agent.assert_called_with(mock_db, agent_id, status="idle", last_run_at=ANY, last_error=None)
        mock_db.commit.assert_called()

        # Check that event bus published idle status with no error
        mock_event_bus.publish.assert_called()
        for call_args in mock_event_bus.publish.call_args_list:
            args, _ = call_args
            event_data = args[1]
            # Look for the final idle status update
            if "status" in event_data and event_data["status"] == "idle":
                assert event_data.get("last_error") is None
                break
        else:
            pytest.fail("No idle status update was published to the event bus")
