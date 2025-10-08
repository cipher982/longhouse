"""
Tests for the PostgreSQL advisory lock-based agent locking system.

Tests the proper distributed locking mechanism that eliminates stuck agents.
"""

from sqlalchemy.orm import Session

from zerg.services.agent_locks import AgentLockManager
from zerg.services.agent_locks import acquire_run_lock_advisory


class TestAgentLockManager:
    """Test PostgreSQL advisory lock-based agent locking."""

    def test_acquire_and_release_lock(self, db_session: Session):
        """Test basic lock acquisition and release."""
        agent_id = 12345

        # Should be able to acquire lock
        acquired = AgentLockManager.acquire_agent_lock(db_session, agent_id)
        assert acquired is True

        # Should be able to release lock
        released = AgentLockManager.release_agent_lock(db_session, agent_id)
        assert released is True

        # Should be able to acquire again after release
        acquired_again = AgentLockManager.acquire_agent_lock(db_session, agent_id)
        assert acquired_again is True

        # Clean up
        AgentLockManager.release_agent_lock(db_session, agent_id)

    def test_concurrent_lock_prevention(self, db_session: Session):
        """Test that the same agent can't be locked twice in same session."""
        agent_id = 12346

        # First acquisition should succeed
        acquired1 = AgentLockManager.acquire_agent_lock(db_session, agent_id)
        assert acquired1 is True

        # Second acquisition should fail (already held)
        acquired2 = AgentLockManager.acquire_agent_lock(db_session, agent_id)
        assert acquired2 is False

        # Release and try again
        released = AgentLockManager.release_agent_lock(db_session, agent_id)
        assert released is True

        # Should be able to acquire after release
        acquired3 = AgentLockManager.acquire_agent_lock(db_session, agent_id)
        assert acquired3 is True

        # Clean up
        AgentLockManager.release_agent_lock(db_session, agent_id)

    def test_context_manager(self, db_session: Session):
        """Test context manager interface."""
        agent_id = 12347

        # Test successful acquisition
        with AgentLockManager.agent_lock(db_session, agent_id) as acquired:
            assert acquired is True

            # Should not be able to acquire again within the context
            acquired_again = AgentLockManager.acquire_agent_lock(db_session, agent_id)
            assert acquired_again is False

        # After context exit, should be able to acquire again
        acquired_after = AgentLockManager.acquire_agent_lock(db_session, agent_id)
        assert acquired_after is True

        # Clean up
        AgentLockManager.release_agent_lock(db_session, agent_id)

    def test_context_manager_exception_handling(self, db_session: Session):
        """Test context manager releases lock even when exception occurs."""
        agent_id = 12348

        # Test exception within context
        try:
            with AgentLockManager.agent_lock(db_session, agent_id) as acquired:
                assert acquired is True
                raise ValueError("Test exception")
        except ValueError:
            pass

        # Lock should be released even after exception
        acquired_after = AgentLockManager.acquire_agent_lock(db_session, agent_id)
        assert acquired_after is True

        # Clean up
        AgentLockManager.release_agent_lock(db_session, agent_id)

    def test_get_locked_agents(self, db_session: Session):
        """Test getting list of currently locked agents."""
        agent_id_1 = 12349
        agent_id_2 = 12350

        # Initially no locks
        locked = AgentLockManager.get_locked_agents(db_session)
        initial_count = len([a for a in locked if a in [agent_id_1, agent_id_2]])

        # Acquire locks for two agents
        AgentLockManager.acquire_agent_lock(db_session, agent_id_1)
        AgentLockManager.acquire_agent_lock(db_session, agent_id_2)

        # Both should appear in locked list
        locked = AgentLockManager.get_locked_agents(db_session)
        test_agents_locked = [a for a in locked if a in [agent_id_1, agent_id_2]]
        assert len(test_agents_locked) == initial_count + 2

        # Release one lock
        AgentLockManager.release_agent_lock(db_session, agent_id_1)

        # Only one should remain locked
        locked = AgentLockManager.get_locked_agents(db_session)
        test_agents_locked = [a for a in locked if a in [agent_id_1, agent_id_2]]
        assert len(test_agents_locked) == initial_count + 1
        assert agent_id_2 in locked
        assert agent_id_1 not in [a for a in locked if a in [agent_id_1, agent_id_2]]

        # Clean up
        AgentLockManager.release_agent_lock(db_session, agent_id_2)

    def test_backward_compatibility_functions(self, db_session: Session):
        """Test backward compatibility wrapper functions."""
        agent_id = 12351

        # Test acquire function
        acquired = acquire_run_lock_advisory(db_session, agent_id)
        assert acquired is True

        # Should not be able to acquire again
        acquired_again = acquire_run_lock_advisory(db_session, agent_id)
        assert acquired_again is False

        # Clean up using the new release function
        from zerg.services.agent_locks import release_run_lock_advisory

        released = release_run_lock_advisory(db_session, agent_id)
        assert released is True

    def test_multiple_different_agents(self, db_session: Session):
        """Test locking multiple different agents simultaneously."""
        agent_ids = [12352, 12353, 12354]

        # Should be able to lock all different agents
        for agent_id in agent_ids:
            acquired = AgentLockManager.acquire_agent_lock(db_session, agent_id)
            assert acquired is True

        # All should appear in locked list
        locked = AgentLockManager.get_locked_agents(db_session)
        for agent_id in agent_ids:
            assert agent_id in locked

        # Clean up
        for agent_id in agent_ids:
            released = AgentLockManager.release_agent_lock(db_session, agent_id)
            assert released is True

    def test_release_unheld_lock(self, db_session: Session):
        """Test releasing a lock that wasn't held by this session."""
        agent_id = 12355

        # Try to release a lock we never acquired
        released = AgentLockManager.release_agent_lock(db_session, agent_id)
        # PostgreSQL advisory unlock returns false if the lock wasn't held
        assert released is False
