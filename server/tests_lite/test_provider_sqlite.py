"""How a provider's SQLite store is opened for reading.

The QA producers and the product's Cursor reader sit on opposite sides of the
verifier boundary and may not import each other, so each has its opener. Every
test here runs over both, which is what keeps them one behaviour.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from zerg.qa.provider_sqlite import open_readonly as qa_open_readonly
from zerg.services.cursor_transcript import _open_readonly as reader_open_readonly

pytestmark = pytest.mark.parametrize("open_readonly", [qa_open_readonly, reader_open_readonly], ids=["qa", "cursor-reader"])


def _closed_wal_store(path: Path) -> Path:
    """A store as its writer leaves it on a clean exit: WAL mode, checkpointed, no sidecars."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=wal")
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO meta VALUES ('0', 'kept')")
    connection.commit()
    connection.close()
    assert sorted(entry.name for entry in path.parent.iterdir()) == [path.name]
    return path


def _fail_the_first_read(monkeypatch, *, message: str, code: int | None) -> list[str]:
    """Make a `mode=ro` open succeed and its first read fail, as SQLite does."""
    real_connect = sqlite3.connect
    opened: list[str] = []

    class Unreadable:
        def execute(self, *_args):
            error = sqlite3.OperationalError(message)
            if code is not None:
                error.sqlite_errorcode = code
            raise error

        def close(self) -> None:
            opened.append("closed")

    def connect(database: str, **kwargs):
        opened.append(database.rsplit("?", 1)[1])
        return Unreadable() if database.endswith("?mode=ro") else real_connect(database, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    return opened


def test_a_store_being_written_is_read_through_its_log(tmp_path: Path, open_readonly) -> None:
    store = tmp_path / "store.db"
    writer = sqlite3.connect(store)
    writer.execute("PRAGMA journal_mode=wal")
    writer.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    writer.execute("INSERT INTO meta VALUES ('0', 'only in the log')")
    writer.commit()
    try:
        assert store.with_name("store.db-wal").stat().st_size > 0
        reader = open_readonly(store)
        try:
            assert reader.execute("SELECT value FROM meta").fetchall() == [("only in the log",)]
            with pytest.raises(sqlite3.OperationalError):
                reader.execute("INSERT INTO meta VALUES ('1', 'x')")
        finally:
            reader.close()
    finally:
        writer.close()


def test_a_closed_store_in_a_directory_that_cannot_be_written_is_read(tmp_path: Path, open_readonly) -> None:
    store = _closed_wal_store(tmp_path / "a dir with spaces" / "store.db")
    store.parent.chmod(0o555)
    try:
        if os.access(store.parent, os.W_OK):
            pytest.skip("this user can write a new -shm into a read-only directory")
        connection = open_readonly(store, timeout=1.0)
        try:
            assert connection.execute("SELECT value FROM meta").fetchall() == [("kept",)]
        finally:
            connection.close()
        # Nothing was written beside it.
        assert sorted(entry.name for entry in store.parent.iterdir()) == [store.name]
    finally:
        store.parent.chmod(0o755)


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("attempt to write a readonly database", sqlite3.SQLITE_READONLY),
        ("attempt to write a readonly database", sqlite3.SQLITE_READONLY_DIRECTORY),
        ("unable to open database file", sqlite3.SQLITE_CANTOPEN),
        # A Python that does not report the code is judged by the message.
        ("attempt to write a readonly database", None),
    ],
)
def test_the_main_file_alone_is_read_when_the_first_read_cannot_use_the_log(
    tmp_path: Path, monkeypatch, open_readonly, message: str, code: int | None
) -> None:
    store = _closed_wal_store(tmp_path / "store.db")
    opened = _fail_the_first_read(monkeypatch, message=message, code=code)
    connection = open_readonly(store)
    try:
        assert opened == ["mode=ro", "closed", "mode=ro&immutable=1"]
        assert connection.execute("SELECT value FROM meta").fetchall() == [("kept",)]
    finally:
        connection.close()


@pytest.mark.parametrize(("message", "code"), [("database is locked", sqlite3.SQLITE_BUSY), ("disk I/O error", sqlite3.SQLITE_IOERR)])
def test_a_store_that_is_busy_is_not_answered_from_its_main_file(
    tmp_path: Path, monkeypatch, open_readonly, message: str, code: int
) -> None:
    """Its writer is still there, so the main file alone would be a stale answer."""
    store = _closed_wal_store(tmp_path / "store.db")
    opened = _fail_the_first_read(monkeypatch, message=message, code=code)
    with pytest.raises(sqlite3.OperationalError, match=message):
        open_readonly(store)
    assert opened == ["mode=ro", "closed"]


def test_a_missing_store_is_an_error_and_is_not_created(tmp_path: Path, open_readonly) -> None:
    with pytest.raises(sqlite3.OperationalError):
        open_readonly(tmp_path / "absent.db")
    assert list(tmp_path.iterdir()) == []
