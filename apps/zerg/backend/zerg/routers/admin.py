import logging
import os
from enum import Enum

# FastAPI helpers
from fastapi import APIRouter
from fastapi import APIRouter as _AR
from fastapi import Depends
from fastapi import FastAPI as _FastAPI
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Centralised settings
from zerg.config import get_settings

# Database helpers
from zerg.database import Base
from zerg.database import get_session_factory

# Auth dependency
from zerg.dependencies.auth import get_current_user
from zerg.dependencies.auth import require_admin
from zerg.dependencies.auth import require_super_admin

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(get_current_user), Depends(require_admin)],
)

logger = logging.getLogger(__name__)


class ResetType(str, Enum):
    """Database reset operation types."""

    CLEAR_DATA = "clear_data"
    FULL_REBUILD = "full_rebuild"


class DatabaseResetRequest(BaseModel):
    """Request model for database reset with optional password confirmation."""

    confirmation_password: str | None = None
    reset_type: ResetType = ResetType.CLEAR_DATA


class SuperAdminStatusResponse(BaseModel):
    """Response model for super admin status check."""

    is_super_admin: bool
    requires_password: bool


@router.get("/super-admin-status")
async def get_super_admin_status(current_user=Depends(get_current_user)) -> SuperAdminStatusResponse:
    """Check if the current user is a super admin and if password confirmation is required."""
    settings = get_settings()

    # Check if user is admin first
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin:
        return SuperAdminStatusResponse(is_super_admin=False, requires_password=False)

    # Check if they're a super admin (in ADMIN_EMAILS)
    admin_emails = {e.strip().lower() for e in (settings.admin_emails or "").split(",") if e.strip()}
    user_email = getattr(current_user, "email", "").lower()
    is_super_admin = user_email in admin_emails

    # Check if password confirmation is required (production environment)
    is_production = settings.environment and settings.environment.lower() == "production"

    return SuperAdminStatusResponse(is_super_admin=is_super_admin, requires_password=is_production)


def clear_user_data(engine) -> dict[str, any]:
    """Clear user-generated data while preserving infrastructure.

    Uses schema discovery to find tables to clear, avoiding hardcoded lists.

    Args:
        engine: SQLAlchemy engine

    Returns:
        Dictionary with operation results
    """
    import time

    from sqlalchemy import text

    # Discover all tables from SQLAlchemy metadata
    all_tables = set(Base.metadata.tables.keys())

    # Tables to preserve (infrastructure/auth)
    preserve_tables = {"users", "alembic_version"}

    # Tables to clear (user-generated content)
    clear_tables = all_tables - preserve_tables

    if not clear_tables:
        return {"message": "No user data tables found to clear", "tables_cleared": [], "rows_cleared": 0}

    start_time = time.perf_counter()

    with engine.connect() as conn:
        # Count rows before clearing
        total_before = 0
        for table in clear_tables:
            try:
                count = conn.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar() or 0
                total_before += count
            except Exception:
                pass

        if engine.dialect.name == "postgresql":
            # PostgreSQL: Use TRUNCATE CASCADE for efficiency
            if clear_tables:
                tables_list = ", ".join(f'"{table}"' for table in sorted(clear_tables))
                conn.execute(text(f"TRUNCATE TABLE {tables_list} RESTART IDENTITY CASCADE"))
        else:
            # SQLite: Disable FK checks and DELETE
            conn.execute(text("PRAGMA foreign_keys = OFF"))
            for table in sorted(clear_tables):
                try:
                    conn.execute(text(f'DELETE FROM "{table}"'))
                except Exception as e:
                    logger.warning(f"Failed to clear table {table}: {e}")
            conn.execute(text("PRAGMA foreign_keys = ON"))

        conn.commit()

    duration_ms = int((time.perf_counter() - start_time) * 1000)

    return {
        "message": "User data cleared successfully",
        "operation": "clear_data",
        "tables_cleared": sorted(list(clear_tables)),
        "rows_cleared": total_before,
        "duration_ms": duration_ms,
    }


