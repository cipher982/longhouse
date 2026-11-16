"""Gateways that retry send their whole budget as the read deadline (catalog-read-path.md).

catalogd cuts a read off at its request deadline. A gateway that sent each
short attempt's socket timeout instead would have every slow read cut before
its retry could join it.
"""

from __future__ import annotations

import time

import zerg.auth.catalog_gateway as auth_gateway
import zerg.services.catalog_read_gateway as read_gateway


def _capture(monkeypatch, module):
    seen: list[dict] = []

    def fake_call(_socket_path, method, *, params=None, timeout_seconds=None, deadline_mono_ns=None):
        seen.append({"method": method, "timeout_seconds": timeout_seconds, "deadline_mono_ns": deadline_mono_ns})
        return {"found": False}

    monkeypatch.setattr(module, "call_catalogd_sync", fake_call)
    monkeypatch.setattr(module, "catalogd_paths", lambda: ("/nonexistent/live.db", "/nonexistent/catalogd.sock"))
    return seen


def test_the_read_gateway_sends_its_whole_budget_as_the_deadline(monkeypatch):
    seen = _capture(monkeypatch, read_gateway)
    started = time.monotonic_ns()
    read_gateway._call("session.read.v2", {"session_id": "s"})
    (call,) = seen
    deadline_s, attempt_s = read_gateway._READ_BUDGETS["session.read.v2"]
    assert call["timeout_seconds"] <= attempt_s
    budget = (call["deadline_mono_ns"] - started) / 1e9
    assert attempt_s < budget <= deadline_s + 0.05, budget


def test_the_auth_gateway_sends_its_whole_budget_as_the_deadline(monkeypatch):
    seen = _capture(monkeypatch, auth_gateway)
    started = time.monotonic_ns()
    auth_gateway._call("auth.device.resolve.v2", {})
    (call,) = seen
    budget = (call["deadline_mono_ns"] - started) / 1e9
    assert call["timeout_seconds"] <= auth_gateway.AUTH_CATALOG_ATTEMPT_TIMEOUT_SECONDS
    assert budget > auth_gateway.AUTH_CATALOG_ATTEMPT_TIMEOUT_SECONDS or (
        auth_gateway.AUTH_CATALOG_ATTEMPT_TIMEOUT_SECONDS >= auth_gateway.AUTH_CATALOG_CALL_DEADLINE_SECONDS
    )
    assert budget <= auth_gateway.AUTH_CATALOG_CALL_DEADLINE_SECONDS + 0.05
