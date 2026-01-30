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
from zerg.managers.message_array_builder import MessageArrayBuilder, MessageArrayResult


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
    def test_system_prompt_includes_instructions(self, db_session, runtime_view, test_fiche, test_thread):
        builder = MessageArrayBuilder(db_session, runtime_view)
        result = builder.with_system_prompt(test_fiche).with_conversation(test_thread.id).build()

        assert len(result.messages) == 1
        assert isinstance(result.messages[0], SystemMessage)
        assert "You are a helpful assistant." in result.messages[0].content

    def test_system_prompt_includes_protocols(self, db_session, runtime_view, test_fiche, test_thread):
        builder = MessageArrayBuilder(db_session, runtime_view)
        result = builder.with_system_prompt(test_fiche).with_conversation(test_thread.id).build()

        # Protocols should be prepended
        content = result.messages[0].content
        assert "<connector_protocol>" in content

    def test_system_prompt_requires_instructions(self, db_session, runtime_view, test_fiche):
        # Create fiche without instructions
        test_fiche.system_instructions = None
        with pytest.raises(RuntimeError, match="has no system_instructions"):
            builder = MessageArrayBuilder(db_session, runtime_view)
            builder.with_system_prompt(test_fiche)

    def test_system_prompt_with_skills_disabled(self, db_session, runtime_view, test_fiche, test_thread):
        builder = MessageArrayBuilder(db_session, runtime_view)
        result = builder.with_system_prompt(test_fiche, include_skills=False).with_conversation(test_thread.id).build()

        # Should still work, just no skills integration
        assert result.skill_integration is None
        assert len(result.messages) == 1


# ---------------------------------------------------------------------------
# Conversation loading tests
# ---------------------------------------------------------------------------


