from __future__ import annotations

import asyncio

from zerg.services.machine_control_channel import MachineControlChannelRegistry


class _FakeWebSocket:
    def __init__(self):
        self.sent: list[dict[str, object]] = []

    async def send_json(self, message):
        self.sent.append(message)


async def _wait_for_sent(websocket: _FakeWebSocket) -> dict[str, object]:
    for _ in range(20):
        if websocket.sent:
            return websocket.sent[0]
        await asyncio.sleep(0)
    raise AssertionError("expected a command frame to be sent")


def test_machine_control_registry_round_trips_command_result():
    async def _run():
        registry = MachineControlChannelRegistry()
        websocket = _FakeWebSocket()
        await registry.register(
            owner_id=7,
            device_id="cinder",
            machine_name="cinder",
            engine_build="abc123",
            supports=["codex.send"],
            websocket=websocket,
        )

        task = asyncio.create_task(
            registry.send_command(
                owner_id=7,
                device_id="cinder",
                session_id="session-1",
                command_type="session.send_text",
                payload={"text": "continue"},
                timeout_secs=1,
                command_id="cmd-1",
            )
        )
        frame = await _wait_for_sent(websocket)
        assert frame == {
            "type": "command",
            "command_id": "cmd-1",
            "session_id": "session-1",
            "command_type": "session.send_text",
            "payload": {"text": "continue"},
        }

        completed = await registry.complete_command(
            {
                "type": "command_result",
                "command_id": "cmd-1",
                "ok": True,
                "result": {"exit_code": 0},
            }
        )
        response = await task

        assert completed is True
        assert response.transport_ok is True
        assert response.message is not None
        assert response.message["ok"] is True

    asyncio.run(_run())


def test_machine_control_registry_rejects_result_from_wrong_connection():
    async def _run():
        registry = MachineControlChannelRegistry()
        websocket = _FakeWebSocket()
        await registry.register(
            owner_id=7,
            device_id="cinder",
            machine_name="cinder",
            engine_build="abc123",
            supports=["codex.send"],
            websocket=websocket,
        )

        task = asyncio.create_task(
            registry.send_command(
                owner_id=7,
                device_id="cinder",
                session_id="session-1",
                command_type="session.send_text",
                payload={"text": "continue"},
                timeout_secs=1,
                command_id="cmd-owner-bound",
            )
        )
        await _wait_for_sent(websocket)

        rejected = await registry.complete_command(
            {
                "type": "command_result",
                "command_id": "cmd-owner-bound",
                "ok": True,
                "result": {"exit_code": 0},
            },
            owner_id=99,
            device_id="other-machine",
        )
        assert rejected is False
        assert task.done() is False

        completed = await registry.complete_command(
            {
                "type": "command_result",
                "command_id": "cmd-owner-bound",
                "ok": True,
                "result": {"exit_code": 0},
            },
            owner_id=7,
            device_id="cinder",
        )
        response = await task

        assert completed is True
        assert response.transport_ok is True

    asyncio.run(_run())


def test_machine_control_registry_reuses_inflight_command_id():
    async def _run():
        registry = MachineControlChannelRegistry()
        websocket = _FakeWebSocket()
        await registry.register(
            owner_id=7,
            device_id="cinder",
            machine_name="cinder",
            engine_build="abc123",
            supports=["codex.send"],
            websocket=websocket,
        )

        task_one = asyncio.create_task(
            registry.send_command(
                owner_id=7,
                device_id="cinder",
                session_id="session-1",
                command_type="session.send_text",
                payload={"text": "continue"},
                timeout_secs=1,
                command_id="cmd-shared",
            )
        )
        task_two = asyncio.create_task(
            registry.send_command(
                owner_id=7,
                device_id="cinder",
                session_id="session-1",
                command_type="session.send_text",
                payload={"text": "continue"},
                timeout_secs=1,
                command_id="cmd-shared",
            )
        )
        await _wait_for_sent(websocket)
        await asyncio.sleep(0)
        assert len(websocket.sent) == 1

        completed = await registry.complete_command(
            {
                "type": "command_result",
                "command_id": "cmd-shared",
                "ok": True,
                "result": {"exit_code": 0},
            },
            owner_id=7,
            device_id="cinder",
        )
        response_one, response_two = await asyncio.gather(task_one, task_two)

        assert completed is True
        assert response_one.transport_ok is True
        assert response_two.transport_ok is True
        assert response_one.message == response_two.message

    asyncio.run(_run())


