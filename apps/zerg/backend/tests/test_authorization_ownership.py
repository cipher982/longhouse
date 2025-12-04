import contextlib

import pytest

from zerg.crud import crud
from zerg.main import app
from tests.conftest import TEST_MODEL, TEST_WORKER_MODEL


def _mk_user(db_session, email: str, role: str = "USER"):
    u = crud.get_user_by_email(db_session, email)
    if u is None:
        u = crud.create_user(db_session, email=email, provider=None, role=role)
    else:
        u.role = role  # type: ignore[attr-defined]
        db_session.commit()
    return u


def _mk_agent_thread(db, owner_id: int):
    a = crud.create_agent(
        db,
        owner_id=owner_id,
        name="a",
        system_instructions="sys",
        task_instructions="task",
        model=TEST_WORKER_MODEL,
        schedule=None,
        config={},
    )
    t = crud.create_thread(db=db, agent_id=a.id, title="t", active=True, agent_state={}, memory_strategy="buffer")
    crud.create_thread_message(db=db, thread_id=t.id, role="user", content="hi")
    return a, t


@pytest.mark.asyncio
async def test_non_owner_cannot_run_thread(client, db_session):
    owner = _mk_user(db_session, "owner@local", "USER")
    other = _mk_user(db_session, "other@local", "USER")
    _, thread = _mk_agent_thread(db_session, owner.id)

    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: other
    try:
        resp = client.post(f"/api/threads/{thread.id}/run")
        assert resp.status_code == 403, resp.text
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]


@pytest.mark.asyncio
async def test_non_owner_cannot_create_thread_for_others_agent(client, db_session):
    owner = _mk_user(db_session, "owner2@local", "USER")
    other = _mk_user(db_session, "other2@local", "USER")
    agent = crud.create_agent(
        db_session,
        owner_id=owner.id,
        name="x",
        system_instructions="sys",
        task_instructions="task",
        model=TEST_WORKER_MODEL,
        schedule=None,
        config={},
    )
    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: other
    try:
        resp = client.post(
            "/api/threads",
            json={"agent_id": agent.id, "title": "t", "thread_type": "chat", "active": True},
        )
        assert resp.status_code == 403, resp.text
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]


@pytest.mark.asyncio
async def test_non_owner_cannot_post_messages(client, db_session):
    owner = _mk_user(db_session, "owner3@local", "USER")
    other = _mk_user(db_session, "other3@local", "USER")
    _, thread = _mk_agent_thread(db_session, owner.id)
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
async def test_non_owner_cannot_run_agent_task(client, db_session):
    owner = _mk_user(db_session, "owner4@local", "USER")
    other = _mk_user(db_session, "other4@local", "USER")
    agent = crud.create_agent(
        db_session,
        owner_id=owner.id,
        name="x",
        system_instructions="sys",
        task_instructions="task",
        model=TEST_WORKER_MODEL,
        schedule=None,
        config={},
    )
    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: other
    try:
        resp = client.post(f"/api/agents/{agent.id}/task")
        assert resp.status_code == 403, resp.text
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]
