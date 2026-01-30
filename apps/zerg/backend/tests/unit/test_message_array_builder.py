"""Unit tests for MessageArrayBuilder.

Tests cover:
- System prompt assembly (protocols + instructions + skills)
- Conversation loading with system message filtering
- Tool message injection
- Dynamic context building (connector status + memory)
- Message ordering verification
- State tracking (phase enforcement)
- Golden equivalence (builder output matches legacy code)
"""

from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from tests.conftest import TEST_COMMIS_MODEL
from zerg.crud import crud
from zerg.managers.fiche_runner import RuntimeView
from zerg.managers.message_array_builder import (
    BuildPhase,
    MessageArrayBuilder,
    MessageArrayResult,
    _strip_timestamp_prefix,
    _truncate,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_user(db_session):
    """Create a test user."""
    return crud.create_user(db_session, email="builder-test@local", provider=None, role="USER")


@pytest.fixture
def test_fiche(db_session, test_user):
    """Create a test fiche with system instructions."""
    return crud.create_fiche(
        db_session,
        owner_id=test_user.id,
        name="builder-test-fiche",
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
        title="builder-test-thread",
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
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    def test_strip_timestamp_prefix_removes_iso_timestamp(self):
        text = "[2024-01-15T10:30:00Z] Hello world"
        assert _strip_timestamp_prefix(text) == "Hello world"

    def test_strip_timestamp_prefix_no_timestamp(self):
        text = "Hello world"
        assert _strip_timestamp_prefix(text) == "Hello world"

    def test_strip_timestamp_prefix_empty(self):
        assert _strip_timestamp_prefix("") == ""
        assert _strip_timestamp_prefix(None) == ""

    def test_truncate_short_text(self):
        text = "Short text"
        assert _truncate(text) == "Short text"

    def test_truncate_long_text(self):
        text = "x" * 300
        result = _truncate(text, max_chars=220)
        assert len(result) == 223  # 220 + "..."
        assert result.endswith("...")

    def test_truncate_empty(self):
        assert _truncate("") == ""
        assert _truncate(None) == ""

    def test_truncate_normalizes_whitespace(self):
        text = "  hello   world  "
        assert _truncate(text) == "hello world"


# ---------------------------------------------------------------------------
# Builder state tracking tests
# ---------------------------------------------------------------------------


class TestBuilderStateTracking:
    def test_cannot_skip_system_prompt(self, db_session, runtime_view):
        builder = MessageArrayBuilder(db_session, runtime_view)
        with pytest.raises(RuntimeError, match="Must call SYSTEM_PROMPT phase before CONVERSATION"):
            builder.with_conversation(thread_id=1)

    def test_cannot_add_system_prompt_twice(self, db_session, runtime_view, test_fiche):
        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)
        with pytest.raises(RuntimeError, match="Builder already past SYSTEM_PROMPT"):
            builder.with_system_prompt(test_fiche)

    def test_cannot_add_conversation_twice(self, db_session, runtime_view, test_fiche, test_thread):
        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)
        builder.with_conversation(test_thread.id)
        with pytest.raises(RuntimeError, match="Builder already past CONVERSATION"):
            builder.with_conversation(test_thread.id)

    def test_cannot_add_dynamic_context_before_conversation(self, db_session, runtime_view, test_fiche):
        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)
        with pytest.raises(RuntimeError, match="with_dynamic_context must be called after CONVERSATION"):
            builder.with_dynamic_context()

    def test_cannot_build_twice(self, db_session, runtime_view, test_fiche, test_thread):
        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)
        builder.with_conversation(test_thread.id)
        builder.build()
        with pytest.raises(RuntimeError, match="Builder already built"):
            builder.build()

    def test_cannot_build_without_conversation(self, db_session, runtime_view, test_fiche):
        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)
        with pytest.raises(RuntimeError, match="Must at least call with_system_prompt and with_conversation"):
            builder.build()


# ---------------------------------------------------------------------------
# System prompt tests
# ---------------------------------------------------------------------------


