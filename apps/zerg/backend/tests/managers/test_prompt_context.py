"""Unit tests for PromptContext module.

Tests cover:
- derive_memory_query() consistent behavior
- get_or_create_tool_message() idempotency
- find_parent_assistant_id() lookup
- build_prompt() unified construction
- PromptContext structure and conversion
"""

from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from tests.conftest import TEST_COMMIS_MODEL
from zerg.connectors.status_builder import FicheContextParts
from zerg.crud import crud
from zerg.managers.fiche_runner import RuntimeView
from zerg.managers.prompt_context import (
    DynamicContextBlock,
    PromptContext,
    build_prompt,
    context_to_messages,
    derive_memory_query,
    find_parent_assistant_id,
    get_or_create_tool_message,
)


def _mock_fiche_context_parts(**kwargs):
    """Return a mock FicheContextParts for testing."""
    return FicheContextParts(
        connector_status='<connector_status captured_at="2026-01-01T00:00Z">\n{}\n</connector_status>',
        current_time="<current_time>2026-01-01T00:00Z</current_time>",
    )


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


@pytest.fixture
def runtime_view(test_fiche):
    """Create a RuntimeView from test fiche."""
    return RuntimeView(
        id=test_fiche.id,
        owner_id=test_fiche.owner_id,
        updated_at=test_fiche.updated_at,
        model=test_fiche.model,
        config=test_fiche.config or {},
        allowed_tools=None,
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

        # Order: internal first, then real (reversed iteration should find real)
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
        # Create first
        tool_msg1, created1 = get_or_create_tool_message(
            db_session,
            thread_id=test_thread.id,
            tool_call_id="tc-456",
            result="First result",
        )
        assert created1 is True

        # Try to create again with same tool_call_id
        tool_msg2, created2 = get_or_create_tool_message(
            db_session,
            thread_id=test_thread.id,
            tool_call_id="tc-456",
            result="Second result",  # Different result
        )

        assert created2 is False
        assert tool_msg2.tool_call_id == "tc-456"
        # Should have original content, not new
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

        # Create assistant message with tool_calls
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

        # Create assistant message with different tool_call_id
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

        # Search for non-matching ID but with fallback enabled (default)
        parent_id = find_parent_assistant_id(
            db_session,
            thread_id=test_thread.id,
            tool_call_ids=["non-existent-tc"],
        )

        # Should find the assistant message as fallback
        assert parent_id is not None


# ---------------------------------------------------------------------------
# build_prompt() tests
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    def test_builds_prompt_from_thread_id(self, db_session, runtime_view, test_fiche, test_thread):
        """Should build prompt using thread_id (run_thread flow)."""
        from zerg.services.thread_service import ThreadService

        # Add some messages
        ThreadService.save_new_messages(
            db_session,
            thread_id=test_thread.id,
            messages=[HumanMessage(content="Hello")],
            processed=True,
        )

        with patch("zerg.connectors.status_builder.build_fiche_context_parts", side_effect=_mock_fiche_context_parts):
            context = build_prompt(
                db_session,
                runtime_view,
                test_fiche,
                thread_id=test_thread.id,
            )

        assert isinstance(context, PromptContext)
        assert "You are a helpful assistant" in context.system_prompt
        assert context.message_count_with_context > 0

    def test_builds_prompt_from_conversation_msgs(self, db_session, runtime_view, test_fiche):
        """Should build prompt using conversation_msgs (continuation flow)."""
        conversation = [
            HumanMessage(content="Hello"),
            AIMessage(content="Hi there"),
        ]

        with patch("zerg.connectors.status_builder.build_fiche_context_parts", side_effect=_mock_fiche_context_parts):
            context = build_prompt(
                db_session,
                runtime_view,
                test_fiche,
                conversation_msgs=conversation,
            )

        assert isinstance(context, PromptContext)
        assert len(context.conversation_history) >= 2

    def test_builds_prompt_with_tool_messages(self, db_session, runtime_view, test_fiche):
        """Should include tool messages in prompt."""
        conversation = [
            HumanMessage(content="Hello"),
            AIMessage(
                content="I'll help",
                tool_calls=[{"id": "tc-build", "name": "spawn_commis", "args": {}}],
            ),
        ]
        tool_msgs = [
            ToolMessage(content="Commis result", tool_call_id="tc-build", name="spawn_commis"),
        ]

        with patch("zerg.connectors.status_builder.build_fiche_context_parts", side_effect=_mock_fiche_context_parts):
            context = build_prompt(
                db_session,
                runtime_view,
                test_fiche,
                conversation_msgs=conversation,
                tool_messages=tool_msgs,
            )

        # Tool messages should be tracked
        assert len(context.tool_messages) == 1

    def test_raises_without_thread_or_conversation(self, db_session, runtime_view, test_fiche):
        """Should raise ValueError when neither thread_id nor conversation_msgs provided."""
        with pytest.raises(ValueError, match="Must provide either thread_id or conversation_msgs"):
            build_prompt(db_session, runtime_view, test_fiche)

    def test_raises_when_tool_messages_without_conversation(self, db_session, runtime_view, test_fiche, test_thread):
        """Should raise ValueError when tool_messages provided with thread_id but no conversation_msgs."""
        tool_msgs = [
            ToolMessage(content="Result", tool_call_id="tc-test", name="spawn_commis"),
        ]

        with pytest.raises(ValueError, match="tool_messages requires conversation_msgs"):
            build_prompt(
                db_session,
                runtime_view,
                test_fiche,
                thread_id=test_thread.id,
                tool_messages=tool_msgs,
            )


# ---------------------------------------------------------------------------
# PromptContext structure tests
# ---------------------------------------------------------------------------


class TestPromptContextStructure:
    def test_dynamic_context_block_frozen(self):
        """DynamicContextBlock should be immutable."""
        block = DynamicContextBlock(tag="TEST", content="test content")
        with pytest.raises(AttributeError):
            block.tag = "MODIFIED"

    def test_context_to_messages_separate_dynamic_blocks(self):
        """Each dynamic block should become a separate SystemMessage."""
        context = PromptContext(
            system_prompt="You are helpful",
            conversation_history=[
                HumanMessage(content="Hello"),
                AIMessage(content="Hi"),
            ],
            dynamic_context=[
                DynamicContextBlock(tag="CONNECTOR_STATUS", content="[INTERNAL CONTEXT]\n<connector_status>ok</connector_status>"),
                DynamicContextBlock(tag="CURRENT_TIME", content="[INTERNAL CONTEXT]\n<current_time>2026-01-01T00:00Z</current_time>"),
            ],
        )

        messages = context_to_messages(context)

        # system + 2 conv + 2 dynamic = 5
        assert len(messages) == 5
        assert isinstance(messages[0], SystemMessage)
        assert messages[0].content == "You are helpful"
        assert isinstance(messages[1], HumanMessage)
        assert isinstance(messages[2], AIMessage)
        # Connector status as separate SystemMessage
        assert isinstance(messages[3], SystemMessage)
        assert "<connector_status>" in messages[3].content
        # Time as separate SystemMessage
        assert isinstance(messages[4], SystemMessage)
        assert "<current_time>" in messages[4].content

    def test_context_to_messages_three_dynamic_blocks(self):
        """With memory, should emit 3 separate dynamic SystemMessages."""
        context = PromptContext(
            system_prompt="You are helpful",
            conversation_history=[HumanMessage(content="Hello")],
            dynamic_context=[
                DynamicContextBlock(tag="CONNECTOR_STATUS", content="[INTERNAL CONTEXT]\n<connector_status>ok</connector_status>"),
                DynamicContextBlock(tag="MEMORY", content="[MEMORY CONTEXT]\nMemory Files:\n- notes.txt: relevant info"),
                DynamicContextBlock(tag="CURRENT_TIME", content="[INTERNAL CONTEXT]\n<current_time>2026-01-01T00:00Z</current_time>"),
            ],
        )

        messages = context_to_messages(context)

        # system + 1 conv + 3 dynamic = 5
        assert len(messages) == 5
        assert "<connector_status>" in messages[2].content
        assert "[MEMORY CONTEXT]" in messages[3].content
        assert "<current_time>" in messages[4].content

    def test_context_to_messages_empty_dynamic(self):
        """Should handle empty dynamic context."""
        context = PromptContext(
            system_prompt="You are helpful",
            conversation_history=[HumanMessage(content="Hello")],
            dynamic_context=[],
        )

        messages = context_to_messages(context)

        assert len(messages) == 2  # system + conv only
        assert isinstance(messages[0], SystemMessage)
        assert isinstance(messages[1], HumanMessage)


# ---------------------------------------------------------------------------
# Integration tests (builder consistency)
# ---------------------------------------------------------------------------


class TestBuilderConsistency:
    def test_run_thread_flow_consistency(self, db_session, runtime_view, test_fiche, test_thread):
        """build_prompt with thread_id should match MessageArrayBuilder output."""
        from zerg.managers.message_array_builder import MessageArrayBuilder
        from zerg.services.thread_service import ThreadService

        ThreadService.save_new_messages(
            db_session,
            thread_id=test_thread.id,
            messages=[HumanMessage(content="Test message")],
            processed=True,
        )

        with patch("zerg.connectors.status_builder.build_fiche_context_parts", side_effect=_mock_fiche_context_parts):
            # New unified way
            context = build_prompt(
                db_session,
                runtime_view,
                test_fiche,
                thread_id=test_thread.id,
            )

            # Old way (MessageArrayBuilder directly)
            builder = MessageArrayBuilder(db_session, runtime_view)
            builder.with_system_prompt(test_fiche)
            builder.with_conversation(test_thread.id)
            builder.with_dynamic_context()
            old_result = builder.build()

        # Message counts should match
        assert context.message_count_with_context == old_result.message_count_with_context

    def test_continuation_flow_consistency(self, db_session, runtime_view, test_fiche):
        """build_prompt with conversation_msgs should match MessageArrayBuilder output."""
        from zerg.managers.message_array_builder import MessageArrayBuilder

        conversation = [
            HumanMessage(content="Hello"),
            AIMessage(content="Hi"),
        ]

        with patch("zerg.connectors.status_builder.build_fiche_context_parts", side_effect=_mock_fiche_context_parts):
            # New unified way
            context = build_prompt(
                db_session,
                runtime_view,
                test_fiche,
                conversation_msgs=conversation,
            )

            # Old way (MessageArrayBuilder directly)
            builder = MessageArrayBuilder(db_session, runtime_view)
            builder.with_system_prompt(test_fiche)
            builder.with_conversation_messages(conversation, filter_system=True)
            builder.with_dynamic_context(conversation_msgs=conversation)
            old_result = builder.build()

        # Message counts should match
        assert context.message_count_with_context == old_result.message_count_with_context
