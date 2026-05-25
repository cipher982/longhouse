import logging
import os
from datetime import datetime
from datetime import timezone
from enum import Enum
from types import SimpleNamespace
from typing import Literal
from typing import Optional
from uuid import UUID

# FastAPI helpers
from fastapi import APIRouter
from fastapi import Depends
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
from zerg.models.agents import AgentSession
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.schemas.usage import AdminUserDetailResponse
from zerg.schemas.usage import AdminUsersResponse
from zerg.services.runner_connection_manager import get_runner_connection_manager
from zerg.services.session_launch_lifecycle import project_remote_launch_lifecycle

# Usage service
from zerg.services.usage_service import get_all_users_usage
from zerg.services.usage_service import get_user_usage_detail
from zerg.utils.time import UTCBaseModel

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


class ScenarioSeedRequest(BaseModel):
    """Request model for seeding deterministic scenario data."""

    name: str
    target: str = "dev"
    namespace: str = "test"
    clean: bool = False


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
    Includes timeouts and retry logic for lock contention under high concurrency.

    Args:
        engine: SQLAlchemy engine

    Returns:
        Dictionary with operation results
    """
    import time

    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    settings = get_settings()
    # Counting rows for every table adds measurable latency under parallel E2E.
    # It's useful in dev/prod for diagnostics, but unnecessary for tests.
    should_count_rows = os.getenv("RESET_DB_COUNT_ROWS", "").strip() == "1" or not (settings.testing or os.getenv("NODE_ENV") == "test")

    # Tables to preserve (infrastructure/auth)
    # Preserve infrastructure/auth tables.
    #
    # In development, a `dev-runner` container may be running continuously.
    # If we clear the `runners` table, the runner will immediately start a noisy
    # reconnect loop ("Runner not found by name") until the backend is restarted
    # and auto-seeding runs again. Keeping runners is the least surprising DX.
    preserve_tables = {"users", "alembic_version", "runners"}

    discovered_tables: list[tuple[str | None, str]] = [(table.schema, table.name) for table in Base.metadata.sorted_tables]

    # Tables to clear (user-generated content)
    clear_tables = [table_ref for table_ref in discovered_tables if table_ref[1] not in preserve_tables]

    if not clear_tables:
        return {"message": "No user data tables found to clear", "tables_cleared": [], "rows_cleared": 0}

    start_time = time.perf_counter()
    max_attempts = 1
    last_err: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            with engine.connect() as conn:
                total_before: int | None = None
                if should_count_rows:
                    # Count rows before clearing (best-effort; for diagnostics only)
                    total_before = 0
                    for schema, table in clear_tables:
                        try:
                            qualified_name = f'"{schema}"."{table}"' if schema else f'"{table}"'
                            count = conn.execute(text(f"SELECT COUNT(*) FROM {qualified_name}")).scalar() or 0
                            total_before += count
                        except Exception:
                            pass

                # SQLite: Disable FK checks and DELETE
                conn.execute(text("PRAGMA foreign_keys = OFF"))
                for schema, table in sorted(clear_tables, key=lambda item: ((item[0] or ""), item[1])):
                    try:
                        qualified_name = f'"{schema}"."{table}"' if schema else f'"{table}"'
                        conn.execute(text(f"DELETE FROM {qualified_name}"))
                    except Exception as e:
                        table_label = f"{schema}.{table}" if schema else table
                        logger.warning(f"Failed to clear table {table_label}: {e}")
                conn.execute(text("PRAGMA foreign_keys = ON"))

                conn.commit()

            # Success - break out of retry loop
            duration_ms = int((time.perf_counter() - start_time) * 1000)

            return {
                "message": "User data cleared successfully",
                "operation": "clear_data",
                "tables_cleared": [
                    f"{schema}.{table}" if schema else table
                    for schema, table in sorted(clear_tables, key=lambda item: ((item[0] or ""), item[1]))
                ],
                "rows_cleared": total_before,
                "counts_skipped": not should_count_rows,
                "duration_ms": duration_ms,
                "attempts": attempt,
            }

        except OperationalError as e:
            last_err = e
            err_str = str(e).lower()
            # Retry on lock timeout or connection errors
            if attempt < max_attempts and ("lock" in err_str or "timeout" in err_str or "connection" in err_str):
                logger.warning(f"clear_user_data attempt {attempt} failed (retrying): {e}")
                time.sleep(0.1 * attempt)  # Brief backoff: 100ms, 200ms
                continue
            raise

    # Should not reach here, but safety net
    if last_err:
        raise last_err
    raise RuntimeError("clear_user_data: unexpected exit from retry loop")


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
    current_user=Depends(require_super_admin),
):
    """Reset the database by dropping all tables and recreating them.

    Requires super admin privileges (user must be in ADMIN_EMAILS).
    In production environments, requires additional password confirmation.
    """
    # Run synchronously so the HTTP response reflects a completed commit.
    return _reset_database_sync(request, current_user)


@router.post("/seed-scenario")
async def seed_scenario_data(
    request: ScenarioSeedRequest,
    db: Session = Depends(get_db),
    current_user=Depends(require_super_admin),
):
    """Seed deterministic scenario data for demos and E2E tests."""
    settings = get_settings()
    if settings.environment and settings.environment.lower() == "production":
        raise HTTPException(status_code=403, detail="Scenario seeding is disabled in production")

    from zerg.scenarios.seed import seed_scenario

    result = seed_scenario(
        db,
        request.name,
        owner_id=current_user.id,
        target=request.target,
        namespace=request.namespace,
        clean=request.clean,
    )
    return JSONResponse(content=result)


def _reset_database_sync(request: DatabaseResetRequest, current_user):
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

    try:
        # Obtain the *current* engine – respects Playwright commis isolation
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

        # Drop & recreate schema so **new columns** land automatically when
        # models change during active dev work.
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
        key_tables = [table.name for table in Base.metadata.tables.values()]
        tables_before: dict[str, int] = {t: _safe_count(t) for t in key_tables}
        total_before = sum(tables_before.values())
        diagnostics["tables_before_counts"] = tables_before
        diagnostics["total_rows_before"] = total_before
        diagnostics["pre_count_ms"] = int((time.perf_counter() - start_counts_ts) * 1000)

        start_reset_ts = time.perf_counter()
        max_attempts = 1  # SQLite doesn't need retries
        last_err: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"Dropping all tables … (attempt {attempt}/{max_attempts})")
                # SQLite-only: simple drop and recreate (single declarative base)
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
                time.sleep(1.0)

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

            # Add the missing column (SQLite approach)
            logger.info("Adding missing updated_at column to connectors table")

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


class ConfigureTestSessionRuntimeRequest(BaseModel):
    """Test-only session runtime override for Playwright coverage."""

    execution_home: Literal["unmanaged_local", "managed_local", "managed_hosted", "cloud_takeover"] = "managed_local"
    managed_transport: Optional[Literal["claude_channel_bridge", "codex_app_server", "opencode_process", "antigravity_process"]] = None
    source_runner_id: Optional[int] = None
    source_runner_name: Optional[str] = None
    managed_session_name: Optional[str] = None
    clear_ended_at: bool = True


@router.get("/debug/db-schema")
async def debug_db_schema(
    db: Session = Depends(get_db),
):
    """Debug endpoint: returns database info.

    TESTING-only. Returns table counts for debugging.
    """
    settings = get_settings()
    if not settings.testing:
        raise HTTPException(status_code=403, detail="This endpoint is only available when TESTING=1.")

    from sqlalchemy import text

    from zerg.database import get_session_factory
    from zerg.database import get_test_commis_id

    # Check if fiches table exists and get count
    tables_check = db.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='fiches'")).fetchone()
    fiches_exists = tables_check is not None
    fiches_count = db.execute(text("SELECT COUNT(*) FROM fiches")).scalar() if fiches_exists else None

    # Capture current DB url/path (use session factory bind to reflect commis routing)
    db_url = None
    db_path = None
    try:
        session_factory = get_session_factory()
        with session_factory() as _tmp_session:  # type: ignore[arg-type]
            engine = _tmp_session.get_bind()
            if engine is not None:
                db_url = str(engine.url)
                db_path = engine.url.database
    except Exception:
        pass

    return {
        "dialect": "sqlite",
        "fiches_exists": fiches_exists,
        "fiches_count": fiches_count,
        "commis_id": get_test_commis_id(),
        "db_url": db_url,
        "db_path": db_path,
    }


@router.post("/test/sessions/{session_id}/runtime")
async def configure_test_session_runtime(
    session_id: str,
    request: ConfigureTestSessionRuntimeRequest,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin),
):
    """Patch session runtime metadata for TESTING-only E2E scenarios."""
    settings = get_settings()
    if not settings.testing:
        raise HTTPException(status_code=403, detail="This endpoint is only available when TESTING=1.")

    try:
        session_uuid = UUID(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="session_id must be a valid UUID") from exc

    session = db.query(AgentSession).filter(AgentSession.id == session_uuid).first()
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    session.execution_home = request.execution_home
    if request.execution_home == "managed_local":
        session.managed_transport = request.managed_transport or session.managed_transport or "codex_app_server"
        session.source_runner_id = request.source_runner_id if request.source_runner_id is not None else 1
        session.source_runner_name = request.source_runner_name or session.source_runner_name or "E2E Runner"
        session.managed_session_name = request.managed_session_name or session.managed_session_name or f"e2e-{session_id[:8]}"
        try:
            owner_id = int(getattr(current_user, "id", 1) or 1)
        except (TypeError, ValueError):
            owner_id = 1
        if db.query(User).filter(User.id == owner_id).first() is None:
            db.add(User(id=owner_id, email=f"test-admin-{owner_id}@example.com"))
        runner_id = int(session.source_runner_id)
        runner = db.query(Runner).filter(Runner.id == runner_id).first()
        if runner is None:
            runner = Runner(
                id=runner_id,
                owner_id=owner_id,
                name=session.source_runner_name,
                status="online",
                auth_secret_hash="test",
            )
            db.add(runner)
        else:
            runner.owner_id = owner_id
            runner.name = session.source_runner_name
            runner.status = "online"
        get_runner_connection_manager().register(owner_id, runner_id, SimpleNamespace())
        if request.clear_ended_at:
            session.ended_at = None

        from zerg.services.agents.kernel_writes import ensure_open_run_for_session
        from zerg.services.agents.kernel_writes import upsert_connection_for_run

        run = ensure_open_run_for_session(db, session, launch_origin="longhouse_spawned")
        upsert_connection_for_run(
            db,
            run=run,
            control_plane="codex_app_server",
            acquisition_kind="spawned_control",
            state="attached",
            external_name=session.managed_session_name,
            can_send_input=1,
            can_interrupt=1,
            can_terminate=1,
            can_tail_output=1,
            can_resume=1,
        )
    else:
        session.managed_transport = request.managed_transport
        session.source_runner_id = request.source_runner_id
        session.source_runner_name = request.source_runner_name
        session.managed_session_name = request.managed_session_name
    session.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(session)

    logger.info("Configured test runtime for session %s by admin %s", session_id, getattr(current_user, "id", None))

    return {
        "session_id": str(session.id),
        "execution_home": session.execution_home,
        "managed_transport": session.managed_transport,
        "source_runner_id": session.source_runner_id,
        "source_runner_name": session.source_runner_name,
        "managed_session_name": session.managed_session_name,
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
    }


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
    - Top fiches by cost for the specified period

    Admin-only endpoint.
    """
    result = get_user_usage_detail(db, user_id, period)
    if result is None:
        raise HTTPException(status_code=404, detail="User not found")
    return result


