from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.services.live_session_dispatch import supports_live_text_dispatch_metadata
from zerg.services.machine_control_channel import get_machine_control_channel_registry
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_COMMAND_INTERRUPT
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_COMMAND_SEND_TEXT
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_COMMAND_STEER_TEXT
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_TRANSPORT_LEGACY_RUNNER
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_TRANSPORT_NONE
from zerg.services.managed_control_dispatcher import MISSING_LEGACY_RUNNER_METADATA_ERROR
from zerg.services.managed_control_dispatcher import dispatch_managed_control_command
from zerg.services.managed_control_dispatcher import select_managed_control_transport


def _session(**overrides):
    values = {
        "id": uuid4(),
        "device_id": "cinder",
        "provider": "codex",
        "execution_home": "managed_local",
        "managed_transport": "codex_app_server",
        "source_runner_id": 17,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeRunnerDispatcher:
    def __init__(self):
        self.calls: list[dict[str, object]] = []
        self.result = {
            "ok": True,
            "data": {
                "exit_code": 0,
                "stdout": "sent",
                "stderr": "",
            },
        }

    async def dispatch_job(self, *, db, owner_id, runner_id, command, timeout_secs, commis_id, run_id):
        self.calls.append(
            {
                "db": db,
                "owner_id": owner_id,
                "runner_id": runner_id,
                "command": command,
                "timeout_secs": timeout_secs,
                "commis_id": commis_id,
                "run_id": run_id,
            }
        )
        return self.result


class _FakeMachineWebSocket:
    def __init__(self):
        self.sent: list[dict[str, object]] = []

    async def send_json(self, message):
        self.sent.append(message)


async def _clear_machine_registry():
    await get_machine_control_channel_registry().clear_for_tests()


@pytest.fixture(autouse=True)
def _reset_machine_registry():
    asyncio.run(_clear_machine_registry())
    yield
    asyncio.run(_clear_machine_registry())


async def _connect_fake_engine(*, owner_id: int = 42, supports: list[str] | None = None) -> _FakeMachineWebSocket:
    websocket = _FakeMachineWebSocket()
    await get_machine_control_channel_registry().register(
        owner_id=owner_id,
        device_id="cinder",
        machine_name="cinder",
        engine_build="abc123",
        supports=supports or ["codex.send"],
        websocket=websocket,
    )
    return websocket


async def _complete_first_machine_command(websocket: _FakeMachineWebSocket, result):
    for _ in range(20):
        if websocket.sent:
            command_id = str(websocket.sent[0]["command_id"])
            await get_machine_control_channel_registry().complete_command(
                {
                    "type": "command_result",
                    "command_id": command_id,
                    **result,
                }
            )
            return
        await asyncio.sleep(0)
    raise AssertionError("expected a machine control command frame")


def test_select_managed_control_transport_defaults_to_legacy_runner():
    assert select_managed_control_transport(_session(source_runner_id=17)) == MANAGED_CONTROL_TRANSPORT_LEGACY_RUNNER


def test_select_managed_control_transport_requires_runner_metadata_for_now():
    assert select_managed_control_transport(_session(source_runner_id=None)) is None


def test_select_managed_control_transport_prefers_engine_channel_when_supported():
    async def _run():
        await _clear_machine_registry()
        try:
            await _connect_fake_engine(owner_id=42, supports=["codex.send"])
            assert (
                select_managed_control_transport(
                    _session(source_runner_id=17),
                    owner_id=42,
                    command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
                )
                == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
            )
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


def test_select_managed_control_transport_supports_claude_engine_channel():
    async def _run():
        await _clear_machine_registry()
        try:
            await _connect_fake_engine(owner_id=42, supports=["claude.send"])
            assert (
                select_managed_control_transport(
                    _session(
                        provider="claude",
                        managed_transport="claude_channel_bridge",
                        source_runner_id=None,
                    ),
                    owner_id=42,
                    command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
                )
                == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
            )
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


def test_select_managed_control_transport_supports_opencode_engine_channel():
    async def _run():
        await _clear_machine_registry()
        try:
            await _connect_fake_engine(owner_id=42, supports=["opencode.send"])
            assert (
                select_managed_control_transport(
                    _session(
                        provider="opencode",
                        managed_transport="opencode_server_bridge",
                        source_runner_id=None,
                    ),
                    owner_id=42,
                    command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
                )
                == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
            )
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


def test_select_managed_control_transport_supports_opencode_interrupt_engine_channel():
    async def _run():
        await _clear_machine_registry()
        try:
            await _connect_fake_engine(owner_id=42, supports=["opencode.interrupt"])
            assert (
                select_managed_control_transport(
                    _session(
                        provider="opencode",
                        managed_transport="opencode_server_bridge",
                        source_runner_id=None,
                    ),
                    owner_id=42,
                    command_type=MANAGED_CONTROL_COMMAND_INTERRUPT,
                )
                == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
            )
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


def test_select_managed_control_transport_routes_antigravity_send_over_engine_channel():
    async def _run():
        await _clear_machine_registry()
        try:
            await _connect_fake_engine(owner_id=42, supports=["antigravity.send"])
            assert (
                select_managed_control_transport(
                    _session(
                        provider="antigravity",
                        managed_transport="antigravity_hook_inbox",
                        source_runner_id=None,
                    ),
                    owner_id=42,
                    command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
                )
                == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
            )
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


def test_select_managed_control_transport_does_not_upgrade_legacy_antigravity_process():
    async def _run():
        await _clear_machine_registry()
        try:
            await _connect_fake_engine(owner_id=42, supports=["antigravity.send"])
            assert (
                select_managed_control_transport(
                    _session(
                        provider="antigravity",
                        managed_transport="antigravity_process",
                        source_runner_id=None,
                    ),
                    owner_id=42,
                    command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
                )
                is None
            )
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


@pytest.mark.parametrize(
    "command_type",
    [MANAGED_CONTROL_COMMAND_INTERRUPT, MANAGED_CONTROL_COMMAND_STEER_TEXT],
)
def test_select_managed_control_transport_rejects_antigravity_non_send_commands(command_type):
    async def _run():
        await _clear_machine_registry()
        try:
            await _connect_fake_engine(
                owner_id=42,
                supports=["antigravity.send", "antigravity.interrupt", "antigravity.steer"],
            )
            assert (
                select_managed_control_transport(
                    _session(
                        provider="antigravity",
                        managed_transport="antigravity_hook_inbox",
                        source_runner_id=None,
                    ),
                    owner_id=42,
                    command_type=command_type,
                )
                is None
            )
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


def test_dispatch_managed_control_command_uses_legacy_runner(monkeypatch):
    dispatcher = _FakeRunnerDispatcher()
    monkeypatch.setattr("zerg.services.managed_control_dispatcher.get_runner_job_dispatcher", lambda: dispatcher)
    db = object()

    result = asyncio.run(
        dispatch_managed_control_command(
            db=db,
            owner_id=42,
            session=_session(source_runner_id=23),
            command="longhouse codex-bridge send --session-id example",
            timeout_secs=9,
            commis_id="managed-control-test",
            run_id="run-1",
        )
    )

    assert result.ok is True
    assert result.transport == MANAGED_CONTROL_TRANSPORT_LEGACY_RUNNER
    assert result.data == {"exit_code": 0, "stdout": "sent", "stderr": ""}
    assert dispatcher.calls == [
        {
            "db": db,
            "owner_id": 42,
            "runner_id": 23,
            "command": "longhouse codex-bridge send --session-id example",
            "timeout_secs": 9,
            "commis_id": "managed-control-test",
            "run_id": "run-1",
        }
    ]


def test_dispatch_managed_control_command_has_no_transport_without_runner_metadata(monkeypatch):
    def _unexpected_dispatcher():
        raise AssertionError("runner dispatcher should not be used without a selected transport")

    monkeypatch.setattr("zerg.services.managed_control_dispatcher.get_runner_job_dispatcher", _unexpected_dispatcher)

    result = asyncio.run(
        dispatch_managed_control_command(
            db=object(),
            owner_id=42,
            session=_session(source_runner_id=None),
            command="longhouse codex-bridge send --session-id example",
            timeout_secs=9,
        )
    )

    assert result.ok is False
    assert result.transport == MANAGED_CONTROL_TRANSPORT_NONE
    assert result.error == MISSING_LEGACY_RUNNER_METADATA_ERROR


def test_dispatch_managed_control_command_uses_engine_channel_when_connected():
    async def _run():
        await _clear_machine_registry()
        try:
            websocket = await _connect_fake_engine(owner_id=42, supports=["codex.send"])
            session = _session(source_runner_id=None)
            completer = asyncio.create_task(
                _complete_first_machine_command(
                    websocket,
                    {
                        "ok": True,
                        "result": {"exit_code": 0, "stdout": "accepted"},
                    },
                )
            )
            result = await dispatch_managed_control_command(
                db=object(),
                owner_id=42,
                session=session,
                command="legacy command is unused for engine transport",
                timeout_secs=1,
                command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
                payload={"text": "continue"},
                commis_id="req-123",
            )
            await completer

            assert result.ok is True
            assert result.transport == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
            assert result.data == {"stdout": "accepted", "exit_code": 0, "stderr": ""}
            assert websocket.sent[0]["command_type"] == MANAGED_CONTROL_COMMAND_SEND_TEXT
            assert websocket.sent[0]["payload"] == {"provider": "codex", "text": "continue"}
            assert websocket.sent[0]["command_id"] == f"managed-control:{session.id}:session.send_text:req-123"
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


def test_dispatch_managed_control_command_sends_antigravity_provider_to_engine_channel():
    async def _run():
        await _clear_machine_registry()
        try:
            websocket = await _connect_fake_engine(owner_id=42, supports=["antigravity.send"])
            session = _session(
                provider="antigravity",
                managed_transport="antigravity_hook_inbox",
                source_runner_id=None,
            )
            completer = asyncio.create_task(
                _complete_first_machine_command(
                    websocket,
                    {
                        "ok": True,
                        "result": {"stdout": "accepted", "exit_code": 0, "stderr": ""},
                    },
                )
            )
            result = await dispatch_managed_control_command(
                db=object(),
                owner_id=42,
                session=session,
                command="legacy command is unused for engine transport",
                timeout_secs=1,
                command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
                payload={"text": "continue"},
                commis_id="req-agy",
            )
            await completer

            assert result.ok is True
            assert result.transport == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
            assert result.data == {"stdout": "accepted", "exit_code": 0, "stderr": ""}
            assert websocket.sent[0]["command_type"] == MANAGED_CONTROL_COMMAND_SEND_TEXT
            assert websocket.sent[0]["payload"] == {"provider": "antigravity", "text": "continue"}
            assert websocket.sent[0]["command_id"] == f"managed-control:{session.id}:session.send_text:req-agy"
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


def test_dispatch_managed_control_command_rejects_malformed_engine_success():
    async def _run():
        await _clear_machine_registry()
        try:
            websocket = await _connect_fake_engine(owner_id=42, supports=["codex.send"])
            completer = asyncio.create_task(
                _complete_first_machine_command(
                    websocket,
                    {
                        "ok": True,
                        "result": {"stdout": "accepted"},
                    },
                )
            )
            result = await dispatch_managed_control_command(
                db=object(),
                owner_id=42,
                session=_session(source_runner_id=None),
                command="legacy command is unused for engine transport",
                timeout_secs=1,
                command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
                payload={"text": "continue"},
            )
            await completer

            assert result.ok is False
            assert result.transport == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
            assert result.error == "Machine Agent control command returned malformed result"
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


def test_live_text_dispatch_metadata_accepts_engine_channel_without_runner_metadata():
    async def _run():
        await _connect_fake_engine(owner_id=42, supports=["codex.send"])
        assert (
            supports_live_text_dispatch_metadata(
                _session(source_runner_id=None),
                owner_id=42,
            )
            is True
        )

    asyncio.run(_run())


def test_live_text_dispatch_metadata_accepts_claude_engine_channel_without_runner_metadata():
    async def _run():
        await _connect_fake_engine(owner_id=42, supports=["claude.send"])
        assert (
            supports_live_text_dispatch_metadata(
                _session(provider="claude", managed_transport="claude_channel_bridge", source_runner_id=None),
                owner_id=42,
            )
            is True
        )

    asyncio.run(_run())


def test_live_text_dispatch_metadata_accepts_opencode_engine_channel_without_runner_metadata():
    async def _run():
        await _connect_fake_engine(owner_id=42, supports=["opencode.send"])
        assert (
            supports_live_text_dispatch_metadata(
                _session(provider="opencode", managed_transport="opencode_server_bridge", source_runner_id=None),
                owner_id=42,
            )
            is True
        )

    asyncio.run(_run())
