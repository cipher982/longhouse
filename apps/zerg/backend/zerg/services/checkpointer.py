"""Checkpointer factory for LangGraph agent state persistence.

This module provides a factory function that returns the appropriate checkpointer
based on the database configuration:
- PostgresSaver for PostgreSQL (production) - enables durable checkpoints
- MemorySaver for SQLite (tests/dev) - fast in-memory checkpoints

The factory handles database detection, connection pooling, and async initialization.
"""

import logging
from typing import Union

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import Engine

logger = logging.getLogger(__name__)

# Global cache for initialized PostgresSaver instances
_postgres_checkpointer_cache: dict[str, BaseCheckpointSaver] = {}


def get_checkpointer(engine: Engine = None) -> BaseCheckpointSaver:
    """Get the appropriate checkpointer based on database configuration.

    For PostgreSQL connections, returns a PostgresSaver that persists checkpoints
    to the database, enabling agent interrupt/resume patterns.

    For SQLite connections (typically tests), returns a MemorySaver for fast
    in-memory checkpointing without database overhead.

    Args:
        engine: SQLAlchemy engine to inspect. If None, uses the default engine
                from zerg.database.

    Returns:
        A checkpointer instance (PostgresSaver or MemorySaver)

    Note:
        PostgresSaver instances are cached by connection URL to avoid repeated
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

    # For PostgreSQL, use PostgresSaver with durable checkpoints
    if "postgresql" in db_url.lower():
        # Check cache to avoid repeated setup
        if db_url in _postgres_checkpointer_cache:
            logger.debug("Returning cached PostgresSaver instance")
            return _postgres_checkpointer_cache[db_url]

        masked_url = engine.url.render_as_string(hide_password=True)
        logger.info(f"Initializing PostgresSaver for PostgreSQL database: {masked_url}")

        try:
            from langgraph.checkpoint.postgres import PostgresSaver

            # Create PostgresSaver with connection string
            # Note: PostgresSaver.from_conn_string() returns a context manager
            # in newer versions (3.0+). We need to enter it to get the actual instance.
            try:
                # Get the context manager and enter it to get the checkpointer instance
                context_manager = PostgresSaver.from_conn_string(db_url)
                checkpointer = context_manager.__enter__()
                logger.info("PostgresSaver initialized successfully")
            except Exception as setup_error:
                logger.error(f"Failed to setup PostgresSaver tables: {setup_error}")
                # Fall back to MemorySaver if setup fails
                logger.warning("Falling back to MemorySaver due to setup failure")
                return MemorySaver()

            # Cache the initialized checkpointer
            _postgres_checkpointer_cache[db_url] = checkpointer
            return checkpointer

        except ImportError:
            logger.error(
                "langgraph-checkpoint-postgres not installed. "
                "Install with: uv add langgraph-checkpoint-postgres"
            )
            logger.warning("Falling back to MemorySaver")
            return MemorySaver()

        except Exception as e:
            logger.error(f"Failed to initialize PostgresSaver: {e}")
            logger.warning("Falling back to MemorySaver")
            return MemorySaver()

    # Fallback for unknown database types
    logger.warning(f"Unknown database type in URL: {db_url}. Using MemorySaver")
    return MemorySaver()


def clear_checkpointer_cache():
    """Clear the PostgresSaver cache.

    This is primarily useful for testing scenarios where you need to
    reinitialize the checkpointer with different configuration.
    """
    global _postgres_checkpointer_cache
    _postgres_checkpointer_cache.clear()
    logger.debug("Checkpointer cache cleared")