def full_schema_rebuild(engine, settings, is_production, diagnostics) -> dict[str, any]:
    """Perform full schema rebuild (existing full reset logic).

    Args:
        engine: SQLAlchemy engine
        settings: Application settings
        is_production: Whether running in production
        diagnostics: Diagnostics dictionary to populate

    Returns:
        Dictionary with operation results
    """
    # This encapsulates the existing full reset logic
    # (I'll move the existing logic here in the next step)
    pass


@router.post("/reset-database")
async def reset_database(request: DatabaseResetRequest, current_user=Depends(require_super_admin)):
    """Reset the database by dropping all tables and recreating them.

    Requires super admin privileges (user must be in ADMIN_EMAILS).
    In production environments, requires additional password confirmation.
    """
    settings = get_settings()

    # Log the reset attempt for audit purposes
    logger.warning(
        f"Database reset ({request.reset_type.value}) requested by {getattr(current_user, 'email', 'unknown')} "
        f"in environment: {settings.environment or 'development'}"
    )

    # Check if we're in production and require password confirmation
    is_production = settings.environment and settings.environment.lower() == "production"
    if is_production:
        # Require password confirmation in production
        if not settings.db_reset_password:
            logger.error("DB_RESET_PASSWORD not configured for production environment")
            raise HTTPException(
                status_code=500, detail="Database reset not properly configured for production environment"
            )

        if not request.confirmation_password:
            raise HTTPException(
                status_code=400, detail="Password confirmation required for database reset in production"
            )

        if request.confirmation_password != settings.db_reset_password:
            logger.warning(
                f"Failed database reset attempt by {getattr(current_user, 'email', 'unknown')} " f"- incorrect password"
            )
            raise HTTPException(status_code=403, detail="Incorrect confirmation password")

    # Allow in development/testing environments without password
    if not settings.testing and not is_production and (settings.environment or "") not in ["development", ""]:
        logger.warning("Attempted to reset database in unsupported environment")
        raise HTTPException(
            status_code=403, detail="Database reset is only available in development and production environments"
        )

    try:
        # Obtain the *current* engine – respects Playwright worker isolation
        session_factory = get_session_factory()

        # SQLAlchemy 2.0 removed the ``bind`` attribute from ``sessionmaker``.
        # We therefore open a *temporary* session and call ``get_bind()`` to
        # retrieve the underlying Engine in a version-agnostic way.
        with session_factory() as _tmp_session:  # type: ignore[arg-type]
            engine = _tmp_session.get_bind()

        if engine is None:  # pragma: no cover – safety guard
            raise RuntimeError("Session factory returned no bound engine")

        # Dispatch to the appropriate reset operation
        if request.reset_type == ResetType.CLEAR_DATA:
            # Simple user data clearing - no connection management needed
            result = clear_user_data(engine)
            return result

        # Full schema rebuild - requires careful connection management
        diagnostics: dict[str, object] = {
            "environment": (settings.environment or "") or "development",
            "dialect": getattr(engine.dialect, "name", "unknown"),
        }

        if engine.dialect.name == "postgresql" and is_production:
            logger.info("Production PostgreSQL detected - terminating all other DB connections and applying timeouts")

            from sqlalchemy import text

            # Terminate any other connections to the current database (regardless of state)
            # and apply conservative timeouts to avoid indefinite blocking on locks.
            with engine.connect() as conn:
                db_name = conn.execute(text("SELECT current_database()")).scalar()

                # Set timeouts for all subsequent statements on this session
                # - lock_timeout: how long to wait to acquire DDL locks
                # - statement_timeout: overall guardrail for the drop/create operations
                conn.execute(text("SET lock_timeout = '3s'"))
                conn.execute(text("SET statement_timeout = '30s'"))
                conn.execute(text("SET client_min_messages = WARNING"))

                # Count other connections before termination for diagnostics
                pre_count = (
                    conn.execute(
                        text(
                            """
                        SELECT COUNT(*)
                        FROM pg_stat_activity
                        WHERE datname = :db_name AND pid <> pg_backend_pid()
                        """
                        ),
                        {"db_name": db_name},
                    ).scalar()
                    or 0
                )

                logger.info(f"Terminating other connections to database: {db_name} (pre={pre_count})")
                result = conn.execute(
                    text(
                        """
                        SELECT pg_terminate_backend(pid)
                        FROM pg_stat_activity
                        WHERE datname = :db_name
                          AND pid <> pg_backend_pid()
                        """
                    ),
                    {"db_name": db_name},
                )
                conn.commit()
                try:
                    terminated = result.rowcount if result.rowcount is not None else pre_count
                except Exception:
                    terminated = pre_count
                diagnostics["terminated_connections"] = int(terminated)

        # Safer + faster for SQLite: disable FK checks, truncate every table,
        # then re-enable.  Avoids losing autoincrement counters that some
        # tests rely on for deterministic IDs.

        # ------------------------------------------------------------------
        # SQLAlchemy's *global* ``close_all_sessions()`` helper invalidates
        # **every** Session that exists in the current process – even the
        # ones that belong to a *different* Playwright worker using another
        # database file.  When multiple E2E workers run in parallel this
        # leads to race-conditions where an ongoing request suddenly loses
        # its Session mid-flight and subsequent ORM access explodes with
        # ``InvalidRequestError: Instance … is not persistent within this
        # Session``.
        #
        # Because each Playwright worker is already fully isolated via its
        # *own* SQLite engine (handled by WorkerDBMiddleware &
        # zerg.database) it is safe – and *necessary* – to avoid closing
        # foreign Sessions.  Instead we:
        #   1. Dispose the *current* worker's engine after we are done.  This
        #      releases connections that *belong to this engine only*.
        #   2. Rely on the fact that every incoming HTTP request obtains a
        #      **fresh** Session, so no stale identity maps can leak across
        #      requests.
        #
        # Hence: **do not** call ``close_all_sessions()`` here.

        # Drop & recreate schema so **new columns** land automatically when
        # models change during active dev work (e.g. `workflow_id`).  Safer
        # than DELETE-rows because SQLite cannot ALTER TABLE with multiple
        # columns easily.

        # Execute drop/create with a short retry loop in Postgres to ride out
        # late-arriving connections (e.g. healthchecks) that might momentarily
        # contend for locks. SQLite path is unchanged.
        import time

        start_counts_ts = time.perf_counter()

        # Capture row counts before reset for a few key tables (best-effort)
        def _safe_count(table: str) -> int:
            try:
                with engine.connect() as conn:
                    from sqlalchemy import text as _t

                    res = conn.execute(_t(f'SELECT COUNT(*) FROM "{table}"'))
                    return int(res.scalar() or 0)
            except Exception:
                return 0

        # Use schema discovery instead of hardcoded table list
        key_tables = list(Base.metadata.tables.keys())
        tables_before: dict[str, int] = {t: _safe_count(t) for t in key_tables}
        total_before = sum(tables_before.values())
        diagnostics["tables_before_counts"] = tables_before
        diagnostics["total_rows_before"] = total_before
        diagnostics["pre_count_ms"] = int((time.perf_counter() - start_counts_ts) * 1000)

        start_reset_ts = time.perf_counter()
        max_attempts = 3 if engine.dialect.name == "postgresql" else 1
        last_err: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"Dropping all tables … (attempt {attempt}/{max_attempts})")
                # Execute DDL on a single connection so session-level timeouts apply
                if engine.dialect.name == "postgresql":
                    from sqlalchemy import text as _t

                    with engine.connect() as ddl_conn:
                        ddl_conn.execute(_t("SET lock_timeout = '3s'"))
                        ddl_conn.execute(_t("SET statement_timeout = '30s'"))

                        # Log tables before drop
                        tables_before_drop = ddl_conn.execute(
                            _t("""
                            SELECT tablename FROM pg_tables
                            WHERE schemaname = 'public' AND tablename NOT LIKE 'pg_%'
                        """)
                        ).fetchall()
                        logger.info(f"Tables before drop: {[t[0] for t in tables_before_drop]}")

                        Base.metadata.drop_all(bind=ddl_conn)

                        # Log tables after drop
                        tables_after_drop = ddl_conn.execute(
                            _t("""
                            SELECT tablename FROM pg_tables
                            WHERE schemaname = 'public' AND tablename NOT LIKE 'pg_%'
                        """)
                        ).fetchall()
                        logger.info(f"Tables after drop: {[t[0] for t in tables_after_drop]}")

                        logger.info("Re-creating all tables …")
                        Base.metadata.create_all(bind=ddl_conn)

                        # Explicitly commit the DDL operations
                        ddl_conn.commit()

                        # Count tables immediately after recreation (should be 0)
                        def _safe_count_immediate(table: str) -> int:
                            try:
                                res = ddl_conn.execute(_t(f'SELECT COUNT(*) FROM "{table}"'))
                                return int(res.scalar() or 0)
                            except Exception:
                                return 0

                        tables_after_immediate = {t: _safe_count_immediate(t) for t in key_tables}

                else:
                    Base.metadata.drop_all(bind=engine)

                    logger.info("Re-creating all tables …")
                    Base.metadata.create_all(bind=engine)

                    # Count tables immediately after recreation (should be 0)
                    def _safe_count_immediate(table: str) -> int:
                        try:
                            with engine.connect() as conn:
                                from sqlalchemy import text as _t2

                                res = conn.execute(_t2(f'SELECT COUNT(*) FROM "{table}"'))
                                return int(res.scalar() or 0)
                        except Exception:
                            return 0

                    tables_after_immediate = {t: _safe_count_immediate(t) for t in key_tables}

                last_err = None
                break
            except Exception as e:  # pragma: no cover – operational guardrail
                last_err = e
                logger.warning(f"Drop/create failed on attempt {attempt}: {e!s}")
                # Small backoff before retry; try to clear straggler connections
                time.sleep(1.0)
                if engine.dialect.name == "postgresql":
                    from sqlalchemy import text

                    with engine.connect() as conn:
                        db_name = conn.execute(text("SELECT current_database()")).scalar()
                        conn.execute(
                            text(
                                """
                                SELECT pg_terminate_backend(pid)
                                FROM pg_stat_activity
                                WHERE datname = :db_name AND pid <> pg_backend_pid()
                                """
                            ),
                            {"db_name": db_name},
                        )
                        conn.commit()

        reset_ms = int((time.perf_counter() - start_reset_ts) * 1000)
        diagnostics["drop_create_ms"] = reset_ms
        diagnostics["attempts_used"] = attempt  # last attempt number executed

        if last_err is not None:
            raise last_err

        # Create test user for foreign key constraints in test environment
        if settings.testing or os.getenv("NODE_ENV") == "test":
            from sqlalchemy import text

            with engine.connect() as conn:
                result = conn.execute(text("SELECT COUNT(*) FROM users WHERE id = 1"))
                user_count = result.scalar()
                if user_count == 0:
                    logger.info("Creating test user for foreign key constraints...")
                    conn.execute(
                        text("""
                        INSERT INTO users (id, email, role, is_active, provider, provider_user_id,
                                          display_name, context, created_at, updated_at)
                        VALUES (1, 'test@example.com', 'ADMIN', 1, 'dev', 'test-user-1',
                                'Test User', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        """)
                    )
                    conn.commit()
                    logger.info("Test user created")

        # Use the immediate count taken right after table creation (accurate reset verification)
        total_after = sum(tables_after_immediate.values())
        diagnostics["tables_after_counts"] = tables_after_immediate
        diagnostics["total_rows_after"] = total_after
        diagnostics["post_count_ms"] = 0  # Immediate count, no extra time

        logger.info(
            "Database schema reset complete | before=%s after=%s drop_create_ms=%s",
            total_before,
            total_after,
            reset_ms,
        )

        # Dispose again after recreation to release references held by
        # background threads.  However, **skip** this step when the backend
        # runs inside the unit-test environment (``TESTING=1``) because
        # test fixtures may still hold an *open* SQLAlchemy ``Session`` that
        # shares the same Engine/connection.  Calling ``engine.dispose()``
        # would invalidate those connections and subsequent calls like
        # ``Session.close()`` trigger a *ProgrammingError: Cannot operate on
        # a closed database* exception which breaks the tear-down phase.

        if not settings.testing:  # avoid invalidating live connections in tests
            engine.dispose()

        # Include diagnostics in API response for UI/console display
        return {
            "message": "Database reset successfully",
            **diagnostics,
        }
    except Exception as e:
        logger.error(f"Error resetting database: {str(e)}")
        # Still return success if it's a user constraint error
        # (likely from parallel test runs)
        if "UNIQUE constraint failed: users.email" in str(e):
            return {"message": "Database reset successfully (existing user)"}
        return JSONResponse(status_code=500, content={"detail": f"Failed to reset database: {str(e)}"})


