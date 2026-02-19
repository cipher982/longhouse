"""Tests for get_session_events() filtering: tool_name, query, roles, and count."""

from datetime import datetime
from datetime import timezone

from sqlalchemy.orm import sessionmaker

from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest


def _make_store(tmp_path, db_name="events_filter.db"):
    db_path = tmp_path / db_name
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    return AgentsStore(db), db


def _seed_session(store):
    ts = datetime(2026, 2, 5, tzinfo=timezone.utc)
    result = store.ingest_session(
        SessionIngest(
            provider="claude",
            environment="test",
            project="filter-test",
            device_id="dev",
            cwd="/tmp",
            git_repo=None,
            git_branch=None,
            started_at=ts,
            events=[
                EventIngest(
                    role="user",
                    content_text="please run a bash command",
                    timestamp=ts,
                    source_path="/tmp/s.jsonl",
                    source_offset=0,
                ),
                EventIngest(
                    role="assistant",
                    content_text="sure, running grep for secretvalue",
                    timestamp=ts,
                    source_path="/tmp/s.jsonl",
                    source_offset=1,
                ),
                EventIngest(
                    role="tool",
                    content_text=None,
                    tool_name="Bash",
                    tool_output_text="grep output: secretvalue found at line 42",
                    timestamp=ts,
                    source_path="/tmp/s.jsonl",
                    source_offset=2,
                ),
                EventIngest(
                    role="tool",
                    content_text=None,
                    tool_name="Read",
                    tool_output_text="file contents here",
                    timestamp=ts,
                    source_path="/tmp/s.jsonl",
                    source_offset=3,
                ),
                EventIngest(
                    role="assistant",
                    content_text="done with the task",
                    timestamp=ts,
                    source_path="/tmp/s.jsonl",
                    source_offset=4,
                ),
            ],
        )
    )
    return result.session_id


def test_filter_by_tool_name(tmp_path):
    store, _ = _make_store(tmp_path)
    session = _seed_session(store)

    events = store.get_session_events(session, tool_name="Bash")
    assert len(events) == 1
    assert events[0].tool_name == "Bash"


def test_filter_by_tool_name_no_match(tmp_path):
    store, _ = _make_store(tmp_path, "no_match.db")
    session = _seed_session(store)

    events = store.get_session_events(session, tool_name="Edit")
    assert events == []


def test_filter_by_query_content_search(tmp_path):
    store, _ = _make_store(tmp_path, "query.db")
    session = _seed_session(store)

    events = store.get_session_events(session, query="secretvalue")
    # Should match assistant content_text and/or tool_output_text containing "secretvalue"
    assert len(events) >= 1
    matched_texts = [
        (e.content_text or "") + (e.tool_output_text or "")
        for e in events
    ]
    assert any("secretvalue" in t for t in matched_texts)


def test_filter_by_query_no_match(tmp_path):
    store, _ = _make_store(tmp_path, "nomatch_query.db")
    session = _seed_session(store)

    events = store.get_session_events(session, query="completelymissingterm")
    assert events == []


def test_filter_by_roles(tmp_path):
    store, _ = _make_store(tmp_path, "roles.db")
    session = _seed_session(store)

    events = store.get_session_events(session, roles=["tool"])
    assert len(events) == 2
    assert all(e.role == "tool" for e in events)


def test_combined_tool_name_and_query(tmp_path):
    store, _ = _make_store(tmp_path, "combined.db")
    session = _seed_session(store)

    # Bash events that also contain "secretvalue" in output
    events = store.get_session_events(session, tool_name="Bash", query="secretvalue")
    assert len(events) == 1
    assert events[0].tool_name == "Bash"


def test_combined_no_intersection(tmp_path):
    store, _ = _make_store(tmp_path, "no_intersect.db")
    session = _seed_session(store)

    # Read tool events containing "secretvalue" — none exist
    events = store.get_session_events(session, tool_name="Read", query="secretvalue")
    assert events == []


def test_count_session_events_unfiltered(tmp_path):
    store, _ = _make_store(tmp_path, "count_all.db")
    session = _seed_session(store)

    total = store.count_session_events(session)
    assert total == 5


def test_count_session_events_by_tool_name(tmp_path):
    store, _ = _make_store(tmp_path, "count_tool.db")
    session = _seed_session(store)

    total = store.count_session_events(session, tool_name="Bash")
    assert total == 1


def test_count_session_events_by_query(tmp_path):
    store, _ = _make_store(tmp_path, "count_query.db")
    session = _seed_session(store)

    total = store.count_session_events(session, query="secretvalue")
    assert total >= 1


def test_count_session_events_no_match(tmp_path):
    store, _ = _make_store(tmp_path, "count_none.db")
    session = _seed_session(store)

    total = store.count_session_events(session, query="completelyabsent")
    assert total == 0


def test_pagination(tmp_path):
    store, _ = _make_store(tmp_path, "pagination.db")
    session = _seed_session(store)

    page1 = store.get_session_events(session, limit=2, offset=0)
    page2 = store.get_session_events(session, limit=2, offset=2)
    all_events = store.get_session_events(session)

    assert len(page1) == 2
    assert len(page2) == 2
    # Pages should not overlap
    page1_ids = {e.id for e in page1}
    page2_ids = {e.id for e in page2}
    assert page1_ids.isdisjoint(page2_ids)
    # Together they cover the first 4 of 5 events
    assert len(page1_ids | page2_ids) == 4
    assert len(all_events) == 5
