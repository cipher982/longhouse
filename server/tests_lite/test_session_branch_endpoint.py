"""The branch endpoint's refusals.

The happy path is proven end to end by the installed-binary canary; what this
file pins is that the endpoint refuses for the right reason and never creates a
half-made session when it does. Each gate exists because something silently
worse happens without it.
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


def _parent(*, provider: str, resume: SessionActionAvailability):
    """A parent session response shaped the way the projector serves one."""

    control = SimpleNamespace(actions=SimpleNamespace(resume=resume))
    return SimpleNamespace(provider=provider, session_state=SimpleNamespace(control=control))


def _client(monkeypatch, *, parent, posture: str | None = "bypass", branch=None):
    from zerg.routers import agents_sessions
    from zerg.routers import session_chat

    monkeypatch.setattr(
        agents_sessions,
        "session_detail_payload",
        lambda **kwargs: parent,
    )
    monkeypatch.setattr(
        session_chat,
        "_branch_permission_posture",
        _async_value(posture),
    )
    if branch is not None:
        monkeypatch.setattr(session_chat, "create_branch_with_first_turn", branch)
    api_app.dependency_overrides[get_current_browser_route_user] = lambda: SimpleNamespace(id=1)
    api_app.dependency_overrides[session_chat._catalog_control_db_dependency] = lambda: None
    return TestClient(api_app, raise_server_exceptions=False)


def _async_value(value):
    async def _call(**kwargs):
        del kwargs
        return value

    return _call


def _post(client, parent_id=None):
    return client.post(
        f"/sessions/{parent_id or uuid4()}/branches",
        json={"message": "keep going", "client_request_id": "branch-1"},
    )


def test_branch_refuses_with_the_reason_resume_reports(monkeypatch):
    """The button and the endpoint must never disagree.

    Both read one predicate. If the endpoint reimplemented availability, a
    session could offer a branch it cannot make, or refuse one it could.
    """

    parent = _parent(
        provider="codex",
        resume=SessionActionAvailability(state="unavailable", reason="contract_missing"),
    )
    try:
        with _client(monkeypatch, parent=parent) as client:
            response = _post(client)
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "contract_missing"


def test_branch_refuses_a_provider_that_cannot_fork(monkeypatch):
    """Resuming and branching are different upstream surfaces.

    Claude can resume a conversation and cannot yet fork one, so resume
    availability alone would have offered a branch that could not be built.
    """

    parent = _parent(provider="claude", resume=SessionActionAvailability(state="available"))
    try:
        with _client(monkeypatch, parent=parent) as client:
            response = _post(client)
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "fork_unsupported"


def test_branch_refuses_when_the_parents_approvals_are_not_positively_known(monkeypatch):
    """Absence of evidence is not bypass.

    permission_mode is non-null with a bypass default and is manufactured in
    more than one place, so a stored bypass may only mean nobody said
    otherwise. Console runs with approvals disabled entirely, so branching on
    an unproven posture would silently drop prompts the user chose to keep.
    """

    parent = _parent(provider="codex", resume=SessionActionAvailability(state="available"))
    try:
        with _client(monkeypatch, parent=parent, posture=None) as client:
            unknown = _post(client)
        with _client(monkeypatch, parent=parent, posture="provider_local") as client:
            stricter = _post(client)
    finally:
        api_app.dependency_overrides.clear()

    assert unknown.status_code == 409
    assert unknown.json()["detail"]["code"] == "permission_mode_unsupported"
    assert stricter.status_code == 409
    assert stricter.json()["detail"]["code"] == "permission_mode_unsupported"


def test_branch_returns_the_created_child_and_its_first_turn(monkeypatch):
    """A branch that passes every gate reports both ids the client needs."""

    from zerg.services.console_turns import CreatedBranch

    child_id, thread_id, turn_id, run_id = uuid4(), uuid4(), uuid4(), uuid4()
    parent = _parent(provider="codex", resume=SessionActionAvailability(state="available"))
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
