import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from typing import Dict
from typing import Iterator

import dotenv
from sqlalchemy import Engine
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker

from zerg.config import get_settings

# Thread-safe caches for per-worker engines/sessionmakers --------------------

_WORKER_ENGINES: Dict[str, Engine] = {}
_WORKER_SESSIONMAKERS: Dict[str, sessionmaker] = {}
_WORKER_LOCK = threading.Lock()


def clear_worker_caches():
    """Clear cached worker engines and sessionmakers.

    This is needed for E2E tests to ensure session factories are created
    with the correct configuration after environment variables are set.
    """
    global _WORKER_ENGINES, _WORKER_SESSIONMAKERS
    with _WORKER_LOCK:
        _WORKER_ENGINES.clear()
        _WORKER_SESSIONMAKERS.clear()


# ---------------------------------------------------------------------------
# Playwright worker-based DB isolation (E2E tests)
# ---------------------------------------------------------------------------

# We *dynamically* route each HTTP/WebSocket request to its own SQLite file
# during Playwright runs.  The current worker id is injected by the middleware
# and stored in a context variable.  Importing here avoids a circular
# dependency (middleware imports *this* module).  The conditional import keeps
# the overhead negligible for production usage.

try:
    from zerg.middleware.worker_db import current_worker_id

except ModuleNotFoundError:
    import contextvars

    current_worker_id = contextvars.ContextVar("current_worker_id", default=None)


_settings = get_settings()

dotenv.load_dotenv()


# Create Base class
Base = declarative_base()

# Import all models at module level to ensure they are registered with Base
# This prevents "no such table" errors when worker databases are created
try:
    from zerg.models.models import Agent  # noqa: F401
    from zerg.models.models import AgentMessage  # noqa: F401
    from zerg.models.models import AgentRun  # noqa: F401
    from zerg.models.models import CanvasLayout  # noqa: F401
    from zerg.models.models import Connector  # noqa: F401
    from zerg.models.models import NodeExecutionState  # noqa: F401
    from zerg.models.models import Thread  # noqa: F401
    from zerg.models.models import ThreadMessage  # noqa: F401
    from zerg.models.models import Trigger  # noqa: F401
    from zerg.models.models import User  # noqa: F401
    from zerg.models.models import Workflow  # noqa: F401
    from zerg.models.models import WorkflowExecution  # noqa: F401
    from zerg.models.models import WorkflowTemplate  # noqa: F401
except ImportError:
    # Handle case where models module might not be available during certain imports
    pass


def make_engine(db_url: str, **kwargs) -> Engine:
    """Create a SQLAlchemy engine with the given URL and options.

    Args:
        db_url: Database connection URL
        **kwargs: Additional arguments for create_engine

    Returns:
        A SQLAlchemy Engine instance
    """
    connect_args = kwargs.pop("connect_args", {})
    if "sqlite" in db_url:
        if "check_same_thread" not in connect_args:
            connect_args["check_same_thread"] = False
        # For file-based databases, use WAL mode for better concurrency
        if ":memory:" not in db_url:
            # Use WAL mode for better concurrency with file-based databases
            connect_args["isolation_level"] = None
        # Enable foreign keys and set timeout
        connect_args["timeout"] = 30

    # For test environments, add pooling configurations
    if os.getenv("NODE_ENV") == "test":
        kwargs.setdefault("pool_pre_ping", True)
        kwargs.setdefault("pool_recycle", 300)

    return create_engine(db_url, connect_args=connect_args, **kwargs)


def make_sessionmaker(engine: Engine) -> sessionmaker:
    """Create a sessionmaker bound to the given engine.

    Args:
        engine: SQLAlchemy Engine instance

    Returns:
        A sessionmaker class
    """
    # `expire_on_commit=False` keeps attributes accessible after a commit,
    # preventing DetachedInstanceError in situations where objects outlive the
    # session lifecycle (e.g. during test helpers that commit and then access
    # attributes after other background DB activity).
    # ``expire_on_commit=True`` forces SQLAlchemy to *reload* objects from the
    # database the next time they are accessed after a commit.  This prevents
    # stale identity-map rows from surviving across the test-suite's
    # reset-database calls where we truncate all tables without restarting the
    # backend process.

    # Determine expire_on_commit based on environment
    # For E2E tests, we need expire_on_commit=False to prevent DetachedInstanceError
    # when objects are returned from API endpoints
    environment = os.getenv("ENVIRONMENT", "")

    # Check multiple indicators for E2E testing context
    is_e2e = (
        environment.startswith("test:e2e")
        or os.getenv("TEST_TYPE") == "e2e"
        or
        # The test_main.py module is only used for E2E tests
        "test_main" in str(engine.url)
    )

    # Use expire_on_commit=False for E2E tests to keep objects accessible
    # after session closes, but True for unit tests to prevent stale data
    if is_e2e:
        expire_on_commit = False
    elif environment == "test" or environment.startswith("test:"):
        # Other test types need expire_on_commit=True for proper isolation
        expire_on_commit = True
    else:
        # Production/development default to False for better performance
        expire_on_commit = False

    return sessionmaker(
        autocommit=False,
        autoflush=False,
        expire_on_commit=expire_on_commit,
        bind=engine,
    )