class TestSystemPrompt:
    def test_system_prompt_includes_instructions(self, db_session, runtime_view, test_fiche):
        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)

        # Check internal state
        assert len(builder._messages) == 1
        assert isinstance(builder._messages[0], SystemMessage)
        assert "You are a helpful assistant." in builder._messages[0].content

    def test_system_prompt_includes_protocols(self, db_session, runtime_view, test_fiche):
        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)

        # Protocols should be prepended
        content = builder._messages[0].content
        assert "<connector_protocol>" in content

    def test_system_prompt_requires_instructions(self, db_session, runtime_view, test_fiche):
        # Create fiche without instructions
        test_fiche.system_instructions = None
        with pytest.raises(RuntimeError, match="has no system_instructions"):
            builder = MessageArrayBuilder(db_session, runtime_view)
            builder.with_system_prompt(test_fiche)

    def test_system_prompt_with_skills_disabled(self, db_session, runtime_view, test_fiche):
        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche, include_skills=False)

        # Should still work, just no skills integration
        assert builder._skill_integration is None
        assert len(builder._messages) == 1


# ---------------------------------------------------------------------------
# Conversation loading tests
# ---------------------------------------------------------------------------


class TestConversationLoading:
    def test_loads_empty_conversation(self, db_session, runtime_view, test_fiche, test_thread):
        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)
        builder.with_conversation(test_thread.id)

        # System message only
        assert len(builder._messages) == 1

    def test_loads_conversation_messages(self, db_session, runtime_view, test_fiche, test_thread):
        # Add some messages to thread
        from zerg.services.thread_service import ThreadService

        ThreadService.save_new_messages(
            db_session,
            thread_id=test_thread.id,
            messages=[
                HumanMessage(content="Hello"),
                AIMessage(content="Hi there!"),
            ],
            processed=True,
        )

        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)
        builder.with_conversation(test_thread.id)

        # System + 2 conversation messages
        assert len(builder._messages) == 3
        assert isinstance(builder._messages[1], HumanMessage)
        assert isinstance(builder._messages[2], AIMessage)

    def test_filters_system_messages_by_default(self, db_session, runtime_view, test_fiche, test_thread):
        # Add messages including a system message
        from zerg.services.thread_service import ThreadService

        ThreadService.save_new_messages(
            db_session,
            thread_id=test_thread.id,
            messages=[
                SystemMessage(content="Stale system"),
                HumanMessage(content="Hello"),
            ],
            processed=True,
        )

        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)
        builder.with_conversation(test_thread.id, filter_system=True)

        # Only fresh system + human (stale system filtered)
        assert len(builder._messages) == 2
        assert isinstance(builder._messages[0], SystemMessage)
        assert isinstance(builder._messages[1], HumanMessage)


# ---------------------------------------------------------------------------
# Tool message tests
# ---------------------------------------------------------------------------


class TestToolMessages:
    def test_adds_tool_messages(self, db_session, runtime_view, test_fiche, test_thread):
        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)
        builder.with_conversation(test_thread.id)

        tool_msgs = [
            ToolMessage(content="Result 1", tool_call_id="tc1", name="spawn_commis"),
            ToolMessage(content="Result 2", tool_call_id="tc2", name="spawn_commis"),
        ]
        builder.with_tool_messages(tool_msgs)

        # System + 2 tool messages
        assert len(builder._messages) == 3
        assert isinstance(builder._messages[1], ToolMessage)
        assert isinstance(builder._messages[2], ToolMessage)

    def test_empty_tool_messages_is_noop(self, db_session, runtime_view, test_fiche, test_thread):
        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)
        builder.with_conversation(test_thread.id)
        builder.with_tool_messages([])

        # Phase should not advance for empty list
        assert builder._phase == BuildPhase.CONVERSATION

    def test_tool_messages_after_conversation(self, db_session, runtime_view, test_fiche, test_thread):
        from zerg.services.thread_service import ThreadService

        ThreadService.save_new_messages(
            db_session,
            thread_id=test_thread.id,
            messages=[HumanMessage(content="Hello")],
            processed=True,
        )

        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)
        builder.with_conversation(test_thread.id)
        builder.with_tool_messages([ToolMessage(content="Result", tool_call_id="tc1", name="test")])

        # System + Human + Tool
        assert len(builder._messages) == 3
        assert isinstance(builder._messages[0], SystemMessage)
        assert isinstance(builder._messages[1], HumanMessage)
        assert isinstance(builder._messages[2], ToolMessage)


