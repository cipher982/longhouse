"""The replayed test schema must be the schema create_all would have emitted."""

import threading

from sqlalchemy import Column
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import event

from tests_lite import _sqlite_schema_copy
from zerg.database import Base
from zerg.database import make_engine
from zerg.models.live_store import LiveBase


def _schema_rows(engine):
    with engine.connect() as connection:
        return connection.exec_driver_sql("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").all()


def test_copied_schema_matches_emitted_ddl(tmp_path):
    assert MetaData.create_all is _sqlite_schema_copy._create_all
    for metadata in (Base.metadata, LiveBase.metadata):
        copied = make_engine(f"sqlite:///{tmp_path / 'copied.db'}")
        emitted = make_engine(f"sqlite:///{tmp_path / 'emitted.db'}")
        metadata.create_all(bind=copied)
        _sqlite_schema_copy._real_create_all(metadata, emitted)

        assert _schema_rows(copied) == _schema_rows(emitted)
        assert len(_schema_rows(copied)) > 50
        with copied.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        for engine in (copied, emitted):
            engine.dispose()
        for name in ("copied.db", "emitted.db"):
            for suffix in ("", "-wal", "-shm"):
                (tmp_path / f"{name}{suffix}").unlink(missing_ok=True)


def test_non_empty_database_takes_the_real_create_all(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE unrelated (id INTEGER)")

    LiveBase.metadata.create_all(bind=engine)

    names = {row[1] for row in _schema_rows(engine)}
    assert "unrelated" in names
    assert set(LiveBase.metadata.tables) <= names
    engine.dispose()


def test_in_place_metadata_edits_never_reuse_a_stale_template(tmp_path):
    metadata = MetaData()
    table = Table("things", metadata, Column("id", Integer, primary_key=True), Column("name", String(10), nullable=True))
    index = Index("ix_things_name", table.c.name)

    def schema_after_create_all(name):
        engine = make_engine(f"sqlite:///{tmp_path / name}")
        metadata.create_all(bind=engine)
        rows = {row[1]: row[3] for row in _schema_rows(engine)}
        engine.dispose()
        return rows

    first = schema_after_create_all("first.db")
    table.c.name.nullable = False
    table.c.name.type = String(20)
    index.unique = True
    second = schema_after_create_all("second.db")

    assert "VARCHAR(10)" in first["things"] and "NOT NULL" not in first["things"].split("name", 1)[1]
    assert "VARCHAR(20) NOT NULL" in second["things"]
    assert first["ix_things_name"].startswith("CREATE INDEX")
    assert second["ix_things_name"].startswith("CREATE UNIQUE INDEX")


def test_replay_never_overwrites_a_concurrent_writer(tmp_path):
    # B decides the database is empty, then A creates the schema and writes a
    # row before B replays anything. A's row must survive.
    path = tmp_path / "shared.db"
    b_checked_empty = threading.Event()
    a_committed = threading.Event()
    b_engine = make_engine(f"sqlite:///{path}")

    @event.listens_for(b_engine, "after_cursor_execute")
    def pause_after_emptiness_check(conn, cursor, statement, parameters, context, executemany):
        if "COUNT(*) FROM sqlite_master" in statement and not b_checked_empty.is_set():
            b_checked_empty.set()
            assert a_committed.wait(timeout=10)

    b_thread = threading.Thread(target=LiveBase.metadata.create_all, kwargs={"bind": b_engine})
    b_thread.start()
    assert b_checked_empty.wait(timeout=10)
    a_engine = make_engine(f"sqlite:///{path}")
    LiveBase.metadata.create_all(bind=a_engine)
    table = next(iter(LiveBase.metadata.sorted_tables))
    with a_engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE a_marker (id INTEGER)")
        connection.exec_driver_sql("INSERT INTO a_marker VALUES (1)")
    a_committed.set()
    b_thread.join(timeout=20)
    assert not b_thread.is_alive()

    with a_engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT id FROM a_marker").scalar_one() == 1
        assert connection.exec_driver_sql(f"SELECT COUNT(*) FROM {table.name}").scalar_one() == 0
    for engine in (a_engine, b_engine):
        engine.dispose()
