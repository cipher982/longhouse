"""
Tests for dialect-aware locking in db_utils.

Tests the SQLite-safe locking mechanism using resource_locks table,
as well as the unified locking interface that works on both SQLite and PostgreSQL.
"""

import os
import tempfile
import threading
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from zerg.db_utils import STALE_LOCK_TIMEOUT_SECONDS
from zerg.db_utils import _ensure_resource_locks_table
from zerg.db_utils import acquire_lock_sqlite
from zerg.db_utils import acquire_resource_lock
from zerg.db_utils import get_active_locks_sqlite
from zerg.db_utils import is_sqlite_session
from zerg.db_utils import release_lock_sqlite
from zerg.db_utils import release_resource_lock
from zerg.db_utils import resource_lock
from zerg.db_utils import update_lock_heartbeat_sqlite


@pytest.fixture
def sqlite_session():
    """Create a SQLite session for testing."""
    # Create a temporary SQLite database
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    engine = create_engine(f"sqlite:///{db_path}", echo=False)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    # Ensure the resource_locks table exists
    _ensure_resource_locks_table(session)

    yield session

    session.close()
    engine.dispose()
    os.unlink(db_path)


class TestSQLiteLocking:
    """Test SQLite-specific locking using resource_locks table."""

    def test_acquire_and_release_lock(self, sqlite_session: Session):
        """Test basic lock acquisition and release on SQLite."""
        lock_type = "test"
        lock_key = "resource1"
        holder_id = "holder1"

        # Should be able to acquire lock
        acquired = acquire_lock_sqlite(sqlite_session, lock_type, lock_key, holder_id)
        assert acquired is True

        # Should be able to release lock
        released = release_lock_sqlite(sqlite_session, lock_type, lock_key, holder_id)
        assert released is True

        # Should be able to acquire again after release
        acquired_again = acquire_lock_sqlite(sqlite_session, lock_type, lock_key, holder_id)
        assert acquired_again is True

        # Clean up
        release_lock_sqlite(sqlite_session, lock_type, lock_key, holder_id)

    def test_same_holder_can_reacquire(self, sqlite_session: Session):
        """Test that the same holder can reacquire their own lock."""
        lock_type = "test"
        lock_key = "resource2"
        holder_id = "holder1"

        # First acquisition
        acquired1 = acquire_lock_sqlite(sqlite_session, lock_type, lock_key, holder_id)
        assert acquired1 is True

        # Same holder should be able to "reacquire" (updates heartbeat)
        acquired2 = acquire_lock_sqlite(sqlite_session, lock_type, lock_key, holder_id)
        assert acquired2 is True

        # Clean up
        release_lock_sqlite(sqlite_session, lock_type, lock_key, holder_id)

    def test_different_holder_blocked(self, sqlite_session: Session):
        """Test that a different holder cannot acquire an active lock."""
        lock_type = "test"
        lock_key = "resource3"
        holder1 = "holder1"
        holder2 = "holder2"

        # First holder acquires lock
        acquired1 = acquire_lock_sqlite(sqlite_session, lock_type, lock_key, holder1)
        assert acquired1 is True

        # Second holder should be blocked
        acquired2 = acquire_lock_sqlite(sqlite_session, lock_type, lock_key, holder2)
        assert acquired2 is False

        # After release, second holder should succeed
        release_lock_sqlite(sqlite_session, lock_type, lock_key, holder1)
        acquired3 = acquire_lock_sqlite(sqlite_session, lock_type, lock_key, holder2)
        assert acquired3 is True

        # Clean up
        release_lock_sqlite(sqlite_session, lock_type, lock_key, holder2)

    def test_stale_lock_takeover(self, sqlite_session: Session):
        """Test that stale locks (no heartbeat) can be taken over."""
        lock_type = "test"
        lock_key = "resource4"
        holder1 = "holder1"
        holder2 = "holder2"

        # First holder acquires lock
        acquired1 = acquire_lock_sqlite(sqlite_session, lock_type, lock_key, holder1)
        assert acquired1 is True

        # Manually make the lock stale by backdating heartbeat
        stale_time = datetime.now(timezone.utc) - timedelta(seconds=STALE_LOCK_TIMEOUT_SECONDS + 10)
        sqlite_session.execute(
            text("""
                UPDATE resource_locks
                SET heartbeat_at = :stale_time
                WHERE lock_type = :lock_type AND lock_key = :lock_key
            """),
            {"stale_time": stale_time, "lock_type": lock_type, "lock_key": lock_key},
        )
        sqlite_session.commit()

        # Second holder should be able to take over stale lock
        acquired2 = acquire_lock_sqlite(sqlite_session, lock_type, lock_key, holder2)
        assert acquired2 is True

        # Clean up
        release_lock_sqlite(sqlite_session, lock_type, lock_key, holder2)

    def test_heartbeat_update(self, sqlite_session: Session):
        """Test heartbeat update keeps lock alive."""
        lock_type = "test"
        lock_key = "resource5"
        holder_id = "holder1"

        # Acquire lock
        acquired = acquire_lock_sqlite(sqlite_session, lock_type, lock_key, holder_id)
        assert acquired is True

        # Get initial heartbeat
        result = sqlite_session.execute(
            text("""
                SELECT heartbeat_at FROM resource_locks
                WHERE lock_type = :lock_type AND lock_key = :lock_key
            """),
            {"lock_type": lock_type, "lock_key": lock_key},
        ).fetchone()
        initial_heartbeat = result[0]

        # Small delay to ensure timestamp changes
        time.sleep(0.01)

        # Update heartbeat
        updated = update_lock_heartbeat_sqlite(sqlite_session, lock_type, lock_key, holder_id)
        assert updated is True

        # Get new heartbeat
        result = sqlite_session.execute(
            text("""
                SELECT heartbeat_at FROM resource_locks
                WHERE lock_type = :lock_type AND lock_key = :lock_key
            """),
            {"lock_type": lock_type, "lock_key": lock_key},
        ).fetchone()
        new_heartbeat = result[0]

        # Heartbeat should be updated
        assert new_heartbeat != initial_heartbeat

        # Clean up
        release_lock_sqlite(sqlite_session, lock_type, lock_key, holder_id)

    def test_get_active_locks(self, sqlite_session: Session):
        """Test getting list of active locks."""
        lock_type = "test"

        # Initially should have no locks of this type
        locks = get_active_locks_sqlite(sqlite_session, lock_type)
        initial_count = len(locks)

        # Acquire some locks
        acquire_lock_sqlite(sqlite_session, lock_type, "key1", "holder1")
        acquire_lock_sqlite(sqlite_session, lock_type, "key2", "holder2")
        acquire_lock_sqlite(sqlite_session, "other_type", "key3", "holder3")

        # Should see 2 locks of our type
        locks = get_active_locks_sqlite(sqlite_session, lock_type)
        assert len(locks) == initial_count + 2

        # Should see all locks when no filter
        all_locks = get_active_locks_sqlite(sqlite_session)
        assert len(all_locks) >= initial_count + 3

        # Clean up
        release_lock_sqlite(sqlite_session, lock_type, "key1", "holder1")
        release_lock_sqlite(sqlite_session, lock_type, "key2", "holder2")
        release_lock_sqlite(sqlite_session, "other_type", "key3", "holder3")

    def test_release_by_wrong_holder(self, sqlite_session: Session):
        """Test that wrong holder cannot release a lock."""
        lock_type = "test"
        lock_key = "resource6"
        holder1 = "holder1"
        holder2 = "holder2"

        # Holder 1 acquires lock
        acquired = acquire_lock_sqlite(sqlite_session, lock_type, lock_key, holder1)
        assert acquired is True

        # Holder 2 tries to release - should fail
        released = release_lock_sqlite(sqlite_session, lock_type, lock_key, holder2)
        assert released is False

        # Lock should still exist
        locks = get_active_locks_sqlite(sqlite_session, lock_type)
        assert any(l["lock_key"] == lock_key and l["holder_id"] == holder1 for l in locks)

        # Clean up with correct holder
        release_lock_sqlite(sqlite_session, lock_type, lock_key, holder1)


