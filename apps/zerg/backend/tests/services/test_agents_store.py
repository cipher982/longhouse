"""Tests for agents store service, particularly JSONL export."""

import json
from datetime import datetime
from datetime import timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AGENTS_SCHEMA
from zerg.models.agents import agents_metadata
from zerg.services.agents_store import AgentsStore


@pytest.fixture(scope="module")
def agents_schema_setup():
    """Create agents schema and tables once per test module."""
    from tests.conftest import test_engine

    # SQLite doesn't have schemas, so AGENTS_SCHEMA is None
    # Just create the tables (no schema creation needed)
    agents_metadata.create_all(bind=test_engine)
    yield

    # Optional cleanup (tables persist for speed)


@pytest.fixture
def agents_db_session(db_session, agents_schema_setup):
    """Provide a db session with agents schema tables ready."""
    from tests.conftest import test_engine

    # Delete data from agents tables before each test
    # (SQLite doesn't support TRUNCATE, use DELETE instead)
    with test_engine.connect() as conn:
        for table in reversed(agents_metadata.sorted_tables):
            try:
                conn.execute(text(f"DELETE FROM {table.name}"))
            except Exception:
                pass
        conn.commit()

    yield db_session


class TestExportSessionJsonl:
    """Tests for export_session_jsonl method."""

    def test_export_dedupes_by_offset(self, agents_db_session):
        """Multi-part assistant messages export as single line, not duplicated."""
        db = agents_db_session
        store = AgentsStore(db)

        # Create a session
        session_id = uuid4()
        session = AgentSession(
            id=session_id,
            provider="claude",
            environment="test",
            project="test",
            started_at=datetime.now(timezone.utc),
            user_messages=1,
            assistant_messages=2,
            tool_calls=1,
        )
        db.add(session)

        # Original JSONL line with text + tool_use
        original_line = json.dumps({
            "type": "assistant",
            "uuid": "msg-mixed",
            "timestamp": "2026-01-28T10:04:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me read that file."},
                    {
                        "type": "tool_use",
                        "id": "tool-xyz",
                        "name": "Read",
                        "input": {"file_path": "/test.py"},
                    },
                ],
            },
        })

        source_path = "/test/session.jsonl"
        source_offset = 100  # Both events from same line

        # Add two events from the same JSONL line (text + tool_use)
        event1 = AgentEvent(
            session_id=session_id,
            role="assistant",
            content_text="Let me read that file.",
            timestamp=datetime(2026, 1, 28, 10, 4, 0, tzinfo=timezone.utc),
            source_path=source_path,
            source_offset=source_offset,
            raw_json=original_line,
        )
        event2 = AgentEvent(
            session_id=session_id,
            role="assistant",
            tool_name="Read",
            tool_input_json={"file_path": "/test.py"},
            timestamp=datetime(2026, 1, 28, 10, 4, 0, tzinfo=timezone.utc),
            source_path=source_path,
            source_offset=source_offset,  # Same offset as event1
            raw_json=original_line,  # Same raw_json as event1
        )
        db.add(event1)
        db.add(event2)
        db.commit()

        # Export
        result = store.export_session_jsonl(session_id)
        assert result is not None
        content, returned_session = result

        # Should be exactly 1 line (deduped by offset)
        lines = content.decode("utf-8").strip().split("\n")
        assert len(lines) == 1
        assert lines[0] == original_line

    def test_export_preserves_file_order(self, agents_db_session):
        """Export orders by source_offset, not timestamp."""
        db = agents_db_session
        store = AgentsStore(db)

        session_id = uuid4()
        session = AgentSession(
            id=session_id,
            provider="claude",
            environment="test",
            project="test",
            started_at=datetime.now(timezone.utc),
            user_messages=2,
            assistant_messages=0,
            tool_calls=0,
        )
        db.add(session)

        source_path = "/test/session.jsonl"

        # Create events with timestamps out of file order
        # Line 2 has earlier timestamp but higher offset
        line1 = json.dumps({"type": "user", "message": {"content": "First line"}})
        line2 = json.dumps({"type": "user", "message": {"content": "Second line"}})

        event1 = AgentEvent(
            session_id=session_id,
            role="user",
            content_text="First line",
            timestamp=datetime(2026, 1, 28, 11, 0, 0, tzinfo=timezone.utc),  # Later time
            source_path=source_path,
            source_offset=0,  # But first in file
            raw_json=line1,
        )
        event2 = AgentEvent(
            session_id=session_id,
            role="user",
            content_text="Second line",
            timestamp=datetime(2026, 1, 28, 10, 0, 0, tzinfo=timezone.utc),  # Earlier time
            source_path=source_path,
            source_offset=100,  # But second in file
            raw_json=line2,
        )
        db.add(event1)
        db.add(event2)
        db.commit()

        # Export
        result = store.export_session_jsonl(session_id)
        assert result is not None
        content, _ = result

        lines = content.decode("utf-8").strip().split("\n")
        assert len(lines) == 2
        # Should be in file order (by offset), not timestamp order
        assert lines[0] == line1  # offset=0 first
        assert lines[1] == line2  # offset=100 second

    def test_export_mixed_raw_and_synthesized(self, agents_db_session):
        """Events without raw_json fall back to synthesized."""
        db = agents_db_session
        store = AgentsStore(db)

        session_id = uuid4()
        session = AgentSession(
            id=session_id,
            provider="claude",
            environment="test",
            project="test",
            started_at=datetime.now(timezone.utc),
            user_messages=2,
            assistant_messages=0,
            tool_calls=0,
        )
        db.add(session)

        source_path = "/test/session.jsonl"
        raw_line = json.dumps({"type": "user", "message": {"content": "Has raw"}})

        event1 = AgentEvent(
            session_id=session_id,
            role="user",
            content_text="Has raw",
            timestamp=datetime(2026, 1, 28, 10, 0, 0, tzinfo=timezone.utc),
            source_path=source_path,
            source_offset=0,
            raw_json=raw_line,
        )
        event2 = AgentEvent(
            session_id=session_id,
            role="user",
            content_text="No raw, synthesized",
            timestamp=datetime(2026, 1, 28, 10, 1, 0, tzinfo=timezone.utc),
            source_path=source_path,
            source_offset=100,
            raw_json=None,  # No raw_json
        )
        db.add(event1)
        db.add(event2)
        db.commit()

        # Export
        result = store.export_session_jsonl(session_id)
        assert result is not None
        content, _ = result

        lines = content.decode("utf-8").strip().split("\n")
        assert len(lines) == 2
        assert lines[0] == raw_line
        # Second line is synthesized
        parsed = json.loads(lines[1])
        assert parsed["role"] == "user"
        assert parsed["content"] == "No raw, synthesized"

    def test_export_legacy_no_raw_json(self, agents_db_session):
        """Legacy events without any raw_json export synthesized format."""
        db = agents_db_session
        store = AgentsStore(db)

        session_id = uuid4()
        session = AgentSession(
            id=session_id,
            provider="claude",
            environment="test",
            project="test",
            started_at=datetime.now(timezone.utc),
            user_messages=1,
            assistant_messages=0,
            tool_calls=0,
        )
        db.add(session)

        event = AgentEvent(
            session_id=session_id,
            role="user",
            content_text="Legacy message",
            timestamp=datetime(2026, 1, 28, 10, 0, 0, tzinfo=timezone.utc),
            raw_json=None,
        )
        db.add(event)
        db.commit()

        # Export
        result = store.export_session_jsonl(session_id)
        assert result is not None
        content, _ = result

        lines = content.decode("utf-8").strip().split("\n")
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["role"] == "user"
        assert parsed["content"] == "Legacy message"

    def test_export_empty_session(self, agents_db_session):
        """Empty session exports empty content."""
        db = agents_db_session
        store = AgentsStore(db)

        session_id = uuid4()
        session = AgentSession(
            id=session_id,
            provider="claude",
            environment="test",
            project="test",
            started_at=datetime.now(timezone.utc),
            user_messages=0,
            assistant_messages=0,
            tool_calls=0,
        )
        db.add(session)
        db.commit()

        result = store.export_session_jsonl(session_id)
        assert result is not None
        content, _ = result
        assert content == b""

    def test_export_nonexistent_session(self, agents_db_session):
        """Nonexistent session returns None."""
        db = agents_db_session
        store = AgentsStore(db)

        result = store.export_session_jsonl(uuid4())
        assert result is None
