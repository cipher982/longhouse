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
