"""Machine Agent control WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any
from typing import Mapping

from fastapi import APIRouter
from fastapi import WebSocket
from fastapi import WebSocketDisconnect

from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.client import CatalogUnavailable
from zerg.config import get_settings
from zerg.database import reset_test_worker_id
from zerg.database import set_test_worker_id
from zerg.dependencies.agents_auth import _validate_device_token_for_request
from zerg.models.device_token import DeviceToken
from zerg.services.catalogd_supervisor import get_catalogd_client
from zerg.services.console_turns import reconcile_starting_console_turns_for_device
from zerg.services.machine_control_channel import get_machine_control_channel_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents/control", tags=["agents"])

CONTROL_HEARTBEAT_TIMEOUT_SECS = 90
CONTROL_HELLO_TIMEOUT_SECS = 10


def _auth_disabled_identity(hello: Mapping[str, Any]) -> tuple[int, str]:
    device_id = str(hello.get("device_id") or hello.get("machine_name") or "test-machine").strip()
    return 0, device_id or "test-machine"


def _validate_websocket_device_token(websocket: WebSocket) -> DeviceToken | None:
    token = websocket.headers.get("x-agents-token")
    if not token:
        return None
    if not token.startswith("zdt_"):
        return None
    return _validate_device_token_for_request(token)


def _control_identity(
    hello: Mapping[str, Any],
    token: DeviceToken | None,
    *,
    auth_disabled: bool,
) -> tuple[int, str] | None:
    """Resolve the same device identity used by the HTTP agents surface."""

    if token is None:
        return _auth_disabled_identity(hello) if auth_disabled else None

    device_id = str(token.device_id)
    hello_device_id = str(hello.get("device_id") or "").strip()
    if hello_device_id and hello_device_id != device_id:
        return None
    return int(token.owner_id), device_id


async def _reconcile_machine_control_operation_result(
    message: dict[str, Any],
    *,
    owner_id: int,
    device_id: str,
) -> bool:
    catalogd = get_catalogd_client()
    if catalogd is None:
        raise CatalogUnavailable("catalogd is not supervised")
    result = await catalogd.call(
        "control.command_result.apply.v2",
        {
            "owner_id": owner_id,
            "device_id": device_id,
            "message": message,
        },
        timeout_seconds=2.0,
    )
    if (
        type(result.get("matched")) is not bool
        or result.get("match_kind") not in {None, "operation"}
        or not isinstance(result.get("commit_seq"), str)
        or not result["commit_seq"].isdecimal()
    ):
        raise CatalogUnavailable("catalog returned an invalid command result")
    return result["matched"]


async def _close_control_ws(websocket: WebSocket, *, code: int = 1008, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except RuntimeError:
        logger.debug("Ignoring machine control websocket close race: %s", reason)


async def _reconcile_console_turns_after_register(*, owner_id: int, device_id: str, registry) -> None:
    try:
        outcomes = await reconcile_starting_console_turns_for_device(
            None,
            owner_id=owner_id,
            device_id=device_id,
            registry=registry,
        )
        if outcomes:
            logger.info(
                "Reconciled %d starting Console turn(s) after control reconnect owner=%s device=%s states=%s",
                len(outcomes),
                owner_id,
                device_id,
                ",".join(outcome.state for outcome in outcomes),
            )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to reconcile starting Console turns after control reconnect owner=%s device=%s",
            owner_id,
            device_id,
        )


@router.websocket("/ws")
async def machine_control_websocket(websocket: WebSocket) -> None:
    settings = get_settings()
    worker_id = websocket.query_params.get("worker")
    worker_token = set_test_worker_id(worker_id) if worker_id else None
    registry = get_machine_control_channel_registry()
    owner_id: int | None = None
    device_id: str | None = None
    console_reconcile_task: asyncio.Task[None] | None = None

    try:
        if not settings.testing and not settings.single_tenant:
            await _close_control_ws(websocket, code=1011, reason="Multi-tenant agents control is not implemented")
            return

        # Authenticate *before* accepting the handshake, the same way
        # routers/websocket.py does. One event loop serves every connection, so
        # an unauthenticated caller must never reach the point where the server
        # holds an accepted socket and waits on it.
        token = await asyncio.to_thread(_validate_websocket_device_token, websocket)
        if token is None and not settings.auth_disabled:
            await _close_control_ws(websocket, code=4401, reason="Invalid or missing device token")
            return

        # Name the caller for the access log, in the same format the HTTP
        # machine surface stamps (dependencies/agents_auth.py). The owner-bound
        # credential is validated above and was otherwise thrown away for
        # logging purposes. Not from the ``hello`` frame below: that arrives
        # after accept and its ``device_id`` is attacker-chosen.
        websocket.state.principal = f"device:{token.id}" if token is not None else "auth-disabled"

        await websocket.accept()

        try:
            hello = await asyncio.wait_for(websocket.receive_json(), timeout=CONTROL_HELLO_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            await _close_control_ws(websocket, reason="Timed out waiting for hello message")
            return
        except WebSocketDisconnect:
            return
        except Exception:
            await _close_control_ws(websocket, reason="Invalid hello message")
            return

        if not isinstance(hello, Mapping) or hello.get("type") != "hello":
            await _close_control_ws(websocket, reason="Expected hello message")
            return

        identity = _control_identity(hello, token, auth_disabled=settings.auth_disabled)
        if identity is None:
            await _close_control_ws(websocket, reason="Device token does not match hello device_id")
            return
        owner_id, device_id = identity

        supports_raw = hello.get("supports") or []
        supports = [str(item) for item in supports_raw] if isinstance(supports_raw, list) else []
        await registry.register(
            owner_id=owner_id,
            device_id=device_id,
            machine_name=str(hello.get("machine_name") or device_id),
            engine_build=str(hello.get("engine_build") or "") or None,
            supports=supports,
            provider_readiness=hello.get("provider_readiness"),
            websocket=websocket,
        )
        console_reconcile_task = asyncio.create_task(
            _reconcile_console_turns_after_register(
                owner_id=owner_id,
                device_id=device_id,
                registry=registry,
            )
        )

        while True:
            try:
                message = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=CONTROL_HEARTBEAT_TIMEOUT_SECS,
                )
            except asyncio.TimeoutError:
                logger.info(
                    "Machine control websocket timed out waiting for heartbeat owner=%s device=%s",
                    owner_id,
                    device_id,
                )
                break
            except WebSocketDisconnect:
                break

            message_type = message.get("type")
            if message_type == "heartbeat":
                await registry.mark_seen(owner_id=owner_id, device_id=device_id)
                try:
                    await websocket.send_json({"type": "heartbeat_ack"})
                except WebSocketDisconnect:
                    break
            elif message_type == "command_result":
                await registry.mark_seen(owner_id=owner_id, device_id=device_id)
                matched = await registry.complete_command(message, owner_id=owner_id, device_id=device_id)
                if not matched:
                    try:
                        matched = await _reconcile_machine_control_operation_result(
                            message,
                            owner_id=owner_id,
                            device_id=device_id,
                        )
                    except (CatalogUnavailable, CatalogRemoteError):
                        logger.exception(
                            "Catalog unavailable reconciling machine operation command_id=%s",
                            message.get("command_id"),
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "Failed to reconcile machine operation result command_id=%s",
                            message.get("command_id"),
                        )
            else:
                logger.warning("Unknown machine control message type from %s: %s", device_id, message_type)
    finally:
        if console_reconcile_task is not None and not console_reconcile_task.done():
            console_reconcile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await console_reconcile_task
        if owner_id is not None and device_id is not None:
            await registry.unregister(owner_id=owner_id, device_id=device_id, websocket=websocket)
        if worker_token is not None:
            reset_test_worker_id(worker_token)
