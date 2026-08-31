"""The branch endpoint's refusals.

The happy path is proven end to end by the installed-binary canary; what this
file pins is that the endpoint refuses for the right reason and never creates a
half-made session when it does.

The endpoint reads one served fact rather than re-deriving availability. These
tests therefore assert the plumbing -- that the refusal reason reaches the
client verbatim -- while the projector tests assert which conditions produce
which reason. Splitting them that way is the point: two implementations of "can
this be branched" would eventually disagree, and a button offering something the
server refuses is the symptom.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.dependencies.browser_route_auth import get_current_browser_route_user  # noqa: E402
from zerg.main import api_app  # noqa: E402
from zerg.services.session_state_contract import SessionActionAvailability  # noqa: E402


def _parent(*, branch: SessionActionAvailability):
    """A parent session response shaped the way the projector serves one."""

    control = SimpleNamespace(actions=SimpleNamespace(branch=branch))
    return SimpleNamespace(provider="codex", session_state=SimpleNamespace(control=control))


def _async_value(value):
    async def _call(**kwargs):
        del kwargs
        return value

    return _call


def _client(monkeypatch, *, parent, branch=None):
    from zerg.routers import agents_sessions
    from zerg.routers import session_chat

    monkeypatch.setattr(agents_sessions, "session_detail_payload", lambda **kwargs: parent)
    if branch is not None:
        monkeypatch.setattr(session_chat, "create_branch_with_first_turn", branch)
    api_app.dependency_overrides[get_current_browser_route_user] = lambda: SimpleNamespace(id=1)
    api_app.dependency_overrides[session_chat._catalog_control_db_dependency] = lambda: None
    return TestClient(api_app, raise_server_exceptions=False)


def _post(client, parent_id=None):
    return client.post(
        f"/sessions/{parent_id or uuid4()}/branches",
        json={"message": "keep going", "client_request_id": "branch-1"},
    )


def test_branch_refuses_with_the_served_reason_verbatim(monkeypatch):
    """Whatever the projector said, the client hears.

    Rewriting the reason here would leave the UI unable to explain a refusal it
    is already rendering a disabled control for.
    """

    for reason in (
        "contract_missing",
        "fork_unsupported",
        "permission_mode_unknown",
        "permission_mode_unsupported",
        "machine_offline",
    ):
        parent = _parent(branch=SessionActionAvailability(state="unavailable", reason=reason))
        try:
            with _client(monkeypatch, parent=parent) as client:
                response = _post(client)
        finally:
            api_app.dependency_overrides.clear()

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == reason


def test_branch_refuses_when_the_session_has_no_control_facts(monkeypatch):
    """An absent control block is unknown, never permission."""

    parent = SimpleNamespace(provider="codex", session_state=SimpleNamespace(control=None))
    try:
        with _client(monkeypatch, parent=parent) as client:
            response = _post(client)
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "control_unknown"


def test_branch_returns_the_created_child_and_its_first_turn(monkeypatch):
    """A branch that passes the gate reports both ids the client needs."""

    from zerg.services.console_turns import CreatedBranch

    child_id, thread_id, turn_id, run_id = uuid4(), uuid4(), uuid4(), uuid4()
    parent = _parent(branch=SessionActionAvailability(state="available"))
    created = CreatedBranch(
        session_id=child_id,
        thread_id=thread_id,
        turn_id=turn_id,
        run_id=run_id,
        state="active",
        created=True,
    )
    try:
        with _client(monkeypatch, parent=parent, branch=_async_value(created)) as client:
            response = _post(client)
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["session_id"] == str(child_id)
    assert body["turn_id"] == str(turn_id)
    assert body["state"] == "active"
    assert body["created"] is True