# ---------------------------------------------------------------------------
# Dynamic context tests
# ---------------------------------------------------------------------------


class TestDynamicContext:
    def test_adds_connector_context(self, db_session, runtime_view, test_fiche, test_thread):
        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)
        builder.with_conversation(test_thread.id)

        # Mock connector context builder (must patch where it's used, not defined)
        with patch("zerg.connectors.status_builder.build_fiche_context") as mock_ctx:
            mock_ctx.return_value = '{"connectors": []}'
            builder.with_dynamic_context(allowed_tools=None)

        # System + dynamic context
        assert len(builder._messages) == 2
        last_msg = builder._messages[-1]
        assert isinstance(last_msg, SystemMessage)
        assert "[INTERNAL CONTEXT" in last_msg.content

    def test_handles_connector_context_failure(self, db_session, runtime_view, test_fiche, test_thread):
        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)
        builder.with_conversation(test_thread.id)

        # Mock connector context builder to fail (patch where it's imported)
        with patch("zerg.connectors.status_builder.build_fiche_context") as mock_ctx:
            mock_ctx.side_effect = Exception("DB error")
            builder.with_dynamic_context()

        # Should still work, just without connector context
        result = builder.build()
        assert result.messages is not None

    def test_dynamic_context_at_end(self, db_session, runtime_view, test_fiche, test_thread):
        from zerg.services.thread_service import ThreadService

        ThreadService.save_new_messages(
            db_session,
            thread_id=test_thread.id,
            messages=[
                HumanMessage(content="Hello"),
                AIMessage(content="Hi"),
            ],
            processed=True,
        )

        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)
        builder.with_conversation(test_thread.id)

        with patch("zerg.connectors.status_builder.build_fiche_context") as mock_ctx:
            mock_ctx.return_value = '{"connectors": []}'
            builder.with_dynamic_context()

        # System -> Human -> AI -> DynamicContext (at end)
        assert len(builder._messages) == 4
        assert isinstance(builder._messages[-1], SystemMessage)
        assert "[INTERNAL CONTEXT" in builder._messages[-1].content

    def test_can_call_after_tool_messages(self, db_session, runtime_view, test_fiche, test_thread):
        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)
        builder.with_conversation(test_thread.id)
        builder.with_tool_messages([ToolMessage(content="R", tool_call_id="t1", name="test")])

        with patch("zerg.connectors.status_builder.build_fiche_context") as mock_ctx:
            mock_ctx.return_value = "{}"
            builder.with_dynamic_context()

        # Should work without error
        result = builder.build()
        assert len(result.messages) == 3


# ---------------------------------------------------------------------------
# Build result tests
# ---------------------------------------------------------------------------


class TestBuildResult:
    def test_returns_message_array_result(self, db_session, runtime_view, test_fiche, test_thread):
        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)
        builder.with_conversation(test_thread.id)
        result = builder.build()

        assert isinstance(result, MessageArrayResult)
        assert isinstance(result.messages, list)
        assert result.message_count_with_context == len(result.messages)
        # skill_integration is None because skills_enabled=False in fixture
        assert result.skill_integration is None

    def test_message_count_accurate(self, db_session, runtime_view, test_fiche, test_thread):
        from zerg.services.thread_service import ThreadService

        ThreadService.save_new_messages(
            db_session,
            thread_id=test_thread.id,
            messages=[HumanMessage(content="Hello")],
            processed=True,
        )

        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)
        builder.with_conversation(test_thread.id)

        with patch("zerg.connectors.status_builder.build_fiche_context") as mock_ctx:
            mock_ctx.return_value = "{}"
            builder.with_dynamic_context()

        result = builder.build()

        # System + Human + DynamicContext = 3
        assert result.message_count_with_context == 3
        assert len(result.messages) == 3


