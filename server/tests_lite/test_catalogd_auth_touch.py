"""Authenticated reads must not take the writer to write nothing.

`get_user` and `resolve_device` used to select `_write_transaction` -- SQLite's
`BEGIN IMMEDIATE`, an exclusive write reservation -- from the caller's *touch
flag* rather than from whether a stamp was actually owed. `last_login` is
written once and never again, and device touches are throttled to one per five
minutes, so in steady state essentially every browser-authenticated request took
the write lock and wrote nothing.

catalogd routes writes through a single-writer thread on purpose, so that made
every authenticated read inherit writer contention. These tests pin the
invariant rather than the implementation: a read that owes no stamp must not
open a write transaction, and one that owes a stamp must still take it.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from sqlalchemy import insert
from sqlalchemy import select
from sqlalchemy import update

from zerg.catalogd import store as catalog_store
from zerg.catalogd.schema import catalog_meta
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.store import CatalogStore
from zerg.models.live_store import LiveDeviceToken
from zerg.models.live_store import LiveUser

TOKEN_HASH = "a" * 64


@pytest.fixture()
def store(tmp_path):
    engine = create_catalog_engine(str(tmp_path / "catalog.db"))
    initialize_catalog_schema(engine)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            insert(LiveUser.__table__).values(
                id=1,
                email="owner@example.com",
                role="ADMIN",
                is_active=True,
                created_at=now,
                updated_at=now,
                last_login=now,
            )
        )
        connection.execute(
            insert(LiveDeviceToken.__table__).values(
                id="11111111-1111-4111-8111-111111111111",
                owner_id=1,
                device_id="machine-a",
                token_hash=TOKEN_HASH,
                created_at=now,
                last_used_at=now,
            )
        )
    try:
        yield CatalogStore(engine)
    finally:
        engine.dispose()


@pytest.fixture()
def forbid_write_transaction(monkeypatch):
    """Make any write reservation an immediate, loud failure.

    Asserting on latency or statement counts would be indirect. Taking the write
    lock at all is the thing being forbidden, so forbid it directly.
    """

    def _refuse(*_args, **_kwargs):
        raise AssertionError("took a write transaction for a read that owes no stamp")

    monkeypatch.setattr(catalog_store, "_write_transaction", _refuse)


def _commit_seq(store: CatalogStore) -> int:
    with store.engine.connect() as connection:
        return int(connection.execute(select(catalog_meta.c.commit_seq)).scalar_one())


def test_resolving_an_established_user_takes_no_write_transaction(store, forbid_write_transaction):
    result = store.get_user(user_id=1, touch_last_login=True)

    assert result["found"] is True
    assert result["touch_due"] is False, "an established user owes no first-login stamp"
    assert result["changed"] is False


def test_resolving_a_throttled_device_takes_no_write_transaction(store, forbid_write_transaction):
    result = store.resolve_device(token_hash=TOKEN_HASH, touch_last_used=True, touch_interval_seconds=300)

    assert result["valid"] is True
    assert result["touch_due"] is False, "a token used seconds ago is inside the throttle"
    assert result["changed"] is False


def test_a_first_login_still_reports_a_stamp_is_owed(store):
    with store.engine.begin() as connection:
        connection.execute(update(LiveUser.__table__).where(LiveUser.__table__.c.id == 1).values(last_login=None))

    assert store.get_user(user_id=1, touch_last_login=True)["touch_due"] is True
    # And declining to ask for it must not smuggle the write in anyway.
    assert store.get_user(user_id=1, touch_last_login=False)["touch_due"] is False


def test_an_expired_device_throttle_reports_a_stamp_is_owed(store):
    stale = datetime.now(UTC) - timedelta(hours=1)
    with store.engine.begin() as connection:
        connection.execute(update(LiveDeviceToken.__table__).values(last_used_at=stale))

    assert store.resolve_device(token_hash=TOKEN_HASH, touch_last_used=True, touch_interval_seconds=300)["touch_due"]
    assert not store.resolve_device(token_hash=TOKEN_HASH, touch_last_used=False, touch_interval_seconds=300)["touch_due"]


def test_the_stamp_is_written_when_owed(store):
    with store.engine.begin() as connection:
        connection.execute(update(LiveUser.__table__).where(LiveUser.__table__.c.id == 1).values(last_login=None))
    before = _commit_seq(store)

    stamped = store.touch_user_login(user_id=1)

    assert stamped["changed"] is True
    assert int(stamped["commit_seq"]) > before
    with store.engine.connect() as connection:
        assert connection.execute(select(LiveUser.__table__.c.last_login)).scalar_one() is not None


def test_a_stamp_already_taken_by_a_concurrent_login_is_not_rewritten(store):
    """The read that decided a stamp was owed ran in an earlier snapshot."""

    before = _commit_seq(store)
    stamped = store.touch_user_login(user_id=1)  # last_login is already set

    assert stamped["changed"] is False
    assert int(stamped["commit_seq"]) == before, "a no-op stamp must not advance the catalog"


def test_a_revoked_device_is_not_stamped(store):
    with store.engine.begin() as connection:
        connection.execute(
            update(LiveDeviceToken.__table__).values(revoked_at=datetime.now(UTC), last_used_at=None)
        )
    before = _commit_seq(store)

    stamped = store.touch_device_token(
        token_id="11111111-1111-4111-8111-111111111111", touch_interval_seconds=300
    )

    assert stamped["changed"] is False
    assert int(stamped["commit_seq"]) == before


def test_a_device_stamp_is_written_once_per_throttle_window(store):
    with store.engine.begin() as connection:
        connection.execute(update(LiveDeviceToken.__table__).values(last_used_at=None))

    first = store.touch_device_token(token_id="11111111-1111-4111-8111-111111111111", touch_interval_seconds=300)
    second = store.touch_device_token(token_id="11111111-1111-4111-8111-111111111111", touch_interval_seconds=300)

    assert first["changed"] is True
    assert second["changed"] is False, "the throttle is re-checked inside the write, not only before it"
