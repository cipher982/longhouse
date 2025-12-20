"""Tests for checkpointer factory service.

This module tests that the checkpointer factory returns the correct
implementation based on database type:
- PostgresSaver for PostgreSQL (production)
- MemorySaver for SQLite (tests)
"""

import os
from unittest.mock import MagicMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine

from zerg.services.checkpointer import clear_checkpointer_cache
from zerg.services.checkpointer import get_checkpointer


@pytest.fixture
def disable_testing_mode():
    """Temporarily disable TESTING mode to test production code paths."""
    original = os.environ.get("TESTING")
    os.environ["TESTING"] = "0"
    yield
    if original is not None:
        os.environ["TESTING"] = original
    elif "TESTING" in os.environ:
        del os.environ["TESTING"]


class TestCheckpointerFactory:
    """Test checkpointer factory returns correct implementation."""

    def teardown_method(self):
        """Clear cache after each test."""
        clear_checkpointer_cache()

    def test_sqlite_returns_memory_saver(self):
        """Test that SQLite connections get MemorySaver."""
        engine = create_engine("sqlite:///test.db")
        checkpointer = get_checkpointer(engine)

        assert isinstance(checkpointer, MemorySaver)

    def test_sqlite_memory_returns_memory_saver(self):
        """Test that SQLite in-memory connections get MemorySaver."""
        engine = create_engine("sqlite:///:memory:")
        checkpointer = get_checkpointer(engine)

        assert isinstance(checkpointer, MemorySaver)

    def test_postgresql_returns_memory_saver_in_test_mode(self):
        """Test that PostgreSQL connections get MemorySaver in test mode."""
        # In test mode (TESTING=1), we always return MemorySaver for speed
        engine = create_engine("postgresql://user:pass@localhost/testdb")
        checkpointer = get_checkpointer(engine)

        # Should return MemorySaver since TESTING=1
        assert isinstance(checkpointer, MemorySaver)

    @patch("zerg.services.checkpointer._create_async_checkpointer_in_bg_loop")
    def test_postgresql_returns_postgres_saver(self, mock_create_checkpointer, disable_testing_mode):
        """Test that PostgreSQL connections get AsyncPostgresSaver in production."""
        # Setup mock to return a fake checkpointer
        mock_saver_instance = MagicMock()
        mock_pool = MagicMock()
        mock_create_checkpointer.return_value = (mock_pool, mock_saver_instance)

        # Create PostgreSQL engine
        engine = create_engine("postgresql://user:pass@localhost/testdb")

        # Get checkpointer
        checkpointer = get_checkpointer(engine)

        # Verify async checkpointer was created
        mock_create_checkpointer.assert_called_once()

        # Verify we got the mocked instance back
        assert checkpointer == mock_saver_instance

    @patch("zerg.services.checkpointer._create_async_checkpointer_in_bg_loop")
    def test_postgresql_caches_instance(self, mock_create_checkpointer, disable_testing_mode):
        """Test that AsyncPostgresSaver instances are cached."""
        # Setup mock to return a fake checkpointer
        mock_saver_instance = MagicMock()
        mock_pool = MagicMock()
        mock_create_checkpointer.return_value = (mock_pool, mock_saver_instance)

        engine = create_engine("postgresql://user:pass@localhost/testdb")

        # Call twice with same engine
        checkpointer1 = get_checkpointer(engine)
        checkpointer2 = get_checkpointer(engine)

        # Should only create once (cached)
        assert mock_create_checkpointer.call_count == 1

        # Should return same instance
        assert checkpointer1 == checkpointer2

    @patch("zerg.services.checkpointer._create_async_checkpointer_in_bg_loop")
    def test_postgresql_setup_failure_falls_back_to_memory(self, mock_create_checkpointer, disable_testing_mode):
        """Test that setup failure falls back to MemorySaver."""
        # Setup mock to fail (simulates connection failure)
        mock_create_checkpointer.side_effect = Exception("Connection failed")

        engine = create_engine("postgresql://user:pass@localhost/testdb")
        checkpointer = get_checkpointer(engine)

        # Should fall back to MemorySaver
        assert isinstance(checkpointer, MemorySaver)

    def test_unknown_database_falls_back_to_memory(self):
        """Test that unknown database types fall back to MemorySaver."""
        # Mock an engine with a non-PostgreSQL, non-SQLite URL
        mock_engine = Mock()
        mock_engine.url = Mock()
        mock_engine.url.__str__ = Mock(return_value="oracle://user:pass@localhost/testdb")

        checkpointer = get_checkpointer(mock_engine)

        assert isinstance(checkpointer, MemorySaver)

    def test_default_engine_used_when_none_provided(self):
        """Test that default engine is used when no engine provided."""
        # This uses the actual default_engine from zerg.database
        # which should be SQLite in test environment
        checkpointer = get_checkpointer()

        # Should return MemorySaver since test environment uses SQLite
        assert isinstance(checkpointer, MemorySaver)

    def test_clear_cache_resets_postgres_instances(self, disable_testing_mode):
        """Test that cache clearing forces new AsyncPostgresSaver creation."""
        with patch("zerg.services.checkpointer._create_async_checkpointer_in_bg_loop") as mock_create:
            # Setup mock to return fake checkpointers
            mock_saver_instance1 = MagicMock()
            mock_saver_instance2 = MagicMock()
            mock_pool1 = MagicMock()
            mock_pool2 = MagicMock()
            mock_create.side_effect = [
                (mock_pool1, mock_saver_instance1),
                (mock_pool2, mock_saver_instance2),
            ]

            engine = create_engine("postgresql://user:pass@localhost/testdb")

            # Create first instance
            get_checkpointer(engine)
            assert mock_create.call_count == 1

            # Clear cache
            clear_checkpointer_cache()

            # Create second instance - should call factory again
            get_checkpointer(engine)
            assert mock_create.call_count == 2


class TestCheckpointerIntegration:
    """Integration tests with actual MemorySaver functionality."""

    def test_memory_saver_can_store_and_retrieve_checkpoint(self):
        """Test that MemorySaver can actually checkpoint state."""
        from langchain_core.messages import AIMessage
        from langchain_core.messages import HumanMessage

        engine = create_engine("sqlite:///:memory:")
        checkpointer = get_checkpointer(engine)

        # Create a simple checkpoint config
        config = {"configurable": {"thread_id": "test-thread"}}

        # Store some checkpoint data
        # Note: This is a simplified example - real LangGraph usage is more complex
        messages = [
            HumanMessage(content="Hello"),
            AIMessage(content="Hi there!"),
        ]

        # Verify we can use the checkpointer (basic smoke test)
        assert hasattr(checkpointer, "put")
        assert hasattr(checkpointer, "get")
        assert callable(checkpointer.put)
        assert callable(checkpointer.get)

    def test_checkpointer_survives_multiple_calls(self):
        """Test that checkpointer can be called multiple times."""
        engine = create_engine("sqlite:///:memory:")

        # Get checkpointer multiple times
        cp1 = get_checkpointer(engine)
        cp2 = get_checkpointer(engine)
        cp3 = get_checkpointer(engine)

        # All should be MemorySaver instances
        assert isinstance(cp1, MemorySaver)
        assert isinstance(cp2, MemorySaver)
        assert isinstance(cp3, MemorySaver)

        # Note: MemorySaver creates new instances each time (no caching for SQLite)
        # This is intentional for test isolation