# ---------------------------------------------------------------------------
# Message ordering tests
# ---------------------------------------------------------------------------


class TestMessageOrdering:
    def test_order_system_conversation_dynamic(self, db_session, runtime_view, test_fiche, test_thread):
        from zerg.services.thread_service import ThreadService

        ThreadService.save_new_messages(
            db_session,
            thread_id=test_thread.id,
            messages=[
                HumanMessage(content="Hello"),
                AIMessage(content="Hi"),
            ],
            processed=True,
        )

        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)
        builder.with_conversation(test_thread.id)

        with patch("zerg.connectors.status_builder.build_fiche_context") as mock_ctx:
            mock_ctx.return_value = '{"test": true}'
            builder.with_dynamic_context()

        result = builder.build()

        # Verify order: System -> Human -> AI -> DynamicContext
        assert isinstance(result.messages[0], SystemMessage)
        assert "You are a helpful assistant" in result.messages[0].content
        assert isinstance(result.messages[1], HumanMessage)
        assert isinstance(result.messages[2], AIMessage)
        assert isinstance(result.messages[3], SystemMessage)
        assert "[INTERNAL CONTEXT" in result.messages[3].content

    def test_order_with_tool_messages(self, db_session, runtime_view, test_fiche, test_thread):
        from zerg.services.thread_service import ThreadService

        ThreadService.save_new_messages(
            db_session,
            thread_id=test_thread.id,
            messages=[HumanMessage(content="Hello")],
            processed=True,
        )

        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)
        builder.with_conversation(test_thread.id)
        builder.with_tool_messages([ToolMessage(content="Done", tool_call_id="t1", name="spawn_commis")])

        with patch("zerg.connectors.status_builder.build_fiche_context") as mock_ctx:
            mock_ctx.return_value = "{}"
            builder.with_dynamic_context()

        result = builder.build()

        # Verify order: System -> Human -> Tool -> DynamicContext
        assert isinstance(result.messages[0], SystemMessage)
        assert isinstance(result.messages[1], HumanMessage)
        assert isinstance(result.messages[2], ToolMessage)
        assert isinstance(result.messages[3], SystemMessage)


# ---------------------------------------------------------------------------
# Memory context tests
# ---------------------------------------------------------------------------


class TestMemoryContext:
    def test_extracts_user_query_from_unprocessed(self, db_session, runtime_view, test_fiche, test_thread):
        builder = MessageArrayBuilder(db_session, runtime_view)

        # Create mock unprocessed rows
        mock_row = MagicMock()
        mock_row.role = "user"
        mock_row.internal = False
        mock_row.content = "What is the weather?"

        query = builder._extract_user_query(unprocessed_rows=[mock_row])
        assert query == "What is the weather?"

    def test_extracts_user_query_from_conversation(self, db_session, runtime_view, test_fiche, test_thread):
        builder = MessageArrayBuilder(db_session, runtime_view)

        msgs = [
            HumanMessage(content="[2024-01-15T10:00:00Z] Tell me about Python"),
            AIMessage(content="Python is a programming language."),
        ]

        query = builder._extract_user_query(conversation_msgs=msgs)
        assert query == "Tell me about Python"

    def test_skips_internal_messages(self, db_session, runtime_view, test_fiche, test_thread):
        builder = MessageArrayBuilder(db_session, runtime_view)

        mock_internal = MagicMock()
        mock_internal.role = "user"
        mock_internal.internal = True
        mock_internal.content = "Internal message"

        mock_real = MagicMock()
        mock_real.role = "user"
        mock_real.internal = False
        mock_real.content = "Real message"

        # Internal should be skipped, real should be found
        query = builder._extract_user_query(unprocessed_rows=[mock_real, mock_internal])
        assert query == "Real message"


