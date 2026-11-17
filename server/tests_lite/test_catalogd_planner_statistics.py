"""catalogd refreshes planner statistics for the whole catalog at startup.

david010's sqlite_stat1 was taken when the catalog held a few hundred rows and
never refreshed, so SQLite planned real-sized tables as tiny ones: a lookup of
ten media hashes scanned media_objects instead of probing its primary key.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

from zerg.catalogd.models import MediaObject
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.store import CatalogStore

_TEN_HASHES = "SELECT * FROM media_objects WHERE media_hash IN ({})".format(", ".join(f"'{index:064x}'" for index in range(10)))


def _plan(store: CatalogStore) -> str:
    with store.engine.connect() as connection:
        return " ".join(str(row[-1]) for row in connection.exec_driver_sql("EXPLAIN QUERY PLAN " + _TEN_HASHES))


def test_startup_statistics_replace_a_stale_tiny_catalog_shape(tmp_path) -> None:
    engine = create_catalog_engine(tmp_path / "catalog.db")
    initialize_catalog_schema(engine)
    store = CatalogStore(engine)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            MediaObject.__table__.insert(),
            [
                {
                    "media_hash": f"{index:064x}",
                    "state": "present",
                    "commit_seq": 1,
                    "observed_at": now,
                    "created_at": now,
                    "updated_at": now,
                }
                for index in range(2_000)
            ],
        )
        # The production shape: statistics from when the table had six rows.
        connection.exec_driver_sql("ANALYZE")
        connection.exec_driver_sql("DELETE FROM sqlite_stat1 WHERE tbl = 'media_objects'")
        connection.exec_driver_sql(
            "INSERT INTO sqlite_stat1(tbl, idx, stat) VALUES "
            "('media_objects', 'sqlite_autoindex_media_objects_1', '6 1'), ('media_objects', 'ix_media_objects_state', '6 6')"
        )
        connection.exec_driver_sql("ANALYZE sqlite_schema")
    engine.dispose()
    assert "SCAN media_objects" in _plan(store)

    tables = store.planner_statistics_tables()
    for table in tables:
        store.refresh_table_statistics(table)
    engine.dispose()

    assert {"media_objects", "projector_state", "raw_objects", "sessions"} <= set(tables)
    assert "USING INDEX sqlite_autoindex_media_objects_1" in _plan(store)


def test_table_statistics_refuse_a_name_that_is_not_a_plain_identifier(tmp_path) -> None:
    engine = create_catalog_engine(tmp_path / "catalog.db")
    initialize_catalog_schema(engine)
    store = CatalogStore(engine)
    try:
        store.refresh_table_statistics('sessions"; DROP TABLE sessions; --')
    except ValueError:
        pass
    else:
        raise AssertionError("an unsafe table name was analyzed")


def test_statistics_refresh_is_stamped_per_table(tmp_path) -> None:
    """Startup refreshes only tables whose stamp is old; restarts do not change table shape."""

    engine = create_catalog_engine(tmp_path / "catalog.db")
    initialize_catalog_schema(engine)
    store = CatalogStore(engine)
    assert store.planner_statistics_refreshed() == {}
    now = datetime.now(UTC).replace(microsecond=0)
    store.refresh_table_statistics("sessions", now)
    store.refresh_table_statistics("media_objects", now)
    assert store.planner_statistics_refreshed() == {"sessions": now, "media_objects": now}
    engine.dispose()


def test_a_table_with_an_unanalyzed_index_is_always_due(tmp_path) -> None:
    engine = create_catalog_engine(tmp_path / "catalog.db")
    initialize_catalog_schema(engine)
    store = CatalogStore(engine)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            MediaObject.__table__.insert(),
            [
                {
                    "media_hash": f"{index:064x}",
                    "state": "present",
                    "commit_seq": 1,
                    "observed_at": now,
                    "created_at": now,
                    "updated_at": now,
                }
                for index in range(50)
            ],
        )
    store.refresh_table_statistics("media_objects", now)
    assert "media_objects" not in store.tables_with_unanalyzed_indexes()
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE INDEX ix_test_media_updated ON media_objects(updated_at)")
    assert "media_objects" in store.tables_with_unanalyzed_indexes()
    engine.dispose()
