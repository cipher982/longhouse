"""SQLite-only database configuration and session management.

This module provides database connection and session management for Zerg.
The codebase is SQLite-only for OSS deployment simplicity.
"""

import logging
import os
import time
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
from zerg.session_execution_home import SessionExecutionHome

logger = logging.getLogger(__name__)

_settings = get_settings()

# ---------------------------------------------------------------------------
# Test-only commis DB routing (E2E isolation)
# ---------------------------------------------------------------------------

_test_commis_id: ContextVar[str | None] = ContextVar("test_commis_id", default=None)
_commis_session_factories: dict[str, sessionmaker] = {}
_commis_factories_lock = Lock()


@contextmanager
def _timed_database_step(name: str) -> Iterator[None]:
    started = time.monotonic()
    logger.info("Database initialization step starting: %s", name)
    try:
        yield
    finally:
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.info("Database initialization step complete: %s elapsed_ms=%.1f", name, elapsed_ms)


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


# Use override=True to ensure proper quote stripping even if vars are inherited from parent process.
# In test/E2E mode, do not override explicit env vars like ENVIRONMENT.
_override_env = os.getenv("TESTING", "").strip().lower() not in {"1", "true", "yes", "on"}
dotenv.load_dotenv(override=_override_env)


# SQLite-only: no schema support
_metadata = MetaData()

# Create Base class
Base = declarative_base(metadata=_metadata)

# Import all models at module level to ensure they are registered with Base
try:
    from zerg.models.agents import SessionEmbedding  # noqa: F401
    from zerg.models.apns_device_registration import APNSDeviceRegistration  # noqa: F401
    from zerg.models.apns_live_activity_registration import APNSLiveActivityRegistration  # noqa: F401
    from zerg.models.apns_widget_push_state import APNSWidgetPushState  # noqa: F401
    from zerg.models.models import Connector  # noqa: F401
    from zerg.models.models import Conversation  # noqa: F401
    from zerg.models.models import ConversationBinding  # noqa: F401
    from zerg.models.models import ConversationMessage  # noqa: F401
    from zerg.models.models import Fiche  # noqa: F401
    from zerg.models.models import FicheMessage  # noqa: F401
    from zerg.models.models import MemoryEmbedding  # noqa: F401
    from zerg.models.models import MemoryFile  # noqa: F401
    from zerg.models.models import Run  # noqa: F401
    from zerg.models.models import SurfaceIngressClaim  # noqa: F401
    from zerg.models.models import Thread  # noqa: F401
    from zerg.models.models import ThreadMessage  # noqa: F401
    from zerg.models.models import Trigger  # noqa: F401
    from zerg.models.models import User  # noqa: F401
    from zerg.models.models import UserSkill  # noqa: F401
    from zerg.models.models import UserTask  # noqa: F401
    from zerg.models.work import Insight  # noqa: F401
except ImportError:
    # Handle case where models module might not be available during certain imports
    pass


