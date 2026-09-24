import sqlite3

from zerg.searchd.store import WAL_SIZE_LIMIT_BYTES
from zerg.searchd.store import _connect


def test_writer_connection_bounds_the_wal_file(tmp_path):
    connection = _connect(tmp_path / "search.db")
    try:
        assert connection.execute("PRAGMA journal_size_limit").fetchone()[0] == WAL_SIZE_LIMIT_BYTES
    finally:
        connection.close()


def test_wal_file_is_truncated_to_the_limit_after_a_checkpoint(tmp_path):
    # Same mechanism with a small limit, so the test does not write 64 MB.
    path = tmp_path / "search.db"
    connection = _connect(path)
    connection.execute("PRAGMA journal_size_limit=65536")
    connection.execute("CREATE TABLE t (payload BLOB)")
    for _ in range(400):
        connection.execute("INSERT INTO t VALUES (randomblob(4096))")
    wal = tmp_path / "search.db-wal"
    assert wal.stat().st_size > 1_000_000
    connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
    connection.execute("INSERT INTO t VALUES (randomblob(16))")  # the log rewinds here
    assert wal.stat().st_size <= 65536
    connection.close()
    assert sqlite3.connect(path).execute("SELECT count(*) FROM t").fetchone()[0] == 401
