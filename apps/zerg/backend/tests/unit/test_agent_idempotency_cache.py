from tests.conftest import TEST_MODEL

from zerg.crud import crud
from zerg.routers import agents as agents_router


def test_idempotency_cache_expires(db_session, monkeypatch):
    agents_router.IDEMPOTENCY_CACHE.clear()

    user = crud.create_user(db_session, email="idempo@local", provider=None, role="USER")
    agent = crud.create_agent(
        db_session,
        owner_id=user.id,
        name="agent",
        system_instructions="sys",
        task_instructions="task",
        model=TEST_MODEL,
        schedule=None,
        config={},
    )

    monkeypatch.setattr(agents_router, "_now", lambda: 1000.0)
    agents_router._store_idempotency_cache("k", user.id, agent.id)

    cached = agents_router._check_idempotency_cache("k", user.id, db_session)
    assert cached is not None

    monkeypatch.setattr(agents_router, "_now", lambda: 1000.0 + agents_router.IDEMPOTENCY_TTL_SECS + 1)
    expired = agents_router._check_idempotency_cache("k", user.id, db_session)
    assert expired is None
    assert ("k", user.id) not in agents_router.IDEMPOTENCY_CACHE
