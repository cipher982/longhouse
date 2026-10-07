"""Machine-facing directory and health summaries.

``/agents/machines`` is the per-owner directory of enrolled machines and
their current control-channel status; it is the launch-sheet data source.
``/agents/machines/summary`` joins that directory with per-machine activity
and sync, the read model behind the Machines surface. ``/agents/machines/health``
is the raw shipping-transport view.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from sqlalchemy.orm import Session

from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_caller
from zerg.dependencies.request_db import no_request_db
from zerg.models.device_token import DeviceToken
from zerg.schemas.machines import ArchiveBacklogControlRequest
from zerg.schemas.machines import ArchiveBacklogControlResponse
from zerg.schemas.machines import ArchiveBacklogResponse
from zerg.schemas.machines import MachineDirectoryEntry
from zerg.schemas.machines import MachineDirectoryResponse
from zerg.schemas.machines import MachineRenameRequest
from zerg.schemas.machines import MachineRenameResponse
from zerg.schemas.machines import MachinesSummaryResponse
from zerg.schemas.machines import RecentModel
from zerg.schemas.machines import RecentModelsResponse
from zerg.schemas.machines import WorkspaceSuggestion
from zerg.schemas.machines import WorkspaceSuggestionsResponse
from zerg.schemas.observability import MachineHealthListResponse
from zerg.schemas.observability import MachineHealthStatus
from zerg.services.agent_heartbeat_health import DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS
from zerg.services.agent_heartbeat_health import machine_transport_health_from_catalog_rows
from zerg.services.catalog_read_gateway import CatalogReadError
from zerg.services.catalog_read_gateway import active_owner_id
from zerg.services.catalog_read_gateway import enrolled_machines
from zerg.services.catalog_read_gateway import machine_heartbeats
from zerg.services.catalog_read_gateway import machine_models
from zerg.services.catalog_read_gateway import machine_workspaces
from zerg.services.catalog_read_gateway import rename_machine
from zerg.services.machine_control_channel import get_machine_control_channel_registry
from zerg.services.machines_directory import build_machines_directory
from zerg.services.machines_summary import build_machines_summary
from zerg.services.observability_views import build_machine_health_list_response
from zerg.services.session_chat_impl import _resolve_agents_owner_id

router = APIRouter(prefix="/agents/machines", tags=["agents"])

ARCHIVE_BACKLOG_CONTROL_COMMAND = "archive.backlog_control"
ARCHIVE_BACKLOG_CONTROL_COMMAND_V2 = "archive.backlog_control.v2"


def _request_owner_id(db: Session | None, device_token: DeviceToken | None) -> int:
    owner_id = getattr(device_token, "owner_id", None)
    if owner_id is not None:
        return int(owner_id)
    if db is not None:
        return _resolve_agents_owner_id(db, device_token)
    owner_id = active_owner_id()
    if owner_id is None:
        raise CatalogReadError("owner_unavailable", "No active Longhouse owner is configured.")
    return owner_id


def archive_backlog_control_command_type(mode: str) -> str:
    """Require lease-aware engines before enabling repair work."""
    return ARCHIVE_BACKLOG_CONTROL_COMMAND if mode == "paused" else ARCHIVE_BACKLOG_CONTROL_COMMAND_V2


@router.get("", response_model=MachineDirectoryResponse)
def list_machines(
    db: Session | None = Depends(no_request_db),
    device_token: DeviceToken | None = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
) -> MachineDirectoryResponse:
    """List enrolled machines for this owner with live control-channel status."""
    try:
        owner_id = _request_owner_id(db, device_token)
        enrollments = enrolled_machines(owner_id).get("enrollments", [])
    except CatalogReadError as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": exc.message}) from exc
    entries = build_machines_directory(owner_id=owner_id, enrollments=enrollments)
    return MachineDirectoryResponse(machines=[MachineDirectoryEntry(**entry.to_response()) for entry in entries])


@router.get("/summary", response_model=MachinesSummaryResponse)
def list_machine_summaries(
    days: int = Query(14, ge=1, le=30, description="Activity window in local calendar days, today included"),
    utc_offset_minutes: int = Query(0, ge=-840, le=840, description="Caller's local offset east of UTC, for day buckets"),
    db: Session | None = Depends(no_request_db),
    device_token: DeviceToken | None = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
) -> MachinesSummaryResponse:
    """Directory, activity and sync for every enrolled machine."""
    try:
        return build_machines_summary(owner_id=_request_owner_id(db, device_token), days=days, utc_offset_minutes=utc_offset_minutes)
    except CatalogReadError as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": exc.message}) from exc


@router.patch("/{device_id}", response_model=MachineRenameResponse)
async def update_machine_name(
    device_id: str,
    request: MachineRenameRequest,
    db: Session | None = Depends(no_request_db),
    device_token: DeviceToken | None = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
) -> MachineRenameResponse:
    """Rename one enrolled machine without changing its routing identity."""
    owner_id = _request_owner_id(db, device_token)
    machine_name = request.machine_name.strip()
    try:
        result = await asyncio.to_thread(
            rename_machine,
            owner_id=owner_id,
            device_id=device_id,
            machine_name=machine_name,
        )
    except CatalogReadError as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": exc.message}) from exc
    if result.get("found") is not True:
        raise HTTPException(status_code=404, detail="Machine not found")
    return MachineRenameResponse(device_id=device_id, machine_name=machine_name, changed=result.get("changed") is True)


@router.get("/health", response_model=MachineHealthListResponse)
def list_machine_health(
    device_id: str | None = Query(None, description="Filter to one device"),
    status: MachineHealthStatus | None = Query(None, description="Filter by derived machine transport state"),
    limit: int = Query(20, ge=1, le=100, description="Max machine rows to return"),
    stale_after_seconds: int = Query(
        DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS,
        ge=60,
        le=24 * 60 * 60,
        description="Treat heartbeats older than this as offline",
    ),
    db: Session | None = Depends(no_request_db),
    device_token: DeviceToken | None = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
) -> MachineHealthListResponse:
    try:
        payload = machine_heartbeats(
            owner_id=_request_owner_id(db, device_token),
            device_id=device_id,
            recent_after=None,
            limit=100,
        )
    except CatalogReadError as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": exc.message}) from exc
    summaries, total = machine_transport_health_from_catalog_rows(
        payload.get("heartbeats", []),
        status=status,
        stale_after_seconds=stale_after_seconds,
        limit=limit,
    )
    return build_machine_health_list_response(summaries, total=total)


@router.get("/{device_id}/workspaces", response_model=WorkspaceSuggestionsResponse)
def list_machine_workspaces(
    device_id: str,
    limit: int = Query(12, ge=1, le=50, description="Max ranked workspaces to return"),
    days_back: int = Query(45, ge=1, le=180, description="Lookback window for recent sessions"),
    db: Session | None = Depends(no_request_db),
    device_token: DeviceToken | None = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
) -> WorkspaceSuggestionsResponse:
    """Frecency-ranked recent workspaces for the launch picker, scoped to one machine."""
    try:
        owner_id = _request_owner_id(db, device_token)
        payload = machine_workspaces(
            owner_id=owner_id,
            device_id=device_id,
            limit=limit,
            days_back=days_back,
        )
    except CatalogReadError as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": exc.message}) from exc
    entries = [WorkspaceSuggestion(**item) for item in payload.get("workspaces", [])]
    return WorkspaceSuggestionsResponse(device_id=device_id, workspaces=entries)


@router.get("/{device_id}/providers/{provider}/models", response_model=RecentModelsResponse)
def list_machine_models(
    device_id: str,
    provider: str,
    limit: int = Query(12, ge=1, le=50, description="Max recent models to return"),
    days_back: int = Query(45, ge=1, le=180, description="Lookback window for recent sessions"),
    db: Session | None = Depends(no_request_db),
    device_token: DeviceToken | None = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
) -> RecentModelsResponse:
    """Recent provider-reported model ids for one enrolled machine."""
    try:
        owner_id = _request_owner_id(db, device_token)
        payload = machine_models(
            owner_id=owner_id,
            device_id=device_id,
            provider=provider,
            limit=limit,
            days_back=days_back,
        )
    except CatalogReadError as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": exc.message}) from exc
    return RecentModelsResponse(
        device_id=device_id,
        provider=provider,
        days_back=days_back,
        models=[RecentModel(**item) for item in payload.get("models", [])],
    )


@router.get("/{device_id}/archive-backlog", response_model=ArchiveBacklogResponse)
def get_machine_archive_backlog(
    device_id: str,
    db: Session | None = Depends(no_request_db),
    device_token: DeviceToken | None = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
) -> ArchiveBacklogResponse:
    try:
        payload = machine_heartbeats(
            owner_id=_request_owner_id(db, device_token),
            device_id=device_id,
            recent_after=None,
            limit=1,
        )
    except CatalogReadError as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": exc.message}) from exc
    summaries, _total = machine_transport_health_from_catalog_rows(
        payload.get("heartbeats", []),
        limit=1,
    )
    if not summaries:
        raise HTTPException(status_code=404, detail="Machine heartbeat not found")
    return ArchiveBacklogResponse(
        device_id=device_id,
        archive_repair=summaries[0].archive_repair,
    )


@router.post("/{device_id}/archive-backlog/control", response_model=ArchiveBacklogControlResponse)
async def control_machine_archive_backlog(
    device_id: str,
    request: ArchiveBacklogControlRequest,
    db: Session | None = Depends(no_request_db),
    device_token: DeviceToken | None = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
) -> ArchiveBacklogControlResponse:
    owner_id = _request_owner_id(db, device_token)
    registry = get_machine_control_channel_registry()
    info = registry.info(owner_id=owner_id, device_id=device_id)
    if info is None:
        raise HTTPException(status_code=503, detail="Machine Agent control channel is offline")
    command_type = archive_backlog_control_command_type(request.mode)
    if not registry.supports(owner_id=owner_id, device_id=device_id, capability=command_type):
        raise HTTPException(status_code=409, detail="Machine Agent does not advertise archive backlog control")

    payload = request.model_dump(exclude_none=True)
    payload.pop("timeout_secs", None)
    command = await registry.send_command(
        owner_id=owner_id,
        device_id=device_id,
        session_id=None,
        command_type=command_type,
        payload=payload,
        timeout_secs=request.timeout_secs or 15,
    )
    if not command.transport_ok:
        raise HTTPException(status_code=503, detail=command.error or "Machine control command failed")
    message = dict(command.message or {})
    if not message.get("ok"):
        error = message.get("error") if isinstance(message.get("error"), dict) else {}
        raise HTTPException(
            status_code=502,
            detail=error.get("message") or "Machine Agent archive backlog control failed",
        )
    return ArchiveBacklogControlResponse(
        device_id=device_id,
        command_id=str(message.get("command_id") or ""),
        result=dict(message.get("result") or {}),
    )
