"""Machine-facing directory and health summaries.

``/agents/machines`` is the per-owner directory of enrolled machines and
their current control-channel status; it is the launch-sheet data source.
``/agents/machines/health`` is the richer observability view used by
operator dashboards.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.device_token import DeviceToken
from zerg.schemas.machines import MachineDirectoryEntry
from zerg.schemas.machines import MachineDirectoryResponse
from zerg.schemas.observability import MachineHealthListResponse
from zerg.schemas.observability import MachineHealthStatus
from zerg.services.agent_heartbeat_health import DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS
from zerg.services.agent_heartbeat_health import list_machine_transport_health
from zerg.services.machines_directory import build_machines_directory
from zerg.services.observability_views import build_machine_health_list_response
from zerg.services.session_chat_impl import _resolve_agents_owner_id

router = APIRouter(prefix="/agents/machines", tags=["agents"])


@router.get("", response_model=MachineDirectoryResponse)
async def list_machines(
    db: Session = Depends(get_db),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> MachineDirectoryResponse:
    """List enrolled machines for this owner with live control-channel status."""
    owner_id = _resolve_agents_owner_id(db, device_token)
    entries = build_machines_directory(db, owner_id=owner_id)
    return MachineDirectoryResponse(machines=[MachineDirectoryEntry(**entry.to_response()) for entry in entries])


@router.get("/health", response_model=MachineHealthListResponse)
async def list_machine_health(
    device_id: str | None = Query(None, description="Filter to one device"),
    status: MachineHealthStatus | None = Query(None, description="Filter by derived machine transport state"),
    limit: int = Query(20, ge=1, le=100, description="Max machine rows to return"),
    stale_after_seconds: int = Query(
        DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS,
        ge=60,
        le=24 * 60 * 60,
        description="Treat heartbeats older than this as offline",
    ),
    db: Session = Depends(get_db),
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> MachineHealthListResponse:
    summaries, total = list_machine_transport_health(
        db,
        device_id=device_id,
        status=status,
        stale_after_seconds=stale_after_seconds,
        limit=limit,
    )
    return build_machine_health_list_response(summaries, total=total)
