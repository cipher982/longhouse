from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_TRANSPORT_LEGACY_RUNNER
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_TRANSPORT_NONE
from zerg.services.managed_control_dispatcher import MISSING_LEGACY_RUNNER_METADATA_ERROR
from zerg.services.managed_control_dispatcher import dispatch_managed_control_command
from zerg.services.managed_control_dispatcher import select_managed_control_transport


def _session(**overrides):
    values = {
        "execution_home": "managed_local",
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


def test_select_managed_control_transport_defaults_to_legacy_runner():
    assert select_managed_control_transport(_session(source_runner_id=17)) == MANAGED_CONTROL_TRANSPORT_LEGACY_RUNNER


def test_select_managed_control_transport_requires_runner_metadata_for_now():
    assert select_managed_control_transport(_session(source_runner_id=None)) is None


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
