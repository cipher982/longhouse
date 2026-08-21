from __future__ import annotations

from zerg.qa.runtime_host_canary_isolation import hide_and_verify_canary_isolation


def test_canary_isolation_proves_all_user_surface_axes_and_preserves_retrieval():
    calls: list[tuple[str, str, dict | None]] = []

    def request(path: str, method: str, body: dict | None) -> dict:
        calls.append((path, method, body))
        if path.endswith("timeline-visibility"):
            return {"hidden": True}
        if path == "sessions/session-1":
            return {"id": "session-1", "user_messages": 0}
        if path.startswith("machines/"):
            return {"workspaces": [{"path": "/safe/workspace"}]}
        return {"sessions": []}

    receipt = hide_and_verify_canary_isolation(
        request,
        session_id="session-1",
        provider="codex",
        project="factory-proof-1",
        device_id="machine-1",
        cwd="/tmp/factory-proof-1",
        owned_processes_dead=lambda: True,
        timeout_seconds=0.1,
    )

    assert receipt["status"] == "pass"
    assert all(receipt["axes"].values())
    assert calls[0] == ("sessions/session-1/timeline-visibility", "PATCH", {"hidden": True})


def test_canary_isolation_refuses_title_debt_even_when_other_surfaces_are_clean():
    def request(path: str, _method: str, _body: dict | None) -> dict:
        if path.endswith("timeline-visibility"):
            return {"hidden": True}
        if path == "sessions/session-1":
            return {"id": "session-1", "user_messages": 1}
        if path.startswith("machines/"):
            return {"workspaces": []}
        return {"sessions": []}

    receipt = hide_and_verify_canary_isolation(
        request,
        session_id="session-1",
        provider="codex",
        project="factory-proof-1",
        device_id="machine-1",
        cwd="/tmp/factory-proof-1",
        owned_processes_dead=lambda: True,
        timeout_seconds=0.02,
    )

    assert receipt["status"] == "fail"
    assert receipt["axes"]["title_debt_absent"] is False
