"""Machine-facing directory and health summaries.

``/agents/machines`` is the per-owner directory of enrolled machines and
their current control-channel status; it is the launch-sheet data source.
``/agents/machines/health`` is the richer observability view used by
operator dashboards.
"""

from __future__ import annotations

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
from zerg.schemas.machines import ProviderLiveProofAcceptedResponse
from zerg.schemas.machines import ProviderLiveProofRequest
from zerg.schemas.machines import WorkspaceSuggestion
from zerg.schemas.machines import WorkspaceSuggestionsResponse
from zerg.schemas.observability import MachineHealthListResponse
from zerg.schemas.observability import MachineHealthStatus
from zerg.services.agent_heartbeat_health import DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS
from zerg.services.agent_heartbeat_health import list_machine_transport_health
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
from zerg.services.write_serializer import get_live_write_serializer

router = APIRouter(prefix="/agents/machines", tags=["agents"])
_catalog_db_dependency = catalog_db_dependency()

PROVIDER_LIVE_PROOF_COMMAND = "provider.live_proof"
ARCHIVE_BACKLOG_CONTROL_COMMAND = "archive.backlog_control"
PROVIDER_LIVE_PROOF_COMMAND_HEADROOM_SECS = 15


@router.get("", response_model=MachineDirectoryResponse)
def list_machines(
    db: Session = Depends(_catalog_db_dependency),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> MachineDirectoryResponse:
    """List enrolled machines for this owner with live control-channel status."""
    owner_id = _resolve_agents_owner_id(db, device_token)
    entries = build_machines_directory(db, owner_id=owner_id)
    return MachineDirectoryResponse(machines=[MachineDirectoryEntry(**entry.to_response()) for entry in entries])


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
    db: Session = Depends(get_db),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> WorkspaceSuggestionsResponse:
    """Frecency-ranked recent workspaces for the launch picker, scoped to one machine."""
    owner_id = _resolve_agents_owner_id(db, device_token)
    entries = build_workspace_suggestions(db, owner_id=owner_id, device_id=device_id, limit=limit, days_back=days_back)
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
    db: Session = Depends(_catalog_db_dependency),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> ArchiveBacklogControlResponse:
    owner_id = _resolve_agents_owner_id(db, device_token)
    registry = get_machine_control_channel_registry()
    info = registry.info(owner_id=owner_id, device_id=device_id)
    if info is None:
        raise HTTPException(status_code=503, detail="Machine Agent control channel is offline")
    if not registry.supports(owner_id=owner_id, device_id=device_id, capability=ARCHIVE_BACKLOG_CONTROL_COMMAND):
        raise HTTPException(status_code=409, detail="Machine Agent does not advertise archive backlog control")

    payload = request.model_dump(exclude_none=True)
    payload.pop("timeout_secs", None)
    command = await registry.send_command(
        owner_id=owner_id,
        device_id=device_id,
        session_id=None,
        command_type=ARCHIVE_BACKLOG_CONTROL_COMMAND,
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
    db: Session = Depends(_catalog_db_dependency),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> MachineControlOperationResponse:
    owner_id = _resolve_agents_owner_id(db, device_token)
    operation = _get_live_machine_control_operation(owner_id=owner_id, operation_id=operation_id)
    if operation is not None:
        return MachineControlOperationResponse(**machine_control_operation_to_response(operation))
    if database_module.live_catalog_enabled():
        raise HTTPException(status_code=404, detail="Machine control operation not found")
    operation = get_machine_control_operation_for_owner(db, owner_id=owner_id, operation_id=operation_id)
    if operation is None:
        raise HTTPException(status_code=404, detail="Machine control operation not found")
    return MachineControlOperationResponse(**machine_control_operation_to_response(operation))


@router.post("/{device_id}/provider-live-proof", response_model=ProviderLiveProofAcceptedResponse, status_code=202)
async def run_provider_live_proof(
    device_id: str,
    request: ProviderLiveProofRequest,
    db: Session = Depends(_catalog_db_dependency),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> ProviderLiveProofAcceptedResponse:
    """Run a typed provider-live proof on a connected provider-capable machine."""
    owner_id = _resolve_agents_owner_id(db, device_token)
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
    db: Session,
    *,
    owner_id: int,
    device_id: str,
    provider: str,
    request_payload: dict,
    timeout_secs: int,
):
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
    return create_provider_live_proof_operation(
        db,
        owner_id=owner_id,
        device_id=device_id,
        provider=provider,
        request_payload=request_payload,
        timeout_secs=timeout_secs,
    )


async def _fail_provider_live_proof_operation(
    db: Session,
    operation,
    *,
    code: str,
    message: str,
) -> None:
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
    fail_machine_control_operation(db, operation, code=code, message=message)