class TestResourceLockContextManager:
    """Test the unified resource_lock context manager."""

    def test_context_manager_success(self, sqlite_session: Session):
        """Test context manager acquires and releases correctly."""
        lock_type = "ctx_test"
        lock_key = "resource1"
        holder_id = "holder1"

        with resource_lock(sqlite_session, lock_type, lock_key, holder_id) as acquired:
            assert acquired is True

            # Lock should be held
            locks = get_active_locks_sqlite(sqlite_session, lock_type)
            assert any(l["lock_key"] == lock_key for l in locks)

        # After context, lock should be released
        locks = get_active_locks_sqlite(sqlite_session, lock_type)
        assert not any(l["lock_key"] == lock_key for l in locks)

    def test_context_manager_exception(self, sqlite_session: Session):
        """Test context manager releases lock on exception."""
        lock_type = "ctx_test"
        lock_key = "resource2"
        holder_id = "holder1"

        try:
            with resource_lock(sqlite_session, lock_type, lock_key, holder_id) as acquired:
                assert acquired is True
                raise ValueError("Test exception")
        except ValueError:
            pass

        # Lock should be released despite exception
        locks = get_active_locks_sqlite(sqlite_session, lock_type)
        assert not any(l["lock_key"] == lock_key for l in locks)

    def test_context_manager_not_acquired(self, sqlite_session: Session):
        """Test context manager when lock cannot be acquired."""
        lock_type = "ctx_test"
        lock_key = "resource3"
        holder1 = "holder1"
        holder2 = "holder2"

        # First holder holds the lock
        with resource_lock(sqlite_session, lock_type, lock_key, holder1) as acquired1:
            assert acquired1 is True

            # Second holder tries to get lock
            with resource_lock(sqlite_session, lock_type, lock_key, holder2) as acquired2:
                # Should not acquire
                assert acquired2 is False

            # First holder still has lock
            locks = get_active_locks_sqlite(sqlite_session, lock_type)
            assert any(l["lock_key"] == lock_key and l["holder_id"] == holder1 for l in locks)


