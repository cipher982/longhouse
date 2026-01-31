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
        f"SQLite {sqlite3.sqlite_version} is below minimum {'.'.join(str(x) for x in SQLITE_MIN_VERSION)}. "
        f"Upgrade SQLite or use Postgres."
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
