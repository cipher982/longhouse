import contextlib

import pytest

from zerg.crud import crud
from zerg.main import app


def _ensure_user_role(db_session, email: str, role: str):
    user = crud.get_user_by_email(db_session, email)
    if user is None:
        user = crud.create_user(db_session, email=email, provider=None, role=role)
    else:
        user.role = role  # type: ignore[attr-defined]
        db_session.commit()
    return user


def _agent_and_thread(db_session, owner_id: int):
    agent = crud.create_agent(
        db_session,
        owner_id=owner_id,
        name="budget-agent",
        system_instructions="sys",
        task_instructions="task",
        model="gpt-4o-mini",
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
async def test_user_budget_denies_when_exhausted(client, db_session, monkeypatch):
    # $1.00 user budget, no global budget
    monkeypatch.setenv("DAILY_COST_PER_USER_CENTS", "100")
    monkeypatch.setenv("DAILY_COST_GLOBAL_CENTS", "0")

    user = _ensure_user_role(db_session, "budget@local", "USER")

    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: user
    try:
        agent, thread = _agent_and_thread(db_session, user.id)

        # Simulate prior usage today: $1.00 already used
        run = crud.create_run(db_session, agent_id=agent.id, thread_id=thread.id, trigger="api", status="queued")
        crud.mark_running(db_session, run.id)
        crud.mark_finished(db_session, run.id, total_tokens=10, total_cost_usd=1.00)

        # Next run should be denied for non-admin
        r = client.post(f"/api/threads/{thread.id}/run")
        assert r.status_code == 429, r.text
        assert "budget" in r.text.lower()
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]


@pytest.mark.asyncio
async def test_admin_exempt_from_budgets(client, db_session, monkeypatch):
    monkeypatch.setenv("DAILY_COST_PER_USER_CENTS", "100")
    monkeypatch.setenv("DAILY_COST_GLOBAL_CENTS", "100")

    admin = _ensure_user_role(db_session, "admin-budget@local", "ADMIN")
    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: admin
    try:
        agent, thread = _agent_and_thread(db_session, admin.id)

        # Simulate high global usage ($2.00) which would otherwise block
        run = crud.create_run(db_session, agent_id=agent.id, thread_id=thread.id, trigger="api", status="queued")
        crud.mark_running(db_session, run.id)
        crud.mark_finished(db_session, run.id, total_tokens=10, total_cost_usd=2.00)

        # Admin should still be allowed to run
        crud.create_thread_message(db=db_session, thread_id=thread.id, role="user", content="next")
        r = client.post(f"/api/threads/{thread.id}/run")
        assert r.status_code == 202, r.text
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]
