"""Tests for Oikos memory tools (save_memory, search_memory, list_memories, forget_memory)."""

import pytest

from zerg.connectors.context import set_credential_resolver
from zerg.connectors.resolver import CredentialResolver
from zerg.services.memory_store import get_memory_store
from zerg.tools.builtin.oikos_memory_tools import forget_memory
from zerg.tools.builtin.oikos_memory_tools import list_memories
from zerg.tools.builtin.oikos_memory_tools import save_memory
from zerg.tools.builtin.oikos_memory_tools import search_memory


def is_error(result) -> bool:
    """Check if result is an error response (dict with ok=False)."""
    return isinstance(result, dict) and result.get("ok") is False


@pytest.fixture
def credential_context(db_session, test_user):
    """Set up credential resolver context for memory tools."""
    resolver = CredentialResolver(fiche_id=1, db=db_session, owner_id=test_user.id)
    token = set_credential_resolver(resolver)
    yield resolver
    set_credential_resolver(None)


class TestSaveMemory:
    """Tests for save_memory tool."""

    def test_save_memory_success(self, credential_context):
        """save_memory should persist content and return confirmation."""
        result = save_memory(
            content="User prefers dark mode",
            type="preference",
        )

        assert "Memory saved" in result
        assert "global" in result
        assert "preference" in result

    def test_save_memory_with_scope(self, credential_context):
        """save_memory with scope='fiche' should work (falls back to global without fiche context)."""
        # Note: Currently fiche scope falls back to global since OikosContext
        # doesn't track fiche_id. This test documents current behavior.
        result = save_memory(
            content="Fiche-scoped note",
            scope="fiche",
        )

        # Falls back to global since no fiche context
        assert "Memory saved" in result
        assert "global" in result

    def test_save_memory_truncates_long_content(self, credential_context):
        """save_memory should truncate long content in the response."""
        long_content = "x" * 200
        result = save_memory(content=long_content)

        assert "Memory saved" in result
        assert "..." in result  # Truncated

    def test_save_memory_empty_content_fails(self, credential_context):
        """save_memory with empty content should fail."""
        result = save_memory(content="")

        assert is_error(result)
        assert result["error_type"] == "validation_error"

    def test_save_memory_requires_context(self):
        """save_memory without user context should fail."""
        result = save_memory(content="No context")

        assert is_error(result)
        assert result["error_type"] == "missing_context"


class TestSearchMemory:
    """Tests for search_memory tool."""

    def test_search_memory_finds_match(self, credential_context):
        """search_memory should find matching memories."""
        save_memory(content="spawn_commis returns SUCCESS instead of WAITING", type="bug")

        result = search_memory(query="spawn_commis")

        assert "Found" in result
        assert "spawn_commis" in result

    def test_search_memory_no_match(self, credential_context):
        """search_memory should report when nothing found."""
        save_memory(content="Something unrelated")

        result = search_memory(query="nonexistent_keyword_xyz")

        assert "No memories found" in result

    def test_search_memory_with_type_filter(self, credential_context):
        """search_memory should filter by type."""
        save_memory(content="Bug about auth", type="bug")
        save_memory(content="Note about auth", type="note")

        bug_result = search_memory(query="auth", type="bug")

        assert "Found 1" in bug_result
        assert "[bug]" in bug_result

    def test_search_memory_shows_date_and_type(self, credential_context):
        """search_memory results should include date and type."""
        save_memory(content="Test memory", type="decision")

        result = search_memory(query="Test memory")

        # Format is: "1. 2026-01-30 [decision]"
        assert "[decision]" in result
        assert "2026" in result or "202" in result  # Year in date

    def test_search_memory_empty_query_fails(self, credential_context):
        """search_memory with empty query should fail."""
        result = search_memory(query="")

        assert is_error(result)
        assert result["error_type"] == "validation_error"

    def test_search_memory_requires_context(self):
        """search_memory without user context should fail."""
        result = search_memory(query="test")

        assert is_error(result)
        assert result["error_type"] == "missing_context"


