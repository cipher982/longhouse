import logging
import os
import threading
from contextlib import contextmanager
from typing import Any
from typing import Dict
from typing import Iterator

import dotenv
from sqlalchemy import Engine
from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from zerg.config import get_settings

# Thread-safe caches for per-worker engines/sessionmakers --------------------

_WORKER_ENGINES: Dict[str, Engine] = {}
_WORKER_SESSIONMAKERS: Dict[str, sessionmaker] = {}
# Per-worker init locks prevent cross-worker serialization during E2E startup.
_WORKER_INIT_LOCKS: Dict[str, threading.Lock] = {}
# Use RLock (reentrant) since _get_postgres_schema_session is called while lock is held
_WORKER_LOCK = threading.RLock()
logger = logging.getLogger(__name__)


def clear_worker_caches():
    """Clear cached worker engines and sessionmakers.

    This is needed for E2E tests to ensure session factories are created
    with the correct configuration after environment variables are set.
    """
    global _WORKER_ENGINES, _WORKER_SESSIONMAKERS, _WORKER_INIT_LOCKS
    with _WORKER_LOCK:
        _WORKER_ENGINES.clear()
        _WORKER_SESSIONMAKERS.clear()
        _WORKER_INIT_LOCKS.clear()


def _get_worker_init_lock(worker_id: str) -> threading.Lock:
    """Get (or create) a per-worker init lock.

    This avoids serializing initialization for *different* Playwright workers
    behind a single global lock while still preventing double-init for the same
    worker_id.
    """
    with _WORKER_LOCK:
        lock = _WORKER_INIT_LOCKS.get(worker_id)
        if lock is None:
            lock = threading.Lock()
            _WORKER_INIT_LOCKS[worker_id] = lock
        return lock


# ---------------------------------------------------------------------------
# Playwright worker-based DB isolation (E2E tests)
# ---------------------------------------------------------------------------

# We *dynamically* route each HTTP/WebSocket request to a worker-specific
# Postgres schema during Playwright runs. The current worker id is injected by
# middleware and stored in a context variable. Importing here avoids a
# circular dependency (middleware imports *this* module). The conditional
# import keeps the overhead negligible for production usage.

try:
    from zerg.middleware.worker_db import current_worker_id

except ModuleNotFoundError:
    import contextvars

    current_worker_id = contextvars.ContextVar("current_worker_id", default=None)


_settings = get_settings()

# Use override=True to ensure proper quote stripping even if vars are inherited from parent process
dotenv.load_dotenv(override=True)


# Create Base class
Base = declarative_base()

# Import all models at module level to ensure they are registered with Base
# This prevents "no such table" errors when worker databases are created
try:
    from zerg.models.models import Agent  # noqa: F401
    from zerg.models.models import AgentMemoryKV  # noqa: F401
    from zerg.models.models import AgentMessage  # noqa: F401
    from zerg.models.models import AgentRun  # noqa: F401
    from zerg.models.models import CanvasLayout  # noqa: F401
    from zerg.models.models import Connector  # noqa: F401
    from zerg.models.models import NodeExecutionState  # noqa: F401
    from zerg.models.models import Thread  # noqa: F401
    from zerg.models.models import ThreadMessage  # noqa: F401
    from zerg.models.models import Trigger  # noqa: F401
    from zerg.models.models import User  # noqa: F401
    from zerg.models.models import UserTask  # noqa: F401
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
    db_url = (db_url or "").strip()
    if not db_url:
        raise ValueError("DATABASE_URL is not set (empty)")

    # Some environments / Makefile exporters include surrounding quotes from `.env`
    # (e.g. DATABASE_URL="postgresql://..."). Be forgiving here.
    if (db_url.startswith('"') and db_url.endswith('"')) or (db_url.startswith("'") and db_url.endswith("'")):
        db_url = db_url[1:-1].strip()

    # Common footgun: many platforms emit `postgres://...` but SQLAlchemy expects `postgresql://...`.
    if db_url.startswith("postgres://"):
        db_url = "postgresql://" + db_url[len("postgres://") :]

    try:
        parsed = make_url(db_url)
    except Exception as e:  # pragma: no cover - defensive, depends on SQLAlchemy parsing
        raise ValueError(f"Invalid DATABASE_URL: {e}") from e

    if parsed.drivername.startswith("sqlite"):
        raise ValueError(
            "SQLite DATABASE_URL is no longer supported. "
            "Set DATABASE_URL to a Postgres URL (e.g. postgresql+psycopg://user:pass@host:5432/dbname)."
        )

    if not parsed.drivername.startswith("postgresql"):
        raise ValueError(
            f"Unsupported DATABASE_URL driver '{parsed.drivername}'. " "Only Postgres is supported (postgresql+psycopg://...)."
        )

    # E2E tests: use moderate pool size (Postgres max_connections increased to 500)
    # With N Playwright workers, each gets its own engine.
    # pool_size=3 + max_overflow=5 = 8 connections max per worker
    # 16 workers × 8 = 128 connections max, well under the 500 limit
    # See: docs/work/e2e-test-infrastructure-redesign.md
    if _settings.e2e_use_postgres_schemas:
        kwargs.setdefault("pool_size", 3)
        kwargs.setdefault("max_overflow", 5)  # Max 8 connections per engine

    # Connection pool health: pre_ping verifies connections before use,
    # pool_recycle closes connections after 5 minutes to handle DB restarts
    kwargs.setdefault("pool_pre_ping", True)
    kwargs.setdefault("pool_recycle", 300)

    return create_engine(db_url, **kwargs)


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


