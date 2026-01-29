"""Unit tests for is_postgres helper function."""

from unittest.mock import MagicMock
from unittest.mock import patch


class TestIsPostgres:
    """Tests for is_postgres helper function."""

    def test_returns_true_for_postgresql(self):
        """is_postgres should return True for PostgreSQL dialect."""
        from zerg import database

        mock_engine = MagicMock()
        mock_engine.dialect.name = "postgresql"

        original = database.default_engine
        database.default_engine = mock_engine
        try:
            result = database.is_postgres()
            assert result is True
        finally:
            database.default_engine = original

    def test_returns_false_for_none_engine(self):
        """is_postgres should return False when default_engine is None."""
        with patch("zerg.database.default_engine", None):
            from zerg import database

            # The function reads default_engine at call time
            original = database.default_engine
            database.default_engine = None
            try:
                result = database.is_postgres()
                assert result is False
            finally:
                database.default_engine = original

    def test_returns_false_for_sqlite(self):
        """is_postgres should return False for SQLite dialect."""
        from zerg import database

        mock_engine = MagicMock()
        mock_engine.dialect.name = "sqlite"

        original = database.default_engine
        database.default_engine = mock_engine
        try:
            result = database.is_postgres()
            assert result is False
        finally:
            database.default_engine = original

    def test_returns_true_for_postgresql_dialect(self):
        """is_postgres should return True for postgresql dialect."""
        from zerg import database

        mock_engine = MagicMock()
        mock_engine.dialect.name = "postgresql"

        original = database.default_engine
        database.default_engine = mock_engine
        try:
            result = database.is_postgres()
            assert result is True
        finally:
            database.default_engine = original
