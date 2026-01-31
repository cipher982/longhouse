import pytest
from sqlalchemy import inspect

from zerg.database import initialize_database
from zerg.database import make_engine


@pytest.mark.xfail(
    reason="SQLite models still use Postgres-only types and schemas; Phase 2 will fix",
    strict=True,
)
def test_initialize_database_sqlite_creates_tables(tmp_path):
    db_path = tmp_path / "zerg.db"
    engine = make_engine(f"sqlite:///{db_path}")

    initialize_database(engine)

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    # Core tables
    assert "users" in tables
    assert "threads" in tables
    assert "fiches" in tables

    # Agents tables (no schema in SQLite)
    assert "sessions" in tables
    assert "events" in tables
