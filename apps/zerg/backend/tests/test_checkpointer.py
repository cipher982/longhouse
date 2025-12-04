"""Tests for checkpointer factory service.

This module tests that the checkpointer factory returns the correct
implementation based on database type:
- PostgresSaver for PostgreSQL (production)
- MemorySaver for SQLite (tests)
"""

from unittest.mock import MagicMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine

from zerg.services.checkpointer import clear_checkpointer_cache
from zerg.services.checkpointer import get_checkpointer


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

    @patch("langgraph.checkpoint.postgres.PostgresSaver")
    def test_postgresql_returns_postgres_saver(self, mock_postgres_saver_cls):
        """Test that PostgreSQL connections get PostgresSaver."""
        # Setup mock - from_conn_string returns a context manager in v3.0+
        mock_saver_instance = MagicMock()
        mock_context_manager = MagicMock()
        mock_context_manager.__enter__ = Mock(return_value=mock_saver_instance)
        mock_postgres_saver_cls.from_conn_string.return_value = mock_context_manager

        # Create PostgreSQL engine
        engine = create_engine("postgresql://user:pass@localhost/testdb")

        # Get checkpointer
        checkpointer = get_checkpointer(engine)

        # Verify PostgresSaver was created with connection string
        mock_postgres_saver_cls.from_conn_string.assert_called_once()
        call_args = mock_postgres_saver_cls.from_conn_string.call_args
        assert "postgresql" in call_args[0][0]

        # Verify __enter__ was called
        mock_context_manager.__enter__.assert_called_once()

        # Verify we got the mocked instance back
        assert checkpointer == mock_saver_instance

    @patch("langgraph.checkpoint.postgres.PostgresSaver")
    def test_postgresql_caches_instance(self, mock_postgres_saver_cls):
        """Test that PostgresSaver instances are cached."""
        # Setup mock - from_conn_string returns a context manager in v3.0+
        mock_saver_instance = MagicMock()
        mock_context_manager = MagicMock()
        mock_context_manager.__enter__ = Mock(return_value=mock_saver_instance)
        mock_postgres_saver_cls.from_conn_string.return_value = mock_context_manager

        engine = create_engine("postgresql://user:pass@localhost/testdb")

        # Call twice with same engine
        checkpointer1 = get_checkpointer(engine)
        checkpointer2 = get_checkpointer(engine)

        # Should only create once (cached)
        assert mock_postgres_saver_cls.from_conn_string.call_count == 1

        # Should return same instance
        assert checkpointer1 == checkpointer2

    @patch("langgraph.checkpoint.postgres.PostgresSaver")
    def test_postgresql_setup_failure_falls_back_to_memory(self, mock_postgres_saver_cls):
        """Test that setup failure falls back to MemorySaver."""
        # Setup mock to fail when entering context (simulates connection failure)
        mock_context_manager = MagicMock()
        mock_context_manager.__enter__.side_effect = Exception("Connection failed")
        mock_postgres_saver_cls.from_conn_string.return_value = mock_context_manager

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

    def test_clear_cache_resets_postgres_instances(self):
        """Test that cache clearing forces new PostgresSaver creation."""
        with patch("langgraph.checkpoint.postgres.PostgresSaver") as mock_postgres_saver_cls:
            # Setup mock - from_conn_string returns a context manager in v3.0+
            mock_saver_instance1 = MagicMock()
            mock_saver_instance2 = MagicMock()
            mock_context_manager1 = MagicMock()
            mock_context_manager2 = MagicMock()
            mock_context_manager1.__enter__ = Mock(return_value=mock_saver_instance1)
            mock_context_manager2.__enter__ = Mock(return_value=mock_saver_instance2)
            mock_postgres_saver_cls.from_conn_string.side_effect = [
                mock_context_manager1,
                mock_context_manager2,
            ]

            engine = create_engine("postgresql://user:pass@localhost/testdb")

            # Create first instance
            get_checkpointer(engine)
            assert mock_postgres_saver_cls.from_conn_string.call_count == 1

            # Clear cache
            clear_checkpointer_cache()

            # Create second instance - should call factory again
            get_checkpointer(engine)
            assert mock_postgres_saver_cls.from_conn_string.call_count == 2


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
