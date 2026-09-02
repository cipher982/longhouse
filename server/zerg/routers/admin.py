import logging
from datetime import datetime
from datetime import timezone
from typing import Literal
from typing import Optional
from uuid import UUID
from uuid import uuid4

# FastAPI helpers
from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from pydantic import BaseModel

# Centralised settings
from zerg.config import get_settings

# Auth dependency
from zerg.dependencies.auth import get_current_user
from zerg.dependencies.auth import require_admin

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(get_current_user), Depends(require_admin)],
)

logger = logging.getLogger(__name__)


class SuperAdminStatusResponse(BaseModel):
    """Response model for super admin status check."""

    is_super_admin: bool


@router.get("/super-admin-status")
async def get_super_admin_status(current_user=Depends(get_current_user)) -> SuperAdminStatusResponse:
    """Check whether the current user is a super admin (listed in ADMIN_EMAILS)."""
    settings = get_settings()

    if getattr(current_user, "role", "USER") != "ADMIN":
        return SuperAdminStatusResponse(is_super_admin=False)

    admin_emails = {e.strip().lower() for e in (settings.admin_emails or "").split(",") if e.strip()}
    user_email = getattr(current_user, "email", "").lower()
    return SuperAdminStatusResponse(is_super_admin=user_email in admin_emails)


@router.post("/reset-database")
async def reset_database():
    """Clear the E2E catalog and search stores between Playwright runs.

    This is a test fixture, not an operator tool: no deployment outside
    ``test:e2e`` has a reset path, so every other environment is refused.
    """
    settings = get_settings()
    if not (settings.testing and settings.environment == "test:e2e"):
        raise HTTPException(status_code=403, detail="This endpoint is available only in test:e2e.")
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


@router.get("/provider-capabilities")
def get_provider_capabilities() -> dict[str, object]:
    """Cookie-authenticated mirror of GET /agents/provider-capabilities.

    Same projection, same code path -- the device-token machine surface and
    this admin page read identical data, they just authenticate differently.
    """
    from zerg.routers.provider_capability_proofs import build_capability_projection_payload

    return build_capability_projection_payload()


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
