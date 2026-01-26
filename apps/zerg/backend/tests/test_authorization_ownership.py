import contextlib

import pytest

from tests.conftest import TEST_COMMIS_MODEL
from zerg.crud import crud
from zerg.main import app


def _mk_user(db_session, email: str, role: str = "USER"):
    u = crud.get_user_by_email(db_session, email)
    if u is None:
        u = crud.create_user(db_session, email=email, provider=None, role=role)
    else:
        u.role = role  # type: ignore[attr-defined]
        db_session.commit()
    return u


def _mk_fiche_thread(db, owner_id: int):
    a = crud.create_fiche(
        db,
        owner_id=owner_id,
        name="a",
        system_instructions="sys",
        task_instructions="task",
        model=TEST_COMMIS_MODEL,
        schedule=None,
        config={},
    )
    t = crud.create_thread(db=db, fiche_id=a.id, title="t", active=True, fiche_state={}, memory_strategy="buffer")
    crud.create_thread_message(db=db, thread_id=t.id, role="user", content="hi")
    return a, t


@pytest.mark.asyncio
async def test_non_owner_cannot_run_thread(client, db_session):
    owner = _mk_user(db_session, "owner@local", "USER")
    other = _mk_user(db_session, "other@local", "USER")
    _, thread = _mk_fiche_thread(db_session, owner.id)

    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: other
    try:
        resp = client.post(f"/api/threads/{thread.id}/run")
        assert resp.status_code == 403, resp.text
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]


@pytest.mark.asyncio
async def test_non_owner_cannot_create_thread_for_others_fiche(client, db_session):
    owner = _mk_user(db_session, "owner2@local", "USER")
    other = _mk_user(db_session, "other2@local", "USER")
    fiche = crud.create_fiche(
        db_session,
        owner_id=owner.id,
        name="x",
        system_instructions="sys",
        task_instructions="task",
        model=TEST_COMMIS_MODEL,
        schedule=None,
        config={},
    )
    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: other
    try:
        resp = client.post(
            "/api/threads",
            json={"fiche_id": fiche.id, "title": "t", "thread_type": "chat", "active": True},
        )
        assert resp.status_code == 403, resp.text
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]


@pytest.mark.asyncio
async def test_non_owner_cannot_post_messages(client, db_session):
    owner = _mk_user(db_session, "owner3@local", "USER")
    other = _mk_user(db_session, "other3@local", "USER")
    _, thread = _mk_fiche_thread(db_session, owner.id)
    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: other
    try:
        resp = client.post(
            f"/api/threads/{thread.id}/messages",
            json={"role": "user", "content": "hi"},
        )
        assert resp.status_code == 403, resp.text
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]


@pytest.mark.asyncio
async def test_non_owner_cannot_run_fiche_task(client, db_session):
    owner = _mk_user(db_session, "owner4@local", "USER")
    other = _mk_user(db_session, "other4@local", "USER")
    fiche = crud.create_fiche(
        db_session,
        owner_id=owner.id,
        name="x",
        system_instructions="sys",
        task_instructions="task",
        model=TEST_COMMIS_MODEL,
        schedule=None,
        config={},
    )
    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: other
    try:
        resp = client.post(f"/api/fiches/{fiche.id}/task")
        assert resp.status_code == 403, resp.text
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]


@pytest.mark.asyncio
async def test_non_owner_cannot_read_agents_or_runs(client, db_session):
    owner = _mk_user(db_session, "owner5@local", "USER")
    other = _mk_user(db_session, "other5@local", "USER")
    fiche, thread = _mk_fiche_thread(db_session, owner.id)
    run = crud.create_course(db_session, fiche_id=fiche.id, thread_id=thread.id, trigger="manual", status="queued")

    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: other
    try:
        resp = client.get(f"/api/fiches/{fiche.id}")
        assert resp.status_code == 403, resp.text

        resp = client.get(f"/api/fiches/{fiche.id}/details?include=runs")
        assert resp.status_code == 403, resp.text

        resp = client.get(f"/api/fiches/{fiche.id}/messages")
        assert resp.status_code == 403, resp.text

        resp = client.post(
            f"/api/fiches/{fiche.id}/messages",
            json={"role": "user", "content": "hi"},
        )
        assert resp.status_code == 403, resp.text

        resp = client.get(f"/api/fiches/{fiche.id}/runs")
        assert resp.status_code == 403, resp.text

        resp = client.get(f"/api/runs/{run.id}")
        assert resp.status_code == 403, resp.text
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]
