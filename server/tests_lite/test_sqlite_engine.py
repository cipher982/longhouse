from zerg.database import _checkpoint_counts
from zerg.database import _run_wal_checkpoint
from zerg.database import make_engine
from zerg.database import make_live_engine


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


def test_checkpoint_counts_interprets_sqlite_wal_tuple():
    # SQLite returns (busy, log_frames, checkpointed_frames), not
    # (busy, checkpointed, remaining).
    assert _checkpoint_counts((0, 5521, 5521)) == (0, 5521, 5521, 0)
    assert _checkpoint_counts((0, 5521, 5000)) == (0, 5521, 5000, 521)


def test_wal_checkpoint_helper_accepts_live_engine(tmp_path):
    db_path = tmp_path / "live.db"
    engine = make_live_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE writes (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql("INSERT INTO writes DEFAULT VALUES")

    payload = _run_wal_checkpoint(engine, label="live", truncate_bytes=0)

    assert payload["label"] == "live"
    assert payload["skipped"] is False
    assert {"busy", "log_frames", "checkpointed_frames", "remaining_frames"} <= set(payload)


def test_get_live_wal_bytes_returns_int_or_none(tmp_path, monkeypatch):
    db_path = tmp_path / "live-wal-probe.db"
    engine = make_live_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE writes (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql("INSERT INTO writes DEFAULT VALUES")

    import zerg.database as database_mod

    original_engine = database_mod.live_engine
    monkeypatch.setattr(database_mod, "live_engine", engine)
    try:
        wal_bytes = database_mod.get_live_wal_bytes()
        assert wal_bytes is not None
        assert isinstance(wal_bytes, int)
        assert wal_bytes >= 0
    finally:
        monkeypatch.setattr(database_mod, "live_engine", original_engine)
