"""
Postgres schema management for E2E test isolation.
Each Playwright commis gets its own schema with full table isolation.
"""

import logging
import zlib

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

SCHEMA_PREFIX = "e2e_commis_"


def get_schema_name(commis_id: str) -> str:
    """Generate schema name for a commis.

    Commis IDs are mapped 1:1 to schemas. This is the safest mental model:
    each Playwright commis (or test-only identifier like ``guardrail_a``) gets
    its own schema and should never collide with others.
    """
    # Sanitize commis_id to prevent SQL injection
    safe_id = "".join(c for c in str(commis_id) if c.isalnum() or c == "_")
    return f"{SCHEMA_PREFIX}{safe_id}"


def recreate_commis_schema(engine: Engine, commis_id: str) -> str:
    """
    Force-recreate schema for a commis with fresh state.

    Uses Postgres advisory locks to prevent race conditions when multiple
    Uvicorn commis initialize schemas concurrently.

    CRITICAL: Always DROP then CREATE to ensure clean state.

    NOTE: This function is DEPRECATED for runtime use. Use ensure_commis_schema()
    instead to avoid DROP+CREATE races. This is only used for globalSetup cleanup.
    """
    schema_name = get_schema_name(commis_id)

    # Generate deterministic lock ID from schema name
    lock_id = zlib.crc32(f"init_schema_{schema_name}".encode())

    # Import Base first to ensure models are registered

    from zerg.database import DB_SCHEMA
    from zerg.database import Base

    with engine.begin() as conn:
        # Advisory lock prevents race between Uvicorn commis
        conn.execute(text(f"SELECT pg_advisory_xact_lock({lock_id})"))

        # Force fresh state - always DROP then CREATE
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {schema_name}"))

        # Create all tables in the commis schema.
        #
        # IMPORTANT: We use schema_translate_map so SQLAlchemy performs both the
        # "does this table exist?" checks and the CREATE TABLE statements in the
        # *target* schema, not the database's default schema (usually public).
        ddl_conn = conn.execution_options(schema_translate_map={DB_SCHEMA: schema_name})
        Base.metadata.create_all(bind=ddl_conn, checkfirst=False)

    logger.debug(f"Recreated schema with fresh state: {schema_name}")
    return schema_name


def ensure_commis_schema(engine: Engine, commis_id: str) -> str:
    """
    Idempotent schema creation. Never DROP during test execution.

    Safe for concurrent uvicorn commis and forward-compatible with migrations.
    Uses CREATE SCHEMA IF NOT EXISTS + create_all(checkfirst=True) to be
    idempotent across multiple processes.

    This is the preferred function for runtime use. Unlike recreate_commis_schema(),
    it won't DROP a schema that another process might be using.

    See: docs/work/e2e-test-infrastructure-redesign.md
    """
    schema_name = get_schema_name(commis_id)

    # Generate deterministic lock ID from schema name
    lock_id = zlib.crc32(f"ensure_schema_{schema_name}".encode())

    from zerg.database import DB_SCHEMA
    from zerg.database import Base

    with engine.begin() as conn:
        # Advisory lock prevents race conditions
        conn.execute(text(f"SELECT pg_advisory_xact_lock({lock_id})"))

        # Create schema if not exists (idempotent)
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema_name}"))

        # Create all tables (checkfirst=True is idempotent and handles migrations).
        #
        # IMPORTANT: schema_translate_map ensures SQLAlchemy checks/creates tables
        # in *this schema* rather than accidentally treating "public" as the
        # default schema and skipping creation because tables exist there.
        ddl_conn = conn.execution_options(schema_translate_map={DB_SCHEMA: schema_name})
        Base.metadata.create_all(bind=ddl_conn, checkfirst=True)

    logger.debug(f"Ensured schema exists: {schema_name}")
    return schema_name


def drop_schema(engine: Engine, commis_id: str) -> None:
    """Drop a commis's schema and all its contents."""
    schema_name = get_schema_name(commis_id)

    with engine.connect() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE"))
        conn.commit()

    logger.debug(f"Dropped schema: {schema_name}")


def drop_all_e2e_schemas(engine: Engine) -> int:
    """Drop all E2E test schemas. Returns count of schemas dropped.

    Drops each schema in a separate transaction to avoid exceeding
    max_locks_per_transaction when there are many schemas with many tables.
    """
    with engine.connect() as conn:
        result = conn.execute(
            text("""
            SELECT schema_name
            FROM information_schema.schemata
            WHERE schema_name LIKE 'e2e_commis_%'
        """)
        )
        schemas = [row[0] for row in result]
        conn.commit()  # Commit the SELECT before starting drops

    # Drop each schema in a separate transaction to avoid lock exhaustion
    for schema in schemas:
        with engine.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
            conn.commit()

    # Keep at INFO - this is rare and useful for debugging E2E infra issues
    logger.info(f"Dropped {len(schemas)} E2E schemas")
    return len(schemas)


def set_search_path(conn, commis_id: str) -> None:
    """Set search_path for a connection to use commis's schema."""
    schema_name = get_schema_name(commis_id)
    conn.execute(text(f"SET search_path TO {schema_name}, public"))
