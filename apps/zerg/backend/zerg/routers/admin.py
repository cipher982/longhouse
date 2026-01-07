import logging
import os
from enum import Enum
from typing import Literal
from typing import Optional

# FastAPI helpers
from fastapi import APIRouter
from fastapi import APIRouter as _AR
from fastapi import Depends
from fastapi import FastAPI as _FastAPI
from fastapi import Header
from fastapi import HTTPException
from fastapi import Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

# Centralised settings
from zerg.config import get_settings

# Database helpers
from zerg.database import Base
from zerg.database import get_db
from zerg.database import get_session_factory

# Auth dependency
from zerg.dependencies.auth import get_current_user
from zerg.dependencies.auth import require_admin
from zerg.dependencies.auth import require_super_admin
from zerg.schemas.usage import AdminUserDetailResponse
from zerg.schemas.usage import AdminUsersResponse

# Usage service
from zerg.services.usage_service import get_all_users_usage
from zerg.services.usage_service import get_user_usage_detail

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(get_current_user), Depends(require_admin)],
)

logger = logging.getLogger(__name__)

try:
    # When E2E_USE_POSTGRES_SCHEMAS=1, WorkerDBMiddleware populates this
    # contextvar so zerg.database.get_session_factory can route to the correct
    # worker schema. We explicitly set it inside threadpool work to avoid
    # relying on contextvar propagation implementation details.
    from zerg.middleware.worker_db import current_worker_id as _current_worker_id