def get_session_factory() -> sessionmaker:
    """Get the default session factory for the application.

    Uses DATABASE_URL from environment or falls back to default SQLite path.

    Returns:
        A sessionmaker instance
    """
    # ---------------------------------------------------------------------
    # Tests often import the *zerg.database* module **before** they get the
    # chance to patch environment variables or monkey-patch the session
    # factory.  Raising at import time therefore breaks the entire test
    # discovery phase.  Instead of hard-failing when the variable is missing
    # we fall back to a local on-disk SQLite file.  When the test-runner sets
    # ``TESTING=1`` (done in *backend/tests/conftest.py*) we create an
    # *in-memory* SQLite engine because the file system location is
    # irrelevant and the tests patch the session/engine anyway.
    # ---------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Playwright E2E tests: isolate database per worker ------------------
    # ------------------------------------------------------------------
    # When the *WorkerDBMiddleware* sets `current_worker_id` we look up a
    # dedicated engine / sessionmaker pair from an in-memory cache.  The very
    # first request for a worker lazily creates `sqlite:///./test_worker_<id>.db`
    # and initialises the schema.
    #
    # Outside the Playwright context – i.e. when *current_worker_id* is None –
    # we fall back to the original single-engine behaviour so that unit
    # tests, dev server sessions, and production deployments remain
    # unaffected.
    # ------------------------------------------------------------------

    worker_id = current_worker_id.get()

    if worker_id is None:
        # --- Legacy/shared behaviour -----------------------------------
        db_url = _settings.database_url

        if not db_url:
            if _settings.testing:
                db_url = "sqlite:///:memory:"
            else:
                db_url = "sqlite:///./app.db"

        engine = make_engine(db_url)
        return make_sessionmaker(engine)

    # --- Per-worker engine/session --------------------------------------
    if worker_id in _WORKER_SESSIONMAKERS:
        return _WORKER_SESSIONMAKERS[worker_id]

    # Lazily build the engine (thread-safe)
    with _WORKER_LOCK:
        if worker_id in _WORKER_SESSIONMAKERS:
            return _WORKER_SESSIONMAKERS[worker_id]

        # Use modern test database manager for proper isolation and cleanup
        if _settings.testing or os.getenv("NODE_ENV") == "test":
            # Use the test database manager for automatic cleanup and isolation
            from zerg.test_db_manager import test_db_manager

            # Get isolated database with automatic cleanup
            db_url = test_db_manager.get_test_database_url(
                worker_id=str(worker_id),
                use_memory=False,  # Use file-based for connection sharing
            )
        else:
            # Fallback to file-based databases for non-test environments
            db_path = Path(__file__).resolve().parents[1] / f"test_worker_{worker_id}.db"
            db_url = f"sqlite:///{db_path}"

        engine = make_engine(db_url)

        # Create tables on first use - ensure all tables are created before proceeding
        # Use a more robust approach for SQLite in-memory databases
        initialize_database(engine)

        # Force a sync checkpoint to ensure all tables are properly created
        if "sqlite" in db_url:
            with engine.connect() as conn:
                # Enable WAL mode and foreign keys for better concurrency
                if ":memory:" not in db_url:
                    from sqlalchemy import text

                    conn.execute(text("PRAGMA journal_mode=WAL"))
                    conn.execute(text("PRAGMA foreign_keys=ON"))
                    conn.execute(text("PRAGMA synchronous=NORMAL"))

                # Ensure all pending writes are committed
                conn.commit()

                # Verify tables were actually created
                if os.getenv("NODE_ENV") == "test":
                    from sqlalchemy import text

                    result = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
                    tables = [row[0] for row in result]
                    print(f"[DEBUG] Worker {worker_id} database tables after creation: {sorted(tables)}")
                    print(f"[DEBUG] Worker {worker_id} database path: {db_url}")
                    import sys

                    sys.stdout.flush()

                    # Specifically check for critical tables
                    required_tables = ["agents", "workflows", "users"]
                    missing_tables = [t for t in required_tables if t not in tables]
                    if missing_tables:
                        print(f"[ERROR] Worker {worker_id} missing critical tables: {missing_tables}")
                        # Try to recreate tables
                        initialize_database(engine)
                        sys.stdout.flush()

                    # Ensure test user exists for foreign key constraints
                    from sqlalchemy import text

                    result = conn.execute(text("SELECT COUNT(*) FROM users WHERE id = 1"))
                    user_count = result.scalar()
                    if user_count == 0:
                        print(f"[DEBUG] Worker {worker_id} creating test user...")
                        # Create a test user for foreign key references
                        conn.execute(
                            text("""
                            INSERT INTO users (id, email, role, is_active, provider, provider_user_id, 
                                              display_name, created_at, updated_at)
                            VALUES (1, 'test@example.com', 'ADMIN', 1, 'dev', 'test-user-1', 
                                   'Test User', datetime('now'), datetime('now'))
                        """)
                        )
                        conn.commit()
                        print(f"[DEBUG] Worker {worker_id} test user created")
                        sys.stdout.flush()

        session_factory = make_sessionmaker(engine)

        _WORKER_ENGINES[worker_id] = engine
        _WORKER_SESSIONMAKERS[worker_id] = session_factory

        return session_factory


