from __future__ import annotations

from io import StringIO

import zerg.qa.runtime_host_canary_isolation as isolation
from zerg.qa.runtime_host_canary_isolation import hide_and_verify_canary_isolation


def test_runtime_host_request_routes_through_machine_api(monkeypatch):
    urls: list[str] = []

    def urlopen(request, timeout):
        assert timeout == 15
        urls.append(request.full_url)
        return StringIO("{}")

    monkeypatch.setattr(isolation.urllib.request, "urlopen", urlopen)

    isolation.runtime_host_request("https://runtime.example", "token", "sessions?limit=1")

    assert urls == ["https://runtime.example/api/agents/sessions?limit=1"]


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
    assert sum(path.startswith("sessions?") for path, _method, _body in calls) == 2
    assert not any(path.startswith("/api/") for path, _method, _body in calls)
    assert not any(path.startswith("sessions/active?") for path, _method, _body in calls)


def test_canary_isolation_fails_when_session_remains_on_broad_user_surface():
    def request(path: str, _method: str, _body: dict | None) -> dict:
        if path.endswith("timeline-visibility"):
            return {"hidden": True}
        if path == "sessions/session-1":
            return {"id": "session-1", "user_messages": 0}
        if path.startswith("machines/"):
            return {"workspaces": []}
        if path.startswith("sessions?") and "provider=" not in path:
            return {"sessions": [{"id": "session-1"}]}
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
    assert receipt["axes"]["default_timeline_absent"] is True
    assert receipt["axes"]["open_absent"] is False


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


def test_canary_isolation_accepts_the_products_explicit_resume_seed_marker():
    def request(path: str, _method: str, _body: dict | None) -> dict:
        if path.endswith("timeline-visibility"):
            return {"hidden": True}
        if path == "sessions/session-1":
            return {
                "id": "session-1",
                "user_messages": 1,
                "first_user_message_preview": "_RESUME_SEED_LONGHOUSE_CODEX_HELM_fixture",
            }
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
        timeout_seconds=0.1,
    )

    assert receipt["status"] == "pass"
    assert receipt["title_debt_basis"] == "resume_seed_marker"


def test_provider_factory_origin_is_not_misclassified_as_title_debt():
    def request(path: str, _method: str, _body: dict | None) -> dict:
        if path.endswith("timeline-visibility"):
            return {"hidden": True}
        if path == "sessions/session-1":
            return {
                "id": "session-1",
                "user_messages": 1,
                "first_user_message_preview": "Exercise the real human launch path",
            }
        if path.startswith("machines/"):
            return {"workspaces": []}
        return {"sessions": []}

    receipt = hide_and_verify_canary_isolation(
        request,
        session_id="session-1",
        provider="codex",
        project="provider-factory-codex-launch-fixture",
        device_id="provider-factory-resume",
        cwd="/tmp/lch-fixture/workspace",
        owned_processes_dead=lambda: True,
        timeout_seconds=0.1,
    )

    assert receipt["status"] == "pass"
    assert receipt["title_debt_basis"] == "origin_ineligible"
