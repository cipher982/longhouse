"""Machine Agent control WebSocket."""

from __future__ import annotations

import logging
from typing import Any
from typing import Mapping

from fastapi import APIRouter
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.database import get_session_factory
from zerg.models.device_token import DeviceToken
from zerg.routers.device_tokens import validate_device_token
from zerg.services.machine_control_channel import get_machine_control_channel_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents/control", tags=["agents"])


def _auth_disabled_identity(hello: Mapping[str, Any]) -> tuple[int, str]:
    device_id = str(hello.get("device_id") or hello.get("machine_name") or "test-machine").strip()
    return 0, device_id or "test-machine"


def _validate_websocket_device_token(websocket: WebSocket, db: Session) -> DeviceToken | None:
    token = websocket.headers.get("x-agents-token")
    if not token:
        return None
    if not token.startswith("zdt_"):
        return None
    return validate_device_token(token, db)


async def _close_control_ws(websocket: WebSocket, *, code: int = 1008, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except RuntimeError:
        logger.debug("Ignoring machine control websocket close race: %s", reason)


@router.websocket("/ws")
async def machine_control_websocket(websocket: WebSocket) -> None:
    settings = get_settings()
    db = get_session_factory()()
    registry = get_machine_control_channel_registry()
    owner_id: int | None = None
    device_id: str | None = None

    await websocket.accept()
    try:
        if not settings.testing and not settings.single_tenant:
            await _close_control_ws(websocket, code=1011, reason="Multi-tenant agents control is not implemented")
            return

        try:
            hello = await websocket.receive_json()
        except WebSocketDisconnect:
            return
        except Exception:
            await _close_control_ws(websocket, reason="Invalid hello message")
            return

        if hello.get("type") != "hello":
            await _close_control_ws(websocket, reason="Expected hello message")
            return

        if settings.auth_disabled:
            owner_id, device_id = _auth_disabled_identity(hello)
        else:
            token = _validate_websocket_device_token(websocket, db)
            if token is None:
                await _close_control_ws(websocket, reason="Invalid or missing device token")
                return
            owner_id = int(token.owner_id)
            device_id = str(token.device_id)
            hello_device_id = str(hello.get("device_id") or "").strip()
            if hello_device_id and hello_device_id != device_id:
                await _close_control_ws(websocket, reason="Device token does not match hello device_id")
                return

        supports_raw = hello.get("supports") or []
        supports = [str(item) for item in supports_raw] if isinstance(supports_raw, list) else []
        await registry.register(
            owner_id=owner_id,
            device_id=device_id,
            machine_name=str(hello.get("machine_name") or device_id),
            engine_build=str(hello.get("engine_build") or "") or None,
            supports=supports,
            websocket=websocket,
        )

        while True:
            try:
                message = await websocket.receive_json()
            except WebSocketDisconnect:
                break

            message_type = message.get("type")
            if message_type == "heartbeat":
                await registry.mark_seen(owner_id=owner_id, device_id=device_id)
            elif message_type == "command_result":
                await registry.mark_seen(owner_id=owner_id, device_id=device_id)
                await registry.complete_command(message)
            else:
                logger.warning("Unknown machine control message type from %s: %s", device_id, message_type)
    finally:
        db.close()
        if owner_id is not None and device_id is not None:
            await registry.unregister(owner_id=owner_id, device_id=device_id, websocket=websocket)
