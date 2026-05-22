from unittest.mock import patch

from sqlalchemy import inspect

from zerg.database import _ensure_agents_fts
from zerg.database import initialize_database
from zerg.database import make_engine


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
    assert "events_fts" in tables


def test_initialize_database_sqlite_does_not_plan_heavy_migrations(monkeypatch, tmp_path):
    import zerg.db_migrations as db_migrations

    db_path = tmp_path / "zerg.db"
    engine = make_engine(f"sqlite:///{db_path}")

    def fail_if_called(_engine):
        raise AssertionError("startup must not scan heavy migration state")

    monkeypatch.setattr(db_migrations, "pending_heavy_migration_names", fail_if_called)

    initialize_database(engine)

    inspector = inspect(engine)
    assert "migration_runs" in set(inspector.get_table_names())


def test_ensure_agents_fts_skips_write_path_when_objects_already_exist(tmp_path):
    db_path = tmp_path / "zerg_busy.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)

    real_connect = engine.connect
    seen_statements: list[str] = []

    class GuardedConnection:
        def __init__(self, conn):
            self._conn = conn

        def __enter__(self):
            self._conn.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._conn.__exit__(exc_type, exc, tb)

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def exec_driver_sql(self, statement, *args, **kwargs):
            sql = str(statement).strip()
            seen_statements.append(sql)
            assert not sql.startswith("CREATE ")
            assert not sql.startswith("INSERT INTO events_fts(events_fts) VALUES('rebuild')")
            return self._conn.exec_driver_sql(statement, *args, **kwargs)

    with patch.object(engine, "connect", side_effect=lambda: GuardedConnection(real_connect())):
        with patch.object(engine, "begin", side_effect=AssertionError("should not enter write transaction")):
            _ensure_agents_fts(engine)

    assert seen_statements
