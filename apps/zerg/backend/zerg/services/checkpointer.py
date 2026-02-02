"""Checkpointer factory for LangGraph fiche state persistence.

This module provides a factory function that returns the appropriate checkpointer
based on the database configuration:
- SqliteSaver for SQLite - durable checkpoints for local/OSS usage
- MemorySaver for tests - fast in-memory checkpoints

The factory handles database detection and connection management.
"""

import logging
import os
import sqlite3
import threading

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import Engine
from sqlalchemy.engine.url import make_url

logger = logging.getLogger(__name__)

# Global cache for initialized checkpointer instances
_sqlite_checkpointer_cache: dict[str, BaseCheckpointSaver] = {}
_sqlite_conn_cache: dict[str, sqlite3.Connection] = {}

# Lock for thread-safe cache access
_sqlite_cache_lock = threading.Lock()


def _extract_sqlite_path(db_url: str) -> str:
    """Extract the file path from a SQLite database URL.

    Handles SQLAlchemy-style URLs including query parameters for URI mode.

    Args:
        db_url: SQLAlchemy-style SQLite URL (e.g., "sqlite:///path/to/db.sqlite")

    Returns:
        The connection string for SQLite (file path, :memory:, or URI with params)
    """
    # Strip surrounding quotes (common from .env files)
    db_url = (db_url or "").strip()
    if (db_url.startswith('"') and db_url.endswith('"')) or (db_url.startswith("'") and db_url.endswith("'")):
        db_url = db_url[1:-1].strip()

    try:
        parsed = make_url(db_url)
        if parsed.database:
            # If there are query parameters, preserve them for URI mode
            # (e.g., ?mode=memory&cache=shared)
            if parsed.query:
                # Reconstruct as file URI for sqlite3's uri=True mode
                query_str = "&".join(f"{k}={v}" for k, v in parsed.query.items())
                return f"file:{parsed.database}?{query_str}"
            return parsed.database
    except Exception:
        pass

    # Fallback: manual parsing
    if ":///" in db_url:
        # Absolute path: sqlite:////absolute/path or sqlite:///./relative
        return db_url.split(":///", 1)[1]
    elif "://" in db_url:
        # Relative path: sqlite://relative (uncommon)
        return db_url.split("://", 1)[1]

    return ":memory:"


def _create_sqlite_checkpointer(db_path: str):
    """Create SqliteSaver for durable checkpoints.

    Uses synchronous SqliteSaver which is thread-safe and works across event loops.
    The check_same_thread=False setting allows the connection to be used from
    any thread, which is safe because SqliteSaver uses internal locking.

    Args:
        db_path: Path to SQLite database file, or :memory: for in-memory

    Returns:
        Tuple of (connection, SqliteSaver instance)
    """
    from langgraph.checkpoint.sqlite import SqliteSaver

    # Detect if this is a URI-style path (with query params)
    use_uri = db_path.startswith("file:")

    # Create connection with thread safety enabled
    # check_same_thread=False is OK because SqliteSaver uses internal locking
    conn = sqlite3.connect(db_path, check_same_thread=False, uri=use_uri)

    # Create the checkpointer - it will create tables on first use via setup()
    saver = SqliteSaver(conn)

    return conn, saver


def get_checkpointer(engine: Engine = None) -> BaseCheckpointSaver:
    """Get the appropriate checkpointer based on database configuration.

    For SQLite connections, returns a SqliteSaver that persists checkpoints to the
    same SQLite database, enabling durable state across restarts. Uses synchronous
    SqliteSaver (not AsyncSqliteSaver) to avoid event loop affinity issues - the
    synchronous version is thread-safe and works across any event loop.

    For tests, returns MemorySaver for fast in-memory checkpointing.

    Args:
        engine: SQLAlchemy engine to inspect. If None, uses the default engine
                from zerg.database.

    Returns:
        A checkpointer instance (SqliteSaver or MemorySaver)

    Note:
        Checkpointer instances are cached by connection URL to avoid repeated
        setup calls. The checkpointer automatically creates required tables
        (checkpoints, checkpoint_writes, checkpoint_blobs) on first use.
    """
    if engine is None:
        from zerg.database import default_engine

        engine = default_engine

    # Some tests pass a lightweight mock Engine; be defensive about URL handling.
    try:
        db_url = str(engine.url.render_as_string(hide_password=False))  # type: ignore[union-attr]
    except Exception:
        db_url = str(getattr(engine, "url", ""))

    # For tests, use MemorySaver for fast in-memory checkpointing
    if os.environ.get("TESTING") == "1":
        logger.debug("Using MemorySaver for test environment")
        return MemorySaver()

    # For SQLite databases (lite_mode), use SqliteSaver for durable checkpoints
    # Use synchronous SqliteSaver to avoid event loop affinity issues
    if "sqlite" in db_url.lower():
        # Thread-safe cache check/update
        with _sqlite_cache_lock:
            if db_url in _sqlite_checkpointer_cache:
                logger.debug("Returning cached SqliteSaver instance")
                return _sqlite_checkpointer_cache[db_url]

            db_path = _extract_sqlite_path(db_url)
            logger.info(f"Initializing SqliteSaver for SQLite database: {db_path}")

            try:
                conn, checkpointer = _create_sqlite_checkpointer(db_path)
                logger.info("SqliteSaver initialized successfully")

                # Cache both the connection and checkpointer
                _sqlite_conn_cache[db_url] = conn
                _sqlite_checkpointer_cache[db_url] = checkpointer
                return checkpointer

            except ImportError as e:
                logger.error(f"langgraph-checkpoint-sqlite not installed: {e}. " "Install with: uv add langgraph-checkpoint-sqlite")
                logger.warning("Falling back to MemorySaver")
                return MemorySaver()

            except Exception as e:
                logger.error(f"Failed to initialize SqliteSaver: {e}")
                logger.warning("Falling back to MemorySaver")
                return MemorySaver()

    # Fallback for unknown database types
    logger.warning(f"Unknown database type in URL: {db_url}. Using MemorySaver")
    return MemorySaver()


def clear_checkpointer_cache():
    """Clear SQLite checkpointer cache.

    This is primarily useful for testing scenarios where you need to
    reinitialize the checkpointer with different configuration.
    """
    # Close any open SQLite connections (thread-safe with lock)
    with _sqlite_cache_lock:
        for conn in _sqlite_conn_cache.values():
            try:
                conn.close()
            except Exception:
                pass

        _sqlite_checkpointer_cache.clear()
        _sqlite_conn_cache.clear()

    logger.debug("Checkpointer cache cleared")
