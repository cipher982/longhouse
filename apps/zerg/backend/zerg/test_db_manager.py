"""
Modern test database manager with automatic cleanup and isolation.
Best practices for 2025: Database-per-test with ephemeral environments.
"""

import atexit
import logging
import os
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)


class TestDatabaseManager:
    """
    Manages isolated test databases with automatic cleanup.

    Key features:
    - Database per test worker/session
    - Automatic cleanup on process exit
    - Temporary directory isolation
    - Optional in-memory databases for speed
    """

    def __init__(self):
        self.active_databases = set()
        self.temp_dir = None
        # Register cleanup on process exit
        atexit.register(self.cleanup_all)

    def get_test_database_url(self, worker_id: str = "0", use_memory: bool = False) -> str:
        """
        Get a unique database URL for this test session.

        Args:
            worker_id: Test worker identifier (from Playwright)
            use_memory: If True, use in-memory SQLite for maximum speed

        Returns:
            Database URL string
        """
        if use_memory:
            # In-memory database - fastest option but can't be shared between processes
            db_url = "sqlite:///:memory:"
            logger.info(f"Using in-memory database for worker {worker_id}")
            return db_url

        # File-based temporary database
        if not self.temp_dir:
            self.temp_dir = tempfile.mkdtemp(prefix="zerg_test_db_")
            logger.info(f"Created test database directory: {self.temp_dir}")

        # Generate unique database name
        test_session_id = str(uuid.uuid4())[:8]
        db_name = f"test_worker_{worker_id}_{test_session_id}.db"
        db_path = Path(self.temp_dir) / db_name

        db_url = f"sqlite:///{db_path}"
        self.active_databases.add(str(db_path))

        logger.info(f"Created test database: {db_path}")
        return db_url

    def cleanup_database(self, db_path: str) -> None:
        """Clean up a specific database file and its SQLite auxiliaries."""
        try:
            base_path = Path(db_path)

            # Remove all SQLite files for this database
            for suffix in ["", "-shm", "-wal", "-journal"]:
                file_path = Path(str(base_path) + suffix)
                if file_path.exists():
                    file_path.unlink()
                    logger.debug(f"Removed: {file_path}")

            self.active_databases.discard(db_path)
            logger.info(f"Cleaned up database: {db_path}")

        except Exception as e:
            logger.warning(f"Failed to clean up database {db_path}: {e}")

    def cleanup_all(self) -> None:
        """Clean up all test databases and temporary directory."""
        logger.info("Starting test database cleanup...")

        # Clean up individual databases
        for db_path in list(self.active_databases):
            self.cleanup_database(db_path)

        # Remove temporary directory
        if self.temp_dir and Path(self.temp_dir).exists():
            try:
                import shutil

                shutil.rmtree(self.temp_dir)
                logger.info(f"Removed test database directory: {self.temp_dir}")
            except Exception as e:
                logger.warning(f"Failed to remove temp directory {self.temp_dir}: {e}")

        logger.info("Test database cleanup completed")

    @contextmanager
    def test_database_session(self, worker_id: str = "0", use_memory: bool = False) -> Generator[str, None, None]:
        """
        Context manager for test database lifecycle.

        Usage:
            with test_db_manager.test_database_session("worker_1") as db_url:
                # Use db_url for your test
                pass
            # Database is automatically cleaned up
        """
        db_url = self.get_test_database_url(worker_id, use_memory)

        # Extract file path for cleanup (if not in-memory)
        db_path = None
        if not use_memory and db_url.startswith("sqlite:///"):
            db_path = db_url.replace("sqlite:///", "")

        try:
            yield db_url
        finally:
            if db_path:
                self.cleanup_database(db_path)


# Global instance for the application
test_db_manager = TestDatabaseManager()


def get_test_database_url() -> str:
    """
    Get database URL for current test environment.

    Reads configuration from environment variables:
    - WORKER_ID: Test worker identifier (from Playwright)
    - USE_MEMORY_DB: Use in-memory database for speed
    """
    worker_id = os.getenv("WORKER_ID", "0")
    use_memory = os.getenv("USE_MEMORY_DB", "false").lower() == "true"

    return test_db_manager.get_test_database_url(worker_id, use_memory)


def cleanup_test_databases():
    """Manual cleanup trigger for test frameworks."""
    test_db_manager.cleanup_all()