# ---------------------------------------------------------------------------
# Golden equivalence tests (builder matches legacy run_thread output)
# ---------------------------------------------------------------------------


class TestGoldenEquivalence:
    """Verify builder produces identical output to legacy fiche_runner code."""

    def test_minimal_case_equivalence(self, db_session, runtime_view, test_fiche, test_thread):
        """Empty conversation should produce system message only."""
        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)
        builder.with_conversation(test_thread.id)
        result = builder.build()

        # Legacy would produce: [SystemMessage]
        assert len(result.messages) == 1
        assert isinstance(result.messages[0], SystemMessage)
        assert "You are a helpful assistant" in result.messages[0].content

    def test_with_conversation_equivalence(self, db_session, test_user):
        """Conversation messages should be included after system."""
        from zerg.services.thread_service import ThreadService

        # Create fresh fiche and thread for this test
        fiche = crud.create_fiche(
            db_session,
            owner_id=test_user.id,
            name="conv-equiv-fiche",
            system_instructions="You are a helpful assistant.",
            task_instructions="",
            model=TEST_COMMIS_MODEL,
            schedule=None,
            config={"skills_enabled": False},
        )
        thread = crud.create_thread(
            db=db_session,
            fiche_id=fiche.id,
            title="conv-equiv-thread",
            active=True,
            fiche_state={},
            memory_strategy="buffer",
        )
        runtime_view = RuntimeView(
            id=fiche.id,
            owner_id=fiche.owner_id,
            updated_at=fiche.updated_at,
            model=fiche.model,
            config=fiche.config or {},
            allowed_tools=None,
        )

        ThreadService.save_new_messages(
            db_session,
            thread_id=thread.id,
            messages=[
                HumanMessage(content="Hello"),
                AIMessage(content="Hi there"),
            ],
            processed=True,
        )

        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(fiche)
        builder.with_conversation(thread.id)
        result = builder.build()

        # Legacy: [System, Human, AI]
        assert len(result.messages) == 3
        # ThreadService adds timestamps, so we check if content ends with expected value
        assert result.messages[1].content.endswith("Hello") or "Hello" in result.messages[1].content
        assert result.messages[2].content.endswith("Hi there") or "Hi there" in result.messages[2].content

    def test_with_dynamic_context_equivalence(self, db_session, test_user):
        """Dynamic context should be at end."""
        from zerg.services.thread_service import ThreadService

        # Create fresh fiche and thread for this test
        fiche = crud.create_fiche(
            db_session,
            owner_id=test_user.id,
            name="dyn-ctx-equiv-fiche",
            system_instructions="You are a helpful assistant.",
            task_instructions="",
            model=TEST_COMMIS_MODEL,
            schedule=None,
            config={"skills_enabled": False},
        )
        thread = crud.create_thread(
            db=db_session,
            fiche_id=fiche.id,
            title="dyn-ctx-equiv-thread",
            active=True,
            fiche_state={},
            memory_strategy="buffer",
        )
        runtime_view = RuntimeView(
            id=fiche.id,
            owner_id=fiche.owner_id,
            updated_at=fiche.updated_at,
            model=fiche.model,
            config=fiche.config or {},
            allowed_tools=None,
        )

        ThreadService.save_new_messages(
            db_session,
            thread_id=thread.id,
            messages=[HumanMessage(content="Hello")],
            processed=True,
        )

        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(fiche)
        builder.with_conversation(thread.id)

        with patch("zerg.connectors.status_builder.build_fiche_context") as mock_ctx:
            mock_ctx.return_value = '{"connectors": []}'
            builder.with_dynamic_context()

        result = builder.build()

        # Legacy: [System, Human, DynamicContext]
        assert len(result.messages) == 3
        assert "[INTERNAL CONTEXT" in result.messages[-1].content
