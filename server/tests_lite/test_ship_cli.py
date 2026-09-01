"""Tests for the one-shot ship command on the Runtime Host CLI."""

from __future__ import annotations

import pytest
from click.exceptions import Exit as ClickExit

from zerg.cli import connect


def test_ship_requires_configured_url(monkeypatch):
    monkeypatch.setattr(connect, "get_zerg_url", lambda config_dir=None: None)

    with pytest.raises(ClickExit) as exc:
        connect.ship(url=None, token=None, file=None, claude_dir=None, verbose=False, quiet=False)
    assert exc.value.exit_code == 1


def test_ship_drives_the_engine_binary_not_a_facade_verb(monkeypatch):
    """`ship` is a Runtime Host verb that delegates to `longhouse-engine ship`.

    The native `longhouse` facade has no `ship`, so shelling out to it would
    always fail with an unrecognized subcommand.
    """
    calls: list[list[str]] = []

    monkeypatch.setattr(connect, "get_zerg_url", lambda config_dir=None: "https://longhouse.test")
    monkeypatch.setattr(connect, "load_token", lambda config_dir=None: "device-token")
    monkeypatch.setattr(connect, "get_engine_executable", lambda: "/opt/longhouse/longhouse-engine")
    monkeypatch.setattr(
        connect.subprocess,
        "run",
        lambda args, **kwargs: calls.append(args) or type("R", (), {"returncode": 0})(),
    )

    with pytest.raises(ClickExit) as exc:
        connect.ship(url=None, token=None, file=None, claude_dir=None, verbose=False, quiet=True)

    assert exc.value.exit_code == 0
    assert calls == [
        [
            "/opt/longhouse/longhouse-engine",
            "ship",
            "--url",
            "https://longhouse.test",
            "--token",
            "device-token",
        ]
    ]