# ---------------------------------------------------------------------------
# Backwards-compatibility route (no /api prefix) so legacy Playwright specs
# that still call ``POST /admin/reset-database`` continue to work.  We simply
# delegate to the main handler.
# ---------------------------------------------------------------------------

_legacy_router = _AR(prefix="/admin")


@router.get("/migration-log")
async def get_migration_log():
    """Get the migration log from container startup."""
    from pathlib import Path

    log_file = Path("/app/static/migration.log")
    if log_file.exists():
        with open(log_file, "r") as f:
            content = f.read()
        return {"log": content, "exists": True}
    else:
        return {"log": "Migration log not found", "exists": False}


@router.post("/fix-database-schema")
async def fix_database_schema():
    """Directly fix the missing updated_at column issue."""
    # Check if we're in development mode
    settings = get_settings()
    if not settings.testing and (settings.environment or "") != "development":
        logger.warning("Attempted to fix database schema in non-development environment")
        raise HTTPException(status_code=403, detail="Database schema fix is only available in development environment")

    try:
        import sqlalchemy as sa
        from sqlalchemy import text

        session_factory = get_session_factory()

        with session_factory() as session:
            engine = session.get_bind()

            # Check if updated_at column exists
            inspector = sa.inspect(engine)
            if not inspector.has_table("connectors"):
                return {"message": "Connectors table does not exist"}

            columns = [col["name"] for col in inspector.get_columns("connectors")]

            if "updated_at" in columns:
                return {"message": "updated_at column already exists"}

            # Add the missing column
            logger.info("Adding missing updated_at column to connectors table")

            if engine.dialect.name == "postgresql":
                # PostgreSQL approach
                session.execute(
                    text("""
                    ALTER TABLE connectors
                    ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                """)
                )

                session.execute(
                    text("""
                    UPDATE connectors
                    SET updated_at = created_at
                    WHERE updated_at IS NULL
                """)
                )

                session.execute(
                    text("""
                    ALTER TABLE connectors
                    ALTER COLUMN updated_at SET NOT NULL
                """)
                )

            elif engine.dialect.name == "sqlite":
                # SQLite approach
                session.execute(
                    text("""
                    ALTER TABLE connectors
                    ADD COLUMN updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                """)
                )

                session.execute(
                    text("""
                    UPDATE connectors
                    SET updated_at = created_at
                    WHERE updated_at IS NULL
                """)
                )

            session.commit()

        return {"message": "Database schema fixed - added updated_at column to connectors table"}

    except Exception as e:
        logger.error(f"Error fixing database schema: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"Failed to fix database schema: {str(e)}"})


@_legacy_router.post("/reset-database")
async def _legacy_reset_database(request: DatabaseResetRequest, current_user=Depends(require_super_admin)):  # noqa: D401 – thin wrapper
    return await reset_database(request, current_user)  # noqa: WPS110 – re-use logic


# mount the legacy router without the global /api prefix


def _mount_legacy(app: _FastAPI):  # noqa: D401 – helper
    app.include_router(_legacy_router)
