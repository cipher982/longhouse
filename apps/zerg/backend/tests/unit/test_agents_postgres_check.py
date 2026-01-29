"""Unit tests for agents router PostgreSQL requirement check."""

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from zerg.routers.agents import require_postgres


class TestRequirePostgres:
    """Tests for require_postgres dependency."""

    def test_passes_with_postgres(self):
        """require_postgres should not raise when using PostgreSQL."""
        with patch("zerg.routers.agents.is_postgres", return_value=True):
            # Should not raise
            require_postgres()

    def test_raises_501_with_sqlite(self):
        """require_postgres should raise 501 when using SQLite."""
        with patch("zerg.routers.agents.is_postgres", return_value=False):
            with pytest.raises(HTTPException) as exc_info:
                require_postgres()

            assert exc_info.value.status_code == 501
            assert "PostgreSQL" in exc_info.value.detail
            assert "SQLite" in exc_info.value.detail

    def test_raises_501_with_no_engine(self):
        """require_postgres should raise 501 when no engine is configured."""
        with patch("zerg.routers.agents.is_postgres", return_value=False):
            with pytest.raises(HTTPException) as exc_info:
                require_postgres()

            assert exc_info.value.status_code == 501