def _get_postgres_schema_session(worker_id: str) -> sessionmaker:
    """Get session factory that uses worker-specific Postgres schema.

    Each worker gets its own Postgres schema (e.g., e2e_worker_0) for full isolation.
    Uses connection event listeners to set search_path on every connection.

    SECURITY: This is only enabled when E2E_USE_POSTGRES_SCHEMAS=1 (test environments).
    The X-Test-Worker header allows schema churning, so never enable in production.

    Args:
        worker_id: Worker ID to use for schema naming

    Returns:
        A sessionmaker configured for the worker's schema
    """
    # Fast-path: cached sessionmaker
    with _WORKER_LOCK:
        cached = _WORKER_SESSIONMAKERS.get(worker_id)
    if cached is not None:
        return cached

    # Use the main DATABASE_URL (Postgres)
    db_url = (_settings.database_url or "").strip()
    if (db_url.startswith('"') and db_url.endswith('"')) or (db_url.startswith("'") and db_url.endswith("'")):
        db_url = db_url[1:-1].strip()

    from sqlalchemy import text

    from zerg.e2e_schema_manager import ensure_worker_schema
    from zerg.e2e_schema_manager import get_schema_name

    schema_name = get_schema_name(worker_id)

    # Avoid doing full create_all(checkfirst=True) work on every cold start when
    # schemas are already pre-created in Playwright globalSetup.
    #
    # If the schema is missing (e.g., globalSetup dropped schemas while a backend
    # process was still running), we fall back to the full ensure.
    admin_engine = create_engine(db_url, poolclass=NullPool)
    schema_exists = False
    try:
        with admin_engine.connect() as conn:
            schema_exists = bool(
                conn.execute(
                    text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :schema"),
                    {"schema": schema_name},
                ).fetchone()
            )
            conn.commit()
    except Exception:
        schema_exists = False

    if not schema_exists:
        ensure_worker_schema(admin_engine, worker_id)

    # Dispose the admin engine (releases any remaining connections). With NullPool
    # this is mostly a no-op but good hygiene.
    try:
        admin_engine.dispose()
    except Exception:
        pass

    # Create schema-routed engine for this worker.
    #
    # NOTE: We prefer wiring schema routing into the *connection itself*
    # (libpq `options=-csearch_path=...`) rather than relying solely on runtime
    # `SET search_path` calls. This prevents subtle cases where a pooled
    # connection might retain a default search_path and write to public instead
    # of the worker schema.
    url = make_url(db_url)
    query = dict(url.query)
    existing_options = (query.get("options") or "").strip()
    schema_options = f"-csearch_path={schema_name},public"
    query["options"] = f"{existing_options} {schema_options}".strip() if existing_options else schema_options
    engine = make_engine(url.set(query=query).render_as_string(hide_password=False))

    # Create test user for foreign key constraints (E2E tests need a user for agent creation)
    with engine.connect() as conn:
        conn.execute(text(f"SET search_path TO {schema_name}, public"))
        result = conn.execute(text("SELECT COUNT(*) FROM users WHERE id = 1"))
        user_count = result.scalar()
        if user_count == 0:
            logger.debug("Worker %s (Postgres schema) creating test user...", worker_id)
            conn.execute(
                text("""
                    INSERT INTO users (id, email, role, is_active, provider, provider_user_id,
                                      display_name, context, created_at, updated_at)
                    VALUES (1, 'test@example.com', 'ADMIN', true, 'dev', 'test-user-1',
                           'Test User', '{}', NOW(), NOW())
                """)
            )
            conn.commit()
            logger.debug("Worker %s (Postgres schema) test user created", worker_id)

    # Add event listener to set search_path on every connection
    @event.listens_for(engine, "connect")
    def set_search_path(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute(f"SET search_path TO {schema_name}, public")
        cursor.close()

    session_factory = make_sessionmaker(engine)

    with _WORKER_LOCK:
        # Another thread could have initialized while we were creating (unlikely
        # with per-worker lock, but keep this safe).
        existing = _WORKER_SESSIONMAKERS.get(worker_id)
        if existing is not None:
            try:
                engine.dispose()
            except Exception:
                pass
            return existing

        _WORKER_ENGINES[worker_id] = engine
        _WORKER_SESSIONMAKERS[worker_id] = session_factory
        return session_factory


def get_session_factory() -> sessionmaker:
    """Get the default session factory for the application.

    Uses DATABASE_URL from environment.

    Returns:
        A sessionmaker instance
    """
    # ------------------------------------------------------------------
    # Playwright E2E tests: isolate database per worker ------------------
    # ------------------------------------------------------------------
    # When the *WorkerDBMiddleware* sets `current_worker_id` we route to
    # a worker-specific Postgres schema for full isolation.
    #
    # Outside the E2E test context (worker_id is None), we use the
    # default engine for unit tests, dev server, and production.
    # ------------------------------------------------------------------

    worker_id = current_worker_id.get()

    if worker_id is None:
        # --- Default behaviour for non-E2E contexts ---
        # CRITICAL: Reuse the cached default_session_factory to prevent
        # engine proliferation. Creating a new engine per call exhausts
        # Postgres connections under load.
        if default_session_factory is not None:
            return default_session_factory

        # Fallback for edge cases where module loaded before DATABASE_URL set
        # (e.g., during test discovery). This should rarely be hit in practice.
        db_url = _settings.database_url
        if not db_url:
            raise ValueError("DATABASE_URL not set in environment")

        logger.warning("get_session_factory() creating engine on-demand (default_session_factory was None)")
        engine = make_engine(db_url)
        return make_sessionmaker(engine)

    # --- Per-worker Postgres schema isolation (E2E tests) ---
    cached = _WORKER_SESSIONMAKERS.get(worker_id)
    if cached is not None:
        return cached

    # Lazily build the engine (per-worker lock avoids cross-worker serialization)
    init_lock = _get_worker_init_lock(worker_id)
    with init_lock:
        cached = _WORKER_SESSIONMAKERS.get(worker_id)
        if cached is not None:
            return cached

        # Route to Postgres schema isolation
        if _settings.e2e_use_postgres_schemas:
            return _get_postgres_schema_session(worker_id)

        # If schema isolation is disabled, something is misconfigured
        raise ValueError(
            f"Worker ID '{worker_id}' detected but E2E_USE_POSTGRES_SCHEMAS is not enabled. "
            "Enable Postgres schema isolation for E2E tests."
        )


# Default engine and sessionmaker instances for app usage
# For unit tests using testcontainers, DATABASE_URL will be set by conftest.py
# which also patches default_engine/default_session_factory after startup.
# For dev/prod, DATABASE_URL must be set in .env file.

# Create a placeholder engine that will be overridden by tests or used in production
if _settings.database_url:
    default_engine = make_engine(_settings.database_url)
    default_session_factory = make_sessionmaker(default_engine)
else:
    # Unit tests will override these in conftest.py before any actual usage
    # This allows the module to be imported during test discovery without crashing
    logger.warning("DATABASE_URL not set - using placeholder (will be overridden by tests)")
    default_engine = None  # type: ignore[assignment]
    default_session_factory = None  # type: ignore[assignment]


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

    except Exception as e:
        session.rollback()  # Auto-rollback on error
        logging.error(f"Database session rolled back due to error: {e}")
        raise  # Re-raise the original exception

    finally:
        session.close()  # Always close


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
    from zerg.models.models import AgentMemoryKV  # noqa: F401
    from zerg.models.models import AgentMessage  # noqa: F401
    from zerg.models.models import AgentRun  # noqa: F401
    from zerg.models.models import CanvasLayout  # noqa: F401
    from zerg.models.models import Connector  # noqa: F401
    from zerg.models.models import NodeExecutionState  # noqa: F401
    from zerg.models.models import Thread  # noqa: F401
    from zerg.models.models import ThreadMessage  # noqa: F401
    from zerg.models.models import Trigger  # noqa: F401
    from zerg.models.models import User  # noqa: F401
    from zerg.models.models import UserTask  # noqa: F401
    from zerg.models.models import Workflow  # noqa: F401
    from zerg.models.models import WorkflowExecution  # noqa: F401
    from zerg.models.models import WorkflowTemplate  # noqa: F401

    target_engine = engine or default_engine

    # Debug: Check what tables will be created
    if os.getenv("NODE_ENV") == "test":
        table_names = [table.name for table in Base.metadata.tables.values()]
        logger.debug("Creating tables: %s", sorted(table_names))

    Base.metadata.create_all(bind=target_engine)

    # Debug: Verify tables were created
    if os.getenv("NODE_ENV") == "test":
        from sqlalchemy import inspect

        inspector = inspect(target_engine)
        tables = inspector.get_table_names()
        logger.debug("Tables created in database: %s", sorted(tables))
