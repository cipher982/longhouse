"""
Tests for the dialect-aware fiche locking system.

Tests the proper distributed locking mechanism that eliminates stuck fiches.
Works on both PostgreSQL (advisory locks) and SQLite (resource_locks table).
"""

from sqlalchemy.orm import Session

from zerg.services.fiche_locks import FicheLockManager


class TestFicheLockManager:
    """Test dialect-aware fiche locking."""

    def test_acquire_and_release_lock(self, db_session: Session):
        """Test basic lock acquisition and release."""
        fiche_id = 12345

        # Should be able to acquire lock
        acquired, holder_id = FicheLockManager.acquire_fiche_lock(db_session, fiche_id)
        assert acquired is True
        assert holder_id is not None

        # Should be able to release lock
        released = FicheLockManager.release_fiche_lock(db_session, fiche_id, holder_id)
        assert released is True

        # Should be able to acquire again after release
        acquired_again, holder_id2 = FicheLockManager.acquire_fiche_lock(db_session, fiche_id)
        assert acquired_again is True

        # Clean up
        FicheLockManager.release_fiche_lock(db_session, fiche_id, holder_id2)

    def test_concurrent_lock_prevention(self, db_session: Session):
        """Test that the same fiche can't be locked twice with different holders."""
        fiche_id = 12346

        # First acquisition should succeed
        acquired1, holder_id1 = FicheLockManager.acquire_fiche_lock(db_session, fiche_id)
        assert acquired1 is True

        # Second acquisition with different holder should fail (already held)
        acquired2, holder_id2 = FicheLockManager.acquire_fiche_lock(db_session, fiche_id)
        assert acquired2 is False

        # Release and try again
        released = FicheLockManager.release_fiche_lock(db_session, fiche_id, holder_id1)
        assert released is True

        # Should be able to acquire after release
        acquired3, holder_id3 = FicheLockManager.acquire_fiche_lock(db_session, fiche_id)
        assert acquired3 is True

        # Clean up
        FicheLockManager.release_fiche_lock(db_session, fiche_id, holder_id3)

    def test_context_manager(self, db_session: Session):
        """Test context manager interface."""
        fiche_id = 12347

        # Test successful acquisition
        with FicheLockManager.fiche_lock(db_session, fiche_id) as acquired:
            assert acquired is True

            # Should not be able to acquire again within the context (different holder)
            acquired_again, _ = FicheLockManager.acquire_fiche_lock(db_session, fiche_id)
            assert acquired_again is False

        # After context exit, should be able to acquire again
        acquired_after, holder_id = FicheLockManager.acquire_fiche_lock(db_session, fiche_id)
        assert acquired_after is True

        # Clean up
        FicheLockManager.release_fiche_lock(db_session, fiche_id, holder_id)

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
        acquired_after, holder_id = FicheLockManager.acquire_fiche_lock(db_session, fiche_id)
        assert acquired_after is True

        # Clean up
        FicheLockManager.release_fiche_lock(db_session, fiche_id, holder_id)

    def test_get_locked_fiches(self, db_session: Session):
        """Test getting list of currently locked fiches."""
        fiche_id_1 = 12349
        fiche_id_2 = 12350

        # Initially no locks
        locked = FicheLockManager.get_locked_fiches(db_session)
        initial_count = len([fiche_id for fiche_id in locked if fiche_id in [fiche_id_1, fiche_id_2]])

        # Acquire locks for two fiches
        _, holder_id1 = FicheLockManager.acquire_fiche_lock(db_session, fiche_id_1)
        _, holder_id2 = FicheLockManager.acquire_fiche_lock(db_session, fiche_id_2)

        # Both should appear in locked list
        locked = FicheLockManager.get_locked_fiches(db_session)
        test_fiches_locked = [fiche_id for fiche_id in locked if fiche_id in [fiche_id_1, fiche_id_2]]
        assert len(test_fiches_locked) == initial_count + 2

        # Release one lock
        FicheLockManager.release_fiche_lock(db_session, fiche_id_1, holder_id1)

        # Only one should remain locked
        locked = FicheLockManager.get_locked_fiches(db_session)
        test_fiches_locked = [fiche_id for fiche_id in locked if fiche_id in [fiche_id_1, fiche_id_2]]
        assert len(test_fiches_locked) == initial_count + 1
        assert fiche_id_2 in locked
        assert fiche_id_1 not in [fiche_id for fiche_id in locked if fiche_id in [fiche_id_1, fiche_id_2]]

        # Clean up
        FicheLockManager.release_fiche_lock(db_session, fiche_id_2, holder_id2)

    def test_multiple_different_fiches(self, db_session: Session):
        """Test locking multiple different fiches simultaneously."""
        fiche_ids = [12352, 12353, 12354]
        holder_ids = []

        # Should be able to lock all different fiches
        for fiche_id in fiche_ids:
            acquired, holder_id = FicheLockManager.acquire_fiche_lock(db_session, fiche_id)
            assert acquired is True
            holder_ids.append(holder_id)

        # All should appear in locked list
        locked = FicheLockManager.get_locked_fiches(db_session)
        for fiche_id in fiche_ids:
            assert fiche_id in locked

        # Clean up
        for fiche_id, holder_id in zip(fiche_ids, holder_ids):
            released = FicheLockManager.release_fiche_lock(db_session, fiche_id, holder_id)
            assert released is True

    def test_release_unheld_lock(self, db_session: Session):
        """Test releasing a lock that wasn't held by this holder."""
        fiche_id = 12355

        # Try to release a lock we never acquired (with a made-up holder_id)
        released = FicheLockManager.release_fiche_lock(db_session, fiche_id, "nonexistent-holder")
        # Should return false if the lock wasn't held by this holder
        assert released is False

    def test_same_holder_can_reacquire(self, db_session: Session):
        """Test that providing the same holder_id allows reacquisition."""
        fiche_id = 12356
        explicit_holder = "test-holder-12356"

        # First acquisition with explicit holder
        acquired1, holder1 = FicheLockManager.acquire_fiche_lock(db_session, fiche_id, holder_id=explicit_holder)
        assert acquired1 is True
        assert holder1 == explicit_holder

        # Same holder can "reacquire" (this is SQLite behavior - updates heartbeat)
        # For Postgres this will return False due to re-entrancy guard
        acquired2, holder2 = FicheLockManager.acquire_fiche_lock(db_session, fiche_id, holder_id=explicit_holder)
        # Result depends on dialect, but holder should match
        assert holder2 == explicit_holder

        # Clean up
        FicheLockManager.release_fiche_lock(db_session, fiche_id, explicit_holder)
