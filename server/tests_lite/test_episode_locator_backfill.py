"""searchd can rebuild an episode's start position from its own tables.

Why this exists: 82k episodes were embedded before the locator column existed,
so semantic recall reports unavailable evidence for the whole historical corpus.
The locator is derived from the transcript, not from the model, so backfilling
it needs no embedding spend — but it does need searchd to reproduce the
embeddings projector's clean-message indices *exactly*. A derivation that is
merely close would write 82k plausible-looking wrong positions, and every one of
them would hydrate neighbours from the wrong part of the conversation.

The load-bearing test here is the equivalence one: the same records fed through
the projector's chunker and through searchd's SQL projection must agree on which
event each episode starts at.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from datetime import UTC
from datetime import datetime

from zerg.searchd.store import SearchStore
from zerg.searchd.store import object_set_hash
from zerg.searchd.store import open_search_database
from zerg.services.clean_events import iter_clean_transcript_events
from zerg.services.session_processing.embeddings import iter_turn_chunks

_BASE_US = 1_720_780_400_000_000


def _transcript() -> list[dict]:
    """A transcript with the shapes the clean projection actually drops.

    Tool results, empty content and whitespace-only content are all skipped, so
    clean indices drift from raw indices — which is exactly the drift a naive
    backfill would get wrong.
    """
    raw = [
        ("user", "how do I resume a claude session", None, None),
        ("assistant", "", None, None),  # dropped: no content
        ("tool", "exit 0", "long tool output here", "Bash"),  # dropped: tool result
        ("assistant", "pass the native id to --resume", None, None),
        ("user", "   ", None, None),  # dropped: whitespace only
        ("user", "and if it rotates mid-session?", None, None),
        ("assistant", "the reset boundary carries both ids", None, None),
    ]
    return [
        {
            "event_id": f"event-{index}",
            "record_ordinal": index,
            "order_time_us": _BASE_US + index,
            "source_position": index,
            "event_subordinal": 0,
            "role": role,
            "content_text": content,
            "tool_name": tool_name,
            "tool_output_text": tool_output,
            "tool_call_id": None,
            "thread_id": None,
            "branch_kind": None,
        }
        for index, (role, content, tool_output, tool_name) in enumerate(raw)
    ]


def _projector_records(rows: list[dict]) -> list[dict]:
    """The record shape embeddings_v2_projector builds before chunking."""
    records = [
        {
            "role": row["role"],
            "content_text": row["content_text"],
            "tool_name": row["tool_name"],
            "tool_output_text": row["tool_output_text"],
            "timestamp": row["order_time_us"],
        }
        for row in rows
    ]
    for index, record in enumerate(records):
        record["id"] = index
    return records


def test_searchd_projection_matches_the_projector_chunker():
    """Equivalence, asserted directly rather than inferred from a passing backfill."""
    rows = _transcript()
    records = _projector_records(rows)

    # What the projector records for each episode.
    chunks = list(iter_turn_chunks(records))
    assert chunks, "fixture must produce at least one episode"

    # What searchd would derive for the same clean indices.
    positions = {
        event.index: records[event.event_id]["timestamp"]
        for event in iter_clean_transcript_events(records)
        if event.event_id is not None
    }

    for chunk in chunks:
        expected = records[chunk.source_event_id_start]["timestamp"]
        assert positions[chunk.event_index_start] == expected, (
            f"episode {chunk.chunk_index} resolves to the wrong event"
        )


def test_backfill_fills_null_locators_from_published_events(tmp_path):
    path = tmp_path / "search.db"
    connection = open_search_database(path)
    store = SearchStore(connection)
    try:
        _seed_published_session(store, connection, rows=_transcript())
        records = _projector_records(_transcript())
        chunks = list(iter_turn_chunks(records))
        for chunk in chunks:
            _insert_episode(connection, chunk.chunk_index, chunk.event_index_start, locator=None)
        connection.commit()

        result = store.backfill_episode_locators(limit=100)

        assert result["resolved"] == len(chunks)
        assert result["unresolved"] == 0
        stored = dict(
            connection.execute("SELECT episode_ordinal, start_order_time_us FROM episode_embeddings").fetchall()
        )
        for chunk in chunks:
            assert stored[chunk.chunk_index] == records[chunk.source_event_id_start]["timestamp"]
    finally:
        connection.close()


def test_verify_mode_reports_agreement_without_writing(tmp_path):
    """The gate before touching 82k rows: does the derivation match what shipped?"""
    path = tmp_path / "search.db"
    connection = open_search_database(path)
    store = SearchStore(connection)
    try:
        _seed_published_session(store, connection, rows=_transcript())
        records = _projector_records(_transcript())
        chunks = list(iter_turn_chunks(records))
        # Seed one correct locator and one deliberately wrong one.
        _insert_episode(
            connection,
            chunks[0].chunk_index,
            chunks[0].event_index_start,
            locator=records[chunks[0].source_event_id_start]["timestamp"],
        )
        _insert_episode(connection, 900, chunks[0].event_index_start, locator=_BASE_US + 999)
        connection.commit()

        result = store.backfill_episode_locators(limit=100, verify=True)

        assert result["agreed"] == 1
        assert result["disagreed"] == 1
        assert "resolved" not in result
        # Verify must not mutate: the wrong locator is still wrong afterwards.
        wrong = connection.execute(
            "SELECT start_order_time_us FROM episode_embeddings WHERE episode_ordinal = 900"
        ).fetchone()[0]
        assert wrong == _BASE_US + 999
    finally:
        connection.close()


def test_episode_from_a_superseded_generation_is_left_alone(tmp_path):
    """A stale generation has no published events, so it must stay unresolved."""
    path = tmp_path / "search.db"
    connection = open_search_database(path)
    store = SearchStore(connection)
    try:
        _seed_published_session(store, connection, rows=_transcript())
        connection.execute(
            """
            INSERT INTO episode_embeddings(
                session_id, owner_id, generation_id, revision, episode_ordinal,
                event_index_start, event_index_end, start_order_time_us,
                model, dims, content_hash, embedding, updated_at
            ) VALUES (?, '42', ?, 1, 0, 0, 1, NULL, 'm', 2, 'h', X'0000', '2026-08-01T00:00:00+00:00')
            """,
            (_SESSION_ID, "00000000-0000-4000-8000-00000000dead"),
        )
        connection.commit()

        result = store.backfill_episode_locators(limit=100)

        assert result["resolved"] == 0
        assert result["scanned"] == 0, "a superseded generation has no published session_index row"
    finally:
        connection.close()


_SESSION_ID = "11111111-1111-4111-8111-111111111111"
_GENERATION_ID = "22222222-2222-4222-8222-222222222222"
_OBJECT_ID = "a" * 64


def _seed_published_session(store: SearchStore, connection, *, rows: list[dict]) -> None:
    store.index_object(
        session_id=_SESSION_ID,
        generation_id=_GENERATION_ID,
        object_id=_OBJECT_ID,
        desired_revision=1,
        provider="claude",
        machine_id="cinder",
        project="zerg",
        environment="local",
        cwd="/tmp",
        git_repo=None,
        opaque_source_id="claude/session.jsonl",
        source_epoch="1",
        records=rows,
    )
    store.publish_generation(
        session_id=_SESSION_ID,
        generation_id=_GENERATION_ID,
        owner_id="42",
        desired_revision=1,
        object_count=1,
        object_set_hash=object_set_hash([_OBJECT_ID]),
        event_count=len(rows),
        project="zerg",
        provider="claude",
        environment="local",
        cwd="/tmp",
        git_repo=None,
        started_at=datetime.now(UTC).isoformat(),
    )
    connection.commit()


def _insert_episode(connection, ordinal: int, event_index_start: int, *, locator: int | None) -> None:
    connection.execute(
        """
        INSERT INTO episode_embeddings(
            session_id, owner_id, generation_id, revision, episode_ordinal,
            event_index_start, event_index_end, start_order_time_us,
            model, dims, content_hash, embedding, updated_at
        ) VALUES (?, '42', ?, 1, ?, ?, ?, ?, 'm', 2, ?, X'0000', '2026-08-01T00:00:00+00:00')
        """,
        (
            _SESSION_ID,
            _GENERATION_ID,
            ordinal,
            event_index_start,
            event_index_start,
            locator,
            f"hash{ordinal}",
        ),
    )
