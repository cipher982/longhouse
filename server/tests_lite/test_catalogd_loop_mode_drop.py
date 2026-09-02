"""The 4->5 catalog migration drops loop_mode wherever a v4 catalog carried it.

Loop mode left with the frozen operator loop. A catalog created before that
still has the column on both session tables; the migration removes it rather
than leaving a server-defaulted column no model declares.
"""

from __future__ import annotations

from sqlalchemy import inspect

from zerg.catalogd.schema import CATALOG_SCHEMA_VERSION
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema

_TABLES = ("sessions", "live_session_catalog")


def _columns(engine, table: str) -> set[str]:
    with engine.connect() as connection:
        return {column["name"] for column in inspect(connection).get_columns(table)}


def test_v4_catalog_loses_loop_mode_columns(tmp_path):
    database = tmp_path / "longhouse-live.db"
    engine = create_catalog_engine(database)
    initialize_catalog_schema(engine)
    for table in _TABLES:
        assert "loop_mode" not in _columns(engine, table)

    # Rewind to v4: the column exists with its old default on both tables.
    with engine.begin() as connection:
        for table in _TABLES:
            connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN loop_mode VARCHAR(32) NOT NULL DEFAULT 'assist'")
        connection.exec_driver_sql("UPDATE catalog_meta SET schema_version = 4 WHERE singleton = 1")
        connection.exec_driver_sql("PRAGMA user_version=4")
    for table in _TABLES:
        assert "loop_mode" in _columns(engine, table)
    engine.dispose()

    engine = create_catalog_engine(database)
    metadata = initialize_catalog_schema(engine)

    assert metadata.schema_version == CATALOG_SCHEMA_VERSION == 5
    for table in _TABLES:
        assert "loop_mode" not in _columns(engine, table)
    engine.dispose()
