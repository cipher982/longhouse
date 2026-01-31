"""Tests for is_sqlite_url helper function.

Ensures quoted DATABASE_URLs are handled correctly.

Tests both the canonical location (db_utils.is_sqlite_url) and the
backward-compatible alias (database._is_sqlite_url).
"""

import pytest

from zerg.db_utils import is_sqlite_url
from zerg.database import _is_sqlite_url


class TestIsSqliteUrl:
    """Test is_sqlite_url() properly detects SQLite URLs."""

    def test_unquoted_sqlite_url(self):
        """Unquoted sqlite URL should be detected."""
        assert is_sqlite_url("sqlite:///path/to/db.sqlite") is True
        assert is_sqlite_url("sqlite:///./test.db") is True
        assert is_sqlite_url("sqlite:///:memory:") is True

    def test_double_quoted_sqlite_url(self):
        """Double-quoted sqlite URL should be detected."""
        assert is_sqlite_url('"sqlite:///path/to/db.sqlite"') is True
        assert is_sqlite_url('"sqlite:///./test.db"') is True
        assert is_sqlite_url('"sqlite:///:memory:"') is True

    def test_single_quoted_sqlite_url(self):
        """Single-quoted sqlite URL should be detected."""
        assert is_sqlite_url("'sqlite:///path/to/db.sqlite'") is True
        assert is_sqlite_url("'sqlite:///./test.db'") is True
        assert is_sqlite_url("'sqlite:///:memory:'") is True

    def test_postgres_url_not_sqlite(self):
        """Postgres URLs should not be detected as SQLite."""
        assert is_sqlite_url("postgresql://user:pass@host:5432/db") is False
        assert is_sqlite_url("postgresql+psycopg://user:pass@host/db") is False
        assert is_sqlite_url("postgres://user:pass@host:5432/db") is False

    def test_quoted_postgres_url_not_sqlite(self):
        """Quoted Postgres URLs should not be detected as SQLite."""
        assert is_sqlite_url('"postgresql://user:pass@host:5432/db"') is False
        assert is_sqlite_url("'postgresql://user:pass@host:5432/db'") is False

    def test_empty_url(self):
        """Empty URLs should return False."""
        assert is_sqlite_url("") is False
        assert is_sqlite_url(None) is False
        assert is_sqlite_url("   ") is False
        assert is_sqlite_url('""') is False
        assert is_sqlite_url("''") is False

    def test_url_with_whitespace(self):
        """URLs with leading/trailing whitespace should be handled."""
        assert is_sqlite_url("  sqlite:///test.db  ") is True
        assert is_sqlite_url('  "sqlite:///test.db"  ') is True
        assert is_sqlite_url("  postgresql://host/db  ") is False

    def test_sqlite_with_parameters(self):
        """SQLite URLs with query parameters should be detected."""
        assert is_sqlite_url("sqlite:///test.db?mode=ro") is True
        assert is_sqlite_url("sqlite:///test.db?mode=rwc&cache=shared") is True

    def test_sqlite_async_driver(self):
        """SQLite with async driver (sqlite+aiosqlite) should be detected."""
        assert is_sqlite_url("sqlite+aiosqlite:///test.db") is True
        assert is_sqlite_url('"sqlite+aiosqlite:///test.db"') is True

    def test_backward_compat_alias(self):
        """database._is_sqlite_url should be an alias for db_utils.is_sqlite_url."""
        # Verify the alias works identically
        assert _is_sqlite_url("sqlite:///test.db") is True
        assert _is_sqlite_url('"sqlite:///test.db"') is True
        assert _is_sqlite_url("postgresql://host/db") is False
