"""SQLite-only database configuration and session management.

This module provides database connection and session management for Zerg.
The codebase is SQLite-only for OSS deployment simplicity.
"""

import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import Lock
from typing import Any
from typing import Iterator

import dotenv
from sqlalchemy import Engine
from sqlalchemy import MetaData
from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from zerg.config import get_settings

logger = logging.getLogger(__name__)

_settings = get_settings()

# ---------------------------------------------------------------------------
# Test-only commis DB routing (E2E isolation)
# ---------------------------------------------------------------------------

_test_commis_id: ContextVar[str | None] = ContextVar("test_commis_id", default=None)
_commis_session_factories: dict[str, sessionmaker] = {}
_commis_factories_lock = Lock()


def set_test_commis_id(commis_id: str | None):
    """Set the current test commis id for DB routing (E2E only)."""
    return _test_commis_id.set(commis_id)


def reset_test_commis_id(token) -> None:
    """Reset the current test commis id to the previous value."""
    _test_commis_id.reset(token)


def get_test_commis_id() -> str | None:
    """Return the current test commis id (E2E only)."""
    return _test_commis_id.get()


def list_test_commis_ids() -> list[str]:
    """Return known test commis ids for E2E DB routing."""
    return list(_commis_session_factories.keys())


def _safe_commis_id(commis_id: str) -> str:
    # Keep filenames stable + safe (allow digits, letters, dash, underscore).
    return "".join(ch for ch in commis_id if ch.isalnum() or ch in {"-", "_"}).strip() or "0"


def _commis_db_url(commis_id: str) -> str:
    base_url = _settings.database_url
    if not base_url:
        raise ValueError("DATABASE_URL not set in environment")

    parsed = make_url(base_url)
    db_path = parsed.database or ""
    if not db_path:
        raise ValueError("DATABASE_URL missing sqlite path")

    # Allow explicit override for E2E db root (handy for temp dirs)
    e2e_db_dir = os.getenv("E2E_DB_DIR")
    if e2e_db_dir:
        base_dir = Path(e2e_db_dir)
        base_name = Path(db_path).stem
    else:
        base_dir = Path(db_path).expanduser().resolve().parent
        base_name = Path(db_path).stem

    safe_id = _safe_commis_id(commis_id)
    commis_path = base_dir / f"{base_name}_commis_{safe_id}.db"
    return f"sqlite:///{commis_path}"


def _get_or_create_commis_session_factory(commis_id: str) -> sessionmaker:
    safe_id = _safe_commis_id(commis_id)
    existing = _commis_session_factories.get(safe_id)
    if existing is not None:
        return existing

    with _commis_factories_lock:
        existing = _commis_session_factories.get(safe_id)
        if existing is not None:
            return existing

        db_url = _commis_db_url(safe_id)
        engine = make_engine(db_url)
        factory = make_sessionmaker(engine)

        # Initialize schema for this commis DB (SQLite-only)
        initialize_database(engine)

        _commis_session_factories[safe_id] = factory
        return factory


# Use override=True to ensure proper quote stripping even if vars are inherited from parent process
dotenv.load_dotenv(override=True)


# SQLite-only: no schema support
_metadata = MetaData()

# Create Base class
Base = declarative_base(metadata=_metadata)

# Import all models at module level to ensure they are registered with Base
try:
    from zerg.models.agents import SessionEmbedding  # noqa: F401
    from zerg.models.models import Connector  # noqa: F401
    from zerg.models.models import Fiche  # noqa: F401
    from zerg.models.models import FicheMessage  # noqa: F401
    from zerg.models.models import Memory  # noqa: F401
    from zerg.models.models import MemoryEmbedding  # noqa: F401
    from zerg.models.models import MemoryFile  # noqa: F401
    from zerg.models.models import Run  # noqa: F401
    from zerg.models.models import Thread  # noqa: F401
    from zerg.models.models import ThreadMessage  # noqa: F401
    from zerg.models.models import Trigger  # noqa: F401
    from zerg.models.models import User  # noqa: F401
    from zerg.models.models import UserSkill  # noqa: F401
    from zerg.models.models import UserTask  # noqa: F401
    from zerg.models.work import FileReservation  # noqa: F401
    from zerg.models.work import Insight  # noqa: F401
except ImportError:
    # Handle case where models module might not be available during certain imports
    pass


