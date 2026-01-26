"""
DEPRECATED: This module is no longer used for E2E tests.

E2E tests now use Postgres schema isolation (see e2e_schema_manager.py).
This file is kept for backwards compatibility with any legacy code,
but should be removed in a future cleanup.

Legacy test database manager with automatic cleanup and isolation.
Best practices for 2025: Database-per-test with ephemeral environments.
"""

import atexit
import logging
import os
import tempfile
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)

# Emit deprecation warning when module is imported
warnings.warn(
    "test_db_manager is deprecated. E2E tests now use Postgres schema isolation "
    "(see e2e_schema_manager.py). This module will be removed in a future release.",
    DeprecationWarning,
    stacklevel=2,
)


class TestDatabaseManager:
    """
    Manages isolated test databases with automatic cleanup.

    Key features:
    - Database per test commis/session
    - Automatic cleanup on process exit
    - Temporary directory isolation
    - Optional in-memory databases for speed

    IMPORTANT: Database paths are DETERMINISTIC based only on commis_id.
    This allows multiple Uvicorn processes to share the same DB file for
    a given Playwright commis, enabling parallel E2E tests with parallel
    backend commis.
    """

    # Deterministic temp directory - shared across all Uvicorn processes
    # Uses /tmp/zerg-e2e-<pid> where pid is the process group leader (parent)
    # so child Uvicorn commis inherit the same path.
    _SHARED_TEMP_DIR: Path | None = None

    def __init__(self):
        self.active_databases = set()
        # Register cleanup on process exit
        atexit.register(self.cleanup_all)

    @classmethod
    def _get_shared_temp_dir(cls) -> Path:
        """Get or create the shared temp directory for this process group."""
        if cls._SHARED_TEMP_DIR is None:
            # Use process group ID for stability across Uvicorn commis
            # Or fall back to a simple /tmp/zerg-e2e directory
            pgid = os.getenv("ZERG_E2E_TEMP_DIR")
            if pgid:
                cls._SHARED_TEMP_DIR = Path(pgid)
            else:
                # Default: /tmp/zerg-e2e (simple, predictable)
                cls._SHARED_TEMP_DIR = Path(tempfile.gettempdir()) / "zerg-e2e"
            cls._SHARED_TEMP_DIR.mkdir(parents=True, exist_ok=True)
            logger.info(f"Using E2E test database directory: {cls._SHARED_TEMP_DIR}")
        return cls._SHARED_TEMP_DIR

    def get_test_database_url(self, commis_id: str = "0", use_memory: bool = False) -> str:
        """
        Get a DETERMINISTIC database URL for this Playwright commis.

        The path is based ONLY on commis_id, so multiple Uvicorn processes
        can share the same DB file. This enables parallel E2E tests.

        Args:
            commis_id: Test commis identifier (from Playwright X-Test-Commis header)
            use_memory: If True, use in-memory SQLite (can't share across processes)

        Returns:
            Database URL string
        """
        if use_memory:
            # In-memory database - fastest option but can't be shared between processes
            db_url = "sqlite:///:memory:"
            logger.info(f"Using in-memory database for commis {commis_id}")
            return db_url

        # File-based database with DETERMINISTIC path (no random UUID!)
        temp_dir = self._get_shared_temp_dir()
        db_name = f"commis_{commis_id}.db"
        db_path = temp_dir / db_name

        db_url = f"sqlite:///{db_path}"
        self.active_databases.add(str(db_path))

        logger.debug(f"Using test database: {db_path}")
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

        # Remove temporary directory (class-level)
        if self._SHARED_TEMP_DIR and self._SHARED_TEMP_DIR.exists():
            try:
                import shutil

                shutil.rmtree(self._SHARED_TEMP_DIR)
                logger.info(f"Removed test database directory: {self._SHARED_TEMP_DIR}")
                TestDatabaseManager._SHARED_TEMP_DIR = None
            except Exception as e:
                logger.warning(f"Failed to remove temp directory {self._SHARED_TEMP_DIR}: {e}")

        logger.info("Test database cleanup completed")

    @contextmanager
    def test_database_session(self, commis_id: str = "0", use_memory: bool = False) -> Generator[str, None, None]:
        """
        Context manager for test database lifecycle.

        Usage:
            with test_db_manager.test_database_session("commis_1") as db_url:
                # Use db_url for your test
                pass
            # Database is automatically cleaned up
        """
        db_url = self.get_test_database_url(commis_id, use_memory)

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
    - COMMIS_ID: Test commis identifier (from Playwright)
    - USE_MEMORY_DB: Use in-memory database for speed
    """
    commis_id = os.getenv("COMMIS_ID", "0")
    use_memory = os.getenv("USE_MEMORY_DB", "false").lower() == "true"

    return test_db_manager.get_test_database_url(commis_id, use_memory)


def cleanup_test_databases():
    """Manual cleanup trigger for test frameworks."""
    test_db_manager.cleanup_all()