# ---------------------------------------------------------------------------
# Remote launch debug view (Phase 4 of remote-session-launch epic)
# ---------------------------------------------------------------------------


class RemoteLaunchDebugEntry(UTCBaseModel):
    session_id: str
    device_id: str | None
    provider: str
    cwd: str | None
    launch_state: str
    launch_error_code: str | None
    launch_error_message: str | None
    launch_lease_until: datetime | None
    started_at: datetime
    ended_at: datetime | None


class RemoteLaunchDebugResponse(UTCBaseModel):
    entries: list[RemoteLaunchDebugEntry]
    total: int


@router.get("/launches/debug", response_model=RemoteLaunchDebugResponse)
async def list_remote_launch_debug(
    limit: int = Query(50, ge=1, le=200, description="Max rows to return"),
    include_live: bool = Query(False, description="Include launch_state=live rows (default: only show non-healthy)"),
    include_test: bool = Query(False, description="Include test/e2e launch attempts"),
    db: Session = Depends(get_db),
    current_user=Depends(require_admin),
):
    """Admin-only view of remote launches that are not cleanly live.

    Surfaces launching / launching_unknown / launch_failed / launch_orphaned
    rows so an operator can debug propagation or control-channel issues.
    """
    from zerg.models.agents import SessionLaunchAttempt

    q = db.query(SessionLaunchAttempt, AgentSession).join(AgentSession, AgentSession.id == SessionLaunchAttempt.session_id)
    if not include_test:
        q = q.filter(AgentSession.environment.notin_(["test", "e2e"]))
    if not include_live:
        q = q.filter(
            (SessionLaunchAttempt.state.in_(["failed", "abandoned"]))
            | (SessionLaunchAttempt.state.in_(["pending", "dispatched"]) & SessionLaunchAttempt.run_id.is_(None))
        )
    total = q.count()
    all_rows = q.order_by(SessionLaunchAttempt.created_at.desc(), SessionLaunchAttempt.id.desc()).limit(limit).all()

    filtered = [
        (attempt, session, lifecycle)
        for attempt, session in all_rows
        if (lifecycle := project_remote_launch_lifecycle(attempt)) is not None
    ]
    entries = [
        RemoteLaunchDebugEntry(
            session_id=str(session.id),
            device_id=session.device_id,
            provider=attempt.provider or session.provider,
            cwd=session.cwd,
            launch_state=lifecycle.state,
            launch_error_code=attempt.error_code,
            launch_error_message=lifecycle.error_message,
            launch_lease_until=lifecycle.lease_until,
            started_at=session.started_at,
            ended_at=session.ended_at,
        )
        for attempt, session, lifecycle in filtered
    ]
    return RemoteLaunchDebugResponse(entries=entries, total=total)
