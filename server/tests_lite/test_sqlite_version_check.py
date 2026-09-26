"""Tests for SQLite version compatibility check.

These tests verify that:
1. The version check function works correctly
2. The current environment meets SQLite 3.35+ requirements
3. The runtime enforces the minimum version

NOTE: Modern Python (3.8+) bundles SQLite 3.35+, so these tests serve as
sanity checks that the environment is properly configured.
"""

import sqlite3

from zerg.database import SQLITE_MIN_VERSION
from zerg.database import check_sqlite_version
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.searchd import store as searchd_store


def test_check_sqlite_version_compatible(tmp_path):
    """Current SQLite version should be compatible (>= 3.35)."""
    db_path = tmp_path / "version_check.db"
    engine = make_engine(f"sqlite:///{db_path}")

    is_compatible, version_str = check_sqlite_version(engine)

    # Modern Python includes SQLite 3.35+ by default
    assert is_compatible is True
    assert version_str == sqlite3.sqlite_version


def test_check_sqlite_version_not_sqlite():
    """Non-SQLite engines return N/A."""
    # This test uses a mock-like approach since we can't easily create
    # a Postgres engine without a real server
    from unittest.mock import MagicMock

    mock_engine = MagicMock()
    mock_engine.dialect.name = "postgresql"

    is_compatible, version_str = check_sqlite_version(mock_engine)

    assert is_compatible is True
    assert version_str == "N/A (not SQLite)"


def test_min_version_constant():
    """Minimum version constant is set correctly (3.35+ for RETURNING support)."""
    assert SQLITE_MIN_VERSION == (3, 35, 0)


def test_current_sqlite_version_meets_minimum():
    """Verify this environment's SQLite meets minimum requirements."""
    current_version = tuple(int(x) for x in sqlite3.sqlite_version.split("."))
    assert current_version >= SQLITE_MIN_VERSION, (
        f"SQLite {sqlite3.sqlite_version} is below minimum {'.'.join(str(x) for x in SQLITE_MIN_VERSION)}. Upgrade SQLite or use Postgres."
    )


def test_initialize_database_rejects_old_sqlite(monkeypatch, tmp_path):
    """initialize_database should reject SQLite below the minimum version."""
    db_path = tmp_path / "version_check_fail.db"
    engine = make_engine(f"sqlite:///{db_path}")

    monkeypatch.setattr(sqlite3, "sqlite_version", "3.8.0")

    try:
        initialize_database(engine)
    except RuntimeError as exc:
        min_ver = ".".join(str(x) for x in SQLITE_MIN_VERSION)
        assert min_ver in str(exc)
    else:
        raise AssertionError("Expected initialize_database to raise on old SQLite")


def test_searchd_min_version_constant():
    """searchd requires 3.43+: events_fts/searchable_fts set contentless_delete=1
    (45cfa8cdf/b438d9bdd, 2026-09-24), an FTS5 option unsupported before then."""
    assert searchd_store.MIN_SQLITE_VERSION == (3, 43, 0)


def test_searchd_check_sqlite_version_compatible():
    """This environment's SQLite meets searchd's floor.

    Modern Python distributions (including uv-managed python-build-standalone
    interpreters) bundle SQLite well past 3.43; this is a sanity check, not a
    version-specific assertion.
    """
    is_compatible, version_str = searchd_store.check_sqlite_version()

    assert is_compatible is True
    assert version_str == sqlite3.sqlite_version


def test_searchd_check_sqlite_version_rejects_old(monkeypatch):
    """3.40.1 -- the exact version that crash-looped the 2026-09-26 factory
    candidate host -- is reported incompatible."""
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.40.1")

    is_compatible, version_str = searchd_store.check_sqlite_version()

    assert is_compatible is False
    assert version_str == "3.40.1"


def test_open_search_database_rejects_old_sqlite_with_clear_message(monkeypatch, tmp_path):
    """No silent stdlib fallback: open_search_database names found/required/remedy up front.

    Before this check, an old SQLite instead failed deep inside
    `_initialize_schema` with a bare `OperationalError: unrecognized option:
    "contentless_delete"` -- exactly what made the 2026-09-26 factory incident
    take so long to place (every caller three layers up just saw "catalogd
    unavailable").
    """
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.40.1")

    try:
        searchd_store.open_search_database(tmp_path / "search.db")
    except searchd_store.SearchdSqliteTooOld as exc:
        message = str(exc)
        assert "3.43.0" in message
        assert "3.40.1" in message
        assert "pysqlite3" in message
    else:
        raise AssertionError("Expected open_search_database to reject SQLite 3.40.1")

    # The rejected attempt must not leave a half-initialized store file behind.
    assert not (tmp_path / "search.db").exists()


def test_open_search_read_database_rejects_old_sqlite(monkeypatch, tmp_path):
    """The read-only path is guarded too.

    An old reader can still crash on an already-created contentless_delete
    table's stored module arguments, not just on the writer's own DDL.
    """
    db_path = tmp_path / "search.db"
    connection = searchd_store.open_search_database(db_path)
    connection.close()

    monkeypatch.setattr(sqlite3, "sqlite_version", "3.40.1")

    try:
        searchd_store.open_search_read_database(db_path)
    except searchd_store.SearchdSqliteTooOld as exc:
        assert "3.43.0" in str(exc)
    else:
        raise AssertionError("Expected open_search_read_database to reject SQLite 3.40.1")