def _configure_sqlite_engine(engine: Engine) -> None:
    """Configure SQLite pragmas for concurrency and durability."""
    busy_timeout_ms = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "5000"))
    synchronous = os.getenv("SQLITE_SYNCHRONOUS", "NORMAL").strip().upper() or "NORMAL"
    journal_mode = os.getenv("SQLITE_JOURNAL_MODE", "WAL").strip().upper() or "WAL"
    foreign_keys = os.getenv("SQLITE_FOREIGN_KEYS", "ON").strip().upper() or "ON"
    wal_autocheckpoint = os.getenv("SQLITE_WAL_AUTOCHECKPOINT")

    @event.listens_for(engine, "connect")
    def set_sqlite_pragmas(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute(f"PRAGMA journal_mode={journal_mode}")
            cursor.execute(f"PRAGMA synchronous={synchronous}")
            cursor.execute(f"PRAGMA foreign_keys={foreign_keys}")
            cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            if wal_autocheckpoint:
                cursor.execute(f"PRAGMA wal_autocheckpoint={wal_autocheckpoint}")
        finally:
            cursor.close()


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
    if (db_url.startswith('"') and db_url.endswith('"')) or (db_url.startswith("'") and db_url.endswith("'")):
        db_url = db_url[1:-1].strip()

    try:
        parsed = make_url(db_url)
    except Exception as e:
        raise ValueError(f"Invalid DATABASE_URL: {e}") from e

    if not parsed.drivername.startswith("sqlite"):
        raise ValueError(
            f"Unsupported DATABASE_URL driver '{parsed.drivername}'. " "Only SQLite is supported (sqlite:///path/to/db.sqlite)."
        )

    # SQLite configuration
    connect_args = kwargs.setdefault("connect_args", {})
    connect_args.setdefault("check_same_thread", False)

    # In-memory SQLite (sqlite:// with no path) requires StaticPool to keep
    # a single connection alive — otherwise each pool checkout creates a new
    # empty database.  File-backed SQLite uses the default QueuePool.
    is_memory = parsed.database in (None, "", ":memory:")
    if is_memory:
        kwargs.setdefault("poolclass", StaticPool)
    else:
        if "timeout" not in connect_args:
            busy_timeout_ms = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "5000"))
            connect_args["timeout"] = busy_timeout_ms / 1000.0

    engine = create_engine(db_url, **kwargs)
    _configure_sqlite_engine(engine)
    return engine


def make_sessionmaker(engine: Engine) -> sessionmaker:
    """Create a sessionmaker bound to the given engine.

    Args:
        engine: SQLAlchemy Engine instance

    Returns:
        A sessionmaker class
    """
    # Determine expire_on_commit based on environment
    environment = os.getenv("ENVIRONMENT", "")

    # Check multiple indicators for E2E testing context
    is_e2e = environment.startswith("test:e2e") or os.getenv("TEST_TYPE") == "e2e" or "test_main" in str(engine.url)

    # Use expire_on_commit=False for E2E tests to keep objects accessible
    # after session closes, but True for unit tests to prevent stale data
    if is_e2e:
        expire_on_commit = False
    elif environment == "test" or environment.startswith("test:"):
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

    Uses DATABASE_URL from environment.

    Returns:
        A sessionmaker instance
    """
    # In E2E, route DB sessions by commis id (X-Test-Commis header / ws param).
    if _settings.testing:
        commis_id = get_test_commis_id()
        if commis_id:
            return _get_or_create_commis_session_factory(commis_id)

    if default_session_factory is not None:
        return default_session_factory

    # Fallback for edge cases where module loaded before DATABASE_URL set
    db_url = _settings.database_url
    if not db_url:
        raise ValueError("DATABASE_URL not set in environment")

    logger.warning("get_session_factory() creating engine on-demand (default_session_factory was None)")
    engine = make_engine(db_url)
    return make_sessionmaker(engine)


# Default engine and sessionmaker instances for app usage
if _settings.database_url:
    default_engine = make_engine(_settings.database_url)
    default_session_factory = make_sessionmaker(default_engine)
else:
    # Unit tests will override these in conftest.py before any actual usage
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
            pass


@contextmanager
def db_session(session_factory: Any = None):
    """
    Database session context manager with automatic commit/rollback.

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
        session.commit()

    except Exception as e:
        session.rollback()
        logging.error(f"Database session rolled back due to error: {e}")
        raise

    finally:
        session.close()


# Minimum SQLite version for required features.
# 3.35+ adds RETURNING, which we rely on for SQLite-safe job claiming.
SQLITE_MIN_VERSION = (3, 35, 0)


def check_sqlite_version(engine: Engine) -> tuple[bool, str]:
    """Check if SQLite version supports required features.

    SQLite 3.35+ is required for RETURNING (used in SQLite-safe job claiming).

    Args:
        engine: SQLAlchemy engine to check

    Returns:
        Tuple of (is_compatible, version_string)
    """
    if engine.dialect.name != "sqlite":
        return True, "N/A (not SQLite)"

    import sqlite3

    version_str = sqlite3.sqlite_version
    version_tuple = tuple(int(x) for x in version_str.split("."))

    is_compatible = version_tuple >= SQLITE_MIN_VERSION
    return is_compatible, version_str


