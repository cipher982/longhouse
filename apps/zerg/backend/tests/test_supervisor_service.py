"""Tests for the SupervisorService - manages supervisor agent and thread lifecycle."""

import pytest

from zerg.services.supervisor_service import (
    SupervisorService,
    SUPERVISOR_THREAD_TYPE,
)


class TestSupervisorService:
    """Test suite for SupervisorService."""

    def test_get_or_create_supervisor_agent_creates_new(self, db_session, test_user):
        """Test that a new supervisor agent is created when none exists."""
        service = SupervisorService(db_session)

        agent = service.get_or_create_supervisor_agent(test_user.id)

        assert agent is not None
        assert agent.name == "Supervisor"
        assert agent.owner_id == test_user.id
        assert agent.config.get("is_supervisor") is True
        assert "spawn_worker" in agent.allowed_tools
        assert "list_workers" in agent.allowed_tools

    def test_get_or_create_supervisor_agent_returns_existing(self, db_session, test_user):
        """Test that existing supervisor agent is returned on subsequent calls."""
        service = SupervisorService(db_session)

        # Create first time
        agent1 = service.get_or_create_supervisor_agent(test_user.id)
        agent1_id = agent1.id

        # Get again - should return same agent
        agent2 = service.get_or_create_supervisor_agent(test_user.id)

        assert agent2.id == agent1_id

    def test_get_or_create_supervisor_thread_creates_new(self, db_session, test_user):
        """Test that a new supervisor thread is created when none exists."""
        service = SupervisorService(db_session)

        agent = service.get_or_create_supervisor_agent(test_user.id)
        thread = service.get_or_create_supervisor_thread(test_user.id, agent)

        assert thread is not None
        assert thread.thread_type == SUPERVISOR_THREAD_TYPE
        assert thread.agent_id == agent.id
        assert thread.title == "Supervisor"

    def test_get_or_create_supervisor_thread_returns_existing(self, db_session, test_user):
        """Test that existing supervisor thread is returned on subsequent calls."""
        service = SupervisorService(db_session)

        agent = service.get_or_create_supervisor_agent(test_user.id)

        # Create first time
        thread1 = service.get_or_create_supervisor_thread(test_user.id, agent)
        thread1_id = thread1.id

        # Get again - should return same thread (one brain per user)
        thread2 = service.get_or_create_supervisor_thread(test_user.id, agent)

        assert thread2.id == thread1_id

    def test_supervisor_per_user_isolation(self, db_session, test_user, other_user):
        """Test that each user gets their own supervisor agent and thread."""
        service = SupervisorService(db_session)

        # Get supervisor for test_user
        agent1 = service.get_or_create_supervisor_agent(test_user.id)
        thread1 = service.get_or_create_supervisor_thread(test_user.id, agent1)

        # Get supervisor for other_user
        agent2 = service.get_or_create_supervisor_agent(other_user.id)
        thread2 = service.get_or_create_supervisor_thread(other_user.id, agent2)

        # Should be different agents and threads
        assert agent1.id != agent2.id
        assert thread1.id != thread2.id

        # Each owned by their respective user
        assert agent1.owner_id == test_user.id
        assert agent2.owner_id == other_user.id

    def test_supervisor_agent_has_correct_tools(self, db_session, test_user):
        """Test that supervisor agent is configured with correct tools."""
        service = SupervisorService(db_session)

        agent = service.get_or_create_supervisor_agent(test_user.id)

        expected_tools = [
            "spawn_worker",
            "list_workers",
            "read_worker_result",
            "read_worker_file",
            "grep_workers",
            "get_worker_metadata",
            "get_current_time",
            "http_request",
            "send_email",
        ]

        for tool in expected_tools:
            assert tool in agent.allowed_tools, f"Missing tool: {tool}"

    def test_get_or_create_supervisor_thread_creates_agent_if_needed(
        self, db_session, test_user
    ):
        """Test that thread creation also creates agent if not provided."""
        service = SupervisorService(db_session)

        # Call without providing agent - should create both
        thread = service.get_or_create_supervisor_thread(test_user.id, agent=None)

        assert thread is not None
        assert thread.thread_type == SUPERVISOR_THREAD_TYPE

        # Verify agent was created
        agent = service.get_or_create_supervisor_agent(test_user.id)
        assert thread.agent_id == agent.id
