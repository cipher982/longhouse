from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select


@pytest.fixture
def cursor_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("TESTING", "1")
    from zerg.catalogd.models import StorageSession
    from zerg.catalogd.schema import create_catalog_engine
    from zerg.catalogd.schema import initialize_catalog_schema
    from zerg.catalogd.store import CatalogStore

    engine = create_catalog_engine(tmp_path / "activity.db")
    initialize_catalog_schema(engine)
    now = datetime.now(UTC)
    source = now - timedelta(days=40)
    session_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            StorageSession.__table__.insert().values(
                session_id=session_id,
                tenant_id="default",
                provider="cursor",
                machine_id="test-machine",
                started_at=source - timedelta(hours=1),
                last_activity_at=now,
                first_user_message_preview="Preserve this historical conversation",
                user_messages=1,
                raw_state="durable",
                render_state="ready",
                commit_seq=1,
                created_at=now,
                updated_at=now,
            )
        )
    yield CatalogStore(engine), engine, session_id, now, source, StorageSession.__table__
    engine.dispose()


def recent_sessions(store):
    return store.list_session_timeline(
        project=None,
        provider="cursor",
        environment=None,
        include_test=False,
        hide_autonomous=False,
        include_automation=True,
        device_id=None,
        days_back=7,
        limit=20,
        offset=0,
    )["total"]


def test_verified_clock_repair_removes_false_recency_without_deleting_history(cursor_catalog):
    store, engine, session_id, now, source, table = cursor_catalog
    params = dict(
        session_id=session_id,
        expected_started_at=source - timedelta(hours=1),
        source_started_at=source - timedelta(hours=1),
        expected_last_activity_at=now,
        source_last_activity_at=source,
        now=now,
    )
    assert recent_sessions(store) == 1
    preview = store.repair_cursor_activity(**params, dry_run=True)
    assert preview["dry_run"] and not preview["repaired"]
    assert recent_sessions(store) == 1
    assert store.repair_cursor_activity(**params, dry_run=False)["repaired"]
    assert recent_sessions(store) == 0
    with engine.connect() as connection:
        row = connection.execute(select(table).where(table.c.session_id == session_id)).mappings().one()
    assert row["raw_state"] == "durable"
    assert row["first_user_message_preview"] == "Preserve this historical conversation"
    assert not store.repair_cursor_activity(**params, dry_run=False)["repaired"]


@pytest.mark.parametrize("invalid_source", ["before_start", "after_expected", "stale_expected"])
def test_clock_repair_refuses_unverified_bounds_and_concurrent_updates(cursor_catalog, invalid_source):
    store, _, session_id, now, source, _ = cursor_catalog
    started = source - timedelta(hours=1)
    expected = now
    if invalid_source == "before_start":
        source -= timedelta(days=1)
    elif invalid_source == "after_expected":
        source = now + timedelta(seconds=1)
    else:
        expected -= timedelta(seconds=1)
    result = store.repair_cursor_activity(
        session_id=session_id,
        expected_started_at=started,
        source_started_at=started,
        expected_last_activity_at=expected,
        source_last_activity_at=source,
        now=now,
        dry_run=False,
    )
    assert not result["repaired"]
    assert recent_sessions(store) == 1


def test_repair_recovers_creation_clock_fabricated_by_historical_import(cursor_catalog):
    store, engine, session_id, now, source, table = cursor_catalog
    with engine.begin() as connection:
        connection.execute(table.update().where(table.c.session_id == session_id).values(started_at=now))
    params = dict(
        session_id=session_id,
        expected_started_at=now,
        source_started_at=source - timedelta(hours=1),
        expected_last_activity_at=now,
        source_last_activity_at=source,
        now=now,
        dry_run=False,
    )
    # An incorrect expected creation clock must not overwrite newer evidence.
    stale = {**params, "expected_started_at": now - timedelta(seconds=1)}
    assert store.repair_cursor_activity(**stale)["reason"] == "compare_and_set_failed"
    assert recent_sessions(store) == 1
    assert store.repair_cursor_activity(**params)["repaired"]
    assert recent_sessions(store) == 0
    with engine.connect() as connection:
        row = connection.execute(select(table).where(table.c.session_id == session_id)).mappings().one()
    assert row["started_at"].replace(tzinfo=UTC) == source - timedelta(hours=1)
    assert row["raw_state"] == "durable"
