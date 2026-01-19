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


def test_idempotency_cache_enforces_size_limit(db_session, monkeypatch):
    """Cache should evict oldest entries when exceeding max size."""
    agents_router.IDEMPOTENCY_CACHE.clear()

    user = crud.create_user(db_session, email="idempo-size@local", provider=None, role="USER")
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

    # Set a small max size for testing
    original_max = agents_router.IDEMPOTENCY_MAX_SIZE
    monkeypatch.setattr(agents_router, "IDEMPOTENCY_MAX_SIZE", 3)

    # Store 5 entries with incrementing timestamps
    for i in range(5):
        monkeypatch.setattr(agents_router, "_now", lambda t=i: 1000.0 + t)
        agents_router._store_idempotency_cache(f"key{i}", user.id, agent.id)

    # Cache should be trimmed to max size (3), keeping the newest entries
    assert len(agents_router.IDEMPOTENCY_CACHE) == 3
    # Oldest entries (key0, key1) should be evicted
    assert (f"key0", user.id) not in agents_router.IDEMPOTENCY_CACHE
    assert (f"key1", user.id) not in agents_router.IDEMPOTENCY_CACHE
    # Newest entries should remain
    assert (f"key2", user.id) in agents_router.IDEMPOTENCY_CACHE
    assert (f"key3", user.id) in agents_router.IDEMPOTENCY_CACHE
    assert (f"key4", user.id) in agents_router.IDEMPOTENCY_CACHE

    # Restore original max size
    monkeypatch.setattr(agents_router, "IDEMPOTENCY_MAX_SIZE", original_max)
    agents_router.IDEMPOTENCY_CACHE.clear()
