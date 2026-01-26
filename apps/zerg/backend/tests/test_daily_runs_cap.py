import contextlib

import pytest

from tests.conftest import TEST_COMMIS_MODEL
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


def _create_fiche_and_thread(db_session, owner_id: int):
    fiche = crud.create_fiche(
        db_session,
        owner_id=owner_id,
        name="quota-fiche",
        system_instructions="sys",
        task_instructions="task",
        model=TEST_COMMIS_MODEL,
        schedule=None,
        config={},
    )
    thread = crud.create_thread(
        db=db_session, fiche_id=fiche.id, title="t", active=True, fiche_state={}, memory_strategy="buffer"
    )
    # Seed one user message so run endpoint has work
    crud.create_thread_message(db=db_session, thread_id=thread.id, role="user", content="hi")
    return fiche, thread


@pytest.mark.asyncio
async def test_non_admin_daily_runs_cap_blocks_on_third(client, db_session, monkeypatch):
    # Cap at 2 per day
    monkeypatch.setenv("DAILY_RUNS_PER_USER", "2")

    # Force current_user to non-admin
    user = _ensure_user_role(db_session, "dev@local", "USER")
    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: user

    try:
        # Prepare fiche + thread
        fiche, thread = _create_fiche_and_thread(db_session, user.id)

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
        fiche, thread = _create_fiche_and_thread(db_session, admin.id)
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
        fiche = crud.create_fiche(
            db_session,
            owner_id=user.id,
            name="quota-fiche",
            system_instructions="sys",
            task_instructions="task",
            model=TEST_COMMIS_MODEL,
            schedule=None,
            config={},
        )
        # First task run allowed
        r1 = client.post(f"/api/fiches/{fiche.id}/task")
        assert r1.status_code == 202, r1.text
        # Second task run blocked
        r2 = client.post(f"/api/fiches/{fiche.id}/task")
        assert r2.status_code == 429, r2.text
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]
