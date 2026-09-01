"""Machine-facing coarse local presence updates."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from typing import Literal

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import Field
from pydantic import field_validator
from sqlalchemy.orm import Session

from zerg.auth.caller import caller_principal
from zerg.database import catalog_db_dependency
from zerg.database import get_db
from zerg.dependencies.agents_auth import verify_agents_caller
from zerg.models.device_token import DeviceToken
from zerg.services.session_chat_impl import _resolve_agents_owner_id
from zerg.utils.time import UTCBaseModel

router = APIRouter(prefix="/agents", tags=["agents"])
_catalog_db_dependency = catalog_db_dependency()


def _machine_presence_db():
    yield None


_machine_presence_db_dependency = get_db if _catalog_db_dependency is get_db else _machine_presence_db

MachinePresenceState = Literal["active", "idle_5m", "idle_10m", "locked", "unknown"]


def _bucket_state_from_idle_seconds(idle_seconds: int) -> MachinePresenceState:
    if idle_seconds >= 10 * 60:
        return "idle_10m"
    if idle_seconds >= 5 * 60:
        return "idle_5m"
    return "active"


def _coarse_idle_seconds(state: MachinePresenceState) -> int | None:
    if state == "active":
        return 0
    if state == "idle_5m":
        return 5 * 60
    if state == "idle_10m":
        return 10 * 60
    return None


class MachinePresenceIn(UTCBaseModel):
    state: MachinePresenceState
    source: str = Field("unknown", max_length=64)
    idle_seconds: int | None = Field(None, ge=0, le=86_400)
    measured_at: datetime | None = None

    @field_validator("source")
    @classmethod
    def normalize_source(cls, value: str) -> str:
        return (value or "unknown").strip()[:64] or "unknown"


class MachinePresenceResponse(UTCBaseModel):
    owner_id: int
    device_id: str
    state: MachinePresenceState
    source: str
    idle_seconds: int | None
    measured_at: datetime
    received_at: datetime


class MachinePresencePolicyResponse(UTCBaseModel):
    enabled: bool
    min_interval_seconds: int = 60


def _machine_presence_identity(db: Session | None, token: DeviceToken | None) -> tuple[int, str]:
    token = caller_principal(token)
    if token is not None and not isinstance(token, DeviceToken):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Machine presence requires a device token",
        )
    owner_id = getattr(token, "owner_id", None)
    if owner_id is None and db is not None:
        owner_id = _resolve_agents_owner_id(db, token)
    if owner_id is None:
        from zerg.services.catalog_read_gateway import active_owner_id

        owner_id = active_owner_id()
    if owner_id is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Machine owner is unavailable")
    device_id = (str(token.device_id or f"device:{token.id}") if isinstance(token, DeviceToken) else "auth-disabled-local")[:255]
    return owner_id, device_id


@router.get("/machine-presence/policy", response_model=MachinePresencePolicyResponse)
async def get_machine_presence_policy(
    db: Session | None = Depends(_machine_presence_db_dependency),
    token: DeviceToken | None = Depends(verify_agents_caller),
) -> MachinePresencePolicyResponse:
    owner_id, _device_id = _machine_presence_identity(db, token)
    result = await _catalog_call("machine.presence.policy.v2", {"owner_id": owner_id})
    return MachinePresencePolicyResponse(enabled=result.get("enabled") is not False)


@router.post("/machine-presence", response_model=MachinePresenceResponse)
async def update_machine_presence(
    payload: MachinePresenceIn,
    db: Session | None = Depends(_machine_presence_db_dependency),
    token: DeviceToken | None = Depends(verify_agents_caller),
) -> MachinePresenceResponse:
    owner_id, device_id = _machine_presence_identity(db, token)
    policy = await _catalog_call("machine.presence.policy.v2", {"owner_id": owner_id})
    enabled = policy.get("enabled") is not False
    if not enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Machine presence collection is disabled",
        )

    now = datetime.now(timezone.utc)
    measured_at = (payload.measured_at or now).astimezone(timezone.utc)
    state: MachinePresenceState = (
        _bucket_state_from_idle_seconds(payload.idle_seconds)
        if payload.idle_seconds is not None and payload.state != "locked"
        else payload.state
    )
    coarse_idle_seconds = _coarse_idle_seconds(state)

    result = await _catalog_call(
        "machine.presence.upsert.v2",
        {
            "owner_id": owner_id,
            "device_id": device_id,
            "state": state,
            "source": payload.source,
            "idle_seconds": coarse_idle_seconds,
            "measured_at": measured_at.isoformat(),
            "received_at": now.isoformat(),
        },
    )
    presence = result.get("presence")
    if not isinstance(presence, dict):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Catalog presence response is invalid")
    return MachinePresenceResponse.model_validate(presence)


async def _catalog_call(method: str, params: dict) -> dict:
    from zerg.catalogd.client import CatalogRemoteError
    from zerg.catalogd.client import CatalogUnavailable
    from zerg.services.catalogd_supervisor import get_catalogd_client

    client = get_catalogd_client()
    if client is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Catalog is unavailable")
    try:
        return await client.call(method, params, timeout_seconds=1.0)
    except (CatalogRemoteError, CatalogUnavailable) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Catalog is unavailable") from exc