class TestListMemories:
    """Tests for list_memories tool."""

    def test_list_memories_shows_recent(self, credential_context):
        """list_memories should show recent memories."""
        save_memory(content="First memory")
        save_memory(content="Second memory")
        save_memory(content="Third memory")

        result = list_memories()

        assert "Recent memories" in result
        assert "3 shown" in result
        assert "Third memory" in result

    def test_list_memories_with_type_filter(self, credential_context):
        """list_memories should filter by type."""
        save_memory(content="Bug one", type="bug")
        save_memory(content="Note one", type="note")

        result = list_memories(type="bug")

        assert "type: bug" in result
        assert "Bug one" in result
        assert "Note one" not in result

    def test_list_memories_empty(self, credential_context):
        """list_memories should report when empty."""
        result = list_memories()

        assert "No memories found" in result

    def test_list_memories_respects_limit(self, credential_context):
        """list_memories should respect limit parameter."""
        for i in range(10):
            save_memory(content=f"Memory number {i}")

        result = list_memories(limit=3)

        assert "3 shown" in result

    def test_list_memories_requires_context(self):
        """list_memories without user context should fail."""
        result = list_memories()

        assert is_error(result)
        assert result["error_type"] == "missing_context"


class TestForgetMemory:
    """Tests for forget_memory tool."""

    def test_forget_memory_deletes(self, credential_context, test_user):
        """forget_memory should delete the specified memory."""
        save_memory(content="Delete me")

        # Get the memory ID from the store
        store = get_memory_store()
        memories = store.list(user_id=test_user.id)
        memory_id = memories[0].id

        result = forget_memory(memory_id=memory_id)

        assert "deleted" in result.lower()

        # Verify it's gone
        remaining = store.list(user_id=test_user.id)
        assert len(remaining) == 0

    def test_forget_memory_not_found(self, credential_context):
        """forget_memory with invalid ID should report not found."""
        result = forget_memory(memory_id="00000000-0000-0000-0000-000000000000")

        assert is_error(result)
        assert result["error_type"] == "not_found"

    def test_forget_memory_invalid_uuid(self, credential_context):
        """forget_memory with malformed UUID should fail gracefully."""
        result = forget_memory(memory_id="not-a-valid-uuid")

        assert is_error(result)
        assert result["error_type"] == "validation_error"

    def test_forget_memory_empty_id_fails(self, credential_context):
        """forget_memory with empty ID should fail."""
        result = forget_memory(memory_id="")

        assert is_error(result)
        assert result["error_type"] == "validation_error"

    def test_forget_memory_requires_context(self):
        """forget_memory without user context should fail."""
        result = forget_memory(memory_id="00000000-0000-0000-0000-000000000000")

        assert is_error(result)
        assert result["error_type"] == "missing_context"


class TestMemoryToolsIntegration:
    """Integration tests for memory tools working together."""

    def test_save_search_forget_flow(self, credential_context, test_user):
        """Full workflow: save -> search -> forget."""
        # Save
        save_result = save_memory(
            content="Integration test: spawn_commis parallel bug",
            type="bug",
        )
        assert "Memory saved" in save_result

        # Search
        search_result = search_memory(query="spawn_commis")
        assert "Found" in search_result
        assert "spawn_commis" in search_result

        # Get ID and forget
        store = get_memory_store()
        memories = store.list(user_id=test_user.id)
        memory_id = memories[0].id

        forget_result = forget_memory(memory_id=memory_id)
        assert "deleted" in forget_result.lower()

        # Verify gone
        search_after = search_memory(query="spawn_commis")
        assert "No memories found" in search_after

    def test_multiple_memories_different_types(self, credential_context):
        """Can save and filter multiple memories by type."""
        save_memory(content="Bug: auth fails", type="bug")
        save_memory(content="Decision: use PostgreSQL", type="decision")
        save_memory(content="Preference: vim mode", type="preference")
        save_memory(content="Fact: server is on cube", type="fact")

        bugs = list_memories(type="bug")
        decisions = list_memories(type="decision")
        all_memories = list_memories()

        assert "1 shown" in bugs
        assert "1 shown" in decisions
        assert "4 shown" in all_memories
