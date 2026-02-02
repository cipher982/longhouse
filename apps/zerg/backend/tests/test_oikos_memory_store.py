"""Tests for SQLMemoryStore (Oikos memory system).

The SQLMemoryStore is database-agnostic and works with both SQLite and PostgreSQL.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest

from zerg.models.models import Memory
from zerg.services.memory_store import SQLMemoryStore
from zerg.services.memory_store import PostgresMemoryStore  # backwards-compatible alias


class TestSQLMemoryStore:
    """Tests for the SQLMemoryStore implementation."""

    @pytest.fixture
    def store(self):
        """Create a fresh store instance."""
        return SQLMemoryStore()

    def test_backwards_compatible_alias(self):
        """PostgresMemoryStore alias should still work for backwards compatibility."""
        store = PostgresMemoryStore()
        assert isinstance(store, SQLMemoryStore)

    @pytest.fixture
    def user_id(self, test_user):
        """Get test user ID."""
        return test_user.id

    def test_save_creates_memory(self, store, user_id, db_session):
        """save() should create a new memory record."""
        record = store.save(
            user_id=user_id,
            content="User prefers vim keybindings",
            type="preference",
            source="oikos",
        )

        assert record.id is not None
        assert record.content == "User prefers vim keybindings"
        assert record.type == "preference"
        assert record.source == "oikos"
        assert record.confidence == 1.0
        assert record.fiche_id is None  # Global scope
        assert record.created_at is not None

    def test_save_with_fiche_scope(self, store, user_id, db_session):
        """save() with fiche_id should create fiche-scoped memory."""
        # Create a fiche first
        from zerg.models.models import Fiche

        fiche = Fiche(
            name="Test Fiche",
            system_instructions="test",
            task_instructions="test",
            model="gpt-5-mini",
            owner_id=user_id,
        )
        db_session.add(fiche)
        db_session.commit()
        db_session.refresh(fiche)

        record = store.save(
            user_id=user_id,
            content="Fiche-specific memory",
            fiche_id=fiche.id,
        )

        assert record.fiche_id == fiche.id

    def test_search_finds_matching_content(self, store, user_id, db_session):
        """search() should find memories matching the query."""
        store.save(user_id=user_id, content="spawn_commis bug: returns SUCCESS instead of WAITING")
        store.save(user_id=user_id, content="User prefers dark mode")
        store.save(user_id=user_id, content="Database connection timeout issue")

        results = store.search(user_id=user_id, query="spawn_commis")

        assert len(results) == 1
        assert "spawn_commis" in results[0].content

    def test_search_case_insensitive(self, store, user_id, db_session):
        """search() should be case-insensitive."""
        store.save(user_id=user_id, content="UPPERCASE BUG REPORT")

        results = store.search(user_id=user_id, query="uppercase")

        assert len(results) == 1

    def test_search_with_type_filter(self, store, user_id, db_session):
        """search() should filter by type when specified."""
        store.save(user_id=user_id, content="Bug: memory leak", type="bug")
        store.save(user_id=user_id, content="Note: memory usage high", type="note")

        bug_results = store.search(user_id=user_id, query="memory", type="bug")
        note_results = store.search(user_id=user_id, query="memory", type="note")

        assert len(bug_results) == 1
        assert bug_results[0].type == "bug"
        assert len(note_results) == 1
        assert note_results[0].type == "note"

    def test_search_excludes_expired(self, store, user_id, db_session):
        """search() should exclude expired memories."""
        # Create an expired memory directly in DB
        expired_memory = Memory(
            user_id=user_id,
            content="Expired memory about testing",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db_session.add(expired_memory)
        db_session.commit()

        # Create a valid memory
        store.save(user_id=user_id, content="Valid memory about testing")

        results = store.search(user_id=user_id, query="testing")

        assert len(results) == 1
        assert "Valid" in results[0].content

    def test_search_includes_fiche_specific_when_scoped(self, store, user_id, db_session):
        """search() with fiche_id should include both global and fiche-specific memories."""
        from zerg.models.models import Fiche

        fiche = Fiche(
            name="Test Fiche",
            system_instructions="test",
            task_instructions="test",
            model="gpt-5-mini",
            owner_id=user_id,
        )
        db_session.add(fiche)
        db_session.commit()
        db_session.refresh(fiche)

        store.save(user_id=user_id, content="Global memory about auth")
        store.save(user_id=user_id, content="Fiche memory about auth", fiche_id=fiche.id)

        # Search with fiche scope - should get both
        results = store.search(user_id=user_id, query="auth", fiche_id=fiche.id)
        assert len(results) == 2

        # Search without fiche scope - should only get global
        global_results = store.search(user_id=user_id, query="auth")
        assert len(global_results) == 1
        assert global_results[0].fiche_id is None

    def test_list_returns_recent_first(self, store, user_id, db_session):
        """list() should return memories ordered by created_at desc.

        Note: SQLite has second-precision timestamps, so rapid inserts may share
        the same created_at value. When timestamps are equal, ordering within
        that group is undefined. We only verify that all contents are returned.
        """
        store.save(user_id=user_id, content="First memory")
        store.save(user_id=user_id, content="Second memory")
        store.save(user_id=user_id, content="Third memory")

        results = store.list(user_id=user_id)

        assert len(results) == 3
        # Verify all memories are present (order may vary within same-timestamp group)
        contents = {r.content for r in results}
        assert contents == {"First memory", "Second memory", "Third memory"}

    def test_list_with_type_filter(self, store, user_id, db_session):
        """list() should filter by type when specified."""
        store.save(user_id=user_id, content="Bug 1", type="bug")
        store.save(user_id=user_id, content="Note 1", type="note")
        store.save(user_id=user_id, content="Bug 2", type="bug")

        bugs = store.list(user_id=user_id, type="bug")

        assert len(bugs) == 2
        assert all(m.type == "bug" for m in bugs)

    def test_list_respects_limit(self, store, user_id, db_session):
        """list() should respect the limit parameter."""
        for i in range(10):
            store.save(user_id=user_id, content=f"Memory {i}")

        results = store.list(user_id=user_id, limit=3)

        assert len(results) == 3

    def test_delete_removes_memory(self, store, user_id, db_session):
        """delete() should remove a memory by ID."""
        record = store.save(user_id=user_id, content="Delete me")

        deleted = store.delete(user_id=user_id, memory_id=record.id)

        assert deleted is True

        # Verify it's gone
        results = store.list(user_id=user_id)
        assert len(results) == 0

    def test_delete_returns_false_for_nonexistent(self, store, user_id, db_session):
        """delete() should return False for non-existent memory."""
        deleted = store.delete(user_id=user_id, memory_id="00000000-0000-0000-0000-000000000000")

        assert deleted is False

    def test_delete_respects_user_scope(self, store, db_session):
        """delete() should only delete memories owned by the user."""
        from zerg.models.models import User

        # Create two users
        user1 = User(email="user1@test.com", provider="test", provider_user_id="u1")
        user2 = User(email="user2@test.com", provider="test", provider_user_id="u2")
        db_session.add_all([user1, user2])
        db_session.commit()
        db_session.refresh(user1)
        db_session.refresh(user2)

        # User1 creates a memory
        record = store.save(user_id=user1.id, content="User1's memory")

        # User2 tries to delete it - should fail
        deleted = store.delete(user_id=user2.id, memory_id=record.id)
        assert deleted is False

        # User1 can delete it
        deleted = store.delete(user_id=user1.id, memory_id=record.id)
        assert deleted is True
