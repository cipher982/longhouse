"""A remote steer waits for the Runtime Host to serve the turn, because the host refuses a steer until it does."""

from __future__ import annotations

from typing import Any

import pytest

from zerg.qa import pi_family_helm as helm


def _diagnostic(primary: str | None) -> dict[str, Any]:
    return {"explain": {"presentation_keys": {"primary": primary}}}


def test_wait_returns_as_soon_as_the_host_serves_a_running_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    served = iter([_diagnostic("idle"), _diagnostic("idle"), _diagnostic("executing")])
    monkeypatch.setattr(helm, "_runtime_get", lambda *_args: next(served))

    result = helm._wait_served_turn("http://host", "token", "session", timeout=5, interval=0)

    assert result["served_as_running"] is True
    assert result["served_primary_key"] == "executing"


def test_wait_accepts_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(helm, "_runtime_get", lambda *_args: _diagnostic("thinking"))

    assert helm._wait_served_turn("http://host", "token", "session", timeout=5, interval=0)["served_as_running"] is True


def test_wait_records_a_turn_the_host_never_serves_and_leaves_the_refusal_to_the_steer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(helm, "_runtime_get", lambda *_args: _diagnostic("idle"))

    result = helm._wait_served_turn("http://host", "token", "session", timeout=0.05, interval=0.01)

    assert result["served_as_running"] is False
    assert result["served_primary_key"] == "idle"


def test_wait_outlasts_a_transient_read_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def read(*_args: Any) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return _diagnostic("executing")

    monkeypatch.setattr(helm, "_runtime_get", read)
    monkeypatch.setattr(helm, "_is_transient_runtime_read_error", lambda _exc: True)

    assert helm._wait_served_turn("http://host", "token", "session", timeout=5, interval=0)["served_as_running"] is True