except Exception:  # pragma: no cover - middleware not present in some contexts
    _current_worker_id = None  # type: ignore[assignment]


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
    Includes timeouts to prevent hanging if SSE connections hold locks.

    Args:
        engine: SQLAlchemy engine

    Returns:
        Dictionary with operation results
    """
    import time

    from sqlalchemy import text

    settings = get_settings()
    # Counting rows for every table adds measurable latency under parallel E2E.
    # It's useful in dev/prod for diagnostics, but unnecessary for tests.
    should_count_rows = os.getenv("RESET_DB_COUNT_ROWS", "").strip() == "1" or not (
        settings.testing or settings.e2e_use_postgres_schemas or os.getenv("NODE_ENV") == "test"
    )

    # Discover all tables from SQLAlchemy metadata
    all_tables = set(Base.metadata.tables.keys())

    # Tables to preserve (infrastructure/auth)
    # Preserve infrastructure/auth tables.
    #
    # In development, a `dev-runner` container may be running continuously.
    # If we clear the `runners` table, the runner will immediately start a noisy
    # reconnect loop ("Runner not found by name") until the backend is restarted
    # and auto-seeding runs again. Keeping runners is the least surprising DX.
    preserve_tables = {"users", "alembic_version", "runners"}

    # Tables to clear (user-generated content)
    clear_tables = all_tables - preserve_tables

    if not clear_tables:
        return {"message": "No user data tables found to clear", "tables_cleared": [], "rows_cleared": 0}

    start_time = time.perf_counter()

    with engine.connect() as conn:
        # CRITICAL: Set timeouts to fail fast if locks can't be acquired
        # This prevents hanging when SSE connections hold transactions open
        if engine.dialect.name == "postgresql":
            conn.execute(text("SET lock_timeout = '5s'"))
            conn.execute(text("SET statement_timeout = '30s'"))

        total_before: int | None = None
        if should_count_rows:
            # Count rows before clearing (best-effort; for diagnostics only)
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
        "counts_skipped": not should_count_rows,
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
async def reset_database(
    request: DatabaseResetRequest,
    x_test_worker: str | None = Header(default=None, alias="X-Test-Worker"),
    current_user=Depends(require_super_admin),
):
    """Reset the database by dropping all tables and recreating them.

    Requires super admin privileges (user must be in ADMIN_EMAILS).
    In production environments, requires additional password confirmation.
    """
    # NOTE: This endpoint intentionally runs synchronously (no threadpool).
    # In E2E, 16 workers will all hit this endpoint at once; serializing the
    # TRUNCATE avoids Postgres lock thrash and statement_timeouts.
    return _reset_database_sync(request, current_user, x_test_worker)


def _reset_database_sync(request: DatabaseResetRequest, current_user, worker_id: str | None):
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
            raise HTTPException(status_code=500, detail="Database reset not properly configured for production environment")

        if not request.confirmation_password:
            raise HTTPException(status_code=400, detail="Password confirmation required for database reset in production")

        if request.confirmation_password != settings.db_reset_password:
            logger.warning(f"Failed database reset attempt by {getattr(current_user, 'email', 'unknown')} - incorrect password")
            raise HTTPException(status_code=403, detail="Incorrect confirmation password")

    # Allow in development/testing environments without password
    if not settings.testing and not is_production and (settings.environment or "") not in ["development", ""]:
        logger.warning("Attempted to reset database in unsupported environment")
        raise HTTPException(status_code=403, detail="Database reset is only available in development and production environments")

    token = None
    if _current_worker_id is not None and worker_id is not None:
        token = _current_worker_id.set(worker_id)

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
    finally:
        if token is not None and _current_worker_id is not None:
            _current_worker_id.reset(token)


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


# ---------------------------------------------------------------------------
# Test Configuration Endpoints (E2E testing only)
# ---------------------------------------------------------------------------


class ConfigureTestModelRequest(BaseModel):
    """Request model for configuring test model."""

    model: str = "gpt-scripted"


@router.get("/debug/db-schema")
async def debug_db_schema(
    db: Session = Depends(get_db),
    x_test_worker: str | None = Header(default=None, alias="X-Test-Worker"),
):
    """Debug endpoint: returns current_schema + search_path for this request.

    TESTING-only. Useful to validate Postgres schema routing (X-Test-Worker).
    """
    settings = get_settings()
    if not settings.testing:
        raise HTTPException(status_code=403, detail="This endpoint is only available when TESTING=1.")

    from sqlalchemy import text

    current_schema = db.execute(text("SELECT current_schema()")).scalar()
    search_path = db.execute(text("SHOW search_path")).scalar()
    agents_unqualified = db.execute(text("SELECT to_regclass('agents')")).scalar()
    agents_public = db.execute(text("SELECT to_regclass('public.agents')")).scalar()
    agents_count = db.execute(text("SELECT COUNT(*) FROM agents")).scalar()
    agents_public_count = db.execute(text("SELECT COUNT(*) FROM public.agents")).scalar()
    return {
        "current_schema": current_schema,
        "search_path": search_path,
        "agents_unqualified": agents_unqualified,
        "agents_public": agents_public,
        "agents_count": agents_count,
        "agents_public_count": agents_public_count,
        "x_test_worker": x_test_worker,
    }


@router.post("/configure-test-model")
async def configure_test_model(
    request: ConfigureTestModelRequest,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin),
):
    """Configure the supervisor agent to use a test model.

    This is a TEST-ONLY endpoint for E2E tests that need deterministic LLM behavior.
    Only available when TESTING=1 is set.

    Args:
        request: Contains the model to use (default: gpt-scripted)

    Returns:
        Success message with agent ID
    """
    settings = get_settings()

    # CRITICAL: Only allow when testing mode is enabled
    # This ensures test models can never be configured in production
    if not settings.testing:
        raise HTTPException(
            status_code=403,
            detail="Test model configuration requires TESTING=1. This endpoint is not available in production.",
        )

    # Valid test models
    valid_test_models = {"gpt-mock", "gpt-scripted"}
    if request.model not in valid_test_models:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid test model: {request.model}. Valid options: {valid_test_models}",
        )

    try:
        from zerg.services.supervisor_service import SupervisorService

        supervisor_service = SupervisorService(db)
        agent = supervisor_service.get_or_create_supervisor_agent(current_user.id)

        # Update agent model
        agent.model = request.model
        db.commit()

        logger.info(f"Configured supervisor agent {agent.id} to use model: {request.model}")

        return {
            "message": f"Supervisor agent configured to use {request.model}",
            "agent_id": agent.id,
            "model": request.model,
        }
    except Exception as e:
        logger.error(f"Error configuring test model: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to configure test model: {str(e)}") from e


# ---------------------------------------------------------------------------
# Admin User Usage Endpoints (Phase 2)
# ---------------------------------------------------------------------------


@router.get("/users", response_model=AdminUsersResponse)
async def list_users_with_usage(
    sort: Literal["cost_today", "cost_7d", "cost_30d", "email", "created_at"] = Query(
        "cost_today",
        description="Sort field: cost_today, cost_7d, cost_30d, email, created_at",
    ),
    order: Literal["asc", "desc"] = Query("desc", description="Sort order: asc or desc"),
    limit: int = Query(50, ge=1, le=200, description="Max results"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    active: Optional[bool] = Query(None, description="Filter by active status (true/false). Omit for all users."),
    db: Session = Depends(get_db),
    current_user=Depends(require_admin),
):
    """List all users with their LLM usage statistics.

    Returns users sorted by the specified field with usage stats for today, 7d, and 30d.
    Admin-only endpoint.
    """
    return get_all_users_usage(db, sort=sort, order=order, limit=limit, offset=offset, active=active)


@router.get("/users/{user_id}/usage", response_model=AdminUserDetailResponse)
async def get_user_usage_details(
    user_id: int,
    period: Literal["today", "7d", "30d"] = Query("7d", description="Period for daily breakdown"),
    db: Session = Depends(get_db),
    current_user=Depends(require_admin),
):
    """Get detailed LLM usage for a specific user.

    Returns:
    - User info with usage summary for all periods
    - Daily breakdown for the specified period
    - Top agents by cost for the specified period

    Admin-only endpoint.
    """
    result = get_user_usage_detail(db, user_id, period)
    if result is None:
        raise HTTPException(status_code=404, detail="User not found")
    return result


@_legacy_router.post("/reset-database")
async def _legacy_reset_database(
    request: DatabaseResetRequest,
    x_test_worker: str | None = Header(default=None, alias="X-Test-Worker"),
    current_user=Depends(require_super_admin),
):  # noqa: D401 – thin wrapper
    return _reset_database_sync(request, current_user, x_test_worker)  # noqa: WPS110 – re-use logic


# mount the legacy router without the global /api prefix


def _mount_legacy(app: _FastAPI):  # noqa: D401 – helper
    app.include_router(_legacy_router)
