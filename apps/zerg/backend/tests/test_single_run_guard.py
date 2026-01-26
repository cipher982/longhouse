"""Test single-run guard functionality to prevent concurrent courses."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.services.fiche_locks import FicheLockManager


class TestSingleRunGuard:
    """Tests for the single-run guard that prevents concurrent fiche execution."""

    def test_acquire_lock_success(self, db: Session):
        """Test that advisory lock successfully locks an idle fiche."""
        # Create test fiche
        fiche = crud.create_fiche(
            db=db,
            owner_id=1,
            name="Test Fiche",
            system_instructions="Test system",
            task_instructions="Test task",
            model="gpt-mock",
        )

        # Should successfully acquire advisory lock for idle fiche
        assert FicheLockManager.acquire_fiche_lock(db, fiche.id) is True
        # Re-acquiring within same session should fail
        assert FicheLockManager.acquire_fiche_lock(db, fiche.id) is False
        # Release
        assert FicheLockManager.release_fiche_lock(db, fiche.id) is True

    def test_acquire_lock_already_held(self, db: Session):
        """Test that acquiring a held advisory lock fails in same session."""
        # Create test fiche and set to running
        fiche = crud.create_fiche(
            db=db,
            owner_id=1,
            name="Test Fiche",
            system_instructions="Test system",
            task_instructions="Test task",
            model="gpt-mock",
        )
        assert FicheLockManager.acquire_fiche_lock(db, fiche.id) is True
        assert FicheLockManager.acquire_fiche_lock(db, fiche.id) is False
        FicheLockManager.release_fiche_lock(db, fiche.id)

    def test_api_returns_409_for_concurrent_runs(self, client: TestClient, db: Session):
        """Test that the API returns 409 Conflict for concurrent run attempts."""
        # Create test fiche
        fiche = crud.create_fiche(
            db=db,
            owner_id=1,
            name="Test Fiche",
            system_instructions="Test system",
            task_instructions="Test task",
            model="gpt-mock",
        )

        # Simulate a concurrent run by acquiring the advisory lock in this session.
        # The API path attempts to acquire the same lock and should return 409.
        # Hold the lock via a dedicated connection to ensure cross-session contention
        from sqlalchemy import text

        lock_conn = db.bind.connect()
        try:
            locked = lock_conn.execute(text("SELECT pg_try_advisory_lock(:aid)"), {"aid": fiche.id}).scalar()
            assert bool(locked) is True

            # API call should return 409 Conflict
            response = client.post(f"/api/fiches/{fiche.id}/task")
            assert response.status_code == 409
        finally:
            # Release and close the dedicated connection
            try:
                lock_conn.execute(text("SELECT pg_advisory_unlock(:aid)"), {"aid": fiche.id}).scalar()
            finally:
                lock_conn.close()
        assert "already running" in response.json()["detail"].lower()

    def test_lock_is_released_on_success(self, db: Session):
        """Test that the advisory lock is properly released when run completes."""
        # Create test fiche
        fiche = crud.create_fiche(
            db=db,
            owner_id=1,
            name="Test Fiche",
            system_instructions="Test system",
            task_instructions="Test task",
            model="gpt-mock",
        )

        # Acquire lock
        assert FicheLockManager.acquire_fiche_lock(db, fiche.id) is True
        # Release
        assert FicheLockManager.release_fiche_lock(db, fiche.id) is True
        # Should be able to acquire lock again
        assert FicheLockManager.acquire_fiche_lock(db, fiche.id) is True
        FicheLockManager.release_fiche_lock(db, fiche.id)
