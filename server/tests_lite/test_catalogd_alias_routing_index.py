"""Live alias table carries the unique provider-session routing index.

Parity with the archive's ux_thread_aliases_provider_session_routing: one
provider-native session id routes to exactly one live thread. Existing catalogs
may hold duplicate rows, so the 3->4 migration dedupes (newest last_seen_at
wins) before creating the index.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zerg.catalogd.schema import CATALOG_SCHEMA_VERSION
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.models.live_store import LiveSessionThreadAlias

_INDEX_NAME = "ux_live_thread_aliases_provider_session_routing"


def _alias(thread_id: str, alias_value: str, *, alias_kind: str = "provider_session_id", seen: datetime) -> LiveSessionThreadAlias:
    return LiveSessionThreadAlias(
        thread_id=thread_id,
        provider="claude",
        alias_kind=alias_kind,
        alias_value=alias_value,
        first_seen_at=seen,
        last_seen_at=seen,
    )


def _index_names(engine) -> set[str]:
    with engine.connect() as connection:
        return {row[1] for row in connection.exec_driver_sql("PRAGMA index_list(live_session_thread_aliases)")}


def test_fresh_catalog_enforces_provider_session_routing(tmp_path):
    engine = create_catalog_engine(tmp_path / "longhouse-live.db")
    initialize_catalog_schema(engine)
    assert _INDEX_NAME in _index_names(engine)

    now = datetime.now(UTC)
    with Session(engine) as db:
        db.add(_alias("thread-1", "native-1", seen=now))
        db.commit()
        # Same native id on a second thread is rejected by the routing index.
        db.add(_alias("thread-2", "native-1", seen=now))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        # Non-routing alias kinds may still share the value across threads.
        db.add(_alias("thread-2", "native-1", alias_kind="source_path", seen=now))
        db.commit()
    engine.dispose()


def test_migration_dedupes_keeping_newest_then_creates_index(tmp_path):
    database = tmp_path / "longhouse-live.db"
    engine = create_catalog_engine(database)
    initialize_catalog_schema(engine)

    # Rewind to a v3 catalog: no routing index, duplicate alias rows.
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.exec_driver_sql(f"DROP INDEX {_INDEX_NAME}")
        connection.exec_driver_sql("UPDATE catalog_meta SET schema_version = 3 WHERE singleton = 1")
        connection.exec_driver_sql("PRAGMA user_version=3")
    with Session(engine) as db:
        db.add(_alias("thread-old", "native-dup", seen=now - timedelta(hours=2)))
        db.add(_alias("thread-mid", "native-dup", seen=now - timedelta(hours=1)))
        db.add(_alias("thread-new", "native-dup", seen=now))
        db.add(_alias("thread-old", "native-dup", alias_kind="source_path", seen=now - timedelta(hours=2)))
        db.add(_alias("thread-solo", "native-solo", seen=now))
        db.commit()
    engine.dispose()

    engine = create_catalog_engine(database)
    metadata = initialize_catalog_schema(engine)

    assert metadata.schema_version == CATALOG_SCHEMA_VERSION
    assert _INDEX_NAME in _index_names(engine)
    with Session(engine) as db:
        survivors = (
            db.query(LiveSessionThreadAlias)
            .filter(
                LiveSessionThreadAlias.alias_kind == "provider_session_id",
                LiveSessionThreadAlias.alias_value == "native-dup",
            )
            .all()
        )
        assert [row.thread_id for row in survivors] == ["thread-new"]
        # Non-routing kinds and non-duplicated values are untouched.
        assert (
            db.query(LiveSessionThreadAlias)
            .filter(LiveSessionThreadAlias.alias_kind == "source_path")
            .count()
            == 1
        )
        assert (
            db.query(LiveSessionThreadAlias)
            .filter(LiveSessionThreadAlias.alias_value == "native-solo")
            .count()
            == 1
        )
        # The index now enforces the invariant going forward.
        db.add(_alias("thread-other", "native-dup", seen=now))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    engine.dispose()
