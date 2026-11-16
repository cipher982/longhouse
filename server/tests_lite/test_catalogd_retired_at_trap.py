"""The retired_at indexes are gone, and session-scoped live reads use the session index.

The statistics below are david010's (2026-11-16): with them, SQLite answered
`session_id = ? AND retired_at IS NULL` through the retired_at index and walked
every live row in the catalog (catalog-read-path.md).
"""

from sqlalchemy import text

from zerg.catalogd.schema import RETIRED_AT_TRAP_INDEXES
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema

_DOGFOOD_STATS = {
    ("raw_objects", "ix_raw_objects_epoch_range"): "1047796 5 1 1",
    ("raw_objects", "ix_raw_objects_machine_source"): "1047796 286 182 4 4",
    ("raw_objects", "ix_raw_objects_object_hash"): "1047796 1",
    ("raw_objects", "ix_raw_objects_retired_at"): "1047796 32",
    ("raw_objects", "ix_raw_objects_session_id"): "1047796 126",
    ("raw_objects", "ix_raw_objects_session_source_order"): "1047796 126 112 112 101 44 1 1",
    ("raw_objects", "ix_raw_objects_source_epoch"): "1047796 5",
    ("raw_objects", "ix_raw_objects_source_identity"): "1047796 1001 1001 1001 15",
    ("raw_objects", "sqlite_autoindex_raw_objects_1"): "1047796 1",
    ("render_objects", "ix_render_objects_generation_first_order"): "1046845 9 2 2 2 2 2 2 2 1",
    ("render_objects", "ix_render_objects_generation_id"): "1046845 9",
    ("render_objects", "ix_render_objects_generation_last_order"): "1046845 9 2 2 2 2 2 2 2",
    ("render_objects", "ix_render_objects_object_hash"): "1046845 1",
    ("render_objects", "ix_render_objects_retired_at"): "1046845 32",
    ("render_objects", "ix_render_objects_retirement_revision"): "1046845 32",
    ("render_objects", "ix_render_objects_session_id"): "1046845 126",
    ("render_objects", "ix_render_objects_source_envelope_id"): "1046845 1",
    ("render_objects", "sqlite_autoindex_render_objects_1"): "1046845 1",
    ("render_objects", "sqlite_autoindex_render_objects_2"): "1046845 9 1",
}
_SESSION_LIVE_READS = {
    "raw_objects": "SELECT envelope_id FROM raw_objects WHERE session_id = 's' AND retired_at IS NULL",
    "render_objects": "SELECT object_id FROM render_objects WHERE session_id = 's' AND retired_at IS NULL",
}


def _indexes(connection) -> set[str]:
    return {name for (name,) in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'index'")}


def _plan(connection, sql: str) -> str:
    return " | ".join(str(row[3]) for row in connection.exec_driver_sql(f"EXPLAIN QUERY PLAN {sql}"))


def _load_dogfood_stats(connection) -> None:
    connection.exec_driver_sql("ANALYZE")
    connection.exec_driver_sql("DELETE FROM sqlite_stat1 WHERE tbl IN ('raw_objects', 'render_objects')")
    for (table, index), stat in _DOGFOOD_STATS.items():
        connection.execute(
            text("INSERT INTO sqlite_stat1 (tbl, idx, stat) VALUES (:t, :i, :s)"),
            {"t": table, "i": index, "s": stat},
        )
    connection.exec_driver_sql("ANALYZE sqlite_master")


def test_a_fresh_catalog_has_no_retired_at_index(tmp_path):
    engine = create_catalog_engine(tmp_path / "longhouse-live.db")
    initialize_catalog_schema(engine)
    with engine.connect() as connection:
        assert not _indexes(connection) & set(RETIRED_AT_TRAP_INDEXES)


def test_startup_drops_the_trap_and_session_reads_use_the_session_index(tmp_path):
    engine = create_catalog_engine(tmp_path / "longhouse-live.db")
    initialize_catalog_schema(engine)
    with engine.begin() as connection:
        # A catalog made by an older binary still has both indexes.
        connection.exec_driver_sql("CREATE INDEX ix_raw_objects_retired_at ON raw_objects (retired_at)")
        connection.exec_driver_sql("CREATE INDEX ix_render_objects_retired_at ON render_objects (retired_at)")
        _load_dogfood_stats(connection)
    with engine.connect() as connection:
        # The trap reproduces: these are the plans david010 ran.
        for table, sql in _SESSION_LIVE_READS.items():
            assert f"ix_{table}_retired_at" in _plan(connection, sql)

    initialize_catalog_schema(engine)
    engine.dispose()  # fresh connections load the schema and statistics again

    with engine.connect() as connection:
        assert not _indexes(connection) & set(RETIRED_AT_TRAP_INDEXES)
        for table, sql in _SESSION_LIVE_READS.items():
            plan = _plan(connection, sql)
            assert "(session_id=?)" in plan and "retired_at" not in plan, plan
