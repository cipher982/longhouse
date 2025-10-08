import contextlib

import pytest

from zerg.crud import crud
from zerg.main import app


def _make_admin_user(db_session):
    user = crud.get_user_by_email(db_session, "admin@local")
    if user is None:
        user = crud.create_user(db_session, email="admin@local", provider=None, role="ADMIN")
    else:
        user.role = "ADMIN"  # type: ignore[attr-defined]
        db_session.commit()
    return user


@pytest.mark.asyncio
async def test_non_admin_create_agent_disallowed_model(client, db_session, _dev_user, monkeypatch):
    # Restrict non-admins to a cheap model
    monkeypatch.setenv("ALLOWED_MODELS_NON_ADMIN", "gpt-4o-mini")

    # Attempt to create agent with a disallowed model
    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: _dev_user
    try:
        resp = client.post(
            "/api/agents",
            json={
                "name": "NA agent",
                "system_instructions": "sys",
                "task_instructions": "task",
                "model": "gpt-4o",  # not in allowlist
                "schedule": None,
                "config": {},
            },
        )
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]
    assert resp.status_code == 422, resp.text
    assert "not allowed" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_non_admin_create_agent_allowed_model(client, db_session, _dev_user, monkeypatch):
    monkeypatch.setenv("ALLOWED_MODELS_NON_ADMIN", "gpt-4o-mini")

    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: _dev_user
    try:
        resp = client.post(
            "/api/agents",
            json={
                "name": "OK agent",
                "system_instructions": "sys",
                "task_instructions": "task",
                "model": "gpt-4o-mini",
                "schedule": None,
                "config": {},
            },
        )
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["model"] == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_admin_bypasses_model_allowlist(client, db_session, monkeypatch):
    # Restrict allowlist, but override current_user to ADMIN
    monkeypatch.setenv("ALLOWED_MODELS_NON_ADMIN", "gpt-4o-mini")
    admin = _make_admin_user(db_session)

    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: admin
    try:
        resp = client.post(
            "/api/agents",
            json={
                "name": "Admin agent",
                "system_instructions": "sys",
                "task_instructions": "task",
                "model": "gpt-4o",  # disallowed for non-admins
                "schedule": None,
                "config": {},
            },
        )
    finally:
        # Clean override regardless of assertion outcome
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]

    assert resp.status_code == 201, resp.text
    assert resp.json()["model"] == "gpt-4o"


@pytest.mark.asyncio
async def test_models_endpoint_filtered_for_non_admin(client, db_session, _dev_user, monkeypatch):
    monkeypatch.setenv("ALLOWED_MODELS_NON_ADMIN", "gpt-4o-mini")
    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: _dev_user
    try:
        resp = client.get("/api/models/")
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]
    assert resp.status_code == 200
    ids = {m["id"] for m in resp.json()}
    assert ids == {"gpt-4o-mini"}


@pytest.mark.asyncio
async def test_models_endpoint_admin_sees_all(client, db_session, monkeypatch):
    monkeypatch.setenv("ALLOWED_MODELS_NON_ADMIN", "gpt-4o-mini")
    admin = _make_admin_user(db_session)

    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: admin
    try:
        resp = client.get("/api/models/")
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]

    assert resp.status_code == 200
    ids = {m["id"] for m in resp.json()}
    # Registry includes more than the single allowed id
    assert "gpt-4o-mini" in ids and len(ids) > 1


@pytest.mark.asyncio
async def test_non_admin_update_agent_disallowed_model(client, db_session, _dev_user, monkeypatch):
    monkeypatch.setenv("ALLOWED_MODELS_NON_ADMIN", "gpt-4o-mini")
    # Ensure current user is non-admin dev user
    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: _dev_user
    try:
        # Create an allowed agent first
        resp = client.post(
            "/api/agents",
            json={
                "name": "Agent",
                "system_instructions": "sys",
                "task_instructions": "task",
                "model": "gpt-4o-mini",
                "schedule": None,
                "config": {},
            },
        )
        assert resp.status_code == 201, resp.text
        aid = resp.json()["id"]

        # Try to update to disallowed model
        resp2 = client.put(
            f"/api/agents/{aid}",
            json={
                "model": "gpt-4o",
            },
        )
        assert resp2.status_code == 422, resp2.text
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]
