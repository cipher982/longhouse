import contextlib

from tests.conftest import TEST_COMMIS_MODEL
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

    fiche = crud.create_fiche(
        db_session,
        owner_id=owner.id,
        name="owning-fiche",
        system_instructions="sys",
        task_instructions="task",
        model=TEST_COMMIS_MODEL,
        schedule=None,
        config={},
    )
    thread = crud.create_thread(
        db=db_session, fiche_id=fiche.id, title="t", active=True, fiche_state={}, memory_strategy="buffer"
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


def test_list_threads_scoped_to_owner(client, db_session):
    owner = _user(db_session, "owner-list@local", "USER")
    other = _user(db_session, "other-list@local", "USER")

    owner_agent = crud.create_fiche(
        db_session,
        owner_id=owner.id,
        name="owner-fiche",
        system_instructions="sys",
        task_instructions="task",
        model=TEST_COMMIS_MODEL,
        schedule=None,
        config={},
    )
    other_agent = crud.create_fiche(
        db_session,
        owner_id=other.id,
        name="other-fiche",
        system_instructions="sys",
        task_instructions="task",
        model=TEST_COMMIS_MODEL,
        schedule=None,
        config={},
    )

    owner_thread = crud.create_thread(
        db=db_session,
        fiche_id=owner_agent.id,
        title="owner-thread",
        active=True,
        fiche_state={},
        memory_strategy="buffer",
    )
    other_thread = crud.create_thread(
        db=db_session,
        fiche_id=other_agent.id,
        title="other-thread",
        active=True,
        fiche_state={},
        memory_strategy="buffer",
    )

    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: owner
    try:
        resp = client.get("/api/threads")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        ids = {t["id"] for t in payload}
        assert owner_thread.id in ids
        assert other_thread.id not in ids
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]


def test_update_thread_ownership_enforced(client, db_session):
    owner = _user(db_session, "owner-update@local", "USER")
    other = _user(db_session, "other-update@local", "USER")

    fiche = crud.create_fiche(
        db_session,
        owner_id=owner.id,
        name="owning-fiche-update",
        system_instructions="sys",
        task_instructions="task",
        model=TEST_COMMIS_MODEL,
        schedule=None,
        config={},
    )
    thread = crud.create_thread(
        db=db_session,
        fiche_id=fiche.id,
        title="t",
        active=True,
        fiche_state={},
        memory_strategy="buffer",
    )

    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: other
    try:
        resp = client.put(f"/api/threads/{thread.id}", json={"title": "nope"})
        assert resp.status_code == 403, resp.text
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]

    app.dependency_overrides[get_current_user] = lambda: owner
    try:
        resp = client.put(f"/api/threads/{thread.id}", json={"title": "ok"})
        assert resp.status_code == 200, resp.text
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]


def test_delete_thread_ownership_enforced(client, db_session):
    owner = _user(db_session, "owner-delete@local", "USER")
    other = _user(db_session, "other-delete@local", "USER")

    fiche = crud.create_fiche(
        db_session,
        owner_id=owner.id,
        name="owning-fiche-delete",
        system_instructions="sys",
        task_instructions="task",
        model=TEST_COMMIS_MODEL,
        schedule=None,
        config={},
    )
    thread = crud.create_thread(
        db=db_session,
        fiche_id=fiche.id,
        title="t",
        active=True,
        fiche_state={},
        memory_strategy="buffer",
    )

    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: other
    try:
        resp = client.delete(f"/api/threads/{thread.id}")
        assert resp.status_code == 403, resp.text
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]

    app.dependency_overrides[get_current_user] = lambda: owner
    try:
        resp = client.delete(f"/api/threads/{thread.id}")
        assert resp.status_code == 204, resp.text
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]
