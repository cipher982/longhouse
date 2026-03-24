"""Agent heartbeat ingest endpoint.

Receives periodic health check payloads from running engine daemons.
Stores latest heartbeat per device_id, retaining 30 days of history.

Authentication: same X-Agents-Token / device token as the ingest endpoint.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from fastapi import Response
from fastapi import status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.agents import AgentHeartbeat
from zerg.models.device_token import DeviceToken
from zerg.services.write_serializer import get_write_serializer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])


class HeartbeatIn(BaseModel):
    """Payload from the engine daemon."""

    version: Optional[str] = None
    daemon_pid: Optional[int] = None
    last_ship_at: Optional[str] = None  # RFC3339 or None
    spool_pending_count: int = 0
    parse_error_count_1h: int = 0
    consecutive_ship_failures: int = 0
    disk_free_bytes: int = 0
    is_offline: bool = False


@router.post("/heartbeat", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def ingest_heartbeat(
    payload: HeartbeatIn,
    request: Request,
    db: Session = Depends(get_db),
    _token: DeviceToken | None = Depends(verify_agents_token),
) -> Response:
    """Accept a heartbeat from an engine daemon.

    Upserts (inserts) a new heartbeat row per device. History is retained
    for 30 days; older rows are cleaned up by the stale agent detection job.
    """
    # Determine device_id: prefer device token, fall back to request metadata
    device_id: str
    if _token is not None:
        device_id = _token.device_id or f"device:{_token.id}"
    else:
        # Dev mode or legacy token — use IP as proxy
        device_id = request.client.host if request.client else "unknown"

    # Parse last_ship_at
    last_ship_at: datetime | None = None
    if payload.last_ship_at:
        try:
            last_ship_at = datetime.fromisoformat(payload.last_ship_at.replace("Z", "+00:00"))
        except ValueError:
            pass

    _device_id = device_id
    _payload_json = json.dumps(payload.model_dump())
    _now = datetime.now(timezone.utc)
    _version = payload.version
    _last_ship = last_ship_at
    _spool = payload.spool_pending_count
    _parse_err = payload.parse_error_count_1h
    _consec = payload.consecutive_ship_failures
    _disk = payload.disk_free_bytes
    _offline = 1 if payload.is_offline else 0

    def _do_heartbeat(write_db: Session) -> None:
        hb = AgentHeartbeat(
            device_id=_device_id,
            received_at=_now,
            version=_version,
            last_ship_at=_last_ship,
            spool_pending=_spool,
            parse_errors_1h=_parse_err,
            consecutive_failures=_consec,
            disk_free_bytes=_disk,
            is_offline=_offline,
            raw_json=_payload_json,
        )
        write_db.add(hb)
        # Prune history >30 days for this device
        cutoff = _now - timedelta(days=30)
        write_db.query(AgentHeartbeat).filter(
            AgentHeartbeat.device_id == _device_id,
            AgentHeartbeat.received_at < cutoff,
        ).delete()

    ws = get_write_serializer()
    await ws.execute_or_direct(_do_heartbeat, db, label="heartbeat")

    return Response(status_code=status.HTTP_204_NO_CONTENT)
