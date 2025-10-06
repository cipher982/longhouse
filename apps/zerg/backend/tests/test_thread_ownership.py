import contextlib

from zerg.crud import crud
from zerg.main import app


def _user(db_session, email: str, role: str):
    u = crud.get_user_by_email(db_session, email) or crud.create_user(db_session, email=email, provider=None, role=role)
    u.role = role  # type: ignore[attr-defined]
    db_session.commit()
    return u


def test_read_thread_ownership_enforced(client, db_session):
    owner = _user(db_session, "owner@local", "USER")
    other = _user(db_session, "other@local", "USER")

    agent = crud.create_agent(
        db_session,
        owner_id=owner.id,
        name="owning-agent",
        system_instructions="sys",
        task_instructions="task",
        model="gpt-4o-mini",
        schedule=None,
        config={},
    )
    thread = crud.create_thread(
        db=db_session, agent_id=agent.id, title="t", active=True, agent_state={}, memory_strategy="buffer"
    )

    from zerg.dependencies.auth import get_current_user

    # Other user should be forbidden
    app.dependency_overrides[get_current_user] = lambda: other
    try:
        r = client.get(f"/api/threads/{thread.id}")
        assert r.status_code == 403, r.text
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]

    # Owner allowed
    app.dependency_overrides[get_current_user] = lambda: owner
    try:
        r = client.get(f"/api/threads/{thread.id}")
        assert r.status_code == 200, r.text
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]

    # Admin allowed
    admin = _user(db_session, "admin@local", "ADMIN")
    app.dependency_overrides[get_current_user] = lambda: admin
    try:
        r = client.get(f"/api/threads/{thread.id}")
        assert r.status_code == 200, r.text
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]
