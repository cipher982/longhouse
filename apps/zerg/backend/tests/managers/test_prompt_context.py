"""Unit tests for message_builder helpers.

Tests cover:
- derive_memory_query() consistent behavior
- get_or_create_tool_message() idempotency
- find_parent_assistant_id() lookup
"""

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage

from tests.conftest import TEST_COMMIS_MODEL
from zerg.crud import crud
from zerg.managers.message_builder import derive_memory_query
from zerg.managers.message_builder import find_parent_assistant_id
from zerg.managers.message_builder import get_or_create_tool_message

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_user(db_session):
    """Create a test user."""
    return crud.create_user(db_session, email="prompt-ctx-test@local", provider=None, role="USER")


@pytest.fixture
def test_fiche(db_session, test_user):
    """Create a test fiche with system instructions."""
    return crud.create_fiche(
        db_session,
        owner_id=test_user.id,
        name="prompt-ctx-fiche",
        system_instructions="You are a helpful assistant.",
        task_instructions="",
        model=TEST_COMMIS_MODEL,
        schedule=None,
        config={"skills_enabled": False},
    )


@pytest.fixture
def test_thread(db_session, test_fiche):
    """Create a test thread."""
    return crud.create_thread(
        db=db_session,
        fiche_id=test_fiche.id,
        title="prompt-ctx-thread",
        active=True,
        fiche_state={},
        memory_strategy="buffer",
    )


# ---------------------------------------------------------------------------
# derive_memory_query() tests
# ---------------------------------------------------------------------------


class TestDeriveMemoryQuery:
    def test_extracts_from_unprocessed_rows(self):
        """Should extract query from latest non-internal user message."""
        mock_row = MagicMock()
        mock_row.role = "user"
        mock_row.internal = False
        mock_row.content = "What is the weather?"

        result = derive_memory_query(unprocessed_rows=[mock_row])
        assert result == "What is the weather?"

    def test_skips_internal_messages(self):
        """Should skip internal user messages."""
        mock_internal = MagicMock()
        mock_internal.role = "user"
        mock_internal.internal = True
        mock_internal.content = "Internal message"

        mock_real = MagicMock()
        mock_real.role = "user"
        mock_real.internal = False
        mock_real.content = "Real message"

        result = derive_memory_query(unprocessed_rows=[mock_real, mock_internal])
        assert result == "Real message"

    def test_extracts_from_conversation_msgs(self):
        """Should extract query from HumanMessage when no unprocessed rows."""
        msgs = [
            HumanMessage(content="Hello"),
            AIMessage(content="Hi"),
            HumanMessage(content="What is Python?"),
        ]

        result = derive_memory_query(conversation_msgs=msgs)
        assert result == "What is Python?"

    def test_strips_timestamp_prefix(self):
        """Should strip timestamp prefix from HumanMessage content."""
        msgs = [HumanMessage(content="[2024-01-15T10:30:00Z] What is AI?")]

        result = derive_memory_query(conversation_msgs=msgs)
        assert result == "What is AI?"

    def test_unprocessed_rows_takes_priority(self):
        """unprocessed_rows should take priority over conversation_msgs."""
        mock_row = MagicMock()
        mock_row.role = "user"
        mock_row.internal = False
        mock_row.content = "From unprocessed"

        msgs = [HumanMessage(content="From conversation")]

        result = derive_memory_query(unprocessed_rows=[mock_row], conversation_msgs=msgs)
        assert result == "From unprocessed"

    def test_returns_none_when_no_query(self):
        """Should return None when no query can be extracted."""
        result = derive_memory_query()
        assert result is None

    def test_returns_none_for_empty_content(self):
        """Should return None for empty content."""
        mock_row = MagicMock()
        mock_row.role = "user"
        mock_row.internal = False
        mock_row.content = "   "

        result = derive_memory_query(unprocessed_rows=[mock_row])
        assert result is None


# ---------------------------------------------------------------------------
# get_or_create_tool_message() tests
# ---------------------------------------------------------------------------


class TestGetOrCreateToolMessage:
    def test_creates_new_tool_message(self, db_session, test_thread):
        """Should create a new ToolMessage if none exists."""
        tool_msg, created = get_or_create_tool_message(
            db_session,
            thread_id=test_thread.id,
            tool_call_id="tc-123",
            result="Task completed successfully",
        )

        assert created is True
        assert tool_msg.tool_call_id == "tc-123"
        assert "Commis completed" in tool_msg.content
        assert "Task completed successfully" in tool_msg.content

    def test_returns_existing_tool_message(self, db_session, test_thread):
        """Should return existing ToolMessage if one exists."""
        tool_msg1, created1 = get_or_create_tool_message(
            db_session,
            thread_id=test_thread.id,
            tool_call_id="tc-456",
            result="First result",
        )
        assert created1 is True

        tool_msg2, created2 = get_or_create_tool_message(
            db_session,
            thread_id=test_thread.id,
            tool_call_id="tc-456",
            result="Second result",
        )

        assert created2 is False
        assert tool_msg2.tool_call_id == "tc-456"
        assert "First result" in tool_msg2.content

    def test_creates_failed_tool_message(self, db_session, test_thread):
        """Should format error message for failed commis."""
        tool_msg, created = get_or_create_tool_message(
            db_session,
            thread_id=test_thread.id,
            tool_call_id="tc-789",
            result="Partial work",
            error="Connection timeout",
            status="failed",
        )

        assert created is True
        assert "Commis failed" in tool_msg.content
        assert "Connection timeout" in tool_msg.content
        assert "Partial work" in tool_msg.content


# ---------------------------------------------------------------------------
# find_parent_assistant_id() tests
# ---------------------------------------------------------------------------


class TestFindParentAssistantId:
    def test_finds_parent_with_matching_tool_call(self, db_session, test_thread):
        """Should find assistant message that issued the tool call."""
        from zerg.services.thread_service import ThreadService

        ThreadService.save_new_messages(
            db_session,
            thread_id=test_thread.id,
            messages=[
                AIMessage(
                    content="I'll spawn a commis",
                    tool_calls=[{"id": "tc-find-test", "name": "spawn_commis", "args": {}}],
                ),
            ],
            processed=True,
        )

        parent_id = find_parent_assistant_id(
            db_session,
            thread_id=test_thread.id,
            tool_call_ids=["tc-find-test"],
        )

        assert parent_id is not None

    def test_returns_none_when_no_match_and_no_fallback(self, db_session, test_thread):
        """Should return None when no matching tool_call found and fallback disabled."""
        parent_id = find_parent_assistant_id(
            db_session,
            thread_id=test_thread.id,
            tool_call_ids=["non-existent-tc"],
            fallback_to_latest=False,
        )

        assert parent_id is None

    def test_fallback_to_latest_assistant(self, db_session, test_thread):
        """Should fallback to most recent assistant with tool_calls when no exact match."""
        from zerg.services.thread_service import ThreadService

        ThreadService.save_new_messages(
            db_session,
            thread_id=test_thread.id,
            messages=[
                AIMessage(
                    content="I'll spawn a commis",
                    tool_calls=[{"id": "tc-other", "name": "spawn_commis", "args": {}}],
                ),
            ],
            processed=True,
        )

        parent_id = find_parent_assistant_id(
            db_session,
            thread_id=test_thread.id,
            tool_call_ids=["non-existent-tc"],
        )

        assert parent_id is not None