def _configure_sqlite_engine(engine: Engine, *, busy_timeout_ms: int | None = None) -> None:
    """Configure SQLite pragmas for concurrency and durability."""
    _busy_timeout_ms = busy_timeout_ms if busy_timeout_ms is not None else int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "5000"))
    synchronous = os.getenv("SQLITE_SYNCHRONOUS", "NORMAL").strip().upper() or "NORMAL"
    journal_mode = os.getenv("SQLITE_JOURNAL_MODE", "WAL").strip().upper() or "WAL"
    foreign_keys = os.getenv("SQLITE_FOREIGN_KEYS", "ON").strip().upper() or "ON"
    # Default to 0 (disabled) — we run PASSIVE checkpoints on a timer instead,
    # which never blocks readers/writers. Auto-checkpoint can stall on large DBs.
    wal_autocheckpoint = os.getenv("SQLITE_WAL_AUTOCHECKPOINT", "0")

    @event.listens_for(engine, "connect")
    def set_sqlite_pragmas(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute(f"PRAGMA journal_mode={journal_mode}")
            cursor.execute(f"PRAGMA synchronous={synchronous}")
            cursor.execute(f"PRAGMA foreign_keys={foreign_keys}")
            cursor.execute(f"PRAGMA busy_timeout={_busy_timeout_ms}")
            cursor.execute(f"PRAGMA wal_autocheckpoint={wal_autocheckpoint}")
        finally:
            cursor.close()


def make_engine(db_url: str, *, busy_timeout_ms: int | None = None, **kwargs) -> Engine:
    """Create a SQLAlchemy engine with the given URL and options.

    Args:
        db_url: Database connection URL
        busy_timeout_ms: Override busy_timeout (default from env or 5000ms)
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
    _busy_timeout = busy_timeout_ms if busy_timeout_ms is not None else int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "5000"))
    is_memory = parsed.database in (None, "", ":memory:")
    if is_memory:
        kwargs.setdefault("poolclass", StaticPool)
    else:
        if "timeout" not in connect_args:
            connect_args["timeout"] = _busy_timeout / 1000.0

    engine = create_engine(db_url, **kwargs)
    _configure_sqlite_engine(engine, busy_timeout_ms=_busy_timeout)
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


def make_write_engine(db_url: str) -> Engine:
    """Create the dedicated writer engine used by the write serializer.

    File-backed SQLite uses a one-connection QueuePool instead of StaticPool.
    The serializer executes writes in worker threads, and reusing the same raw
    sqlite connection across thread hops via StaticPool can produce broken ORM
    state on live request paths. In-memory SQLite still relies on StaticPool so
    the ephemeral database survives across checkouts.
    """

    parsed = make_url(db_url)
    is_memory = parsed.database in (None, "", ":memory:")
    if is_memory:
        return make_engine(db_url, busy_timeout_ms=30000)
    return make_engine(db_url, busy_timeout_ms=30000, pool_size=1, max_overflow=0)


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

    # Dedicated write engine: a single checked-out connection for file-backed
    # SQLite, or StaticPool for in-memory SQLite.
    _write_engine = make_write_engine(_settings.database_url)
    _write_session_factory = make_sessionmaker(_write_engine)
else:
    # Unit tests will override these in conftest.py before any actual usage
    logger.warning("DATABASE_URL not set - using placeholder (will be overridden by tests)")
    default_engine = None  # type: ignore[assignment]
    default_session_factory = None  # type: ignore[assignment]
    _write_engine = None
    _write_session_factory = None


def configure_write_serializer() -> None:
    """Configure the WriteSerializer with the dedicated write engine.

    Call once at startup (from lifespan) after database_url is set.
    No-op if write engine is not available (tests).
    """
    from zerg.services.write_serializer import get_write_serializer

    ws = get_write_serializer()
    if not ws.is_configured:
        ws.configure_resolver(_resolve_write_session_factory)


def get_write_session_factory() -> sessionmaker | None:
    """Return the current write session factory.

    In E2E, request handling can route to per-commis SQLite files via
    ``X-Test-Commis``. Serialized writes must follow that same routing or they
    will write to the wrong database and violate foreign keys.
    """
    if _settings.testing:
        commis_id = get_test_commis_id()
        if commis_id:
            return _get_or_create_commis_session_factory(commis_id)
    return _write_session_factory


def _resolve_write_session_factory() -> sessionmaker:
    session_factory = get_write_session_factory()
    if session_factory is None:
        raise RuntimeError("Write session factory unavailable")
    return session_factory


def get_write_engine() -> Engine | None:
    """Return the dedicated write engine (for WAL checkpoint etc.)."""
    return _write_engine


def _get_db_from_factory(session_factory: Any = None) -> Iterator[Session]:
    """Internal database-session iterator with optional factory override."""
    factory = session_factory or get_session_factory()
    db = factory()
    try:
        yield db
    finally:
        try:
            db.close()
        except Exception:
            pass


def get_db() -> Iterator[Session]:
    """FastAPI dependency provider for database sessions."""
    yield from _get_db_from_factory()


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


def _pre_migrate_session_inputs_identity_columns(engine: Engine) -> None:
    """Rename the old overloaded request id before additive auto-migration runs."""
    if engine.dialect.name != "sqlite":
        return

    try:
        with engine.connect() as conn:
            exists = conn.execute(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_inputs'")).fetchone()
            if not exists:
                return

            columns = {row[1] for row in conn.execute(text("PRAGMA table_info(session_inputs)"))}
            if "request_id" not in columns:
                return

            conn.execute(text("DROP INDEX IF EXISTS ix_session_inputs_session_owner_request"))
            if "client_request_id" not in columns:
                conn.execute(text("ALTER TABLE session_inputs RENAME COLUMN request_id TO client_request_id"))
            else:
                conn.execute(
                    text(
                        """
                        UPDATE session_inputs
                        SET client_request_id = request_id
                        WHERE client_request_id IS NULL
                          AND request_id IS NOT NULL
                        """
                    )
                )
            conn.commit()
    except Exception:
        logger.error(
            "session_inputs request_id rename migration FAILED — client_request_id dedupe may be incomplete",
            exc_info=True,
        )


def initialize_database(engine: Engine = None) -> None:
    """Initialize database tables using the given engine.

    If no engine is provided, uses the default engine.

    Args:
        engine: Optional engine to use, defaults to default_engine
    """
    init_started = time.monotonic()

    # Import all models to ensure they are registered with Base
    from zerg.models.agents import SessionEmbedding  # noqa: F401
    from zerg.models.apns_device_registration import APNSDeviceRegistration  # noqa: F401
    from zerg.models.apns_live_activity_registration import APNSLiveActivityRegistration  # noqa: F401
    from zerg.models.apns_widget_push_state import APNSWidgetPushState  # noqa: F401
    from zerg.models.models import Connector  # noqa: F401
    from zerg.models.models import Conversation  # noqa: F401
    from zerg.models.models import ConversationBinding  # noqa: F401
    from zerg.models.models import ConversationMessage  # noqa: F401
    from zerg.models.models import Fiche  # noqa: F401
    from zerg.models.models import FicheMessage  # noqa: F401
    from zerg.models.models import JobRun  # noqa: F401
    from zerg.models.models import MemoryEmbedding  # noqa: F401
    from zerg.models.models import MemoryFile  # noqa: F401
    from zerg.models.models import Run  # noqa: F401
    from zerg.models.models import SurfaceIngressClaim  # noqa: F401
    from zerg.models.models import Thread  # noqa: F401
    from zerg.models.models import ThreadMessage  # noqa: F401
    from zerg.models.models import User  # noqa: F401
    from zerg.models.models import UserTask  # noqa: F401
    from zerg.models.work import Insight  # noqa: F401

    target_engine = engine or default_engine

    if target_engine is None:
        raise ValueError("No engine provided and default_engine is None")

    with _timed_database_step("sqlite_version_check"):
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

    with _timed_database_step("metadata_create_all"):
        Base.metadata.create_all(bind=target_engine)

    # Phase 2 (Option D): auto-derive missing columns from SQLAlchemy metadata
    # is now the authority for additive schema drift. Runs BEFORE
    # `_migrate_agents_columns` so the residual imperative blocks
    # (data-normalization UPDATEs, legacy backfills, index management) can
    # safely reference any column declared on the model. Phase 1's parity check
    # on a real dev DB returned zero new "would add" entries, confirming both
    # paths converge to the same end state. New additive columns should be
    # added to the SQLAlchemy model with appropriate ``server_default`` and rely
    # on this path — NOT on `_migrate_agents_columns`.
    with _timed_database_step("pre_migrate_session_inputs_identity_columns"):
        _pre_migrate_session_inputs_identity_columns(target_engine)
    with _timed_database_step("auto_add_missing_columns"):
        _auto_add_missing_columns(target_engine, Base.metadata, apply=True)

    # Residual imperative migrations: index/table adjustments, data
    # normalization, and non-trivial backfills.
    with _timed_database_step("residual_agents_migrations"):
        _migrate_agents_columns(target_engine)
    with _timed_database_step("cleanup_legacy_agents_tables"):
        _cleanup_legacy_agents_tables(target_engine)

    if target_engine.dialect.name == "sqlite":
        # Keep a ledger table ready for explicit heavy migrations.
        from zerg.db_migrations import ensure_migration_ledger

        with _timed_database_step("heavy_migration_ledger"):
            ensure_migration_ledger(target_engine)

    # SQLite-only: ensure FTS5 index for agent events
    if target_engine.dialect.name == "sqlite":
        with _timed_database_step("agents_fts"):
            _ensure_agents_fts(target_engine)

    # Debug: Verify tables were created
    if os.getenv("NODE_ENV") == "test":
        from sqlalchemy import inspect

        inspector = inspect(target_engine)
        tables = inspector.get_table_names()
        logger.debug("Tables created in database: %s", sorted(tables))

    elapsed_ms = (time.monotonic() - init_started) * 1000
    logger.info("Database initialization complete elapsed_ms=%.1f", elapsed_ms)


def _migrate_agents_columns(engine: Engine) -> None:
    """Residual SQLite migrations not absorbed by the auto-derive path.

    Additive ALTER TABLE ADD COLUMN drift is now handled by
    ``_auto_add_missing_columns`` against the SQLAlchemy metadata. The blocks
    below are NOT pure column adds — they cover:

      * Always-run data-normalization UPDATEs (legacy enum values, etc.).
      * Backfills the auto path can't express (CASE expressions, cross-table
        subqueries, columns whose initial value depends on the row's own id).
      * Index creates / drops / table drops the model layer cannot model.
      * Heartbeat counter columns whose model uses Python ``default=0`` rather
        than ``server_default=text("0")``; the auto path can't backfill those.
      * CREATE TABLE IF NOT EXISTS guards left as belt-and-suspenders.

    DO NOT add new ALTER TABLE ADD COLUMN entries here. Add the column to the
    SQLAlchemy model with appropriate ``server_default`` and rely on
    ``_auto_add_missing_columns`` (which runs before this function via
    ``initialize_database``).
    """
    if engine.dialect.name != "sqlite":
        return

    try:
        with engine.connect() as conn:
            columns = {row[1] for row in conn.execute(text("PRAGMA table_info(sessions)"))}
            # Pure additive ALTER ADDs (summary, summary_title, needs_embedding,
            # summary_event_count, last_summarized_event_id,
            # last_attention_push_at, last_attention_push_state, user_state,
            # user_state_at, managed_transport, source_runner_id,
            # source_runner_name, managed_session_name, loop_thread_id,
            # is_sidechain, launch_state, launch_error_code, launch_error_message,
            # launch_lease_until, launch_command_id, launch_client_request_id,
            # continued_from_session_id, continuation_kind, origin_label,
            # branched_from_event_id, is_writable_head, device_name, execution_home,
            # loop_mode, transcript_revision, summary_revision, embedding_revision,
            # thread_root_session_id, last_activity_at) are now handled by
            # _auto_add_missing_columns. Remaining work below is non-additive:
            # legacy backfills + always-run normalization UPDATEs + index creates.
            if columns:
                # Backfill: derive transcript_revision from per-session message
                # counts on legacy rows that pre-date the column. The auto path
                # adds the column with server_default=0; this seeds rows that
                # already had content. WHERE clause keeps it idempotent.
                conn.execute(
                    text(
                        """
                        UPDATE sessions
                        SET transcript_revision = 1
                        WHERE transcript_revision = 0
                          AND COALESCE(user_messages, 0) + COALESCE(assistant_messages, 0) + COALESCE(tool_calls, 0) > 0
                        """
                    )
                )
                # Backfill: summary_revision tracks transcript_revision when
                # summary fields are populated.
                conn.execute(
                    text(
                        """
                        UPDATE sessions
                        SET summary_revision = COALESCE(transcript_revision, 0)
                        WHERE summary_revision = 0
                          AND (
                                COALESCE(summary, '') <> ''
                             OR COALESCE(summary_title, '') <> ''
                             OR last_summarized_event_id IS NOT NULL
                             OR COALESCE(summary_event_count, 0) > 0
                          )
                        """
                    )
                )
                # Backfill: embedding_revision tracks transcript_revision when
                # the row is already embedded (needs_embedding = 0).
                conn.execute(
                    text(
                        """
                        UPDATE sessions
                        SET embedding_revision = COALESCE(transcript_revision, 0)
                        WHERE embedding_revision = 0
                          AND COALESCE(needs_embedding, 1) = 0
                        """
                    )
                )
            # Always-run normalization: drop legacy 'legacy' execution_home and
            # any out-of-enum strings to the unmanaged-local default.
            conn.execute(
                text(
                    f"""
                    UPDATE sessions
                    SET execution_home = '{SessionExecutionHome.UNMANAGED_LOCAL.value}'
                    WHERE execution_home IS NULL OR execution_home = 'legacy'
                    """
                )
            )
            conn.execute(
                text(
                    f"""
                    UPDATE sessions
                    SET execution_home = '{SessionExecutionHome.UNMANAGED_LOCAL.value}'
                    WHERE execution_home NOT IN ('unmanaged_local', 'managed_local', 'managed_hosted', 'cloud_takeover')
                    """
                )
            )
            # Always-run normalization: legacy 'manual' loop_mode → 'assist';
            # any out-of-enum value → 'assist'.
            conn.execute(text("UPDATE sessions SET loop_mode = 'assist' WHERE loop_mode IS NULL OR loop_mode = 'manual'"))
            conn.execute(
                text(
                    """
                    UPDATE sessions
                    SET loop_mode = 'assist'
                    WHERE loop_mode NOT IN ('assist', 'autopilot')
                    """
                )
            )
            # Backfill: every session is its own thread root unless explicitly
            # branched. The model column has no server_default (CHAR(36) refers
            # to id), so seed legacy rows here.
            conn.execute(text("UPDATE sessions SET thread_root_session_id = id WHERE thread_root_session_id IS NULL"))
            # Backfill: legacy last_activity_at from MAX(events.timestamp).
            events_exists = conn.execute(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'")).fetchone()
            if events_exists:
                conn.execute(
                    text(
                        "UPDATE sessions SET last_activity_at = ("
                        "SELECT MAX(e.timestamp) FROM events e WHERE e.session_id = sessions.id"
                        ") WHERE last_activity_at IS NULL"
                    )
                )
            # Multi-column indexes the model layer doesn't declare on these columns.
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_sessions_execution_home ON sessions(execution_home)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_sessions_source_runner_id ON sessions(source_runner_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_sessions_thread_head ON sessions(thread_root_session_id, is_writable_head)"))
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_sessions_continued_from_started ON sessions(continued_from_session_id, started_at)")
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_sessions_last_activity_at ON sessions(last_activity_at)"))
            conn.commit()
    except Exception:
        logger.debug("sessions table migration skipped (table may not exist yet)", exc_info=True)

    try:
        with engine.begin() as conn:
            launch_attempts_exists = conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_launch_attempts'")
            ).fetchone()
            if launch_attempts_exists:
                attempt_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(session_launch_attempts)"))}
                session_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(sessions)"))}
                if {"owner_id", "session_id"}.issubset(attempt_columns) and {"id", "owner_id"}.issubset(session_columns):
                    conn.execute(
                        text(
                            """
                            UPDATE session_launch_attempts
                            SET owner_id = (
                                SELECT sessions.owner_id
                                FROM sessions
                                WHERE sessions.id = session_launch_attempts.session_id
                            )
                            WHERE owner_id IS NULL
                              AND session_id IS NOT NULL
                            """
                        )
                    )
                    conn.execute(
                        text(
                            """
                            CREATE INDEX IF NOT EXISTS ix_session_launch_attempts_owner_request
                            ON session_launch_attempts(owner_id, device_id, client_request_id, created_at)
                            """
                        )
                    )
    except Exception:
        logger.debug("session_launch_attempts migration skipped (table may not exist yet)", exc_info=True)

    try:
        with engine.begin() as conn:
            runtime_state_exists = conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_runtime_state'")
            ).fetchone()
            if runtime_state_exists:
                columns = {row[1] for row in conn.execute(text("PRAGMA table_info(session_runtime_state)"))}
                conn.execute(
                    text(
                        """
                        CREATE INDEX IF NOT EXISTS ix_runtime_state_session_updated_version
                        ON session_runtime_state(session_id, updated_at, runtime_version)
                        """
                    )
                )
                # terminal_reason / terminal_source ALTER ADDs handled by
                # _auto_add_missing_columns (pure nullable adds). The
                # phase-source normalization UPDATE below remains because it is
                # always-run data hygiene, not a column-add backfill.
                if {
                    "phase",
                    "phase_source",
                    "active_tool",
                    "last_runtime_signal_at",
                    "last_live_at",
                    "freshness_expires_at",
                    "terminal_state",
                    "updated_at",
                }.issubset(columns):
                    conn.execute(
                        text(
                            """
                            UPDATE session_runtime_state
                            SET phase = 'idle',
                                active_tool = NULL,
                                last_runtime_signal_at = NULL,
                                last_live_at = NULL,
                                freshness_expires_at = NULL,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE phase_source = 'progress'
                              AND (terminal_state IS NULL OR terminal_state = '')
                              AND (
                                  phase <> 'idle'
                                  OR active_tool IS NOT NULL
                                  OR last_runtime_signal_at IS NOT NULL
                                  OR last_live_at IS NOT NULL
                                  OR freshness_expires_at IS NOT NULL
                              )
                            """
                        )
                    )
    except Exception:
        logger.debug("session runtime state truth normalization skipped (table may not exist yet)", exc_info=True)

    try:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS session_runtime_events"))
    except Exception:
        logger.exception("session_runtime_events table removal failed")
        raise

    # Reflection feature removed — drop table and stamping column.
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS reflection_runs"))
            session_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(sessions)"))}
            if "reflected_at" in session_cols:
                conn.execute(text("ALTER TABLE sessions DROP COLUMN reflected_at"))
    except Exception:
        logger.debug("reflection cleanup skipped", exc_info=True)

    # Daily-digest feature removed — drop User columns.
    try:
        with engine.begin() as conn:
            user_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(users)"))}
            if "digest_enabled" in user_cols:
                conn.execute(text("ALTER TABLE users DROP COLUMN digest_enabled"))
            if "last_digest_sent_at" in user_cols:
                conn.execute(text("ALTER TABLE users DROP COLUMN last_digest_sent_at"))
    except Exception:
        logger.debug("daily digest cleanup skipped", exc_info=True)

    # BYO-key LLM provider DB config removed — drop table.
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS llm_provider_configs"))
    except Exception:
        logger.debug("llm_provider_configs drop skipped", exc_info=True)

    try:
        with engine.connect() as conn:
            columns = {row[1] for row in conn.execute(text("PRAGMA table_info(agent_heartbeats)"))}
            if columns and "spool_dead" not in columns:
                conn.execute(text("ALTER TABLE agent_heartbeats ADD COLUMN spool_dead INTEGER DEFAULT 0"))
                conn.execute(text("UPDATE agent_heartbeats SET spool_dead = 0 WHERE spool_dead IS NULL"))
            if columns and "last_ship_attempt_at" not in columns:
                conn.execute(text("ALTER TABLE agent_heartbeats ADD COLUMN last_ship_attempt_at DATETIME"))
            if columns and "last_ship_result" not in columns:
                conn.execute(text("ALTER TABLE agent_heartbeats ADD COLUMN last_ship_result VARCHAR(64)"))
            if columns and "last_ship_latency_ms" not in columns:
                conn.execute(text("ALTER TABLE agent_heartbeats ADD COLUMN last_ship_latency_ms INTEGER"))
            if columns and "last_ship_http_status" not in columns:
                conn.execute(text("ALTER TABLE agent_heartbeats ADD COLUMN last_ship_http_status INTEGER"))
            if columns and "ship_attempts_1h" not in columns:
                conn.execute(text("ALTER TABLE agent_heartbeats ADD COLUMN ship_attempts_1h INTEGER DEFAULT 0"))
                conn.execute(text("UPDATE agent_heartbeats SET ship_attempts_1h = 0 WHERE ship_attempts_1h IS NULL"))
            if columns and "ship_successes_1h" not in columns:
                conn.execute(text("ALTER TABLE agent_heartbeats ADD COLUMN ship_successes_1h INTEGER DEFAULT 0"))
                conn.execute(text("UPDATE agent_heartbeats SET ship_successes_1h = 0 WHERE ship_successes_1h IS NULL"))
            if columns and "ship_rate_limited_1h" not in columns:
                conn.execute(text("ALTER TABLE agent_heartbeats ADD COLUMN ship_rate_limited_1h INTEGER DEFAULT 0"))
                conn.execute(text("UPDATE agent_heartbeats SET ship_rate_limited_1h = 0 WHERE ship_rate_limited_1h IS NULL"))
            if columns and "ship_server_errors_1h" not in columns:
                conn.execute(text("ALTER TABLE agent_heartbeats ADD COLUMN ship_server_errors_1h INTEGER DEFAULT 0"))
                conn.execute(text("UPDATE agent_heartbeats SET ship_server_errors_1h = 0 WHERE ship_server_errors_1h IS NULL"))
            if columns and "ship_payload_rejections_1h" not in columns:
                conn.execute(text("ALTER TABLE agent_heartbeats ADD COLUMN ship_payload_rejections_1h INTEGER DEFAULT 0"))
                conn.execute(text("UPDATE agent_heartbeats SET ship_payload_rejections_1h = 0 WHERE ship_payload_rejections_1h IS NULL"))
            if columns and "ship_payload_too_large_1h" not in columns:
                conn.execute(text("ALTER TABLE agent_heartbeats ADD COLUMN ship_payload_too_large_1h INTEGER DEFAULT 0"))
                conn.execute(text("UPDATE agent_heartbeats SET ship_payload_too_large_1h = 0 WHERE ship_payload_too_large_1h IS NULL"))
            if columns and "ship_retryable_client_errors_1h" not in columns:
                conn.execute(text("ALTER TABLE agent_heartbeats ADD COLUMN ship_retryable_client_errors_1h INTEGER DEFAULT 0"))
                conn.execute(
                    text("UPDATE agent_heartbeats SET ship_retryable_client_errors_1h = 0 " "WHERE ship_retryable_client_errors_1h IS NULL")
                )
            if columns and "ship_connect_errors_1h" not in columns:
                conn.execute(text("ALTER TABLE agent_heartbeats ADD COLUMN ship_connect_errors_1h INTEGER DEFAULT 0"))
                conn.execute(text("UPDATE agent_heartbeats SET ship_connect_errors_1h = 0 WHERE ship_connect_errors_1h IS NULL"))
            if columns and "ship_latency_p50_ms_1h" not in columns:
                conn.execute(text("ALTER TABLE agent_heartbeats ADD COLUMN ship_latency_p50_ms_1h INTEGER"))
            if columns and "ship_latency_p95_ms_1h" not in columns:
                conn.execute(text("ALTER TABLE agent_heartbeats ADD COLUMN ship_latency_p95_ms_1h INTEGER"))
            conn.commit()
    except Exception:
        logger.debug("agent_heartbeats table migration skipped (table may not exist yet)", exc_info=True)

    # session_turns table migrations
    # source_kind, timing_confidence (server_default-backed), expected_user_text_hash
    # and baseline_observation_cursor (pure nullable) ALTER ADDs handled by
    # _auto_add_missing_columns. Remaining work: legacy backfill from
    # baseline_runtime_cursor + index creates.
    try:
        with engine.connect() as conn:
            columns = {row[1] for row in conn.execute(text("PRAGMA table_info(session_turns)"))}
            if columns:
                if "baseline_runtime_cursor" in columns:
                    conn.execute(
                        text(
                            """
                            UPDATE session_turns
                            SET baseline_observation_cursor = baseline_runtime_cursor
                            WHERE baseline_observation_cursor IS NULL
                              AND baseline_runtime_cursor IS NOT NULL
                            """
                        )
                    )
                conn.execute(
                    text(
                        """
                        CREATE INDEX IF NOT EXISTS ix_session_turns_session_order
                        ON session_turns(session_id, user_submitted_at, created_at, id)
                        """
                    )
                )
                conn.execute(
                    text(
                        """
                        CREATE INDEX IF NOT EXISTS ix_session_turns_session_state_created
                        ON session_turns(session_id, state, created_at)
                        """
                    )
                )
            conn.commit()
    except Exception:
        logger.debug("session_turns table migration skipped (table may not exist yet)", exc_info=True)

    # session_tasks table migrations
    # retry_later_count ALTER ADD handled by _auto_add_missing_columns
    # (server_default=0, NOT NULL — auto path emits matching DDL).

    # session_inputs table migrations
    try:
        with engine.connect() as conn:
            session_inputs_exists = conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_inputs'")
            ).fetchone()
            if session_inputs_exists:
                conn.execute(
                    text(
                        """
                        CREATE UNIQUE INDEX IF NOT EXISTS ix_session_inputs_session_owner_client_request
                        ON session_inputs(session_id, owner_id, client_request_id)
                        WHERE client_request_id IS NOT NULL
                        """
                    )
                )
                conn.commit()
    except Exception:
        logger.error(
            "session_inputs idempotency index migration FAILED — client_request_id dedupe will not be enforced; "
            "duplicate iOS retries may create duplicate rows",
            exc_info=True,
        )
        try:
            from zerg.metrics import database_migrations_failed_total

            database_migrations_failed_total.labels(
                migration_name="session_inputs_idempotency_index",
            ).inc()
        except Exception:
            logger.debug("failed to emit database_migrations_failed_total metric", exc_info=True)

    # session_messages table migrations
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS session_messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        from_session_id CHAR(36) NOT NULL,
                        to_session_id CHAR(36) NOT NULL,
                        text TEXT NOT NULL,
                        source_event_id INTEGER,
                        delivery_status VARCHAR(32) NOT NULL DEFAULT 'queued',
                        delivery_attempts INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT,
                        delivered_via VARCHAR(32),
                        delivered_at DATETIME,
                        acknowledged_at DATETIME,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            columns = {row[1] for row in conn.execute(text("PRAGMA table_info(session_messages)"))}
            if columns:
                if "source_event_id" not in columns:
                    conn.execute(text("ALTER TABLE session_messages ADD COLUMN source_event_id INTEGER"))
                if "delivery_status" not in columns:
                    conn.execute(text("ALTER TABLE session_messages ADD COLUMN delivery_status VARCHAR(32) NOT NULL DEFAULT 'queued'"))
                if "delivery_attempts" not in columns:
                    conn.execute(text("ALTER TABLE session_messages ADD COLUMN delivery_attempts INTEGER NOT NULL DEFAULT 0"))
                if "last_error" not in columns:
                    conn.execute(text("ALTER TABLE session_messages ADD COLUMN last_error TEXT"))
                if "delivered_via" not in columns:
                    conn.execute(text("ALTER TABLE session_messages ADD COLUMN delivered_via VARCHAR(32)"))
                if "delivered_at" not in columns:
                    conn.execute(text("ALTER TABLE session_messages ADD COLUMN delivered_at DATETIME"))
                if "acknowledged_at" not in columns:
                    conn.execute(text("ALTER TABLE session_messages ADD COLUMN acknowledged_at DATETIME"))
                if "created_at" not in columns:
                    conn.execute(text("ALTER TABLE session_messages ADD COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP"))
                if "updated_at" not in columns:
                    conn.execute(text("ALTER TABLE session_messages ADD COLUMN updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"))
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_session_messages_to_status_created
                    ON session_messages(to_session_id, delivery_status, created_at)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_session_messages_from_created
                    ON session_messages(from_session_id, created_at)
                    """
                )
            )
            conn.commit()
    except Exception:
        logger.debug("session_messages table migration skipped", exc_info=True)

    # insights table migrations
    # `origin` has Python default= but no server_default (legacy rows must
    # stay NULL so the title/tags backfill can promote system rows to
    # 'system' while leaving manual rows untouched). _auto_add_missing_columns
    # correctly skips this; we add it imperatively here.
    # `archived_at` is fully nullable and is handled by _auto_add_missing_columns.
    try:
        with engine.connect() as conn:
            columns = {row[1] for row in conn.execute(text("PRAGMA table_info(insights)"))}
            if columns:
                if "origin" not in columns:
                    conn.execute(text("ALTER TABLE insights ADD COLUMN origin VARCHAR(20)"))
                    columns.add("origin")
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_insights_origin ON insights(origin)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_insights_archived_at ON insights(archived_at)"))
                if "title" in columns:
                    conn.execute(
                        text(
                            """
                            UPDATE insights
                            SET origin = 'system'
                            WHERE origin IS NULL
                              AND title IN ('Stale ingest detected', 'Ingest recovered')
                            """
                        )
                    )
                if "tags" in columns:
                    conn.execute(
                        text(
                            """
                            UPDATE insights
                            SET origin = 'system'
                            WHERE origin IS NULL
                              AND COALESCE(tags, '') LIKE '%stale-agent%'
                            """
                        )
                    )
                conn.commit()
    except Exception:
        logger.debug("insights table migration skipped (table may not exist yet)", exc_info=True)

    # session_branches table migrations
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS session_branches (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id CHAR(36) NOT NULL,
                        parent_branch_id INTEGER,
                        branched_at_source_path TEXT,
                        branched_at_offset BIGINT,
                        branch_reason VARCHAR(32) NOT NULL DEFAULT 'root',
                        is_head INTEGER NOT NULL DEFAULT 0,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                    )
                    """
                )
            )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_session_branches_session_created " "ON session_branches(session_id, created_at)")
            )
            conn.execute(
                text("CREATE UNIQUE INDEX IF NOT EXISTS ix_session_branches_head " "ON session_branches(session_id) WHERE is_head = 1")
            )
            conn.execute(
                text(
                    """
                    INSERT INTO session_branches (
                        session_id,
                        parent_branch_id,
                        branched_at_source_path,
                        branched_at_offset,
                        branch_reason,
                        is_head
                    )
                    SELECT
                        s.id,
                        NULL,
                        NULL,
                        NULL,
                        'root',
                        1
                    FROM sessions s
                    WHERE NOT EXISTS (
                        SELECT 1 FROM session_branches b WHERE b.session_id = s.id
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    UPDATE session_branches
                    SET is_head = 0
                    WHERE is_head = 1
                      AND id NOT IN (
                        SELECT MAX(id)
                        FROM session_branches
                        WHERE is_head = 1
                        GROUP BY session_id
                      )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    UPDATE session_branches
                    SET is_head = 1
                    WHERE id IN (
                        SELECT latest.id
                        FROM (
                            SELECT session_id, MAX(id) AS id
                            FROM session_branches
                            GROUP BY session_id
                        ) latest
                        WHERE NOT EXISTS (
                            SELECT 1
                            FROM session_branches heads
                            WHERE heads.session_id = latest.session_id
                              AND heads.is_head = 1
                        )
                    )
                    """
                )
            )
            conn.commit()
    except Exception:
        logger.debug("session_branches table migration skipped", exc_info=True)

    # events table migrations
    # All ALTER ADD COLUMN entries (tool_call_id, branch_id, event_uuid,
    # parent_event_uuid, raw_json_z, raw_json_codec, event_origin,
    # provisional_state/key/cursor/seq/complete, reconciled_event_id) are now
    # handled by _auto_add_missing_columns. Remaining work below: dedup index
    # rebuild + supporting indexes.
    try:
        with engine.connect() as conn:
            columns = {row[1] for row in conn.execute(text("PRAGMA table_info(events)"))}
            if columns:
                dedup_idx_sql_row = conn.execute(
                    text(
                        """
                        SELECT sql
                        FROM sqlite_master
                        WHERE type = 'index' AND name = 'ix_events_dedup'
                        LIMIT 1
                        """
                    )
                ).fetchone()
                dedup_needs_rebuild = True
                if dedup_idx_sql_row and isinstance(dedup_idx_sql_row[0], str):
                    normalized = " ".join(dedup_idx_sql_row[0].lower().split())
                    expected_fragment = "on events(session_id, branch_id, source_path, source_offset, event_hash)"
                    dedup_needs_rebuild = expected_fragment not in normalized
                if dedup_needs_rebuild:
                    conn.execute(text("DROP INDEX IF EXISTS ix_events_dedup"))
                    conn.execute(
                        text(
                            """
                            CREATE UNIQUE INDEX IF NOT EXISTS ix_events_dedup
                            ON events(session_id, branch_id, source_path, source_offset, event_hash)
                            WHERE source_path IS NOT NULL
                            """
                        )
                    )
                conn.execute(
                    text("CREATE INDEX IF NOT EXISTS ix_events_session_branch_timestamp " "ON events(session_id, branch_id, timestamp)")
                )
                conn.execute(
                    text(
                        """
                        DROP INDEX IF EXISTS ix_events_session_uuid
                        """
                    )
                )
                conn.execute(
                    text(
                        """
                        CREATE UNIQUE INDEX IF NOT EXISTS ix_events_session_branch_uuid
                        ON events(session_id, branch_id, event_uuid)
                        WHERE event_uuid IS NOT NULL
                        """
                    )
                )
                conn.execute(
                    text(
                        """
                        CREATE UNIQUE INDEX IF NOT EXISTS ix_events_provisional_key
                        ON events(session_id, provisional_key)
                        WHERE provisional_key IS NOT NULL
                        """
                    )
                )
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_events_event_origin ON events(event_origin)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_events_provisional_state ON events(provisional_state)"))
                if "event_origin" in columns:
                    conn.execute(text("DELETE FROM events WHERE event_origin = 'live_provisional'"))
                conn.commit()
    except Exception:
        logger.debug("events table migration skipped (table may not exist yet)", exc_info=True)

    # source_lines table migrations (full source-line archive for lossless export)
    try:
        with engine.connect() as conn:
            source_lines_exists = (
                conn.execute(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_lines' LIMIT 1")).fetchone() is not None
            )
            if not source_lines_exists:
                conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS source_lines (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            session_id CHAR(36) NOT NULL,
                            source_path TEXT NOT NULL,
                            source_offset BIGINT NOT NULL,
                            branch_id INTEGER NOT NULL,
                            revision INTEGER NOT NULL DEFAULT 1,
                            is_branch_copy INTEGER NOT NULL DEFAULT 0,
                            raw_json TEXT NOT NULL,
                            raw_json_z BLOB,
                            raw_json_codec INTEGER NOT NULL DEFAULT 0,
                            line_hash VARCHAR(64) NOT NULL,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                        )
                        """
                    )
                )
            else:
                source_line_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(source_lines)"))}
                if source_line_columns and "branch_id" not in source_line_columns:
                    # Lightweight compatibility add. Full legacy rebuild is explicit via `longhouse migrate`.
                    conn.execute(text("ALTER TABLE source_lines ADD COLUMN branch_id INTEGER"))
                if source_line_columns and "revision" not in source_line_columns:
                    conn.execute(text("ALTER TABLE source_lines ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"))
                if source_line_columns and "is_branch_copy" not in source_line_columns:
                    conn.execute(text("ALTER TABLE source_lines ADD COLUMN is_branch_copy INTEGER NOT NULL DEFAULT 0"))
                if source_line_columns and "raw_json_z" not in source_line_columns:
                    conn.execute(text("ALTER TABLE source_lines ADD COLUMN raw_json_z BLOB"))
                if source_line_columns and "raw_json_codec" not in source_line_columns:
                    conn.execute(text("ALTER TABLE source_lines ADD COLUMN raw_json_codec INTEGER NOT NULL DEFAULT 0"))

            conn.execute(
                text(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_source_line_revision
                    ON source_lines(session_id, branch_id, source_path, source_offset, revision)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_source_line_hash
                    ON source_lines(session_id, branch_id, source_path, source_offset, line_hash)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_source_lines_session_offset
                    ON source_lines(session_id, branch_id, source_offset)
                    """
                )
            )
            conn.commit()
    except Exception:
        logger.debug("source_lines table migration skipped", exc_info=True)

    # session_observations table migrations (raw append-only session observation bus)
    try:
        with engine.connect() as conn:
            observations_exists = (
                conn.execute(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_observations' LIMIT 1")).fetchone()
                is not None
            )
            if not observations_exists:
                conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS session_observations (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            observation_id VARCHAR(512) NOT NULL UNIQUE,
                            session_id CHAR(36),
                            runtime_key VARCHAR(255),
                            provider VARCHAR(64) NOT NULL,
                            device_id VARCHAR(255),
                            source_domain VARCHAR(32) NOT NULL,
                            source VARCHAR(128) NOT NULL,
                            kind VARCHAR(64) NOT NULL,
                            source_path TEXT,
                            source_offset BIGINT,
                            source_cursor VARCHAR(512),
                            observed_at DATETIME NOT NULL,
                            received_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            payload_json TEXT,
                            payload_json_z BLOB,
                            payload_json_codec INTEGER NOT NULL DEFAULT 0
                        )
                        """
                    )
                )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_session_observations_session_observed
                    ON session_observations(session_id, observed_at, id)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_session_observations_session_source_kind
                    ON session_observations(session_id, source, kind, id)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_session_observations_session_source_kind_observed
                    ON session_observations(session_id, source, kind, observed_at, id)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_session_observations_domain_kind
                    ON session_observations(source_domain, kind, observed_at)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_session_observations_source_cursor
                    ON session_observations(source, source_cursor)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_session_observations_runtime_key
                    ON session_observations(runtime_key)
                    """
                )
            )
            conn.commit()
    except Exception as exc:
        raise RuntimeError("Failed to initialize session_observations table") from exc

    # job_runs table migrations
    # error_type ALTER ADD handled by _auto_add_missing_columns (pure nullable).

    # commis_jobs table migrations
    # parent_run_id ALTER ADD handled by _auto_add_missing_columns (pure nullable).
    # Remaining work: index rekey (drop legacy ix_commis_jobs_idempotency, recreate
    # as composite with parent_run_id + tool_call_id partial unique).
    try:
        with engine.connect() as conn:
            columns = {row[1] for row in conn.execute(text("PRAGMA table_info(commis_jobs)"))}
            if columns:
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_commis_jobs_parent_run_id ON commis_jobs(parent_run_id)"))
                conn.execute(text("DROP INDEX IF EXISTS ix_commis_jobs_idempotency"))
                conn.execute(
                    text(
                        """
                        CREATE UNIQUE INDEX IF NOT EXISTS ix_commis_jobs_idempotency
                        ON commis_jobs(parent_run_id, tool_call_id)
                        WHERE parent_run_id IS NOT NULL AND tool_call_id IS NOT NULL
                        """
                    )
                )
                conn.commit()
    except Exception:
        logger.debug("commis_jobs table migration skipped (table may not exist yet)", exc_info=True)

    # runners table migrations
    try:
        with engine.connect() as conn:
            columns = {row[1] for row in conn.execute(text("PRAGMA table_info(runners)"))}
            if columns:
                if "availability_policy" not in columns:
                    conn.execute(text("ALTER TABLE runners ADD COLUMN availability_policy VARCHAR(20)"))
                conn.execute(
                    text(
                        """
                        UPDATE runners
                        SET availability_policy = CASE
                            WHEN COALESCE(TRIM(availability_policy), '') != '' THEN availability_policy
                            WHEN json_extract(runner_metadata, '$.availability_policy') IN ('always_on', 'on_demand', 'ephemeral')
                                THEN json_extract(runner_metadata, '$.availability_policy')
                            WHEN name LIKE 'lh-vm-canary-%' THEN 'ephemeral'
                            WHEN json_extract(runner_metadata, '$.install_mode') = 'desktop' THEN 'on_demand'
                            ELSE 'always_on'
                        END
                        """
                    )
                )
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_runners_availability_policy ON runners(availability_policy)"))
                conn.commit()
    except Exception:
        logger.debug("runners table migration skipped (table may not exist yet)", exc_info=True)

    # Session identity kernel: indexes on the new nullable thread_id/run_id
    # columns added to existing child tables. _auto_add_missing_columns() adds
    # the columns themselves, but never creates indexes on already-existing
    # tables — so without this block, prod DBs that pre-date the kernel never
    # get the (thread_id, ...) lookups Phase 2/3 will rely on. Pure
    # CREATE INDEX IF NOT EXISTS — idempotent, matches the spec.
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_sessions_primary_thread_id "
                    "ON sessions(primary_thread_id) WHERE primary_thread_id IS NOT NULL"
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_events_thread_id ON events(thread_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_source_lines_thread_id ON source_lines(thread_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_session_observations_thread_id ON session_observations(thread_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_session_runtime_state_thread_id " "ON session_runtime_state(thread_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_session_runtime_state_run_id ON session_runtime_state(run_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_session_turns_thread_id ON session_turns(thread_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_session_turns_run_id ON session_turns(run_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_session_inputs_thread_id ON session_inputs(thread_id)"))
            # One control attachment per (run, control_plane) — capability
            # projection invariant. Phase 2 upsert depends on this.
            conn.execute(
                text("CREATE UNIQUE INDEX IF NOT EXISTS ux_connections_run_plane " "ON session_connections(run_id, control_plane)")
            )
            conn.commit()
    except Exception:
        logger.debug("session identity kernel index migration skipped", exc_info=True)

    # Historical session identity stamping rewrites large archive tables
    # (events/source_lines/observations). Keep it out of startup; explicit
    # heavy migrations own that work.


def _auto_add_missing_columns(
    engine: Engine,
    *metadatas: MetaData,
    apply: bool = False,
) -> list[tuple[str, str]]:
    """Auto-derive missing columns from SQLAlchemy metadata against live schema.

    For each Table in each MetaData, diffs PRAGMA table_info against the model
    columns and emits ``ALTER TABLE … ADD COLUMN`` for the missing ones using
    SQLAlchemy's :class:`CreateColumn` compiler so the DDL comes from the column
    object itself (preserves type/nullability/server_default).

    SQLite ALTER TABLE cannot add primary-key, unique, or columns whose default
    is non-constant — those are logged and skipped, never raised.

    Args:
        engine: target SQLAlchemy engine (SQLite only — no-op otherwise).
        *metadatas: one or more MetaData containers to scan.
        apply: when False (default), only log what would be added; when True,
            execute the ALTERs through the WriteSerializer when configured,
            falling back to a direct connection at startup before the
            serializer is wired (mirrors the existing imperative migrator).

    Returns:
        ``[(table_name, column_name), …]`` — what was (or would be) added.
    """
    if engine.dialect.name != "sqlite":
        return []

    from sqlalchemy.schema import CreateColumn

    dialect = engine.dialect
    pending: list[tuple[str, str, str]] = []  # (table, col, ddl)
    skipped: list[tuple[str, str, str]] = []  # (table, col, reason)

    with engine.connect() as conn:
        existing_tables = {row[0] for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for md in metadatas:
            for table in md.tables.values():
                if table.name not in existing_tables:
                    continue  # create_all handles greenfield; skip
                live_cols = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table.name})"))}
                for col in table.columns:
                    if col.name in live_cols:
                        continue
                    if col.primary_key:
                        skipped.append((table.name, col.name, "primary_key"))
                        continue
                    if col.unique:
                        skipped.append((table.name, col.name, "unique"))
                        continue
                    # SQLite ALTER ADD COLUMN cannot preserve a REFERENCES clause:
                    # CreateColumn(col).compile(...) emits only the bare type for
                    # ALTER, so the FK would be silently dropped. Skip and force
                    # a human to write an imperative migration (often a table
                    # rebuild) that wires the constraint correctly.
                    if col.foreign_keys:
                        skipped.append((table.name, col.name, "foreign_key"))
                        continue
                    # Columns using Python-side ``default=`` only (no ``server_default``)
                    # cannot be backfilled by ALTER ADD COLUMN: SQLAlchemy applies the
                    # Python default on INSERT, not as a SQLite DEFAULT clause. Auto-derive
                    # would emit plain ``INTEGER`` (no DEFAULT) and legacy rows would stay
                    # NULL — silently breaking counter columns like
                    # AgentHeartbeat.spool_dead/ship_attempts_1h. Leave these for the
                    # imperative migrator which adds the column WITH a literal DEFAULT
                    # and runs an UPDATE backfill.
                    if col.server_default is None and col.default is not None:
                        skipped.append(
                            (
                                table.name,
                                col.name,
                                "python_default_only_no_server_default",
                            )
                        )
                        continue
                    default = col.server_default
                    if default is not None and getattr(default, "arg", None) is not None:
                        arg = default.arg
                        # NOTE: this skip currently false-positives ``server_default=func.now()``
                        # — SQLite accepts ``DEFAULT (CURRENT_TIMESTAMP)`` and the dialect
                        # would compile it correctly, but FunctionElement fails the
                        # isinstance/has-text check. Left intentionally conservative: any
                        # affected column is currently handled by the imperative migrator
                        # and create_all paths. Tighten only if a real case appears.
                        if not isinstance(arg, (str, int, float, bool)) and not hasattr(arg, "text"):
                            skipped.append((table.name, col.name, "non_constant_default"))
                            continue
                    # SQLite cannot ALTER ADD a NOT NULL column without a literal
                    # DEFAULT — there are no rows to seed otherwise. Leave these
                    # to the imperative migrator (typically nullable add + UPDATE
                    # backfill + later constraint tightening via heavy migration).
                    if not col.nullable and default is None:
                        skipped.append((table.name, col.name, "not_null_without_default"))
                        continue
                    try:
                        ddl = str(CreateColumn(col).compile(dialect=dialect))
                    except Exception as exc:
                        skipped.append((table.name, col.name, f"compile_failed:{exc}"))
                        continue
                    pending.append((table.name, col.name, ddl))

    for table_name, col_name, _ddl in pending:
        logger.info("auto-derive: %s add %s.%s", "would" if not apply else "applying", table_name, col_name)
    for table_name, col_name, reason in skipped:
        if reason == "python_default_only_no_server_default":
            logger.info(
                "auto-derive skip: %s.%s has Python default= but no server_default; " "leaving for imperative migrator",
                table_name,
                col_name,
            )
        elif reason == "foreign_key":
            logger.info(
                "auto-derive skip: %s.%s has foreign-key constraint; "
                "SQLite ALTER cannot preserve REFERENCES — leaving for imperative migration",
                table_name,
                col_name,
            )
        else:
            logger.info("auto-derive: skip %s.%s (%s)", table_name, col_name, reason)

    if not apply or not pending:
        return [(t, c) for t, c, _ in pending]

    def _apply(conn) -> None:
        for table_name, _col_name, ddl in pending:
            conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {ddl}"))

    # Mirror the existing imperative migrator: at startup the WriteSerializer
    # is not yet configured (initialize_database() runs before it), so use a
    # direct engine connection. When a configured event loop is available we
    # could route through the serializer, but startup is the only caller.
    with engine.begin() as conn:
        _apply(conn)

    return [(t, c) for t, c, _ in pending]


def _cleanup_legacy_agents_tables(engine: Engine) -> None:
    """Drop removed legacy SQLite tables/columns so existing instances converge."""
    if engine.dialect.name != "sqlite":
        return

    try:
        with engine.connect() as conn:
            legacy_tables = ("file_reservations", "memories", "sync_operations")
            for table_name in legacy_tables:
                exists = conn.execute(
                    text(
                        """
                        SELECT 1
                        FROM sqlite_master
                        WHERE type = 'table' AND name = :table_name
                        LIMIT 1
                        """
                    ),
                    {"table_name": table_name},
                ).fetchone()
                if exists is None:
                    continue
                conn.execute(text(f"DROP TABLE {table_name}"))
                logger.info("Dropped legacy %s table", table_name)

            thread_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(threads)"))}
            if "memory_strategy" in thread_columns:
                conn.execute(text("ALTER TABLE threads DROP COLUMN memory_strategy"))
                logger.info("Dropped legacy threads.memory_strategy column")

            conn.commit()
    except Exception:
        logger.debug("legacy memory cleanup skipped", exc_info=True)


def _ensure_agents_fts(engine: Engine) -> None:
    """Ensure FTS5 index and triggers exist for agent events (SQLite only)."""
    try:
        with engine.connect() as conn:
            object_rows = conn.exec_driver_sql(
                """
                SELECT type, name
                FROM sqlite_master
                WHERE (type = 'table' AND name = 'events_fts')
                   OR (type = 'trigger' AND name IN ('events_ai', 'events_ad', 'events_au'))
                """
            ).fetchall()
            existing_objects = {(str(row[0]), str(row[1])) for row in object_rows}
            fts_exists = ("table", "events_fts") in existing_objects
            missing_triggers = {
                "events_ai",
                "events_ad",
                "events_au",
            } - {name for obj_type, name in existing_objects if obj_type == "trigger"}

            fts_has_rows = fts_exists and conn.exec_driver_sql("SELECT 1 FROM events_fts LIMIT 1").fetchone() is not None
            events_has_rows = conn.exec_driver_sql("SELECT 1 FROM events LIMIT 1").fetchone() is not None
            needs_rebuild = fts_exists and not fts_has_rows and events_has_rows

        if fts_exists and not missing_triggers and not needs_rebuild:
            return

        with engine.begin() as conn:
            if not fts_exists:
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

            if "events_ai" in missing_triggers:
                conn.exec_driver_sql(
                    """
                    CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
                      INSERT INTO events_fts(rowid, content_text, tool_output_text, tool_name, role, session_id)
                      VALUES (new.id, new.content_text, new.tool_output_text, new.tool_name, new.role, new.session_id);
                    END
                    """
                )
            if "events_ad" in missing_triggers:
                conn.exec_driver_sql(
                    """
                    CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
                      INSERT INTO events_fts(events_fts, rowid, content_text, tool_output_text, tool_name, role, session_id)
                      VALUES('delete', old.id, old.content_text, old.tool_output_text, old.tool_name, old.role, old.session_id);
                    END
                    """
                )
            if "events_au" in missing_triggers:
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

            if needs_rebuild:
                conn.exec_driver_sql("INSERT INTO events_fts(events_fts) VALUES('rebuild')")
    except Exception as exc:  # pragma: no cover - surface missing FTS5 support
        raise RuntimeError(f"Failed to initialize FTS5 index (events_fts): {exc}") from exc


# ---------------------------------------------------------------------------
# WAL checkpoint background task
# ---------------------------------------------------------------------------

_wal_checkpoint_task = None

WAL_CHECKPOINT_INTERVAL = int(os.getenv("SQLITE_WAL_CHECKPOINT_INTERVAL", "60"))
WAL_TRUNCATE_BYTES = int(os.getenv("SQLITE_WAL_TRUNCATE_BYTES", str(512 * 1024 * 1024)))


def _sqlite_wal_path() -> Path | None:
    if default_engine is None:
        return None
    database = getattr(default_engine.url, "database", None)
    if not database:
        return None
    return Path(database).expanduser().resolve().with_name(Path(database).name + "-wal")


def get_wal_bytes() -> int | None:
    """Return current SQLite WAL file size in bytes, or None if unknown.

    Phase 1 instrumentation: surfaced via /api/health so the engine and
    operators can see when WAL pressure is the actual ingest bottleneck.
    """
    wal = _sqlite_wal_path()
    if wal is None:
        return None
    try:
        return wal.stat().st_size if wal.exists() else 0
    except OSError:
        return None


def _checkpoint_counts(row) -> tuple[int, int, int, int]:
    """Return (busy, log_frames, checkpointed_frames, remaining_frames)."""
    if row is None:
        return (0, 0, 0, 0)
    busy = int(row[0] or 0)
    log_frames = int(row[1] or 0)
    checkpointed_frames = int(row[2] or 0)
    remaining_frames = max(log_frames - checkpointed_frames, 0)
    return busy, log_frames, checkpointed_frames, remaining_frames


async def start_wal_checkpoint_loop() -> None:
    """Start periodic PASSIVE WAL checkpoints.

    PASSIVE never blocks readers or writers — it checkpoints whatever pages
    it can without waiting. This prevents WAL growth on busy instances
    without causing the stalls that auto-checkpoint can trigger.
    """
    import asyncio

    global _wal_checkpoint_task

    def _do_checkpoint():
        """Run checkpoint in a thread — never block the event loop."""
        if default_engine is not None:
            with default_engine.connect() as conn:
                result = conn.exec_driver_sql("PRAGMA wal_checkpoint(PASSIVE)")
                busy, log_frames, checkpointed_frames, remaining_frames = _checkpoint_counts(result.fetchone())
                if log_frames > 0:
                    logger.info(
                        "WAL checkpoint: %d frames in log, %d checkpointed, %d remaining",
                        log_frames,
                        checkpointed_frames,
                        remaining_frames,
                    )
                if busy or remaining_frames:
                    return
                wal_path = _sqlite_wal_path()
                wal_size = wal_path.stat().st_size if wal_path is not None and wal_path.exists() else 0
                if WAL_TRUNCATE_BYTES <= 0 or wal_size < WAL_TRUNCATE_BYTES:
                    return
                truncate_result = conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
                t_busy, t_log_frames, t_checkpointed_frames, t_remaining_frames = _checkpoint_counts(truncate_result.fetchone())
                if t_busy:
                    logger.warning(
                        "WAL truncate checkpoint was busy: %d frames in log, %d checkpointed, %d remaining, size=%d",
                        t_log_frames,
                        t_checkpointed_frames,
                        t_remaining_frames,
                        wal_size,
                    )
                else:
                    logger.info(
                        "WAL truncated after passive checkpoint: size=%d threshold=%d",
                        wal_size,
                        WAL_TRUNCATE_BYTES,
                    )

    async def _loop():
        while True:
            try:
                await asyncio.sleep(WAL_CHECKPOINT_INTERVAL)
                await asyncio.to_thread(_do_checkpoint)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.warning("WAL checkpoint failed (non-fatal)", exc_info=True)

    _wal_checkpoint_task = asyncio.create_task(_loop())


async def stop_wal_checkpoint_loop() -> None:
    """Stop the WAL checkpoint background task."""
    global _wal_checkpoint_task
    if _wal_checkpoint_task and not _wal_checkpoint_task.done():
        _wal_checkpoint_task.cancel()
        try:
            await _wal_checkpoint_task
        except Exception:
            pass
        _wal_checkpoint_task = None
