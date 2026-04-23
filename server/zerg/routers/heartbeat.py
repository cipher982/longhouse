"""Agent heartbeat ingest endpoint.

Receives periodic health check payloads from running engine daemons.
Stores latest heartbeat per device_id, retaining 30 days of history.

Authentication: same X-Agents-Token / device token as the ingest endpoint.
"""

from __future__ import annotations

import json
import logging
import time
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
from zerg.metrics import agents_heartbeat_payload_bytes
from zerg.metrics import agents_heartbeat_requests_total
from zerg.metrics import agents_heartbeat_write_seconds
from zerg.models.agents import AgentHeartbeat
from zerg.models.device_token import DeviceToken
from zerg.observability import get_tracer
from zerg.observability import set_span_attributes
from zerg.services.write_serializer import get_write_serializer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])


class HeartbeatIn(BaseModel):
    """Payload from the engine daemon."""

    version: Optional[str] = None
    daemon_pid: Optional[int] = None
    last_ship_at: Optional[str] = None  # RFC3339 last successful ship or None
    last_ship_attempt_at: Optional[str] = None  # RFC3339 last ship attempt or None
    last_ship_result: Optional[str] = None
    last_ship_latency_ms: Optional[int] = None
    last_ship_http_status: Optional[int] = None
    spool_pending_count: int = 0
    spool_dead_count: int = 0
    parse_error_count_1h: int = 0
    consecutive_ship_failures: int = 0
    ship_attempts_1h: int = 0
    ship_successes_1h: int = 0
    ship_rate_limited_1h: int = 0
    ship_server_errors_1h: int = 0
    ship_payload_rejections_1h: int = 0
    ship_payload_too_large_1h: int = 0
    ship_retryable_client_errors_1h: int = 0
    ship_connect_errors_1h: int = 0
    ship_latency_p50_ms_1h: Optional[int] = None
    ship_latency_p95_ms_1h: Optional[int] = None
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
    tracer = get_tracer(__name__)
    auth_kind_label = "device_token" if _token is not None else "none"
    request_status_label = "internal_error"
    with tracer.start_as_current_span("longhouse.heartbeat") as span:
        set_span_attributes(
            span,
            {
                "http.route": "/api/agents/heartbeat",
                "longhouse.heartbeat.auth_kind": auth_kind_label,
            },
        )

        try:
            with tracer.start_as_current_span("longhouse.heartbeat.validate") as validate_span:
                # Determine device_id: prefer device token, fall back to request metadata
                device_id: str
                if _token is not None:
                    device_id = _token.device_id or f"device:{_token.id}"
                else:
                    # Dev mode or legacy token — use IP as proxy
                    device_id = request.client.host if request.client else "unknown"

                last_ship_at: datetime | None = None
                if payload.last_ship_at:
                    try:
                        last_ship_at = datetime.fromisoformat(payload.last_ship_at.replace("Z", "+00:00"))
                    except ValueError:
                        pass

                wire_bytes = len(await request.body())
                payload_json = json.dumps(payload.model_dump())
                agents_heartbeat_payload_bytes.observe(wire_bytes)
                set_span_attributes(
                    validate_span,
                    {
                        "longhouse.device.id": device_id,
                        "longhouse.build.version": payload.version,
                        "longhouse.heartbeat.last_ship_attempt_at": payload.last_ship_attempt_at,
                        "longhouse.heartbeat.last_ship_result": payload.last_ship_result,
                        "longhouse.heartbeat.ship_attempts_1h": payload.ship_attempts_1h,
                        "longhouse.heartbeat.spool_pending_count": payload.spool_pending_count,
                        "longhouse.heartbeat.spool_dead_count": payload.spool_dead_count,
                        "longhouse.heartbeat.payload_bytes_wire": wire_bytes,
                        "longhouse.heartbeat.is_offline": payload.is_offline,
                    },
                )
                set_span_attributes(
                    span,
                    {
                        "longhouse.device.id": device_id,
                        "longhouse.build.version": payload.version,
                        "longhouse.heartbeat.is_offline": payload.is_offline,
                    },
                )

            _device_id = device_id
            _payload_json = payload_json
            _now = datetime.now(timezone.utc)
            _version = payload.version
            _last_ship = last_ship_at
            _spool = payload.spool_pending_count
            _spool_dead = payload.spool_dead_count
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
                    spool_dead=_spool_dead,
                    parse_errors_1h=_parse_err,
                    consecutive_failures=_consec,
                    disk_free_bytes=_disk,
                    is_offline=_offline,
                    raw_json=_payload_json,
                )
                write_db.add(hb)
                cutoff = _now - timedelta(days=30)
                write_db.query(AgentHeartbeat).filter(
                    AgentHeartbeat.device_id == _device_id,
                    AgentHeartbeat.received_at < cutoff,
                ).delete()

            ws = get_write_serializer()
            with tracer.start_as_current_span("longhouse.heartbeat.write") as write_span:
                write_started = time.monotonic()
                await ws.execute_or_direct(_do_heartbeat, db, label="heartbeat")
                write_ms = round((time.monotonic() - write_started) * 1000, 1)
                agents_heartbeat_write_seconds.observe(write_ms / 1000.0)
                set_span_attributes(
                    write_span,
                    {
                        "longhouse.device.id": _device_id,
                        "longhouse.heartbeat.write_ms": write_ms,
                    },
                )

            request_status_label = "ok"
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        except Exception:
            logger.exception("Failed to ingest heartbeat")
            request_status_label = "internal_error"
            raise
        finally:
            agents_heartbeat_requests_total.labels(
                auth_kind=auth_kind_label,
                status=request_status_label,
            ).inc()
