"""
Tests for the PostgreSQL advisory lock-based fiche locking system.

Tests the proper distributed locking mechanism that eliminates stuck fiches.
"""

from sqlalchemy.orm import Session

from zerg.services.fiche_locks import FicheLockManager


class TestFicheLockManager:
    """Test PostgreSQL advisory lock-based fiche locking."""

    def test_acquire_and_release_lock(self, db_session: Session):
        """Test basic lock acquisition and release."""
        fiche_id = 12345

        # Should be able to acquire lock
        acquired = FicheLockManager.acquire_fiche_lock(db_session, fiche_id)
        assert acquired is True

        # Should be able to release lock
        released = FicheLockManager.release_fiche_lock(db_session, fiche_id)
        assert released is True

        # Should be able to acquire again after release
        acquired_again = FicheLockManager.acquire_fiche_lock(db_session, fiche_id)
        assert acquired_again is True

        # Clean up
        FicheLockManager.release_fiche_lock(db_session, fiche_id)

    def test_concurrent_lock_prevention(self, db_session: Session):
        """Test that the same fiche can't be locked twice in same session."""
        fiche_id = 12346

        # First acquisition should succeed
        acquired1 = FicheLockManager.acquire_fiche_lock(db_session, fiche_id)
        assert acquired1 is True

        # Second acquisition should fail (already held)
        acquired2 = FicheLockManager.acquire_fiche_lock(db_session, fiche_id)
        assert acquired2 is False

        # Release and try again
        released = FicheLockManager.release_fiche_lock(db_session, fiche_id)
        assert released is True

        # Should be able to acquire after release
        acquired3 = FicheLockManager.acquire_fiche_lock(db_session, fiche_id)
        assert acquired3 is True

        # Clean up
        FicheLockManager.release_fiche_lock(db_session, fiche_id)

    def test_context_manager(self, db_session: Session):
        """Test context manager interface."""
        fiche_id = 12347

        # Test successful acquisition
        with FicheLockManager.fiche_lock(db_session, fiche_id) as acquired:
            assert acquired is True

            # Should not be able to acquire again within the context
            acquired_again = FicheLockManager.acquire_fiche_lock(db_session, fiche_id)
            assert acquired_again is False

        # After context exit, should be able to acquire again
        acquired_after = FicheLockManager.acquire_fiche_lock(db_session, fiche_id)
        assert acquired_after is True

        # Clean up
        FicheLockManager.release_fiche_lock(db_session, fiche_id)

    def test_context_manager_exception_handling(self, db_session: Session):
        """Test context manager releases lock even when exception occurs."""
        fiche_id = 12348

        # Test exception within context
        try:
            with FicheLockManager.fiche_lock(db_session, fiche_id) as acquired:
                assert acquired is True
                raise ValueError("Test exception")
        except ValueError:
            pass

        # Lock should be released even after exception
        acquired_after = FicheLockManager.acquire_fiche_lock(db_session, fiche_id)
        assert acquired_after is True

        # Clean up
        FicheLockManager.release_fiche_lock(db_session, fiche_id)

    def test_get_locked_fiches(self, db_session: Session):
        """Test getting list of currently locked fiches."""
        fiche_id_1 = 12349
        fiche_id_2 = 12350

        # Initially no locks
        locked = FicheLockManager.get_locked_fiches(db_session)
        initial_count = len([fiche_id for fiche_id in locked if fiche_id in [fiche_id_1, fiche_id_2]])

        # Acquire locks for two fiches
        FicheLockManager.acquire_fiche_lock(db_session, fiche_id_1)
        FicheLockManager.acquire_fiche_lock(db_session, fiche_id_2)

        # Both should appear in locked list
        locked = FicheLockManager.get_locked_fiches(db_session)
        test_fiches_locked = [fiche_id for fiche_id in locked if fiche_id in [fiche_id_1, fiche_id_2]]
        assert len(test_fiches_locked) == initial_count + 2

        # Release one lock
        FicheLockManager.release_fiche_lock(db_session, fiche_id_1)

        # Only one should remain locked
        locked = FicheLockManager.get_locked_fiches(db_session)
        test_fiches_locked = [fiche_id for fiche_id in locked if fiche_id in [fiche_id_1, fiche_id_2]]
        assert len(test_fiches_locked) == initial_count + 1
        assert fiche_id_2 in locked
        assert fiche_id_1 not in [fiche_id for fiche_id in locked if fiche_id in [fiche_id_1, fiche_id_2]]

        # Clean up
        FicheLockManager.release_fiche_lock(db_session, fiche_id_2)

    def test_multiple_different_fiches(self, db_session: Session):
        """Test locking multiple different fiches simultaneously."""
        fiche_ids = [12352, 12353, 12354]

        # Should be able to lock all different fiches
        for fiche_id in fiche_ids:
            acquired = FicheLockManager.acquire_fiche_lock(db_session, fiche_id)
            assert acquired is True

        # All should appear in locked list
        locked = FicheLockManager.get_locked_fiches(db_session)
        for fiche_id in fiche_ids:
            assert fiche_id in locked

        # Clean up
        for fiche_id in fiche_ids:
            released = FicheLockManager.release_fiche_lock(db_session, fiche_id)
            assert released is True

    def test_release_unheld_lock(self, db_session: Session):
        """Test releasing a lock that wasn't held by this session."""
        fiche_id = 12355

        # Try to release a lock we never acquired
        released = FicheLockManager.release_fiche_lock(db_session, fiche_id)
        # PostgreSQL advisory unlock returns false if the lock wasn't held
        assert released is False