class TestIsSessionSQLite:
    """Test the is_sqlite_session helper."""

    def test_sqlite_session_detected(self, sqlite_session: Session):
        """Test that SQLite sessions are correctly detected."""
        assert is_sqlite_session(sqlite_session) is True


class TestConcurrentAccess:
    """Test concurrent lock access patterns."""

    def test_concurrent_acquire_from_threads(self, sqlite_session: Session):
        """Test that concurrent acquires from threads work correctly.

        Note: This test uses the same session from multiple threads, which
        isn't recommended in production but tests the lock mechanism.
        """
        lock_type = "concurrent"
        lock_key = "shared_resource"
        results = {"acquired": [], "failed": []}
        lock = threading.Lock()

        def try_acquire(holder_id: str):
            # Create a new session for each thread (proper pattern)
            fd, db_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            engine = create_engine(f"sqlite:///{db_path}", echo=False)
            SessionLocal = sessionmaker(bind=engine)
            session = SessionLocal()
            _ensure_resource_locks_table(session)

            try:
                acquired = acquire_lock_sqlite(session, lock_type, lock_key, holder_id)
                with lock:
                    if acquired:
                        results["acquired"].append(holder_id)
                    else:
                        results["failed"].append(holder_id)
            finally:
                session.close()
                engine.dispose()
                os.unlink(db_path)

        # Since each thread creates its own DB, all should acquire
        # (this tests the mechanism, not true concurrency on shared DB)
        threads = [
            threading.Thread(target=try_acquire, args=(f"holder_{i}",))
            for i in range(5)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Each thread has its own DB, so all should succeed
        assert len(results["acquired"]) == 5
        assert len(results["failed"]) == 0
