import logging
from datetime import datetime
from datetime import timezone
from enum import Enum
from typing import Literal
from typing import Optional
from uuid import UUID
from uuid import uuid4

# FastAPI helpers
from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

# Centralised settings
from zerg.config import get_settings

# Database helpers
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


@router.post("/reset-database")
async def reset_database(
    request: DatabaseResetRequest,
    current_user=Depends(require_super_admin),
):
    """Reset the database by dropping all tables and recreating them.

    Requires super admin privileges (user must be in ADMIN_EMAILS).
    In production environments, requires additional password confirmation.
    """
    settings = get_settings()
    if settings.testing and settings.environment == "test:e2e":
        from zerg.services.catalogd_supervisor import get_catalogd_client
        from zerg.services.search_v2_projector import start_search_v2_projector
        from zerg.services.search_v2_projector import stop_search_v2_projector
        from zerg.services.searchd_supervisor import get_searchd_projector_client
        from zerg.services.semantic_v2_projector import start_semantic_v2_projector
        from zerg.services.semantic_v2_projector import stop_semantic_v2_projector
        from zerg.services.session_pubsub import reset_pubsub_for_test

        catalog = get_catalogd_client()
        search = get_searchd_projector_client()
        if catalog is None or search is None:
            raise HTTPException(status_code=503, detail="E2E catalog or search owner is unavailable")
        await stop_search_v2_projector()
        await stop_semantic_v2_projector()
        try:
            catalog_result = await catalog.call("test.user_data.reset.v2", {}, timeout_seconds=5.0)
            search_result = await search.call("test.user_data.reset.v2", {}, timeout_seconds=5.0)
        finally:
            semantic_started = start_semantic_v2_projector()
            search_started = start_search_v2_projector()
        if not semantic_started or not search_started:
            raise HTTPException(status_code=503, detail="E2E projectors did not restart after reset")
        reset_pubsub_for_test()
        return {
            "message": "E2E catalog and search data cleared successfully",
            "operation": "clear_data",
            "catalog": catalog_result,
            "search": search_result,
        }
    # Run synchronously so the HTTP response reflects a completed commit.
    return _reset_database_sync(request, current_user)


def _reset_database_sync(request: DatabaseResetRequest, current_user):
    raise HTTPException(status_code=503, detail="Archive reset is unavailable while storage is isolated")


@router.get("/provider-capabilities")
def get_provider_capabilities() -> dict[str, object]:
    """Cookie-authenticated mirror of GET /agents/provider-capabilities
    (docs/specs/provider-factory-coherence.md, Phase 5 UI). Same projection,
    same code path -- the device-token machine surface and this admin page
    read identical data, they just authenticate differently."""
    from zerg.routers.provider_capability_proofs import build_capability_projection_payload

    return build_capability_projection_payload()


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
    raise HTTPException(status_code=503, detail="Archive schema repair is unavailable while storage is isolated")


# ---------------------------------------------------------------------------
# Test Configuration Endpoints (E2E testing only)
# ---------------------------------------------------------------------------


class ConfigureTestSessionRuntimeRequest(BaseModel):
    """Test-only session runtime override for Playwright coverage."""

    provider: Literal["claude", "codex", "cursor", "opencode", "antigravity", "pi"] = "codex"
    project: str = "e2e"
    cwd: str = "/tmp"
    execution_home: Literal["managed_local"] = "managed_local"
    managed_transport: Optional[
        Literal[
            "claude_channel_bridge",
            "codex_app_server",
            "opencode_server_bridge",
            "opencode_process",
            "antigravity_hook_inbox",
            "antigravity_process",
        ]
    ] = None
    source_runner_id: Optional[int] = None
    source_runner_name: Optional[str] = None
    managed_session_name: Optional[str] = None
    observed_at: Optional[datetime] = None


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

    from zerg.database import get_test_worker_id

    # Capture current DB url/path (use session factory bind to reflect worker routing)
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
        "worker_id": get_test_worker_id(),
        "db_url": db_url,
        "db_path": db_path,
    }


@router.post("/test/sessions/{session_id}/runtime")
async def configure_test_session_runtime(
    session_id: str,
    request: ConfigureTestSessionRuntimeRequest,
    current_user=Depends(require_admin),
):
    """Materialize managed runtime facts through the canonical E2E catalog."""
    settings = get_settings()
    if not settings.testing or settings.environment != "test:e2e":
        raise HTTPException(status_code=403, detail="This endpoint is available only in test:e2e.")

    try:
        session_id = str(UUID(session_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="session_id must be a valid UUID") from exc

    from datetime import timedelta

    from zerg.services.catalogd_supervisor import get_catalogd_client

    observed_at = request.observed_at or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    managed_transport = request.managed_transport or "codex_app_server"
    source_runner_name = request.source_runner_name or "E2E Runner"
    managed_session_name = request.managed_session_name or f"e2e-{session_id[:8]}"
    owner_id = int(current_user.id)
    client = get_catalogd_client()
    if client is None:
        raise HTTPException(status_code=503, detail="E2E catalog is unavailable")
    launch = await client.call(
        "session.launch.local.create.v2",
        {
            "launch": {
                "owner_id": owner_id,
                "git_repo": None,
                "git_branch": None,
                "started_at": observed_at.isoformat(),
                "expires_at": (observed_at + timedelta(minutes=5)).isoformat(),
                "plan": {
                    "session_id": session_id,
                    "provider": request.provider,
                    "provider_session_id": str(uuid4()),
                    "source_name": source_runner_name,
                    "source_runner_id": request.source_runner_id,
                    "cwd": request.cwd,
                    "project": request.project,
                    "display_name": "Browser E2E managed session",
                    "managed_session_name": managed_session_name,
                    "loop_mode": "assist",
                    "permission_mode": "bypass",
                    "launch_actor": "human_shell",
                    "launch_surface": "terminal",
                    "environment": "test:e2e",
                    "origin_kind": "helm",
                    "hidden_from_default_timeline": 0,
                    "managed_transport": managed_transport,
                    "attach_command": "",
                    "provider_config": {},
                },
            }
        },
        timeout_seconds=5.0,
    )
    run_id = str(launch["run_id"])
    await client.call(
        "session.launch.local.finish.v2",
        {
            "outcome": {
                "session_id": session_id,
                "run_id": run_id,
                "owner_id": owner_id,
                "device_id": source_runner_name,
                "state": "adopted",
                "error_code": None,
                "error_message": None,
                "observed_at": (observed_at + timedelta(seconds=1)).isoformat(),
            }
        },
        timeout_seconds=5.0,
    )

    return {
        "session_id": session_id,
        "execution_home": "managed_local",
        "managed_transport": managed_transport,
        "source_runner_id": request.source_runner_id,
        "source_runner_name": source_runner_name,
        "managed_session_name": managed_session_name,
        "ended_at": None,
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
    - Top automations by cost for the specified period

    Admin-only endpoint.
    """
    result = get_user_usage_detail(db, user_id, period)
    if result is None:
        raise HTTPException(status_code=404, detail="User not found")
    return result
