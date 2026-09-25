"""The copied test schema must be the schema create_all would have emitted."""

from sqlalchemy import MetaData

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
