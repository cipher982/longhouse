"""Machine Agent managed-control WebSocket registry."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Mapping

from fastapi import WebSocket

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MachineControlConnectionInfo:
    owner_id: int
    device_id: str
    machine_name: str | None
    engine_build: str | None
    supports: frozenset[str]
    connected_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True)
class MachineControlCommandResponse:
    transport_ok: bool
    message: Mapping[str, Any] | None = None
    error: str | None = None


@dataclass
class _MachineControlConnection:
    info: MachineControlConnectionInfo
    websocket: WebSocket
    send_lock: asyncio.Lock


@dataclass
class _PendingCommand:
    key: tuple[int, str]
    future: asyncio.Future[Mapping[str, Any]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MachineControlChannelRegistry:
    """In-memory registry for typed Machine Agent control channels."""

    def __init__(self) -> None:
        self._connections: dict[tuple[int, str], _MachineControlConnection] = {}
        self._pending: dict[str, _PendingCommand] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        *,
        owner_id: int,
        device_id: str,
        machine_name: str | None,
        engine_build: str | None,
        supports: list[str] | tuple[str, ...] | set[str] | frozenset[str],
        websocket: WebSocket,
    ) -> None:
        key = (owner_id, device_id)
        now = _utc_now()
        info = MachineControlConnectionInfo(
            owner_id=owner_id,
            device_id=device_id,
            machine_name=machine_name,
            engine_build=engine_build,
            supports=frozenset(str(item) for item in supports if str(item).strip()),
            connected_at=now,
            last_seen_at=now,
        )
        async with self._lock:
            if key in self._connections:
                logger.warning("Replacing machine control channel for owner=%s device=%s", owner_id, device_id)
                self._fail_pending_for_key(key, "Machine control channel was replaced")
            self._connections[key] = _MachineControlConnection(
                info=info,
                websocket=websocket,
                send_lock=asyncio.Lock(),
            )
        logger.info("Registered machine control channel for owner=%s device=%s", owner_id, device_id)

    async def unregister(self, *, owner_id: int, device_id: str, websocket: WebSocket) -> bool:
        key = (owner_id, device_id)
        async with self._lock:
            connection = self._connections.get(key)
            if connection is None or connection.websocket is not websocket:
                return False
            del self._connections[key]
            self._fail_pending_for_key(key, "Machine control channel disconnected")
        logger.info("Unregistered machine control channel for owner=%s device=%s", owner_id, device_id)
        return True

    async def mark_seen(self, *, owner_id: int, device_id: str) -> None:
        key = (owner_id, device_id)
        async with self._lock:
            connection = self._connections.get(key)
            if connection is None:
                return
            info = connection.info
            connection.info = MachineControlConnectionInfo(
                owner_id=info.owner_id,
                device_id=info.device_id,
                machine_name=info.machine_name,
                engine_build=info.engine_build,
                supports=info.supports,
                connected_at=info.connected_at,
                last_seen_at=_utc_now(),
            )

    def info(self, *, owner_id: int, device_id: str) -> MachineControlConnectionInfo | None:
        connection = self._connections.get((owner_id, device_id))
        return connection.info if connection is not None else None

    def list_for_owner(self, *, owner_id: int) -> list[MachineControlConnectionInfo]:
        """Return infos for every currently-connected machine belonging to owner."""
        return [connection.info for (conn_owner, _device), connection in self._connections.items() if conn_owner == owner_id]

    def is_online(self, *, owner_id: int, device_id: str) -> bool:
        return (owner_id, device_id) in self._connections

    def supports(self, *, owner_id: int, device_id: str, capability: str) -> bool:
        info = self.info(owner_id=owner_id, device_id=device_id)
        return info is not None and capability in info.supports

    async def send_command(
        self,
        *,
        owner_id: int,
        device_id: str,
        session_id: str | None,
        command_type: str,
        payload: Mapping[str, Any] | None = None,
        timeout_secs: int = 15,
        command_id: str | None = None,
    ) -> MachineControlCommandResponse:
        key = (owner_id, device_id)
        command_id = command_id or str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Mapping[str, Any]] = loop.create_future()
        should_send = True
        async with self._lock:
            connection = self._connections.get(key)
            if connection is None:
                return MachineControlCommandResponse(
                    transport_ok=False,
                    error="Machine Agent control channel is offline",
                )
            pending = self._pending.get(command_id)
            if pending is not None:
                if pending.key != key:
                    return MachineControlCommandResponse(
                        transport_ok=False,
                        error="Machine Agent control command id is already in flight for another connection",
                    )
                future = pending.future
                should_send = False
            else:
                self._pending[command_id] = _PendingCommand(key=key, future=future)
            websocket = connection.websocket
            send_lock = connection.send_lock

        frame = {
            "type": "command",
            "command_id": command_id,
            "command_type": command_type,
            "payload": dict(payload or {}),
        }
        if session_id is not None:
            frame["session_id"] = session_id
        if should_send:
            try:
                async with send_lock:
                    await websocket.send_json(frame)
            except Exception as exc:
                async with self._lock:
                    pending = self._pending.pop(command_id, None)
                if pending is not None and not pending.future.done():
                    pending.future.set_exception(RuntimeError("Failed to send command to Machine Agent control channel"))
                    pending.future.exception()
                logger.warning(
                    "Failed to send machine control command %s to owner=%s device=%s: %s",
                    command_id,
                    owner_id,
                    device_id,
                    exc,
                )
                return MachineControlCommandResponse(
                    transport_ok=False,
                    error="Failed to send command to Machine Agent control channel",
                )

        try:
            message = await asyncio.wait_for(asyncio.shield(future), timeout=max(1, int(timeout_secs)))
        except asyncio.TimeoutError:
            async with self._lock:
                pending = self._pending.pop(command_id, None)
            if pending is not None and not pending.future.done():
                pending.future.set_exception(RuntimeError(f"Machine Agent control command timed out after {timeout_secs} seconds"))
                pending.future.exception()
            return MachineControlCommandResponse(
                transport_ok=False,
                error=f"Machine Agent control command timed out after {timeout_secs} seconds",
            )
        except Exception as exc:
            async with self._lock:
                self._pending.pop(command_id, None)
            return MachineControlCommandResponse(
                transport_ok=False,
                error=str(exc),
            )

        return MachineControlCommandResponse(transport_ok=True, message=message)

    async def complete_command(
        self,
        message: Mapping[str, Any],
        *,
        owner_id: int | None = None,
        device_id: str | None = None,
    ) -> bool:
        command_id = str(message.get("command_id") or "").strip()
        if not command_id:
            return False
        async with self._lock:
            pending = self._pending.get(command_id)
            if pending is not None and owner_id is not None and device_id is not None:
                if pending.key != (owner_id, device_id):
                    logger.warning(
                        "Received command_result for command_id=%s from wrong owner/device",
                        command_id,
                    )
                    return False
            if pending is not None:
                self._pending.pop(command_id, None)
        if pending is None:
            logger.warning("Received command_result for unknown command_id=%s", command_id)
            return False
        if not pending.future.done():
            pending.future.set_result(message)
        return True

    async def clear_for_tests(self) -> None:
        async with self._lock:
            for pending in self._pending.values():
                if not pending.future.done():
                    pending.future.set_exception(RuntimeError("Machine control registry reset"))
            self._pending.clear()
            self._connections.clear()

    def _fail_pending_for_key(self, key: tuple[int, str], message: str) -> None:
        for command_id, pending in list(self._pending.items()):
            if pending.key != key:
                continue
            self._pending.pop(command_id, None)
            if not pending.future.done():
                pending.future.set_exception(RuntimeError(message))


_registry: MachineControlChannelRegistry | None = None


def get_machine_control_channel_registry() -> MachineControlChannelRegistry:
    global _registry
    if _registry is None:
        _registry = MachineControlChannelRegistry()
    return _registry
