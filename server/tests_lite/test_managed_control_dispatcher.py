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
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_COMMAND_ANSWER_PAUSE
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_COMMAND_INTERRUPT
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_COMMAND_SEND_TEXT
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_COMMAND_STEER_TEXT
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_TRANSPORT_NONE
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_UNAVAILABLE_ERROR
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


def test_select_managed_control_transport_requires_engine_channel_even_with_runner_metadata():
    assert (
        select_managed_control_transport(
            _session(source_runner_id=17),
            owner_id=42,
            command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
        )
        is None
    )


def test_select_managed_control_transport_returns_none_without_engine_channel():
    assert select_managed_control_transport(_session(source_runner_id=None)) is None


def test_select_managed_control_transport_uses_engine_channel_when_supported():
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


def test_select_managed_control_transport_requires_engine_channel_for_pause_answers():
    assert (
        select_managed_control_transport(
            _session(provider="claude", managed_transport="claude_channel_bridge", source_runner_id=17),
            owner_id=42,
            command_type=MANAGED_CONTROL_COMMAND_ANSWER_PAUSE,
        )
        is None
    )


def test_select_managed_control_transport_supports_claude_pause_answer_engine_channel():
    async def _run():
        await _clear_machine_registry()
        try:
            await _connect_fake_engine(owner_id=42, supports=["claude.answer_pause"])
            assert (
                select_managed_control_transport(
                    _session(
                        provider="claude",
                        managed_transport="claude_channel_bridge",
                        source_runner_id=17,
                    ),
                    owner_id=42,
                    command_type=MANAGED_CONTROL_COMMAND_ANSWER_PAUSE,
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


def test_select_managed_control_transport_rejects_antigravity_process_transport():
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


def test_dispatch_managed_control_command_has_no_transport_without_engine_channel():
    result = asyncio.run(
        dispatch_managed_control_command(
            db=object(),
            owner_id=42,
            session=_session(source_runner_id=23),
            timeout_secs=9,
            command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
        )
    )

    assert result.ok is False
    assert result.transport == MANAGED_CONTROL_TRANSPORT_NONE
    assert result.error == MANAGED_CONTROL_UNAVAILABLE_ERROR


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


def test_dispatch_managed_control_command_routes_opencode_send_over_engine_channel():
    async def _run():
        await _clear_machine_registry()
        try:
            websocket = await _connect_fake_engine(owner_id=42, supports=["opencode.send"])
            session = _session(
                provider="opencode",
                managed_transport="opencode_server_bridge",
                source_runner_id=None,
            )
            completer = asyncio.create_task(
                _complete_first_machine_command(
                    websocket,
                    {
                        "ok": True,
                        "result": {
                            "exit_code": 0,
                            "stdout": "",
                            "stderr": "",
                            "provider": "opencode",
                            "transport": "opencode_server_bridge",
                            "provider_session_id": "ses_test",
                        },
                    },
                )
            )
            result = await dispatch_managed_control_command(
                db=object(),
                owner_id=42,
                session=session,
                timeout_secs=1,
                command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
                payload={"text": "hello from browser"},
                commis_id="req-opencode-send",
            )
            await completer

            assert result.ok is True
            assert result.transport == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
            assert result.data == {
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "provider": "opencode",
                "transport": "opencode_server_bridge",
                "provider_session_id": "ses_test",
            }
            assert websocket.sent[0]["command_type"] == MANAGED_CONTROL_COMMAND_SEND_TEXT
            assert websocket.sent[0]["payload"] == {
                "provider": "opencode",
                "text": "hello from browser",
            }
            assert (
                websocket.sent[0]["command_id"] == f"managed-control:{session.id}:session.send_text:req-opencode-send"
            )
        finally:
            await _clear_machine_registry()

    asyncio.run(_run())


def test_dispatch_managed_control_command_routes_opencode_interrupt_over_engine_channel():
    async def _run():
        await _clear_machine_registry()
        try:
            websocket = await _connect_fake_engine(owner_id=42, supports=["opencode.interrupt"])
            session = _session(
                provider="opencode",
                managed_transport="opencode_server_bridge",
                source_runner_id=None,
            )
            completer = asyncio.create_task(
                _complete_first_machine_command(
                    websocket,
                    {
                        "ok": True,
                        "result": {
                            "exit_code": 0,
                            "stdout": "",
                            "stderr": "",
                            "provider": "opencode",
                            "transport": "opencode_server_bridge",
                            "provider_session_id": "ses_test",
                        },
                    },
                )
            )
            result = await dispatch_managed_control_command(
                db=object(),
                owner_id=42,
                session=session,
                timeout_secs=1,
                command_type=MANAGED_CONTROL_COMMAND_INTERRUPT,
                commis_id="req-opencode-interrupt",
            )
            await completer

            assert result.ok is True
            assert result.transport == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
            assert result.data == {
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "provider": "opencode",
                "transport": "opencode_server_bridge",
                "provider_session_id": "ses_test",
            }
            assert websocket.sent[0]["command_type"] == MANAGED_CONTROL_COMMAND_INTERRUPT
            assert websocket.sent[0]["payload"] == {"provider": "opencode"}
            assert websocket.sent[0]["command_id"] == (
                f"managed-control:{session.id}:session.interrupt:req-opencode-interrupt"
            )
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
