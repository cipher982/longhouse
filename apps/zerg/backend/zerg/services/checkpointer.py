"""Checkpointer factory for LangGraph agent state persistence.

This module provides a factory function that returns the appropriate checkpointer
based on the database configuration:
- AsyncPostgresSaver for PostgreSQL (production) - enables durable checkpoints with async support
- MemorySaver for SQLite (tests/dev) - fast in-memory checkpoints

The factory handles database detection, connection pooling, and async initialization.
"""

import asyncio
import logging
import threading
from typing import Union

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import Engine

logger = logging.getLogger(__name__)

# Global cache for initialized checkpointer instances
_postgres_checkpointer_cache: dict[str, BaseCheckpointSaver] = {}
_async_postgres_pool_cache: dict[str, "AsyncConnectionPool"] = {}

# Persistent event loop for async operations (kept alive in background thread)
_bg_loop: asyncio.AbstractEventLoop | None = None
_bg_thread: threading.Thread | None = None
_bg_lock = threading.Lock()


def _get_or_create_bg_loop() -> asyncio.AbstractEventLoop:
    """Get or create a persistent background event loop.

    This loop runs in a daemon thread and stays alive for the lifetime of the process.
    Used for async operations like opening the connection pool.
    """
    global _bg_loop, _bg_thread

    with _bg_lock:
        if _bg_loop is None or not _bg_loop.is_running():
            _bg_loop = asyncio.new_event_loop()

            def _run_loop():
                asyncio.set_event_loop(_bg_loop)
                _bg_loop.run_forever()

            _bg_thread = threading.Thread(target=_run_loop, daemon=True)
            _bg_thread.start()
            # Give the loop a moment to start
            import time
            time.sleep(0.01)

        return _bg_loop


