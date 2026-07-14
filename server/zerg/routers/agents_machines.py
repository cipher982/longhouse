"""Machine-facing directory and health summaries.

``/agents/machines`` is the per-owner directory of enrolled machines and
their current control-channel status; it is the launch-sheet data source.
``/agents/machines/health`` is the richer observability view used by
operator dashboards.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from sqlalchemy.orm import Session

import zerg.database as database_module
from zerg.database import catalog_db_dependency
from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.device_token import DeviceToken
from zerg.schemas.machines import ArchiveBacklogControlRequest
from zerg.schemas.machines import ArchiveBacklogControlResponse
from zerg.schemas.machines import ArchiveBacklogResponse
from zerg.schemas.machines import MachineControlOperationResponse
from zerg.schemas.machines import MachineDirectoryEntry
from zerg.schemas.machines import MachineDirectoryResponse
from zerg.schemas.machines import MachineRenameRequest
from zerg.schemas.machines import MachineRenameResponse
from zerg.schemas.machines import ProviderLiveProofAcceptedResponse
from zerg.schemas.machines import ProviderLiveProofRequest
from zerg.schemas.machines import WorkspaceSuggestion
from zerg.schemas.machines import WorkspaceSuggestionsResponse
from zerg.schemas.observability import MachineHealthListResponse
from zerg.schemas.observability import MachineHealthStatus
from zerg.services.agent_heartbeat_health import DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS
from zerg.services.agent_heartbeat_health import list_machine_transport_health
from zerg.services.catalog_read_gateway import CatalogReadError
from zerg.services.catalog_read_gateway import active_owner_id
from zerg.services.catalog_read_gateway import enrolled_machines
from zerg.services.catalog_read_gateway import machine_operation
from zerg.services.catalog_read_gateway import machine_workspaces
from zerg.services.catalog_read_gateway import rename_machine
from zerg.services.machine_control_channel import get_machine_control_channel_registry
from zerg.services.machine_control_operations import ActiveMachineControlOperationError
from zerg.services.machine_control_operations import create_live_provider_live_proof_operation
from zerg.services.machine_control_operations import create_provider_live_proof_operation
from zerg.services.machine_control_operations import fail_live_machine_control_operation
from zerg.services.machine_control_operations import fail_machine_control_operation
from zerg.services.machine_control_operations import get_live_machine_control_operation_for_owner
from zerg.services.machine_control_operations import get_machine_control_operation_for_owner
from zerg.services.machine_control_operations import machine_control_operation_to_response
from zerg.services.machines_directory import build_machines_directory
from zerg.services.observability_views import build_machine_health_list_response
from zerg.services.session_chat_impl import _resolve_agents_owner_id
from zerg.services.workspace_suggestions import build_workspace_suggestions
from zerg.services.write_serializer import get_catalog_write_serializer
from zerg.services.write_serializer import get_live_write_serializer

router = APIRouter(prefix="/agents/machines", tags=["agents"])
_catalog_db_dependency = catalog_db_dependency()

PROVIDER_LIVE_PROOF_COMMAND = "provider.live_proof"
ARCHIVE_BACKLOG_CONTROL_COMMAND = "archive.backlog_control"
ARCHIVE_BACKLOG_CONTROL_COMMAND_V2 = "archive.backlog_control.v2"
PROVIDER_LIVE_PROOF_COMMAND_HEADROOM_SECS = 15


def _legacy_machine_db():
    if database_module.live_catalog_enabled():
        yield None
        return
    with database_module.get_catalog_session_factory()() as db:
        yield db


_machine_read_db_dependency = get_db if _catalog_db_dependency is get_db else _legacy_machine_db


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


def _legacy_enrollments(db: Session, owner_id: int) -> list[dict[str, object]]:
    rows = db.query(DeviceToken).filter(DeviceToken.owner_id == owner_id, DeviceToken.revoked_at.is_(None)).all()
    return [
        {
            "device_id": row.device_id,
            "machine_name": row.machine_name,
            "last_used_at": row.last_used_at,
            "created_at": row.created_at,
        }
        for row in rows
    ]


def archive_backlog_control_command_type(mode: str) -> str:
    """Require lease-aware engines before enabling repair work."""
    return ARCHIVE_BACKLOG_CONTROL_COMMAND if mode == "paused" else ARCHIVE_BACKLOG_CONTROL_COMMAND_V2


@router.get("", response_model=MachineDirectoryResponse)
def list_machines(
    db: Session | None = Depends(_machine_read_db_dependency),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> MachineDirectoryResponse:
    """List enrolled machines for this owner with live control-channel status."""
    try:
        owner_id = _request_owner_id(db, device_token)
        enrollments = (
            enrolled_machines(owner_id).get("enrollments", [])
            if database_module.live_catalog_enabled()
            else _legacy_enrollments(db, owner_id)  # type: ignore[arg-type]
        )
    except CatalogReadError as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": exc.message}) from exc
    entries = build_machines_directory(owner_id=owner_id, enrollments=enrollments)
    return MachineDirectoryResponse(machines=[MachineDirectoryEntry(**entry.to_response()) for entry in entries])


@router.patch("/{device_id}", response_model=MachineRenameResponse)
async def update_machine_name(
    device_id: str,
    request: MachineRenameRequest,
    db: Session | None = Depends(_machine_read_db_dependency),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> MachineRenameResponse:
    """Rename one enrolled machine without changing its routing identity."""
    owner_id = _request_owner_id(db, device_token)
    machine_name = request.machine_name.strip()
    if database_module.live_catalog_enabled():
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

    assert db is not None
    rows = (
        db.query(DeviceToken)
        .filter(
            DeviceToken.owner_id == owner_id,
            DeviceToken.device_id == device_id,
            DeviceToken.revoked_at.is_(None),
        )
        .all()
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Machine not found")
    changed = any(row.machine_name != machine_name for row in rows)
    if changed:
        serializer = get_catalog_write_serializer()

        def _rename(wdb: Session) -> None:
            wdb.query(DeviceToken).filter(
                DeviceToken.owner_id == owner_id,
                DeviceToken.device_id == device_id,
                DeviceToken.revoked_at.is_(None),
            ).update({DeviceToken.machine_name: machine_name}, synchronize_session=False)

        await serializer.execute_or_direct(_rename, db, label="machine-rename")
    return MachineRenameResponse(device_id=device_id, machine_name=machine_name, changed=changed)


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


@router.get("/{device_id}/workspaces", response_model=WorkspaceSuggestionsResponse)
def list_machine_workspaces(
    device_id: str,
    limit: int = Query(12, ge=1, le=50, description="Max ranked workspaces to return"),
    days_back: int = Query(45, ge=1, le=180, description="Lookback window for recent sessions"),
    db: Session | None = Depends(_machine_read_db_dependency),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> WorkspaceSuggestionsResponse:
    """Frecency-ranked recent workspaces for the launch picker, scoped to one machine."""
    if database_module.live_catalog_enabled():
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

    assert db is not None
    owner_id = _request_owner_id(db, device_token)
    entries = build_workspace_suggestions(
        db,
        owner_id=owner_id,
        device_id=device_id,
        limit=limit,
        days_back=days_back,
    )
    return WorkspaceSuggestionsResponse(
        device_id=device_id,
        workspaces=[WorkspaceSuggestion(**entry.to_response()) for entry in entries],
    )


@router.get("/{device_id}/archive-backlog", response_model=ArchiveBacklogResponse)
def get_machine_archive_backlog(
    device_id: str,
    db: Session = Depends(get_db),
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> ArchiveBacklogResponse:
    summaries, _total = list_machine_transport_health(
        db,
        device_id=device_id,
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
    db: Session | None = Depends(_machine_read_db_dependency),
    device_token: DeviceToken | None = Depends(verify_agents_token),
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


@router.get("/operations/{operation_id}", response_model=MachineControlOperationResponse)
def get_machine_control_operation(
    operation_id: str,
    db: Session | None = Depends(_machine_read_db_dependency),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> MachineControlOperationResponse:
    owner_id = _request_owner_id(db, device_token)
    if database_module.live_catalog_enabled():
        try:
            payload = machine_operation(owner_id=owner_id, operation_id=operation_id)
        except CatalogReadError as exc:
            raise HTTPException(status_code=503, detail={"code": exc.code, "message": exc.message}) from exc
        operation_payload = payload.get("operation")
        if payload.get("found") is not True or not isinstance(operation_payload, dict):
            raise HTTPException(status_code=404, detail="Machine control operation not found")
        return MachineControlOperationResponse(**operation_payload)
    operation = _get_live_machine_control_operation(owner_id=owner_id, operation_id=operation_id)
    if operation is not None:
        return MachineControlOperationResponse(**machine_control_operation_to_response(operation))
    assert db is not None
    operation = get_machine_control_operation_for_owner(db, owner_id=owner_id, operation_id=operation_id)
    if operation is None:
        raise HTTPException(status_code=404, detail="Machine control operation not found")
    return MachineControlOperationResponse(**machine_control_operation_to_response(operation))


@router.post("/{device_id}/provider-live-proof", response_model=ProviderLiveProofAcceptedResponse, status_code=202)
async def run_provider_live_proof(
    device_id: str,
    request: ProviderLiveProofRequest,
    db: Session | None = Depends(_machine_read_db_dependency),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> ProviderLiveProofAcceptedResponse:
    """Run a typed provider-live proof on a connected provider-capable machine."""
    owner_id = _request_owner_id(db, device_token)
    registry = get_machine_control_channel_registry()
    info = registry.info(owner_id=owner_id, device_id=device_id)
    if info is None:
        raise HTTPException(status_code=503, detail="Machine Agent control channel is offline")

    capability = f"{request.provider}.live_proof"
    if not registry.supports(owner_id=owner_id, device_id=device_id, capability=capability):
        raise HTTPException(
            status_code=409,
            detail=f"Machine Agent does not advertise {capability}",
        )

    payload = request.model_dump(exclude_none=True)
    machine_timeout_secs = _provider_live_proof_machine_timeout_secs(request)
    operation_timeout_secs = machine_timeout_secs + PROVIDER_LIVE_PROOF_COMMAND_HEADROOM_SECS
    try:
        operation = await _create_provider_live_proof_operation(
            db,
            owner_id=owner_id,
            device_id=device_id,
            provider=request.provider,
            request_payload=payload,
            timeout_secs=operation_timeout_secs,
        )
    except ActiveMachineControlOperationError:
        raise HTTPException(
            status_code=409,
            detail=f"Provider live proof already in flight for {device_id}/{request.provider}",
        ) from None

    command = await registry.send_command_nowait(
        owner_id=owner_id,
        device_id=device_id,
        session_id=None,
        command_type=PROVIDER_LIVE_PROOF_COMMAND,
        payload=payload,
        command_id=operation.command_id,
    )
    if not command.transport_ok:
        await _fail_provider_live_proof_operation(
            db,
            operation,
            code="machine_control_dispatch_failed",
            message=command.error or "Machine control command failed",
        )
        raise HTTPException(
            status_code=503,
            detail={
                "operation_id": operation.id,
                "code": "machine_control_dispatch_failed",
                "message": command.error or "Machine control command failed",
            },
        ) from None

    return ProviderLiveProofAcceptedResponse(
        operation_id=operation.id,
        device_id=device_id,
        provider=request.provider,
        status="running",
        status_url=f"/api/agents/machines/operations/{operation.id}",
    )


def _provider_live_proof_machine_timeout_secs(request: ProviderLiveProofRequest) -> int:
    if request.timeout_secs is not None:
        return request.timeout_secs
    return 120


def _get_live_machine_control_operation(*, owner_id: int, operation_id: str):
    if not database_module.live_store_configured():
        return None
    live_session_factory = database_module.get_live_session_factory()
    if live_session_factory is None:
        return None
    with live_session_factory() as live_db:
        return get_live_machine_control_operation_for_owner(live_db, owner_id=owner_id, operation_id=operation_id)


async def _create_provider_live_proof_operation(
    db: Session | None,
    *,
    owner_id: int,
    device_id: str,
    provider: str,
    request_payload: dict,
    timeout_secs: int,
):
    if database_module.live_catalog_enabled():
        from zerg.catalogd.client import CatalogRemoteError
        from zerg.services.catalogd_supervisor import get_catalogd_client

        catalogd = get_catalogd_client()
        if catalogd is None:
            raise RuntimeError("Live machine operation catalog is unavailable")
        operation_id = str(uuid4())
        command_id = f"machine-op:{operation_id}"
        try:
            result = await catalogd.call(
                "machine.operation.prepare.v2",
                {
                    "operation_id": operation_id,
                    "owner_id": owner_id,
                    "device_id": device_id,
                    "provider": provider,
                    "command_type": PROVIDER_LIVE_PROOF_COMMAND,
                    "command_id": command_id,
                    "request_payload": request_payload,
                    "timeout_secs": timeout_secs,
                },
                timeout_seconds=1.0,
            )
        except CatalogRemoteError as exc:
            if exc.code == "conflict":
                raise ActiveMachineControlOperationError("provider live proof already in flight") from exc
            raise
        operation = result.get("operation")
        if not isinstance(operation, dict):
            raise RuntimeError("Live machine operation catalog returned an invalid operation")
        return SimpleNamespace(id=operation["operation_id"], command_id=operation["command_id"])
    if database_module.live_store_configured():
        live_ws = get_live_write_serializer()
        if live_ws.is_configured:
            return await live_ws.execute(
                lambda live_db: create_live_provider_live_proof_operation(
                    live_db,
                    owner_id=owner_id,
                    device_id=device_id,
                    provider=provider,
                    request_payload=request_payload,
                    timeout_secs=timeout_secs,
                ),
                auto_commit=False,
                label="live-machine-control-operation",
            )
    assert db is not None
    return create_provider_live_proof_operation(
        db,
        owner_id=owner_id,
        device_id=device_id,
        provider=provider,
        request_payload=request_payload,
        timeout_secs=timeout_secs,
    )


async def _fail_provider_live_proof_operation(
    db: Session | None,
    operation,
    *,
    code: str,
    message: str,
) -> None:
    if database_module.live_catalog_enabled():
        from zerg.services.catalogd_supervisor import get_catalogd_client

        catalogd = get_catalogd_client()
        if catalogd is None:
            return
        await catalogd.call(
            "control.operation.finish.v2",
            {
                "operation_id": str(operation.id),
                "status": "failed",
                "result": None,
                "error": {"code": code, "message": message},
            },
            timeout_seconds=1.0,
        )
        return
    if operation.__class__.__name__ == "LiveMachineControlOperation":
        live_ws = get_live_write_serializer()
        if live_ws.is_configured:
            await live_ws.execute(
                lambda live_db: fail_live_machine_control_operation(
                    live_db,
                    operation,
                    code=code,
                    message=message,
                ),
                auto_commit=False,
                label="live-machine-control-fail",
            )
            return
    assert db is not None
    fail_machine_control_operation(db, operation, code=code, message=message)
