"""Test single-run guard functionality to prevent concurrent agent runs."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.services.agent_locks import AgentLockManager


class TestSingleRunGuard:
    """Tests for the single-run guard that prevents concurrent agent execution."""

    def test_acquire_lock_success(self, db: Session):
        """Test that advisory lock successfully locks an idle agent."""
        # Create test agent
        agent = crud.create_agent(
            db=db,
            owner_id=1,
            name="Test Agent",
            system_instructions="Test system",
            task_instructions="Test task",
            model="gpt-4",
        )

        # Should successfully acquire advisory lock for idle agent
        assert AgentLockManager.acquire_agent_lock(db, agent.id) is True
        # Re-acquiring within same session should fail
        assert AgentLockManager.acquire_agent_lock(db, agent.id) is False
        # Release
        assert AgentLockManager.release_agent_lock(db, agent.id) is True

    def test_acquire_lock_already_held(self, db: Session):
        """Test that acquiring a held advisory lock fails in same session."""
        # Create test agent and set to running
        agent = crud.create_agent(
            db=db,
            owner_id=1,
            name="Test Agent",
            system_instructions="Test system",
            task_instructions="Test task",
            model="gpt-4",
        )
        assert AgentLockManager.acquire_agent_lock(db, agent.id) is True
        assert AgentLockManager.acquire_agent_lock(db, agent.id) is False
        AgentLockManager.release_agent_lock(db, agent.id)

    def test_api_returns_409_for_concurrent_runs(self, client: TestClient, db: Session):
        """Test that the API returns 409 Conflict for concurrent run attempts."""
        # Create test agent
        agent = crud.create_agent(
            db=db,
            owner_id=1,
            name="Test Agent",
            system_instructions="Test system",
            task_instructions="Test task",
            model="gpt-4",
        )

        # Simulate a concurrent run by acquiring the advisory lock in this session.
        # The API path attempts to acquire the same lock and should return 409.
        # Hold the lock via a dedicated connection to ensure cross-session contention
        from sqlalchemy import text

        lock_conn = db.bind.connect()
        try:
            locked = lock_conn.execute(text("SELECT pg_try_advisory_lock(:aid)"), {"aid": agent.id}).scalar()
            assert bool(locked) is True

            # API call should return 409 Conflict
            response = client.post(f"/api/agents/{agent.id}/task")
            assert response.status_code == 409
        finally:
            # Release and close the dedicated connection
            try:
                lock_conn.execute(text("SELECT pg_advisory_unlock(:aid)"), {"aid": agent.id}).scalar()
            finally:
                lock_conn.close()
        assert "already running" in response.json()["detail"].lower()

    def test_lock_is_released_on_success(self, db: Session):
        """Test that the advisory lock is properly released when run completes."""
        # Create test agent
        agent = crud.create_agent(
            db=db,
            owner_id=1,
            name="Test Agent",
            system_instructions="Test system",
            task_instructions="Test task",
            model="gpt-4",
        )

        # Acquire lock
        assert AgentLockManager.acquire_agent_lock(db, agent.id) is True
        # Release
        assert AgentLockManager.release_agent_lock(db, agent.id) is True
        # Should be able to acquire lock again
        assert AgentLockManager.acquire_agent_lock(db, agent.id) is True
        AgentLockManager.release_agent_lock(db, agent.id)
