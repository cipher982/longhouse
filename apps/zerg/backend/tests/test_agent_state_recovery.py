"""
Tests for the agent state recovery system.

Tests the startup recovery mechanism that prevents stuck agents.
"""

from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from zerg.crud.crud import create_agent
from zerg.models.models import Agent
from zerg.models.models import AgentRun
from zerg.models.models import Thread
from zerg.services.agent_state_recovery import check_postgresql_advisory_lock_support
from zerg.services.agent_state_recovery import initialize_agent_state_system
from zerg.services.agent_state_recovery import perform_startup_agent_recovery


class TestAgentStateRecovery:
    """Test agent state recovery functionality."""

    @pytest.mark.asyncio
    async def test_startup_recovery_no_stuck_agents(self, db_session: Session):
        """Test startup recovery when no agents are stuck."""
        # Create some normal agents
        agent1 = create_agent(
            db_session,
            owner_id=1,
            name="Normal Agent 1",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-4",
        )

        agent2 = create_agent(
            db_session,
            owner_id=1,
            name="Normal Agent 2",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-4",
        )

        # Both should be idle by default
        assert agent1.status == "idle"
        assert agent2.status == "idle"

        # Recovery should find no stuck agents
        with patch("zerg.services.agent_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered = await perform_startup_agent_recovery()

        assert recovered == []

    @pytest.mark.asyncio
    async def test_startup_recovery_with_stuck_agents(self, db_session: Session):
        """Test startup recovery finds and fixes stuck agents."""
        # Create agents
        agent1 = create_agent(
            db_session,
            owner_id=1,
            name="Stuck Agent 1",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-4",
        )

        agent2 = create_agent(
            db_session,
            owner_id=1,
            name="Normal Agent",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-4",
        )

        # Manually set agent1 to running status (simulating stuck state)
        db_session.query(Agent).filter(Agent.id == agent1.id).update({"status": "running"})
        db_session.commit()

        # Verify setup
        stuck_agent = db_session.query(Agent).filter(Agent.id == agent1.id).first()
        assert stuck_agent.status == "running"

        # Run recovery
        with patch("zerg.services.agent_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered = await perform_startup_agent_recovery()

        # Should have recovered the stuck agent
        assert agent1.id in recovered
        assert agent2.id not in recovered

        # Verify agent1 was fixed
        recovered_agent = db_session.query(Agent).filter(Agent.id == agent1.id).first()
        assert recovered_agent.status == "idle"
        assert "Recovered from stuck running state" in recovered_agent.last_error

        # Verify agent2 was untouched
        normal_agent = db_session.query(Agent).filter(Agent.id == agent2.id).first()
        assert normal_agent.status == "idle"
        assert normal_agent.last_error is None

    @pytest.mark.asyncio
    async def test_startup_recovery_with_active_runs(self, db_session: Session):
        """Test that agents with active runs are NOT recovered."""
        # Create agent and thread
        agent = create_agent(
            db_session,
            owner_id=1,
            name="Agent with Active Run",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-4",
        )

        thread = Thread(agent_id=agent.id, title="Test Thread", thread_type="manual")
        db_session.add(thread)
        db_session.flush()

        # Set agent to running status
        db_session.query(Agent).filter(Agent.id == agent.id).update({"status": "running"})

        # Create an active run for this agent
        run = AgentRun(agent_id=agent.id, thread_id=thread.id, status="running", trigger="manual")
        db_session.add(run)
        db_session.commit()

        # Run recovery
        with patch("zerg.services.agent_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered = await perform_startup_agent_recovery()

        # Should NOT recover this agent because it has an active run
        assert agent.id not in recovered

        # Agent should still be running
        agent_after = db_session.query(Agent).filter(Agent.id == agent.id).first()
        assert agent_after.status == "running"

    @pytest.mark.asyncio
    async def test_startup_recovery_uppercase_status(self, db_session: Session):
        """Test recovery handles uppercase RUNNING status."""
        # Create agent
        agent = create_agent(
            db_session,
            owner_id=1,
            name="Uppercase Stuck Agent",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-4",
        )

        # Set to uppercase RUNNING status
        db_session.query(Agent).filter(Agent.id == agent.id).update({"status": "RUNNING"})
        db_session.commit()

        # Run recovery
        with patch("zerg.services.agent_state_recovery.get_session_factory", return_value=lambda: db_session):
            recovered = await perform_startup_agent_recovery()

        # Should recover the uppercase status agent
        assert agent.id in recovered

        # Verify it was fixed
        recovered_agent = db_session.query(Agent).filter(Agent.id == agent.id).first()
        assert recovered_agent.status == "idle"

    def test_postgresql_advisory_lock_support(self, db_session: Session):
        """Test PostgreSQL advisory lock support detection."""
        with patch("zerg.services.agent_state_recovery.get_session_factory", return_value=lambda: db_session):
            supported = check_postgresql_advisory_lock_support()

        # Should return True for PostgreSQL (which our test uses)
        assert isinstance(supported, bool)
        # We can't guarantee the specific result as it depends on the test database

    @pytest.mark.asyncio
    async def test_initialize_agent_state_system(self, db_session: Session):
        """Test full initialization of the agent state system."""
        # Create a stuck agent
        agent = create_agent(
            db_session,
            owner_id=1,
            name="Initialization Test Agent",
            system_instructions="Test",
            task_instructions="Test",
            model="gpt-4",
        )

        # Set to running status
        db_session.query(Agent).filter(Agent.id == agent.id).update({"status": "running"})
        db_session.commit()

        # Initialize the system
        with patch("zerg.services.agent_state_recovery.get_session_factory", return_value=lambda: db_session):
            result = await initialize_agent_state_system()

        # Should have results
        assert "recovered_agents" in result
        assert "advisory_locks_available" in result
        assert agent.id in result["recovered_agents"]
        assert isinstance(result["advisory_locks_available"], bool)

        # Agent should be recovered
        recovered_agent = db_session.query(Agent).filter(Agent.id == agent.id).first()
        assert recovered_agent.status == "idle"
