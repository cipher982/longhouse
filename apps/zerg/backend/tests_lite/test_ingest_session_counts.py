"""Tests that ingest_session correctly counts user/assistant/tool events.

Tool-call events (assistant role + tool_name set) must count toward tool_calls
only, not assistant_messages, so the UI shows accurate conversation turns.
"""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from zerg.database import initialize_database, make_engine
from zerg.services.agents_store import AgentsStore, EventIngest, SessionIngest


def _make_store(tmp_path):
    db_path = tmp_path / "counts.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    db = sessionmaker(bind=engine)()
    return AgentsStore(db), db


def _ts():
    return datetime(2026, 2, 22, tzinfo=timezone.utc)


def test_tool_call_events_count_as_tools_not_turns(tmp_path):
    """Assistant events with tool_name set should increment tool_calls, not assistant_messages."""
    store, db = _make_store(tmp_path)
    ts = _ts()
    session_id = uuid4()

    result = store.ingest_session(
        SessionIngest(
            id=session_id,
            provider="claude",
            environment="test",
            project="test",
            device_id="dev",
            cwd="/tmp",
            started_at=ts,
            events=[
                # 1 user turn
                EventIngest(role="user", content_text="hi", timestamp=ts, source_path="/s.jsonl", source_offset=0),
                # 1 assistant tool-call (should count as tool, not assistant turn)
                EventIngest(
                    role="assistant",
                    tool_name="Bash",
                    tool_input_json={"command": "ls"},
                    timestamp=ts,
                    source_path="/s.jsonl",
                    source_offset=1,
                ),
                # 1 tool result
                EventIngest(
                    role="tool",
                    tool_name="Bash",
                    tool_output_text="file.txt",
                    timestamp=ts,
                    source_path="/s.jsonl",
                    source_offset=2,
                ),
                # 1 assistant text response
                EventIngest(
                    role="assistant",
                    content_text="Done.",
                    timestamp=ts,
                    source_path="/s.jsonl",
                    source_offset=3,
                ),
            ],
        )
    )

    assert result.events_inserted == 4

    from zerg.models.agents import AgentSession
    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    assert session is not None

    assert session.user_messages == 1, f"expected 1 user turn, got {session.user_messages}"
    assert session.assistant_messages == 1, f"expected 1 assistant turn, got {session.assistant_messages}"
    assert session.tool_calls == 1, f"expected 1 tool call, got {session.tool_calls}"


def test_multiple_tool_calls_per_turn(tmp_path):
    """Each assistant tool-call event increments tool_calls independently."""
    store, db = _make_store(tmp_path)
    ts = _ts()
    session_id = uuid4()

    store.ingest_session(
        SessionIngest(
            id=session_id,
            provider="claude",
            environment="test",
            project="test",
            device_id="dev",
            cwd="/tmp",
            started_at=ts,
            events=[
                EventIngest(role="user", content_text="do stuff", timestamp=ts, source_path="/s.jsonl", source_offset=0),
                # 3 tool calls
                EventIngest(role="assistant", tool_name="Glob", tool_input_json={}, timestamp=ts, source_path="/s.jsonl", source_offset=1),
                EventIngest(role="tool", tool_name="Glob", tool_output_text="a.py", timestamp=ts, source_path="/s.jsonl", source_offset=2),
                EventIngest(role="assistant", tool_name="Read", tool_input_json={}, timestamp=ts, source_path="/s.jsonl", source_offset=3),
                EventIngest(role="tool", tool_name="Read", tool_output_text="...", timestamp=ts, source_path="/s.jsonl", source_offset=4),
                EventIngest(role="assistant", tool_name="Edit", tool_input_json={}, timestamp=ts, source_path="/s.jsonl", source_offset=5),
                EventIngest(role="tool", tool_name="Edit", tool_output_text="done", timestamp=ts, source_path="/s.jsonl", source_offset=6),
                # 1 final text
                EventIngest(role="assistant", content_text="All done.", timestamp=ts, source_path="/s.jsonl", source_offset=7),
            ],
        )
    )

    from zerg.models.agents import AgentSession
    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()

    assert session.user_messages == 1
    assert session.assistant_messages == 1
    assert session.tool_calls == 3


def test_compaction_only_append_does_not_inflate_turn_counts(tmp_path):
    """Appending compaction metadata should not create fake user/assistant turns."""
    store, db = _make_store(tmp_path)
    ts = _ts()
    session_id = uuid4()

    source_path = "/compaction/session.jsonl"
    store.ingest_session(
        SessionIngest(
            id=session_id,
            provider="claude",
            environment="test",
            project="test",
            device_id="dev",
            cwd="/tmp",
            started_at=ts,
            events=[
                EventIngest(role="user", content_text="remember yellow", timestamp=ts, source_path=source_path, source_offset=0),
                EventIngest(role="assistant", content_text="noted", timestamp=ts, source_path=source_path, source_offset=1),
            ],
        )
    )

    append_result = store.ingest_session(
        SessionIngest(
            id=session_id,
            provider="claude",
            environment="test",
            project="test",
            device_id="dev",
            cwd="/tmp",
            started_at=ts,
            ended_at=ts,
            events=[
                EventIngest(
                    role="system",
                    content_text="Session compacted to summary",
                    timestamp=ts,
                    source_path=source_path,
                    source_offset=2,
                    raw_json='{"type":"summary","summary":"Session compacted to summary","leafUuid":"leaf-1"}',
                ),
                EventIngest(
                    role="system",
                    content_text="Conversation compacted [trigger=auto]",
                    timestamp=ts,
                    source_path=source_path,
                    source_offset=3,
                    raw_json='{"type":"system","subtype":"compact_boundary","timestamp":"2026-02-22T00:00:00Z"}',
                ),
            ],
        )
    )
    assert append_result.events_inserted == 2

    from zerg.models.agents import AgentSession

    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    assert session is not None
    assert session.user_messages == 1
    assert session.assistant_messages == 1
    assert session.tool_calls == 0
