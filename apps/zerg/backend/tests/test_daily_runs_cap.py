import contextlib

import pytest

from zerg.crud import crud
from zerg.main import app
from tests.conftest import TEST_MODEL, TEST_WORKER_MODEL


def _ensure_user_role(db_session, email: str, role: str):
    user = crud.get_user_by_email(db_session, email)
    if user is None:
        user = crud.create_user(db_session, email=email, provider=None, role=role)
    else:
        user.role = role  # type: ignore[attr-defined]
        db_session.commit()
    return user


def _create_agent_and_thread(db_session, owner_id: int):
    agent = crud.create_agent(
        db_session,
        owner_id=owner_id,
        name="quota-agent",
        system_instructions="sys",
        task_instructions="task",
        model=TEST_WORKER_MODEL,
        schedule=None,
        config={},
    )
    thread = crud.create_thread(
        db=db_session, agent_id=agent.id, title="t", active=True, agent_state={}, memory_strategy="buffer"
    )
    # Seed one user message so run endpoint has work
    crud.create_thread_message(db=db_session, thread_id=thread.id, role="user", content="hi")
    return agent, thread


@pytest.mark.asyncio
async def test_non_admin_daily_runs_cap_blocks_on_third(client, db_session, monkeypatch):
    # Cap at 2 per day
    monkeypatch.setenv("DAILY_RUNS_PER_USER", "2")

    # Force current_user to non-admin
    user = _ensure_user_role(db_session, "dev@local", "USER")
    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: user

    try:
        # Prepare agent + thread
        agent, thread = _create_agent_and_thread(db_session, user.id)

        # First run: allowed
        r1 = client.post(f"/api/threads/{thread.id}/run")
        assert r1.status_code == 202, r1.text

        # Add another user message for the second run
        crud.create_thread_message(db=db_session, thread_id=thread.id, role="user", content="2")
        r2 = client.post(f"/api/threads/{thread.id}/run")
        assert r2.status_code == 202, r2.text

        # Add message again
        crud.create_thread_message(db=db_session, thread_id=thread.id, role="user", content="3")
        # Third run: should be blocked
        r3 = client.post(f"/api/threads/{thread.id}/run")
        assert r3.status_code == 429, r3.text
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]


@pytest.mark.asyncio
async def test_admin_exempt_from_daily_runs_cap(client, db_session, monkeypatch):
    monkeypatch.setenv("DAILY_RUNS_PER_USER", "1")

    admin = _ensure_user_role(db_session, "admin@local", "ADMIN")
    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: admin
    try:
        agent, thread = _create_agent_and_thread(db_session, admin.id)
        # First run
        crud.create_thread_message(db=db_session, thread_id=thread.id, role="user", content="hi")
        r1 = client.post(f"/api/threads/{thread.id}/run")
        assert r1.status_code == 202
        # Second run should still be allowed for admin
        crud.create_thread_message(db=db_session, thread_id=thread.id, role="user", content="next")
        r2 = client.post(f"/api/threads/{thread.id}/run")
        assert r2.status_code == 202
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]


@pytest.mark.asyncio
async def test_task_run_respects_daily_cap(client, db_session, monkeypatch):
    monkeypatch.setenv("DAILY_RUNS_PER_USER", "1")

    user = _ensure_user_role(db_session, "user1@local", "USER")
    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: user
    try:
        agent = crud.create_agent(
            db_session,
            owner_id=user.id,
            name="quota-agent",
            system_instructions="sys",
            task_instructions="task",
            model=TEST_WORKER_MODEL,
            schedule=None,
            config={},
        )
        # First task run allowed
        r1 = client.post(f"/api/agents/{agent.id}/task")
        assert r1.status_code == 202, r1.text
        # Second task run blocked
        r2 = client.post(f"/api/agents/{agent.id}/task")
        assert r2.status_code == 429, r2.text
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]
