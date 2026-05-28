"""Machine-facing directory and health summaries.

``/agents/machines`` is the per-owner directory of enrolled machines and
their current control-channel status; it is the launch-sheet data source.
``/agents/machines/health`` is the richer observability view used by
operator dashboards.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.device_token import DeviceToken
from zerg.schemas.machines import MachineDirectoryEntry
from zerg.schemas.machines import MachineDirectoryResponse
from zerg.schemas.machines import ProviderLiveProofRequest
from zerg.schemas.machines import ProviderLiveProofResponse
from zerg.schemas.observability import MachineHealthListResponse
from zerg.schemas.observability import MachineHealthStatus
from zerg.services.agent_heartbeat_health import DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS
from zerg.services.agent_heartbeat_health import list_machine_transport_health
from zerg.services.machine_control_channel import get_machine_control_channel_registry
from zerg.services.machines_directory import build_machines_directory
from zerg.services.observability_views import build_machine_health_list_response
from zerg.services.session_chat_impl import _resolve_agents_owner_id

router = APIRouter(prefix="/agents/machines", tags=["agents"])

PROVIDER_LIVE_PROOF_COMMAND = "provider.live_proof"
PROVIDER_LIVE_PROOF_COMMAND_HEADROOM_SECS = 15
_PROVIDER_LIVE_PROOF_IN_FLIGHT: set[tuple[int, str, str]] = set()
_PROVIDER_LIVE_PROOF_IN_FLIGHT_LOCK = asyncio.Lock()


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


@router.post("/{device_id}/provider-live-proof", response_model=ProviderLiveProofResponse)
async def run_provider_live_proof(
    device_id: str,
    request: ProviderLiveProofRequest,
    db: Session = Depends(get_db),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> ProviderLiveProofResponse:
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

    in_flight_key = (owner_id, device_id, request.provider)
    if not await _claim_provider_live_proof(in_flight_key):
        raise HTTPException(
            status_code=409,
            detail=f"Provider live proof already in flight for {device_id}/{request.provider}",
        )

    machine_timeout_secs = _provider_live_proof_machine_timeout_secs(request)
    try:
        command = await registry.send_command(
            owner_id=owner_id,
            device_id=device_id,
            session_id=None,
            command_type=PROVIDER_LIVE_PROOF_COMMAND,
            payload=request.model_dump(exclude_none=True),
            timeout_secs=machine_timeout_secs + PROVIDER_LIVE_PROOF_COMMAND_HEADROOM_SECS,
        )
    finally:
        await _release_provider_live_proof(in_flight_key)
    if not command.transport_ok:
        raise HTTPException(status_code=503, detail=command.error or "Machine control command failed")
    message = dict(command.message or {})
    if not message.get("ok"):
        error = message.get("error") if isinstance(message.get("error"), dict) else {}
        error_message = error.get("message") or "Machine Agent provider live proof failed"
        raise HTTPException(
            status_code=502,
            detail={
                "code": error.get("code") or "machine_agent_provider_live_proof_failed",
                "message": error_message,
            },
        )
    result = message.get("result")
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="Machine Agent returned malformed provider live proof result")

    return ProviderLiveProofResponse(
        device_id=device_id,
        provider=request.provider,
        command_id=str(message.get("command_id") or ""),
        result=result,
    )


def _provider_live_proof_machine_timeout_secs(request: ProviderLiveProofRequest) -> int:
    if request.timeout_secs is not None:
        return request.timeout_secs
    if request.run_live_token_contract:
        return min(request.live_token_timeout_secs + 60, 900)
    return 120


async def _claim_provider_live_proof(key: tuple[int, str, str]) -> bool:
    async with _PROVIDER_LIVE_PROOF_IN_FLIGHT_LOCK:
        if key in _PROVIDER_LIVE_PROOF_IN_FLIGHT:
            return False
        _PROVIDER_LIVE_PROOF_IN_FLIGHT.add(key)
        return True


async def _release_provider_live_proof(key: tuple[int, str, str]) -> None:
    async with _PROVIDER_LIVE_PROOF_IN_FLIGHT_LOCK:
        _PROVIDER_LIVE_PROOF_IN_FLIGHT.discard(key)
