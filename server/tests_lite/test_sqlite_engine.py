from zerg.database import make_engine


def _pragma_scalar(conn, name: str):
    return conn.exec_driver_sql(f"PRAGMA {name}").scalar()


def test_make_engine_allows_sqlite(tmp_path):
    db_path = tmp_path / "zerg.db"
    engine = make_engine(f"sqlite:///{db_path}")
    assert engine.dialect.name == "sqlite"


def test_sqlite_pragmas_configured(tmp_path, monkeypatch):
    db_path = tmp_path / "zerg.db"

    monkeypatch.setenv("SQLITE_BUSY_TIMEOUT_MS", "1234")
    monkeypatch.setenv("SQLITE_SYNCHRONOUS", "FULL")
    monkeypatch.setenv("SQLITE_JOURNAL_MODE", "WAL")
    monkeypatch.setenv("SQLITE_FOREIGN_KEYS", "ON")
    monkeypatch.setenv("SQLITE_WAL_AUTOCHECKPOINT", "2000")

    engine = make_engine(f"sqlite:///{db_path}")

    with engine.connect() as conn:
        journal_mode = _pragma_scalar(conn, "journal_mode")
        busy_timeout = _pragma_scalar(conn, "busy_timeout")
        foreign_keys = _pragma_scalar(conn, "foreign_keys")
        synchronous = _pragma_scalar(conn, "synchronous")
        wal_autocheckpoint = _pragma_scalar(conn, "wal_autocheckpoint")

    assert str(journal_mode).lower() == "wal"
    assert int(busy_timeout) == 1234
    assert int(foreign_keys) == 1
    assert int(synchronous) == 2  # FULL
    assert int(wal_autocheckpoint) == 2000


def test_sqlite_connect_does_not_need_writer_lock(tmp_path):
    import sqlite3

    db_path = tmp_path / "zerg.db"
    seed_engine = make_engine(f"sqlite:///{db_path}")
    with seed_engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE lock_probe (id INTEGER PRIMARY KEY)")

    locker = sqlite3.connect(str(db_path), timeout=0.1, isolation_level=None)
    try:
        locker.execute("PRAGMA journal_mode=WAL")
        locker.execute("BEGIN IMMEDIATE")
        locker.execute("INSERT INTO lock_probe DEFAULT VALUES")

        engine = make_engine(f"sqlite:///{db_path}", busy_timeout_ms=1)
        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM lock_probe").scalar() == 0
    finally:
        locker.rollback()
        locker.close()