class TestConversationLoading:
    def test_loads_empty_conversation(self, db_session, runtime_view, test_fiche, test_thread):
        builder = MessageArrayBuilder(db_session, runtime_view)
        result = builder.with_system_prompt(test_fiche).with_conversation(test_thread.id).build()

        # System message only
        assert len(result.messages) == 1

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
        result = builder.with_system_prompt(test_fiche).with_conversation(test_thread.id).build()

        # System + 2 conversation messages
        assert len(result.messages) == 3
        assert isinstance(result.messages[1], HumanMessage)
        assert isinstance(result.messages[2], AIMessage)

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
        result = builder.with_system_prompt(test_fiche).with_conversation(test_thread.id, filter_system=True).build()

        # Only fresh system + human (stale system filtered)
        assert len(result.messages) == 2
        assert isinstance(result.messages[0], SystemMessage)
        assert isinstance(result.messages[1], HumanMessage)

    def test_with_conversation_messages_filters_system(self, db_session, runtime_view, test_fiche):
        builder = MessageArrayBuilder(db_session, runtime_view)
        conversation = [
            SystemMessage(content="Stale system"),
            HumanMessage(content="Hello"),
            AIMessage(content="Hi"),
        ]

        result = (
            builder.with_system_prompt(test_fiche)
            .with_conversation_messages(conversation, filter_system=True)
            .build()
        )

        # System prompt + human + AI (stale system filtered)
        assert len(result.messages) == 3
        assert isinstance(result.messages[0], SystemMessage)
        assert isinstance(result.messages[1], HumanMessage)
        assert isinstance(result.messages[2], AIMessage)


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
        result = builder.build()

        # System + 2 tool messages
        assert len(result.messages) == 3
        assert isinstance(result.messages[1], ToolMessage)
        assert isinstance(result.messages[2], ToolMessage)

    def test_empty_tool_messages_is_noop(self, db_session, runtime_view, test_fiche, test_thread):
        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)
        builder.with_conversation(test_thread.id)
        builder.with_tool_messages([])
        with patch("zerg.connectors.status_builder.build_fiche_context") as mock_ctx:
            mock_ctx.return_value = "{}"
            builder.with_dynamic_context()
        result = builder.build()

        # System + dynamic context (no tool messages)
        assert len(result.messages) == 2
        assert isinstance(result.messages[-1], SystemMessage)

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
        result = builder.build()

        # System + Human + Tool
        assert len(result.messages) == 3
        assert isinstance(result.messages[0], SystemMessage)
        assert isinstance(result.messages[1], HumanMessage)
        assert isinstance(result.messages[2], ToolMessage)


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

        result = builder.build()

        # System + dynamic context
        assert len(result.messages) == 2
        last_msg = result.messages[-1]
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

        result = builder.build()

        # System -> Human -> AI -> DynamicContext (at end)
        assert len(result.messages) == 4
        assert isinstance(result.messages[-1], SystemMessage)
        assert "[INTERNAL CONTEXT" in result.messages[-1].content

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
        assert isinstance(result.messages[0], SystemMessage)
        assert isinstance(result.messages[1], ToolMessage)
        assert isinstance(result.messages[2], SystemMessage)


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
    def test_memory_context_uses_unprocessed_rows(self, db_session, runtime_view, test_fiche, test_thread):
        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)
        builder.with_conversation(test_thread.id)

        mock_row = MagicMock()
        mock_row.role = "user"
        mock_row.internal = False
        mock_row.content = "What is the weather?"

        long_snippet = "x" * 300

        with (
            patch("zerg.connectors.status_builder.build_fiche_context", return_value="{}"),
            patch(
                "zerg.services.memory_search.search_memory_files",
                return_value=[{"path": "memory.txt", "snippets": [long_snippet]}],
            ) as mock_search,
            patch("zerg.crud.knowledge_crud.search_knowledge_documents", return_value=[]),
            patch("zerg.services.memory_embeddings.embeddings_enabled", return_value=False),
        ):
            builder.with_dynamic_context(unprocessed_rows=[mock_row])
            result = builder.build()

        assert mock_search.call_args.kwargs["query"] == "What is the weather?"
        content = result.messages[-1].content
        assert "[MEMORY CONTEXT]" in content
        line = next(line for line in content.splitlines() if line.startswith("- memory.txt:"))
        snippet = line.split(":", 1)[1].strip()
        assert snippet.endswith("...")
        assert len(snippet) == 223  # 220 + "..."

    def test_memory_context_uses_conversation_for_query(self, db_session, runtime_view, test_fiche, test_thread):
        from zerg.services.thread_service import ThreadService

        ThreadService.save_new_messages(
            db_session,
            thread_id=test_thread.id,
            messages=[HumanMessage(content="Tell me about Python")],
            processed=True,
        )
        conversation_msgs = ThreadService.get_thread_messages_as_langchain(db_session, test_thread.id)

        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)
        builder.with_conversation(test_thread.id)

        with (
            patch("zerg.connectors.status_builder.build_fiche_context", return_value="{}"),
            patch("zerg.services.memory_search.search_memory_files", return_value=[{"path": "memory.txt", "snippets": ["Note"]}]) as mock_search,
            patch("zerg.crud.knowledge_crud.search_knowledge_documents", return_value=[]),
            patch("zerg.services.memory_embeddings.embeddings_enabled", return_value=False),
        ):
            builder.with_dynamic_context(conversation_msgs=conversation_msgs)
            result = builder.build()

        assert mock_search.call_args.kwargs["query"] == "Tell me about Python"
        assert "[MEMORY CONTEXT]" in result.messages[-1].content

    def test_memory_context_skips_internal_messages(self, db_session, runtime_view, test_fiche, test_thread):
        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)
        builder.with_conversation(test_thread.id)

        mock_internal = MagicMock()
        mock_internal.role = "user"
        mock_internal.internal = True
        mock_internal.content = "Internal message"

        mock_real = MagicMock()
        mock_real.role = "user"
        mock_real.internal = False
        mock_real.content = "Real message"

        with (
            patch("zerg.connectors.status_builder.build_fiche_context", return_value="{}"),
            patch("zerg.services.memory_search.search_memory_files", return_value=[{"path": "memory.txt", "snippets": ["Note"]}]) as mock_search,
            patch("zerg.crud.knowledge_crud.search_knowledge_documents", return_value=[]),
            patch("zerg.services.memory_embeddings.embeddings_enabled", return_value=False),
        ):
            builder.with_dynamic_context(unprocessed_rows=[mock_real, mock_internal])
            result = builder.build()

        assert mock_search.call_args.kwargs["query"] == "Real message"
        assert "[MEMORY CONTEXT]" in result.messages[-1].content

    def test_memory_context_fallback_to_internal_state(self, db_session, runtime_view, test_fiche):
        builder = MessageArrayBuilder(db_session, runtime_view)
        builder.with_system_prompt(test_fiche)

        # Add a HumanMessage directly to the builder's state
        builder.with_conversation_messages([HumanMessage(content="My internal query")])

        with (
            patch("zerg.connectors.status_builder.build_fiche_context", return_value="{}"),
            patch("zerg.services.memory_search.search_memory_files", return_value=[{"path": "m.txt", "snippets": ["S"]}]) as mock_search,
            patch("zerg.crud.knowledge_crud.search_knowledge_documents", return_value=[]),
            patch("zerg.services.memory_embeddings.embeddings_enabled", return_value=False),
        ):
            # Call without arguments - should fallback to internal state
            builder.with_dynamic_context()
            builder.build()

        assert mock_search.called
        assert mock_search.call_args.kwargs["query"] == "My internal query"


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