# Default engine and sessionmaker instances for app usage
# Reuse the same relaxed resolution logic from *get_session_factory* so that
# importing this module never crashes – the returned engine is still safe for
# overwriting in tests via ``zerg.database.default_engine = …``.

_resolved_db_url = _settings.database_url or ("sqlite:///:memory:" if _settings.testing else "sqlite:///./app.db")

default_engine = make_engine(_resolved_db_url)
default_session_factory = make_sessionmaker(default_engine)


def get_db(session_factory: Any = None) -> Iterator[Session]:
    """Dependency provider for database sessions.

    Args:
        session_factory: Optional custom session factory

    Yields:
        SQLAlchemy Session object
    """
    factory = session_factory or get_session_factory()
    db = factory()
    try:
        yield db
    finally:
        try:
            db.close()
        except Exception:
            # Ignore errors during session close, such as when the database
            # connection has been terminated unexpectedly (e.g., during reset operations)
            pass


# ============================================================================
# Carmack-Style Unified Session Management
# ============================================================================


@contextmanager
def db_session(session_factory: Any = None):
    """
    Carmack-style database session context manager.

    Single way to manage database sessions in services and background tasks.
    Handles all error cases automatically - impossible to leak connections.

    Key principles:
    1. Auto-commit on success
    2. Auto-rollback on error
    3. Always close session
    4. Clear error messages

    Usage:
        with db_session() as db:
            user = crud.create_user(db, user_data)
            # Automatic commit + close

        # On error: automatic rollback + close

    Args:
        session_factory: Optional custom session factory

    Yields:
        SQLAlchemy Session object with automatic lifecycle management
    """
    factory = session_factory or get_session_factory()
    session = factory()

    try:
        yield session
        session.commit()  # Auto-commit on success
        logging.debug("Database session committed successfully")

    except Exception as e:
        session.rollback()  # Auto-rollback on error
        logging.error(f"Database session rolled back due to error: {e}")
        raise  # Re-raise the original exception

    finally:
        session.close()  # Always close
        logging.debug("Database session closed")


# Legacy alias for backward compatibility
def get_db_session(session_factory: Any = None):
    """
    Legacy alias for db_session() - DEPRECATED.

    Use db_session() directly for better clarity.
    """
    logging.warning("get_db_session() is deprecated - use db_session() instead")
    return db_session(session_factory)


def initialize_database(engine: Engine = None) -> None:
    """Initialize database tables using the given engine.

    If no engine is provided, uses the default engine.

    Args:
        engine: Optional engine to use, defaults to default_engine
    """
    # Import all models to ensure they are registered with Base
    # We need to import the models explicitly to ensure they're registered
    from zerg.models.models import Agent  # noqa: F401
    from zerg.models.models import AgentMessage  # noqa: F401
    from zerg.models.models import AgentRun  # noqa: F401
    from zerg.models.models import CanvasLayout  # noqa: F401
    from zerg.models.models import Connector  # noqa: F401
    from zerg.models.models import NodeExecutionState  # noqa: F401
    from zerg.models.models import Thread  # noqa: F401
    from zerg.models.models import ThreadMessage  # noqa: F401
    from zerg.models.models import Trigger  # noqa: F401
    from zerg.models.models import User  # noqa: F401
    from zerg.models.models import Workflow  # noqa: F401
    from zerg.models.models import WorkflowExecution  # noqa: F401
    from zerg.models.models import WorkflowTemplate  # noqa: F401

    target_engine = engine or default_engine

    # Debug: Check what tables will be created
    import os

    if os.getenv("NODE_ENV") == "test":
        table_names = [table.name for table in Base.metadata.tables.values()]
        print(f"[DEBUG] Creating tables: {sorted(table_names)}")

    Base.metadata.create_all(bind=target_engine)

    # Debug: Verify tables were created
    if os.getenv("NODE_ENV") == "test":
        from sqlalchemy import text

        with target_engine.connect() as conn:
            # Check what tables actually exist (SQLite specific)
            result = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            tables = [row[0] for row in result]
            print(f"[DEBUG] Tables created in database: {sorted(tables)}")