def _create_async_checkpointer_in_bg_loop(db_url: str):
    """Create AsyncConnectionPool and AsyncPostgresSaver in the background loop context.

    This ensures the pool is created with a valid running event loop reference.

    Args:
        db_url: PostgreSQL connection string

    Returns:
        Tuple of (pool, checkpointer)
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg_pool import AsyncConnectionPool

    async def _create_and_setup():
        # Create pool within async context - this ensures proper loop association
        pool = AsyncConnectionPool(
            conninfo=db_url,
            min_size=1,
            max_size=10,
            open=False,
        )
        # Create checkpointer
        checkpointer = AsyncPostgresSaver(pool)
        # Open pool
        await pool.open()

        # Setup tables - need to handle CONCURRENTLY indexes outside transaction
        # The setup() method uses CREATE INDEX CONCURRENTLY which fails in transactions
        # Run migrations manually with autocommit mode
        try:
            await checkpointer.setup()
        except Exception as setup_err:
            # If setup fails due to concurrent index, run migrations manually
            err_str = str(setup_err).lower()
            if "concurrently" in err_str or "transaction" in err_str:
                logger.info("Running checkpointer migrations with autocommit connection")
                import psycopg
                from langgraph.checkpoint.postgres.base import BasePostgresSaver

                # Use a direct autocommit connection for migrations
                async with await psycopg.AsyncConnection.connect(
                    db_url, autocommit=True
                ) as conn:
                    # Get migrations from LangGraph
                    migrations = BasePostgresSaver.MIGRATIONS

                    # Track which migrations have run
                    try:
                        result = await conn.execute(
                            "SELECT v FROM checkpoint_migrations"
                        )
                        existing = {row[0] for row in await result.fetchall()}
                    except Exception:
                        existing = set()

                    for i, migration in enumerate(migrations):
                        if i in existing:
                            continue
                        try:
                            await conn.execute(migration)
                            # Record migration
                            await conn.execute(
                                "INSERT INTO checkpoint_migrations (v) VALUES (%s) ON CONFLICT DO NOTHING",
                                (i,)
                            )
                            logger.debug(f"Applied checkpoint migration {i}")
                        except Exception as mig_err:
                            # Ignore "already exists" errors
                            if "already exists" not in str(mig_err).lower():
                                logger.warning(f"Migration {i} warning: {mig_err}")

                    logger.info("Completed checkpointer migrations with autocommit")
            else:
                raise

        return pool, checkpointer

    # Run creation in background loop
    bg_loop = _get_or_create_bg_loop()
    future = asyncio.run_coroutine_threadsafe(_create_and_setup(), bg_loop)
    return future.result(timeout=30.0)


def get_checkpointer(engine: Engine = None) -> BaseCheckpointSaver:
    """Get the appropriate checkpointer based on database configuration.

    For PostgreSQL connections, returns an AsyncPostgresSaver that persists checkpoints
    to the database, enabling agent interrupt/resume patterns with full async support.

    For SQLite connections (typically tests), returns a MemorySaver for fast
    in-memory checkpointing without database overhead.

    Args:
        engine: SQLAlchemy engine to inspect. If None, uses the default engine
                from zerg.database.

    Returns:
        A checkpointer instance (AsyncPostgresSaver or MemorySaver)

    Note:
        AsyncPostgresSaver instances are cached by connection URL to avoid repeated
        setup calls. The checkpointer automatically creates required tables
        (checkpoints, checkpoint_writes) on first use.
    """
    if engine is None:
        from zerg.database import default_engine

        engine = default_engine

    # Some tests pass a lightweight mock Engine; be defensive about URL handling.
    try:
        db_url = str(engine.url.render_as_string(hide_password=False))  # type: ignore[union-attr]
    except Exception:
        db_url = str(getattr(engine, "url", ""))

    # For SQLite databases, use MemorySaver (tests, local dev)
    if "sqlite" in db_url.lower():
        logger.debug("Using MemorySaver for SQLite database")
        return MemorySaver()

    # For PostgreSQL, use AsyncPostgresSaver with durable checkpoints
    if "postgresql" in db_url.lower():
        # Check cache to avoid repeated setup
        if db_url in _postgres_checkpointer_cache:
            logger.debug("Returning cached AsyncPostgresSaver instance")
            return _postgres_checkpointer_cache[db_url]

        masked_url = engine.url.render_as_string(hide_password=True)
        logger.info(f"Initializing AsyncPostgresSaver for PostgreSQL database: {masked_url}")

        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            from psycopg_pool import AsyncConnectionPool

            # Create pool and checkpointer in the background loop context
            # This ensures the pool is associated with a running event loop
            try:
                pool, checkpointer = _create_async_checkpointer_in_bg_loop(db_url)
                logger.info("AsyncPostgresSaver initialized successfully")
            except Exception as setup_error:
                logger.error(f"Failed to setup AsyncPostgresSaver: {setup_error}")
                # Fall back to MemorySaver if setup fails
                logger.warning("Falling back to MemorySaver due to setup failure")
                return MemorySaver()

            # Cache the initialized checkpointer and pool
            _postgres_checkpointer_cache[db_url] = checkpointer
            _async_postgres_pool_cache[db_url] = pool
            return checkpointer

        except ImportError as e:
            logger.error(
                f"langgraph-checkpoint-postgres or psycopg_pool not installed: {e}. "
                "Install with: uv add langgraph-checkpoint-postgres psycopg-pool"
            )
            logger.warning("Falling back to MemorySaver")
            return MemorySaver()

        except Exception as e:
            logger.error(f"Failed to initialize AsyncPostgresSaver: {e}")
            logger.warning("Falling back to MemorySaver")
            return MemorySaver()

    # Fallback for unknown database types
    logger.warning(f"Unknown database type in URL: {db_url}. Using MemorySaver")
    return MemorySaver()


def clear_checkpointer_cache():
    """Clear the AsyncPostgresSaver cache.

    This is primarily useful for testing scenarios where you need to
    reinitialize the checkpointer with different configuration.
    """
    global _postgres_checkpointer_cache, _async_postgres_pool_cache

    # Close any open connection pools using the background loop
    for pool in _async_postgres_pool_cache.values():
        try:
            bg_loop = _get_or_create_bg_loop()
            future = asyncio.run_coroutine_threadsafe(pool.close(), bg_loop)
            future.result(timeout=5.0)
        except Exception:
            pass

    _postgres_checkpointer_cache.clear()
    _async_postgres_pool_cache.clear()
    logger.debug("Checkpointer cache cleared")