def test_machine_control_registry_fails_inflight_command_on_disconnect():
    """A Machine Agent that disconnects mid-command must fail the pending send.

    This is the most plausible "no babysitting" steer-loop failure: the engine
    drops its control WebSocket while a send_text is in flight. The pending
    future must resolve to transport_ok=False rather than hanging until timeout,
    so the API can surface a clean error and not mark the input delivered.
    """

    async def _run():
        registry = MachineControlChannelRegistry()
        websocket = _FakeWebSocket()
        await registry.register(
            owner_id=7,
            device_id="cinder",
            machine_name="cinder",
            engine_build="abc123",
            supports=["codex.send"],
            websocket=websocket,
        )

        task = asyncio.create_task(
            registry.send_command(
                owner_id=7,
                device_id="cinder",
                session_id="session-1",
                command_type="session.send_text",
                payload={"text": "continue"},
                timeout_secs=30,
                command_id="cmd-disconnect",
            )
        )
        await _wait_for_sent(websocket)

        # Engine drops the control channel while the command is still pending.
        unregistered = await registry.unregister(owner_id=7, device_id="cinder", websocket=websocket)
        response = await task

        assert unregistered is True
        assert response.transport_ok is False
        assert response.error == "Machine control channel disconnected"
        # The device is gone; a late result for the dropped command is rejected.
        completed = await registry.complete_command(
            {
                "type": "command_result",
                "command_id": "cmd-disconnect",
                "ok": True,
                "result": {"exit_code": 0},
            }
        )
        assert completed is False

    asyncio.run(_run())


def test_machine_control_registry_fails_inflight_command_on_reconnect_replace():
    """A reconnecting Machine Agent replaces the channel and fails old pending sends.

    If the engine reconnects (new WebSocket for the same owner/device) while a
    command is in flight on the prior connection, the stale pending command must
    fail rather than wait for a result that will never arrive on the old socket.
    """

    async def _run():
        registry = MachineControlChannelRegistry()
        first_socket = _FakeWebSocket()
        await registry.register(
            owner_id=7,
            device_id="cinder",
            machine_name="cinder",
            engine_build="abc123",
            supports=["codex.send"],
            websocket=first_socket,
        )

        task = asyncio.create_task(
            registry.send_command(
                owner_id=7,
                device_id="cinder",
                session_id="session-1",
                command_type="session.send_text",
                payload={"text": "continue"},
                timeout_secs=30,
                command_id="cmd-replaced",
            )
        )
        await _wait_for_sent(first_socket)

        # Engine reconnects: same owner/device, fresh socket replaces the old one.
        second_socket = _FakeWebSocket()
        await registry.register(
            owner_id=7,
            device_id="cinder",
            machine_name="cinder",
            engine_build="abc123",
            supports=["codex.send"],
            websocket=second_socket,
        )
        response = await task

        assert response.transport_ok is False
        assert response.error == "Machine control channel was replaced"
        assert registry.is_online(owner_id=7, device_id="cinder") is True

    asyncio.run(_run())


def test_machine_control_registry_reports_offline_device():
    async def _run():
        registry = MachineControlChannelRegistry()
        response = await registry.send_command(
            owner_id=7,
            device_id="missing",
            session_id="session-1",
            command_type="session.send_text",
            payload={"text": "continue"},
            timeout_secs=1,
        )

        assert response.transport_ok is False
        assert response.error == "Machine Agent control channel is offline"

    asyncio.run(_run())


def test_machine_control_registry_can_send_without_pending_future():
    async def _run():
        registry = MachineControlChannelRegistry()
        websocket = _FakeWebSocket()
        await registry.register(
            owner_id=7,
            device_id="cinder",
            machine_name="cinder",
            engine_build="abc123",
            supports=["provider.live_proof"],
            websocket=websocket,
        )

        response = await registry.send_command_nowait(
            owner_id=7,
            device_id="cinder",
            session_id=None,
            command_type="provider.live_proof",
            payload={"provider": "claude"},
            command_id="machine-op:test",
        )

        assert response.transport_ok is True
        assert websocket.sent == [
            {
                "type": "command",
                "command_id": "machine-op:test",
                "command_type": "provider.live_proof",
                "payload": {"provider": "claude"},
            }
        ]

        completed = await registry.complete_command(
            {
                "type": "command_result",
                "command_id": "machine-op:test",
                "ok": True,
                "result": {"exit_code": 0},
            },
            owner_id=7,
            device_id="cinder",
        )
        assert completed is False

    asyncio.run(_run())