def initialize_database(engine: Engine = None) -> None:
    """Initialize database tables using the given engine.

    If no engine is provided, uses the default engine.

    Args:
        engine: Optional engine to use, defaults to default_engine
    """
    # Import all models to ensure they are registered with Base
    from zerg.models.agents import AgentsBase
    from zerg.models.agents import SessionEmbedding  # noqa: F401
    from zerg.models.models import Connector  # noqa: F401
    from zerg.models.models import Fiche  # noqa: F401
    from zerg.models.models import FicheMessage  # noqa: F401
    from zerg.models.models import Memory  # noqa: F401
    from zerg.models.models import MemoryEmbedding  # noqa: F401
    from zerg.models.models import MemoryFile  # noqa: F401
    from zerg.models.models import Run  # noqa: F401
    from zerg.models.models import Thread  # noqa: F401
    from zerg.models.models import ThreadMessage  # noqa: F401
    from zerg.models.models import User  # noqa: F401
    from zerg.models.models import UserTask  # noqa: F401
    from zerg.models.work import FileReservation  # noqa: F401
    from zerg.models.work import Insight  # noqa: F401

    target_engine = engine or default_engine

    if target_engine is None:
        raise ValueError("No engine provided and default_engine is None")

    # Check SQLite version for required features
    is_compatible, version_str = check_sqlite_version(target_engine)
    if not is_compatible:
        min_ver = ".".join(str(x) for x in SQLITE_MIN_VERSION)
        raise RuntimeError(f"SQLite version {version_str} is below minimum {min_ver}. Upgrade SQLite to use this application.")
    logger.debug(f"SQLite version {version_str} meets requirements")

    # Strip any schema references for SQLite (which doesn't support schemas)
    target_engine = target_engine.execution_options(schema_translate_map={"zerg": None, "agents": None})

    # Debug: Check what tables will be created
    if os.getenv("NODE_ENV") == "test":
        table_names = [table.name for table in Base.metadata.tables.values()]
        logger.debug("Creating tables: %s", sorted(table_names))

    # Create main tables
    Base.metadata.create_all(bind=target_engine)

    # Create agents tables
    AgentsBase.metadata.create_all(bind=target_engine)

    # Migrate existing tables: add columns that create_all() won't ALTER into place
    _migrate_agents_columns(target_engine)

    # SQLite-only: ensure FTS5 index for agent events
    if target_engine.dialect.name == "sqlite":
        _ensure_agents_fts(target_engine)

    # Debug: Verify tables were created
    if os.getenv("NODE_ENV") == "test":
        from sqlalchemy import inspect

        inspector = inspect(target_engine)
        tables = inspector.get_table_names()
        logger.debug("Tables created in database: %s", sorted(tables))


def _migrate_agents_columns(engine: Engine) -> None:
    """Add columns to existing tables that create_all() won't ALTER in.

    SQLite's CREATE TABLE IF NOT EXISTS is a no-op on existing tables, so new
    columns must be added explicitly via ALTER TABLE.
    """
    if engine.dialect.name != "sqlite":
        return

    try:
        with engine.connect() as conn:
            columns = {row[1] for row in conn.execute(text("PRAGMA table_info(sessions)"))}
            if "summary" not in columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN summary TEXT"))
            if "summary_title" not in columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN summary_title VARCHAR(200)"))
            if "needs_embedding" not in columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN needs_embedding INTEGER DEFAULT 1"))
                conn.execute(text("UPDATE sessions SET needs_embedding = 1 WHERE needs_embedding IS NULL"))
            conn.commit()
    except Exception:
        logger.debug("sessions table migration skipped (table may not exist yet)", exc_info=True)


def _ensure_agents_fts(engine: Engine) -> None:
    """Ensure FTS5 index and triggers exist for agent events (SQLite only)."""
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
                    content_text,
                    tool_output_text,
                    tool_name,
                    role,
                    session_id UNINDEXED,
                    content='events',
                    content_rowid='id'
                )
                """
            )

            conn.exec_driver_sql(
                """
                CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
                  INSERT INTO events_fts(rowid, content_text, tool_output_text, tool_name, role, session_id)
                  VALUES (new.id, new.content_text, new.tool_output_text, new.tool_name, new.role, new.session_id);
                END
                """
            )
            conn.exec_driver_sql(
                """
                CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
                  INSERT INTO events_fts(events_fts, rowid, content_text, tool_output_text, tool_name, role, session_id)
                  VALUES('delete', old.id, old.content_text, old.tool_output_text, old.tool_name, old.role, old.session_id);
                END
                """
            )
            conn.exec_driver_sql(
                """
                CREATE TRIGGER IF NOT EXISTS events_au AFTER UPDATE ON events BEGIN
                  INSERT INTO events_fts(events_fts, rowid, content_text, tool_output_text, tool_name, role, session_id)
                  VALUES('delete', old.id, old.content_text, old.tool_output_text, old.tool_name, old.role, old.session_id);
                  INSERT INTO events_fts(rowid, content_text, tool_output_text, tool_name, role, session_id)
                  VALUES (new.id, new.content_text, new.tool_output_text, new.tool_name, new.role, new.session_id);
                END
                """
            )

            # Backfill only when the FTS table is empty and events already exist.
            fts_has_rows = conn.exec_driver_sql("SELECT 1 FROM events_fts LIMIT 1").fetchone() is not None
            events_has_rows = conn.exec_driver_sql("SELECT 1 FROM events LIMIT 1").fetchone() is not None
            if not fts_has_rows and events_has_rows:
                conn.exec_driver_sql("INSERT INTO events_fts(events_fts) VALUES('rebuild')")
    except Exception as exc:  # pragma: no cover - surface missing FTS5 support
        raise RuntimeError(f"Failed to initialize FTS5 index (events_fts): {exc}") from exc
