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

    refreshed = store.refresh_planner_statistics()
    engine.dispose()

    assert refreshed["tables"] > 1
    assert "USING INDEX sqlite_autoindex_media_objects_1" in _plan(store)
