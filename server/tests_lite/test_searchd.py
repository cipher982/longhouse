from __future__ import annotations

import asyncio
import base64
import hashlib
import sqlite3
import threading
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from uuid import UUID
from uuid import uuid4

import numpy as np
import pytest

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.client import CatalogUnavailable
from zerg.embedding_space import ACTIVE_EMBEDDING_DIMS
from zerg.embedding_space import ACTIVE_EMBEDDING_MODEL
from zerg.searchd.server import SearchDaemon
from zerg.searchd.server import _embedding_write_params
from zerg.searchd.store import _PUBLISH_AGGREGATES_SQL
from zerg.searchd.store import _SEARCH_SQL
from zerg.searchd.store import _SEARCHABLE_SEARCH_SQL
from zerg.searchd.store import SCHEMA_GENERATION
from zerg.searchd.store import SearchStore
from zerg.searchd.store import _bounded_worklog_content
from zerg.searchd.store import _fts_query
from zerg.searchd.store import object_set_hash
from zerg.searchd.store import open_search_database


def _records(text: str) -> list[dict]:
    return [
        {
            "event_id": "event-1",
            "record_ordinal": 0,
            "order_time_us": 1_720_780_400_000_000,
            "source_position": 10,
            "event_subordinal": 0,
            "role": "user",
            "interaction_kind": "durable_user_message",
            "content_text": text,
            "tool_name": None,
            "tool_output_text": None,
            "tool_call_id": None,
            "thread_id": "thread-subagent",
            "branch_kind": "subagent",
        },
        {
            "event_id": "event-2",
            "record_ordinal": 1,
            "order_time_us": 1_720_780_400_000_001,
            "source_position": 11,
            "event_subordinal": 0,
            "role": "assistant",
            "interaction_kind": "provider_system",
            "content_text": "indexed answer",
            "tool_name": None,
            "tool_output_text": None,
            "tool_call_id": None,
            "thread_id": "thread-subagent",
            "branch_kind": "subagent",
        },
    ]


def _search_params(query: str) -> dict:
    return {
        "owner_id": "42",
        "query": query,
        "project": None,
        "provider": None,
        "environment": None,
        "window_start_us": None,
        "window_end_us": None,
        "limit": 10,
        "include_snippets": True,
    }


def test_worklog_export_bounds_oversized_messages_without_splitting_utf8():
    content = "a" * (128 * 1024) + "💾"

    bounded = _bounded_worklog_content(content)

    assert len(bounded.encode("utf-8")) <= 128 * 1024
    assert bounded.endswith("[Longhouse worklog export truncated oversized message]")
    assert "�" not in bounded


def test_worklog_export_preserves_messages_within_the_boundary():
    assert _bounded_worklog_content("ordinary message") == "ordinary message"


def test_embedding_write_contract_accepts_full_desired_episode_set():
    vector = np.zeros(ACTIVE_EMBEDDING_DIMS, dtype=np.float32)
    vector[0] = 1.0
    params = _embedding_write_params(
        {
            "session_id": str(uuid4()),
            "owner_id": "owner-1",
            "generation_id": str(uuid4()),
            "revision": "3",
            "model": ACTIVE_EMBEDDING_MODEL,
            "dims": ACTIVE_EMBEDDING_DIMS,
            "complete": True,
            "desired_episode_ordinals": [0, 1, 2],
            "episodes": [
                {
                    "episode_ordinal": 2,
                    "event_index_start": 4,
                    "event_index_end": 5,
                    "start_order_time_us": 123,
                    "content_hash": "c" * 64,
                    "embedding": base64.b64encode(vector.tobytes()).decode("ascii"),
                }
            ],
        }
    )

    assert params["desired_episode_ordinals"] == [0, 1, 2]


@pytest.mark.parametrize("ordinals", [[0, 0], [-1], [True]])
def test_embedding_write_contract_rejects_invalid_desired_episode_set(ordinals):
    with pytest.raises(ValueError, match="desired embedding episode ordinals"):
        _embedding_write_params(
            {
                "session_id": str(uuid4()),
                "owner_id": "owner-1",
                "generation_id": str(uuid4()),
                "revision": "3",
                "model": ACTIVE_EMBEDDING_MODEL,
                "dims": ACTIVE_EMBEDDING_DIMS,
                "complete": True,
                "desired_episode_ordinals": ordinals,
                "episodes": [],
            }
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("search.db", '"search db"'),
        ("--no-verify", '"no verify"'),
        ("server/zerg/searchd/store.py", '"server zerg searchd store py"'),
        ('"exact closing text"', '"exact closing text"'),
        ("repair session recall", "repair session recall"),
        ("50068012e", "50068012e"),
        ("---", ""),
    ],
)
def test_fts_query_preserves_compact_identifiers_as_phrases(raw, expected):
    assert _fts_query(raw) == expected


def test_search_preserves_every_natural_query_term(tmp_path):
    connection = open_search_database(tmp_path / "search.db")
    try:
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'events_fts_vocab'").fetchone() is None
        query, token_count, compiled_token_count = SearchStore(connection)._compile_fts_query("please explain managed session recovery")
        assert query == "please explain managed session recovery"
        assert (token_count, compiled_token_count) == (5, 5)
    finally:
        connection.close()


def test_searchd_rebuilds_an_incompatible_disposable_store(tmp_path):
    path = tmp_path / "search.db"
    connection = open_search_database(path)
    previous_store_id = connection.execute("SELECT store_id FROM search_meta").fetchone()[0]
    connection.execute("UPDATE search_meta SET schema_generation = 'obsolete'")
    connection.close()

    rebuilt = open_search_database(path)
    try:
        meta = rebuilt.execute("SELECT schema_version, schema_generation FROM search_meta").fetchone()
        assert tuple(meta) == (1, SCHEMA_GENERATION)
        assert rebuilt.execute("SELECT store_id FROM search_meta").fetchone()[0] != previous_store_id
        assert rebuilt.execute("SELECT COUNT(*) FROM session_index").fetchone()[0] == 0
    finally:
        rebuilt.close()


def test_nullable_episode_column_is_added_without_discarding_the_store(tmp_path):
    """Adding a nullable locator must not cost a full re-index and re-embed.

    On the real corpus a discard means republishing 22k sessions into a 17GB
    index and re-embedding 82k episodes, all to add one integer that old rows
    are allowed to be missing. Old rows without a locator report unavailable
    evidence, which is honest; losing them is not.
    """
    path = tmp_path / "search.db"
    connection = open_search_database(path)
    store_id = connection.execute("SELECT store_id FROM search_meta").fetchone()[0]
    connection.execute("ALTER TABLE episode_embeddings DROP COLUMN start_order_time_us")
    connection.execute(
        """
        INSERT INTO episode_embeddings(
            session_id, owner_id, generation_id, revision, episode_ordinal,
            event_index_start, event_index_end, model, dims, content_hash, embedding, updated_at
        ) VALUES ('s', '42', 'g', 1, 0, 0, 1, 'm', 2, 'h', X'0000', '2026-08-01T00:00:00+00:00')
        """
    )
    connection.commit()
    connection.close()

    upgraded = open_search_database(path)
    try:
        assert upgraded.execute("SELECT store_id FROM search_meta").fetchone()[0] == store_id
        row = upgraded.execute("SELECT episode_ordinal, start_order_time_us FROM episode_embeddings").fetchone()
        assert tuple(row) == (0, None)
    finally:
        upgraded.close()


def test_episode_embeddings_deduplicate_exact_replays(tmp_path):
    connection = open_search_database(tmp_path / "search.db")
    store = SearchStore(connection)
    session_id = str(uuid4())
    published_generation = str(uuid4())
    owner_id = "owner-1"
    try:
        connection.execute(
            """
            INSERT INTO session_index (
                session_id, generation_id, owner_id, desired_revision, indexed_through,
                object_count, object_set_hash, event_count, user_messages, assistant_messages,
                tool_calls, is_sidechain, project, provider, environment, cwd, git_repo,
                started_at, published_at
            ) VALUES (?, ?, ?, 1, 1, 1, 'hash', 2, 1, 1, 0, 0, 'proj', 'codex', 'local', NULL, NULL, ?, ?)
            """,
            (session_id, published_generation, owner_id, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        first = store.write_episode_embeddings(
            session_id=session_id,
            owner_id=owner_id,
            generation_id=published_generation,
            revision=1,
            model="test-model",
            dims=2,
            episodes=[
                {
                    "episode_ordinal": 0,
                    "event_index_start": 0,
                    "event_index_end": 1,
                    "start_order_time_us": 100,
                    "content_hash": "a" * 64,
                    "embedding": np.array([1, 0], dtype=np.float32).tobytes(),
                },
                {
                    "episode_ordinal": 1,
                    "event_index_start": 2,
                    "event_index_end": 3,
                    "start_order_time_us": 200,
                    "content_hash": "b" * 64,
                    "embedding": np.array([0, 1], dtype=np.float32).tobytes(),
                },
            ],
        )
        assert first == {"written": 2, "skipped": 0}
        identity = store.read_episode_embedding_hashes(session_id=session_id, model="test-model")
        assert identity == {
            "hashes": {"0": "a" * 64, "1": "b" * 64},
            "published_generation_id": published_generation,
            "published_revision": "1",
        }
        with pytest.raises(ValueError, match="published session identity"):
            store.write_episode_embeddings(
                session_id=session_id,
                owner_id=owner_id,
                generation_id=published_generation,
                revision=2,
                model="test-model",
                dims=2,
                episodes=[],
            )
        replay = store.write_episode_embeddings(
            session_id=session_id,
            owner_id=owner_id,
            generation_id=published_generation,
            revision=1,
            model="test-model",
            dims=2,
            episodes=[
                {
                    "episode_ordinal": 0,
                    "event_index_start": 0,
                    "event_index_end": 1,
                    "start_order_time_us": 100,
                    "content_hash": "a" * 64,
                    "embedding": np.array([1, 0], dtype=np.float32).tobytes(),
                }
            ],
        )
        # A true replay -- same generation, same text -- still costs nothing.
        assert replay == {"written": 0, "skipped": 1}
    finally:
        connection.close()

def test_episode_embeddings_refresh_revision_on_unchanged_hash(tmp_path):
    """A same-generation revision bump must move reused vectors to the new fence."""

    connection = open_search_database(tmp_path / "search.db")
    store = SearchStore(connection)
    session_id = str(uuid4())
    generation_id = str(uuid4())
    owner_id = "owner-1"
    episode = {
        "episode_ordinal": 0,
        "event_index_start": 0,
        "event_index_end": 1,
        "start_order_time_us": 100,
        "content_hash": "a" * 64,
        "embedding": np.array([1, 0], dtype=np.float32).tobytes(),
    }
    try:
        connection.execute(
            """
            INSERT INTO session_index (
                session_id, generation_id, owner_id, desired_revision, indexed_through,
                object_count, object_set_hash, event_count, user_messages, assistant_messages,
                tool_calls, is_sidechain, project, provider, environment, cwd, git_repo,
                started_at, published_at
            ) VALUES (?, ?, ?, 1, 1, 1, 'hash', 2, 1, 1, 0, 0, 'proj', 'codex', 'local', NULL, NULL, ?, ?)
            """,
            (session_id, generation_id, owner_id, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        assert store.write_episode_embeddings(
            session_id=session_id,
            owner_id=owner_id,
            generation_id=generation_id,
            revision=1,
            model="test-model",
            dims=2,
            complete=True,
            desired_episode_ordinals=[0],
            episodes=[episode],
        ) == {"written": 1, "skipped": 0}

        connection.execute(
            "UPDATE session_index SET desired_revision = 2, indexed_through = 2 WHERE session_id = ?",
            (session_id,),
        )
        assert (
            store.read_episode_embedding_hashes(
                session_id=session_id,
                model="test-model",
                dims=2,
            )["hashes"]
            == {}
        )
        assert store.write_episode_embeddings(
            session_id=session_id,
            owner_id=owner_id,
            generation_id=generation_id,
            revision=2,
            model="test-model",
            dims=2,
            complete=True,
            desired_episode_ordinals=[0],
            episodes=[episode],
        ) == {"written": 1, "skipped": 0}
        row = connection.execute(
            "SELECT revision FROM episode_embeddings WHERE session_id = ? AND episode_ordinal = 0 AND model = 'test-model'",
            (session_id,),
        ).fetchone()
        publication = connection.execute(
            "SELECT revision FROM embedding_publications WHERE session_id = ? AND model = 'test-model'",
            (session_id,),
        ).fetchone()
        assert (row["revision"], publication["revision"]) == (2, 2)
    finally:
        connection.close()


def test_embedding_source_reads_only_the_fenced_published_projection(tmp_path):
    connection = open_search_database(tmp_path / "search.db")
    store = SearchStore(connection)
    session_id = str(uuid4())
    generation_id = str(uuid4())
    source_epoch = str(uuid4())
    object_id = hashlib.sha256(b"embedding-source").hexdigest()
    try:
        store.index_object(
            session_id=session_id,
            generation_id=generation_id,
            object_id=object_id,
            desired_revision=7,
            provider="codex",
            machine_id="machine",
            project="longhouse",
            environment="local",
            cwd=None,
            git_repo=None,
            opaque_source_id="source.jsonl",
            source_epoch=source_epoch,
            records=_records("published semantic source"),
        )
        assert (
            store.publish_generation(
                session_id=session_id,
                generation_id=generation_id,
                owner_id="owner-1",
                desired_revision=7,
                object_count=1,
                object_set_hash=object_set_hash([object_id]),
                event_count=2,
                project="longhouse",
                provider="codex",
                environment="local",
                cwd=None,
                git_repo=None,
                started_at="2026-01-01T00:00:00+00:00",
            )["published"]
            is True
        )

        first = store.read_embedding_source(
            session_id=session_id,
            expected_generation_id=None,
            expected_revision=None,
            offset=0,
            limit=1,
        )
        assert first["generation_id"] == generation_id
        assert first["revision"] == "7"
        assert first["owner_id"] == "owner-1"
        assert first["event_count"] == 2
        assert first["has_more"] is True
        assert first["records"][0]["content_text"] == "published semantic source"

        second = store.read_embedding_source(
            session_id=session_id,
            expected_generation_id=generation_id,
            expected_revision=7,
            offset=1,
            limit=1,
        )
        assert second["has_more"] is False
        assert second["records"][0]["content_text"] == "indexed answer"
        with pytest.raises(ValueError, match="revision changed"):
            store.read_embedding_source(
                session_id=session_id,
                expected_generation_id=generation_id,
                expected_revision=8,
                offset=0,
                limit=1,
            )
    finally:
        connection.close()


def test_startup_prunes_every_inactive_embedding_space(tmp_path):
    connection = open_search_database(tmp_path / "search.db")
    store = SearchStore(connection)
    try:
        for model in (ACTIVE_EMBEDDING_MODEL, "retired/model"):
            connection.execute(
                """
                INSERT INTO episode_embeddings(
                    session_id, owner_id, generation_id, revision, episode_ordinal,
                    event_index_start, event_index_end, start_order_time_us,
                    model, dims, content_hash, embedding, updated_at
                ) VALUES (?, '42', 'generation', 1, 0, 0, 1, 100, ?, 2, ?, ?, '2026-08-01T00:00:00+00:00')
                """,
                (model, model, hashlib.sha256(model.encode()).hexdigest(), np.array([1, 0], dtype="float32").tobytes()),
            )
        connection.commit()

        assert store.prune_inactive_embedding_spaces(active_model=ACTIVE_EMBEDDING_MODEL) == {
            "vectors": 1,
            "publications": 0,
        }
        assert connection.execute("SELECT model FROM episode_embeddings").fetchall()[0][0] == ACTIVE_EMBEDDING_MODEL
    finally:
        connection.close()


def test_complete_write_preserves_untouched_episodes_via_desired_ordinals(tmp_path):
    """Regression guard: a `complete=True` write must not delete episodes that
    are still current but weren't rewritten in this call. Before
    desired_episode_ordinals existed, `complete=True` pruned episode_embeddings
    down to just this call's own `episodes`, so any chunk whose hash already
    matched (and was therefore never re-sent) got silently deleted.
    """
    connection = open_search_database(tmp_path / "search.db")
    store = SearchStore(connection)
    session_id = str(uuid4())
    published_generation = str(uuid4())
    owner_id = "owner-1"
    try:
        connection.execute(
            """
            INSERT INTO session_index (
                session_id, generation_id, owner_id, desired_revision, indexed_through,
                object_count, object_set_hash, event_count, user_messages, assistant_messages,
                tool_calls, is_sidechain, project, provider, environment, cwd, git_repo,
                started_at, published_at
            ) VALUES (?, ?, ?, 1, 1, 1, 'hash', 2, 1, 1, 0, 0, 'proj', 'codex', 'local', NULL, NULL, ?, ?)
            """,
            (session_id, published_generation, owner_id, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        # Seed two already-current episodes, as if a prior pass embedded them.
        store.write_episode_embeddings(
            session_id=session_id,
            owner_id=owner_id,
            generation_id=published_generation,
            revision=1,
            model="test-model",
            dims=2,
            episodes=[
                {
                    "episode_ordinal": 0,
                    "event_index_start": 0,
                    "event_index_end": 1,
                    "start_order_time_us": None,
                    "content_hash": "a" * 64,
                    "embedding": np.array([1, 0], dtype=np.float32).tobytes(),
                },
                {
                    "episode_ordinal": 1,
                    "event_index_start": 2,
                    "event_index_end": 3,
                    "start_order_time_us": None,
                    "content_hash": "b" * 64,
                    "embedding": np.array([0, 1], dtype=np.float32).tobytes(),
                },
            ],
        )

        # A later batch only rewrites ordinal 2 (a genuinely new chunk), but marks
        # the pass complete and declares the full desired set as [0, 1, 2].
        store.write_episode_embeddings(
            session_id=session_id,
            owner_id=owner_id,
            generation_id=published_generation,
            revision=1,
            model="test-model",
            dims=2,
            complete=True,
            desired_episode_ordinals=[0, 1, 2],
            episodes=[
                {
                    "episode_ordinal": 2,
                    "event_index_start": 4,
                    "event_index_end": 5,
                    "start_order_time_us": None,
                    "content_hash": "c" * 64,
                    "embedding": np.array([0, 0], dtype=np.float32).tobytes(),
                }
            ],
        )

        hashes = store.read_episode_embedding_hashes(session_id=session_id, model="test-model")["hashes"]
        assert hashes == {"0": "a" * 64, "1": "b" * 64, "2": "c" * 64}
    finally:
        connection.close()


def test_complete_write_without_desired_ordinals_still_prunes_stale_rows(tmp_path):
    """A truly final, single-call completion (no desired_episode_ordinals) must
    still delete a genuinely stale episode (one that no longer exists in the
    session), preserving the original prune behavior for the simple case.
    """
    connection = open_search_database(tmp_path / "search.db")
    store = SearchStore(connection)
    session_id = str(uuid4())
    published_generation = str(uuid4())
    owner_id = "owner-1"
    try:
        connection.execute(
            """
            INSERT INTO session_index (
                session_id, generation_id, owner_id, desired_revision, indexed_through,
                object_count, object_set_hash, event_count, user_messages, assistant_messages,
                tool_calls, is_sidechain, project, provider, environment, cwd, git_repo,
                started_at, published_at
            ) VALUES (?, ?, ?, 1, 1, 1, 'hash', 2, 1, 1, 0, 0, 'proj', 'codex', 'local', NULL, NULL, ?, ?)
            """,
            (session_id, published_generation, owner_id, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        store.write_episode_embeddings(
            session_id=session_id,
            owner_id=owner_id,
            generation_id=published_generation,
            revision=1,
            model="test-model",
            dims=2,
            episodes=[
                {
                    "episode_ordinal": 0,
                    "event_index_start": 0,
                    "event_index_end": 1,
                    "start_order_time_us": None,
                    "content_hash": "a" * 64,
                    "embedding": np.array([1, 0], dtype=np.float32).tobytes(),
                },
                {
                    "episode_ordinal": 1,
                    "event_index_start": 2,
                    "event_index_end": 3,
                    "start_order_time_us": None,
                    "content_hash": "b" * 64,
                    "embedding": np.array([0, 1], dtype=np.float32).tobytes(),
                },
            ],
        )

        store.write_episode_embeddings(
            session_id=session_id,
            owner_id=owner_id,
            generation_id=published_generation,
            revision=1,
            model="test-model",
            dims=2,
            complete=True,
            episodes=[
                {
                    "episode_ordinal": 0,
                    "event_index_start": 0,
                    "event_index_end": 1,
                    "start_order_time_us": None,
                    "content_hash": "a" * 64,
                    "embedding": np.array([1, 0], dtype=np.float32).tobytes(),
                }
            ],
        )

        hashes = store.read_episode_embedding_hashes(session_id=session_id, model="test-model")["hashes"]
        assert hashes == {"0": "a" * 64}
    finally:
        connection.close()


def test_publish_aggregate_uses_session_generation_index(tmp_path):
    connection = open_search_database(tmp_path / "search.db")
    try:
        plan = connection.execute(
            f"EXPLAIN QUERY PLAN {_PUBLISH_AGGREGATES_SQL}",
            ("session", "generation", 7, "session", "generation"),
        ).fetchall()
        details = [str(row[3]) for row in plan]
        assert not any(detail == "SCAN e" for detail in details)
        assert any("ix_search_events_session_generation_order" in detail for detail in details)
    finally:
        connection.close()


def test_archive_search_uses_fts_rank_top_k_without_temp_sort(tmp_path):
    connection = open_search_database(tmp_path / "search.db")
    try:
        plan = connection.execute(
            f"EXPLAIN QUERY PLAN {_SEARCH_SQL}",
            ("search db", "42", None, None, None, None, None, None, None, None, None, None, 10),
        ).fetchall()
        details = [str(row[3]) for row in plan]
        assert any("events_fts" in detail and "VIRTUAL TABLE INDEX 32:" in detail for detail in details)
        assert not any("TEMP B-TREE FOR ORDER BY" in detail for detail in details)
    finally:
        connection.close()


def test_searchable_search_walks_rowid_descending_and_sorts_only_candidates(tmp_path):
    """The interactive lane must not rank the whole match set.

    Rank-ordered FTS (`VIRTUAL TABLE INDEX 32:`) scores every match before the
    limit applies, which cost seconds on broad terms. Walking rowid-descending
    (`192:`) lets FTS5 stop early, so the temp sort that remains covers only the
    bounded candidate window rather than the full doclist.
    """

    connection = open_search_database(tmp_path / "search.db")
    try:
        plan = connection.execute(
            f"EXPLAIN QUERY PLAN {_SEARCHABLE_SEARCH_SQL}",
            ("search db", "42", None, None, None, None, None, None, None, None, None, None, 50_000, 10, "search db"),
        ).fetchall()
        details = [str(row[3]) for row in plan]
        assert any("searchable_fts" in detail and "VIRTUAL TABLE INDEX 192:" in detail for detail in details)
        assert not any("VIRTUAL TABLE INDEX 32:" in detail for detail in details)
        # The owner/project/window predicates must be evaluated inside the walk.
        # Applied afterwards they made narrow windows slower, not faster.
        assert any("SEARCH e USING INTEGER PRIMARY KEY" in detail for detail in details)
    finally:
        connection.close()


def test_candidate_walk_never_touches_event_text(tmp_path):
    """The walk must read narrow metadata rows, not rows carrying event text.

    Filter columns total ~20 bytes, but when they shared a row with content_text
    and tool_output_text the walk had to touch ~2.4 KB records to reach them. A
    broad query faulted 36-93 MB of page cache to return 5 results, which on a
    volume where a random read costs 600 us is seconds, not milliseconds.
    """

    connection = open_search_database(tmp_path / "search.db")
    try:
        metadata_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(searchable_events)").fetchall()}
        assert "content_text" not in metadata_columns
        assert "tool_output_text" not in metadata_columns

        candidate_sql, _, _ = _SEARCHABLE_SEARCH_SQL.partition("), top AS (")
        assert "searchable_text" not in candidate_sql, "candidate walk must not join the text table"

        # Deleting metadata must retire the text and its FTS entry with it, so
        # the four call sites that delete from searchable_events stay correct
        # without knowing the tables were split.
        connection.execute(
            "INSERT INTO searchable_events(source_event_id, owner_id, project, provider, environment,"
            " order_time_us, session_id, generation_id, source_object_id, record_ordinal, event_id,"
            " role, tool_name, indexed_through, event_count)"
            " VALUES (1, '42', NULL, 'codex', 'local', 1, 's', 'g', 'o', 0, 'e', 'user', NULL, 1, 1)"
        )
        connection.execute(
            "INSERT INTO searchable_text(source_event_id, content_text, tool_output_text) VALUES (1, 'retained needle', NULL)"
        )
        assert connection.execute("SELECT COUNT(*) FROM searchable_fts WHERE searchable_fts MATCH 'needle'").fetchone()[0] == 1

        connection.execute("DELETE FROM searchable_events WHERE source_event_id = 1")
        assert connection.execute("SELECT COUNT(*) FROM searchable_text").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM searchable_fts WHERE searchable_fts MATCH 'needle'").fetchone()[0] == 0
    finally:
        connection.close()


def test_searchable_search_snippets_only_the_returned_page(tmp_path):
    """Snippet cost must not scale with the candidate window.

    Real events carry multi-KB tool output, so building snippets for every
    candidate made search cost track stored text rather than results — enough to
    blow the deadline on hosted data even with ranking already bounded.
    """

    candidate_source, _, remainder = _SEARCHABLE_SEARCH_SQL.partition("), top AS (")
    assert "snippet(" not in candidate_source, "candidate walk must not build snippets"
    assert "snippet(" in remainder, "the returned page still needs snippets"


@pytest.mark.asyncio
async def test_search_reads_remain_live_while_projection_writer_is_busy(tmp_path):
    socket_parent = Path("/tmp") / f"lhs-{uuid4().hex[:8]}"
    socket_parent.mkdir(mode=0o700)
    socket_path = socket_parent / "s"
    daemon = SearchDaemon(database_path=tmp_path / "search.db", socket_path=socket_path)
    await daemon.start()
    writer_started = threading.Event()
    release_writer = threading.Event()

    def block_writer():
        writer_started.set()
        release_writer.wait(timeout=2)

    blocked = asyncio.create_task(daemon._run(block_writer))
    client = CatalogClient(socket_path)
    try:
        assert await asyncio.to_thread(writer_started.wait, 1)
        worklog = await asyncio.wait_for(
            client.call(
                "worklog.day.v2",
                {
                    "owner_id": "42",
                    "window_start_us": 1_720_780_399_000_000,
                    "window_end_us": 1_720_780_401_000_000,
                    "include_test": False,
                    "section": "sessions",
                    "snapshot_id": None,
                    "offset": 0,
                    "limit": 100,
                },
            ),
            timeout=0.2,
        )
        assert worklog["items"] == []
        assert not blocked.done()
    finally:
        release_writer.set()
        await blocked
        await client.close()
        await daemon.close()
        socket_parent.rmdir()


@pytest.mark.asyncio
async def test_timed_out_search_is_interrupted_before_later_reads(tmp_path):
    socket_parent = Path("/tmp") / f"lhs-{uuid4().hex[:8]}"
    socket_parent.mkdir(mode=0o700)
    socket_path = socket_parent / "s"
    daemon = SearchDaemon(database_path=tmp_path / "search.db", socket_path=socket_path)
    await daemon.start()
    assert daemon._all_read_workers
    client = CatalogClient(socket_path)

    def slow_search(connection, **_params):
        connection.execute("BEGIN")
        return connection.execute(
            """
            WITH RECURSIVE counter(value) AS (
                SELECT 0 UNION ALL SELECT value + 1 FROM counter WHERE value < 100000000
            )
            SELECT SUM(value) FROM counter
            """
        ).fetchone()

    original_searches = [worker.store.search for worker in daemon._all_read_workers]
    for worker in daemon._all_read_workers:
        worker.store.search = lambda connection=worker.connection, **params: slow_search(connection, **params)
    try:
        with pytest.raises((CatalogRemoteError, CatalogUnavailable)) as timeout:
            await client.call("search.query.v2", _search_params("slow"), timeout_seconds=0.05)
        if isinstance(timeout.value, CatalogRemoteError):
            assert timeout.value.code == "deadline_exceeded"
        for worker, original_search in zip(daemon._all_read_workers, original_searches, strict=True):
            worker.store.search = original_search
        ping = await client.call("search.ping.v2", timeout_seconds=0.2)
        assert ping["ready"] is True
        assert all(worker.connection.in_transaction is False for worker in daemon._all_read_workers)
        for _ in range(20):
            assert daemon._read_workers is not None
            queued_workers = list(daemon._read_workers._queue)
            if len(queued_workers) == len(daemon._all_read_workers):
                break
            await asyncio.sleep(0.01)
        assert sorted(id(worker) for worker in queued_workers) == sorted(id(worker) for worker in daemon._all_read_workers)
    finally:
        for worker, original_search in zip(daemon._all_read_workers, original_searches, strict=True):
            worker.store.search = original_search
        await client.close()
        await daemon.close()
        socket_parent.rmdir()


@pytest.mark.asyncio
async def test_dense_rpc_enforces_space_and_refreshes_after_write_and_delete(tmp_path):
    """The daemon wiring, not a hand-called index load, owns snapshot freshness."""

    socket_parent = Path("/tmp") / f"lhs-{uuid4().hex[:8]}"
    socket_parent.mkdir(mode=0o700)
    socket_path = socket_parent / "s"
    daemon = SearchDaemon(database_path=tmp_path / "search.db", socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    session_id = str(uuid4())
    generation_id = str(uuid4())
    owner_id = "42"
    vector = np.zeros(ACTIVE_EMBEDDING_DIMS, dtype=np.float32)
    vector[0] = 1.0

    def publish_stub() -> None:
        assert daemon._connection is not None
        daemon._connection.execute(
            """
            INSERT INTO session_index(
                session_id, generation_id, owner_id, desired_revision, indexed_through,
                object_count, object_set_hash, event_count, user_messages, assistant_messages,
                tool_calls, is_sidechain, project, provider, environment, cwd, git_repo,
                started_at, published_at
            ) VALUES (?, ?, ?, 1, 1, 1, 'hash', 2, 1, 1, 0, 0,
                      'longhouse', 'codex', 'local', NULL, NULL, ?, ?)
            """,
            (session_id, generation_id, owner_id, "2026-08-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00"),
        )
        daemon._connection.commit()
        assert daemon._dense_index is not None
        daemon._dense_index.load(daemon._connection)

    query_params = {
        "model": ACTIVE_EMBEDDING_MODEL,
        "owner_id": owner_id,
        "dims": ACTIVE_EMBEDDING_DIMS,
        "query_embedding": base64.b64encode(vector.tobytes()).decode("ascii"),
        "limit": 5,
        "project": None,
        "provider": None,
        "environment": None,
        "exclude_environments": None,
        "since_iso": None,
    }
    try:
        await daemon._run(publish_stub)
        # A session that has not been embedded yet makes the corpus less fresh,
        # not wrong. It used to refuse every dense query in the store; now the
        # query runs and the shortfall is reported back to the caller.
        unpublished = await client.call("search.embedding.query.v2", query_params)
        assert unpublished["results"] == []
        assert unpublished["coverage"]["integrity_ready"] is True
        assert unpublished["coverage"]["complete"] is False
        assert unpublished["coverage"]["unpublished_sessions"] == 1
        assert unpublished["coverage"]["missing_session_ids"] == [session_id]

        with pytest.raises(CatalogRemoteError) as mismatch:
            await client.call("search.embedding.query.v2", {**query_params, "model": "wrong-space"})
        assert mismatch.value.code == "embedding_space_mismatch"

        write_params = {
            "session_id": session_id,
            "owner_id": owner_id,
            "generation_id": generation_id,
            "revision": "1",
            "model": ACTIVE_EMBEDDING_MODEL,
            "dims": ACTIVE_EMBEDDING_DIMS,
            "complete": True,
            "desired_episode_ordinals": [0],
            "episodes": [
                {
                    "episode_ordinal": 0,
                    "event_index_start": 0,
                    "event_index_end": 1,
                    "start_order_time_us": 1,
                    "content_hash": "a" * 64,
                    "embedding": base64.b64encode(vector.tobytes()).decode("ascii"),
                }
            ],
        }
        with pytest.raises(CatalogRemoteError) as write_mismatch:
            await client.call("search.embedding.write.v2", {**write_params, "model": "wrong-space"})
        assert write_mismatch.value.code == "embedding_space_mismatch"
        with pytest.raises(CatalogRemoteError) as hashes_mismatch:
            await client.call(
                "search.embedding.hashes.v2",
                {"session_id": session_id, "model": "wrong-space", "dims": ACTIVE_EMBEDDING_DIMS},
            )
        assert hashes_mismatch.value.code == "embedding_space_mismatch"

        await client.call("search.embedding.write.v2", write_params)
        query_result = await client.call("search.embedding.query.v2", query_params)
        assert [row["session_id"] for row in query_result["results"]] == [session_id]
        assert query_result["store_id"] == (await client.call("search.ping.v2"))["store_id"]
        assert query_result["schema_generation"] == SCHEMA_GENERATION
        assert query_result["coverage"] == {
            "integrity_ready": True,
            "complete": True,
            "unpublished_sessions": 0,
            "expected_sessions": 1,
            "published_sessions": 1,
            "expected_episodes": 1,
            "current_episodes": 1,
            "invalid_vectors": 0,
            "unnormalized_vectors": 0,
            "unlocatable_episodes": 0,
            "episode_count_mismatches": 0,
            "missing_session_ids": [],
            "stale": False,
        }

        await client.call("search.session.delete.v2", {"session_id": session_id})
        deleted_result = await client.call("search.embedding.query.v2", query_params)
        assert deleted_result["results"] == []
        assert deleted_result["coverage"]["integrity_ready"] is True
        assert deleted_result["coverage"]["complete"] is True
        assert deleted_result["coverage"]["expected_sessions"] == 0
    finally:
        await client.close()
        await daemon.close()
        socket_parent.rmdir()


@pytest.mark.asyncio
async def test_dense_refresh_coalesces_concurrent_writer_mutations(tmp_path):
    socket_parent = Path("/tmp") / f"lhs-{uuid4().hex[:8]}"
    socket_parent.mkdir(mode=0o700)
    daemon = SearchDaemon(database_path=tmp_path / "search.db", socket_path=socket_parent / "s")
    await daemon.start()
    assert daemon._dense_index is not None
    original_load = daemon._dense_index.load
    loads = 0

    def counted_load(connection):
        nonlocal loads
        loads += 1
        original_load(connection)

    daemon._dense_index.load = counted_load
    try:
        await asyncio.gather(*(daemon._run_with_dense_refresh(lambda value=value: {"value": value}) for value in range(12)))
        assert 1 <= loads < 12
        assert daemon._dense_index.coverage.integrity_ready is True
    finally:
        await daemon.close()
        socket_parent.rmdir()


@pytest.mark.asyncio
async def test_dense_refresh_defers_full_rebuild_while_a_corrupt_session_blocks_it(tmp_path):
    socket_parent = Path("/tmp") / f"lhs-{uuid4().hex[:8]}"
    socket_parent.mkdir(mode=0o700)
    daemon = SearchDaemon(database_path=tmp_path / "search.db", socket_path=socket_parent / "s")
    await daemon.start()
    assert daemon._dense_index is not None
    assert daemon._store is not None
    original_load = daemon._dense_index.load
    loads = 0

    def counted_load(connection):
        nonlocal loads
        loads += 1
        original_load(connection)

    daemon._dense_index.load = counted_load
    daemon._dense_index.invalidate(allow_stale_reads=False)
    daemon._dense_known_unservable = True
    daemon._dense_index._nonrelational_blocking_session_ids = frozenset({"bad-session"})
    try:
        # An unrelated session's write cannot repair a value-level defect, so
        # rebuilding the whole matrix for it only burns CPU behind a shut gate.
        result = await daemon._run_with_dense_refresh(lambda session_id: {"committed": True}, session_id="other-session")
        assert result == {"committed": True}
        assert loads == 0
        assert daemon._dense_index.coverage.integrity_ready is False
        assert daemon._dense_index.coverage.as_dict()["stale"] is True

        # Rewriting the blocking session is the only thing that can clear it.
        assert await daemon._run_with_dense_refresh(lambda session_id: {"committed": True}, session_id="bad-session") == {
            "committed": True
        }
        assert loads == 1
        assert daemon._dense_index.coverage.integrity_ready is True
        assert daemon._dense_known_unservable is False
    finally:
        await daemon.close()
        socket_parent.rmdir()


@pytest.mark.asyncio
async def test_dense_refresh_retries_nonrelational_failure_only_for_blocking_session(tmp_path):
    socket_parent = Path("/tmp") / f"lhs-{uuid4().hex[:8]}"
    socket_parent.mkdir(mode=0o700)
    daemon = SearchDaemon(database_path=tmp_path / "search.db", socket_path=socket_parent / "s")
    await daemon.start()
    assert daemon._dense_index is not None
    assert daemon._store is not None
    original_load = daemon._dense_index.load
    loads = 0

    def counted_load(connection):
        nonlocal loads
        loads += 1
        original_load(connection)

    daemon._dense_index.load = counted_load
    daemon._dense_index.invalidate()
    daemon._dense_index._nonrelational_blocking_session_ids = frozenset({"bad-session"})
    daemon._dense_known_unservable = True
    daemon._store.embedding_snapshot_candidate_complete = lambda **_kwargs: True
    try:
        await daemon._run_with_dense_refresh(lambda session_id: {"session_id": session_id}, session_id="other-session")
        assert loads == 0
        await daemon._run_with_dense_refresh(lambda session_id: {"session_id": session_id}, session_id="bad-session")
        assert loads == 1
        assert daemon._dense_index.coverage.integrity_ready is True
    finally:
        await daemon.close()
        socket_parent.rmdir()


@pytest.mark.asyncio
async def test_dense_refresh_does_not_treat_stale_relational_blockers_as_value_failures(tmp_path):
    socket_parent = Path("/tmp") / f"lhs-{uuid4().hex[:8]}"
    socket_parent.mkdir(mode=0o700)
    daemon = SearchDaemon(database_path=tmp_path / "search.db", socket_path=socket_parent / "s")
    await daemon.start()
    assert daemon._dense_index is not None
    assert daemon._store is not None
    original_load = daemon._dense_index.load
    loads = 0

    def counted_load(connection):
        nonlocal loads
        loads += 1
        original_load(connection)

    daemon._dense_index.load = counted_load
    daemon._dense_index.invalidate()
    daemon._dense_index._blocking_session_ids = frozenset({"old-missing-publication"})
    daemon._dense_index._nonrelational_blocking_session_ids = frozenset()
    daemon._dense_known_unservable = True
    daemon._store.embedding_snapshot_candidate_complete = lambda **_kwargs: True
    try:
        await daemon._run_with_dense_refresh(lambda session_id: {"session_id": session_id}, session_id="new-final-blocker")
        assert loads == 1
        assert daemon._dense_index.coverage.integrity_ready is True
    finally:
        await daemon.close()
        socket_parent.rmdir()


@pytest.mark.asyncio
async def test_dense_refresh_retries_without_reopening_coverage_gate(tmp_path):
    socket_parent = Path("/tmp") / f"lhs-{uuid4().hex[:8]}"
    socket_parent.mkdir(mode=0o700)
    daemon = SearchDaemon(database_path=tmp_path / "search.db", socket_path=socket_parent / "s")
    await daemon.start()
    assert daemon._dense_index is not None
    original_load = daemon._dense_index.load
    attempts = 0

    def transient_load(connection):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("transient test failure")
        original_load(connection)

    daemon._dense_index.load = transient_load
    try:
        result = await daemon._run_with_dense_refresh(lambda: {"committed": True})
        assert result == {"committed": True}
        assert attempts == 2
        assert daemon._dense_index.coverage.integrity_ready is True
    finally:
        await daemon.close()
        socket_parent.rmdir()


@pytest.mark.asyncio
async def test_searchd_close_acknowledges_committed_refresh_waiter(tmp_path):
    socket_parent = Path("/tmp") / f"lhs-{uuid4().hex[:8]}"
    socket_parent.mkdir(mode=0o700)
    daemon = SearchDaemon(database_path=tmp_path / "search.db", socket_path=socket_parent / "s")
    await daemon.start()
    assert daemon._dense_index is not None
    entered = threading.Event()
    release = threading.Event()

    def blocked_load(_connection):
        entered.set()
        assert release.wait(timeout=2)

    daemon._dense_index.load = blocked_load
    mutation = asyncio.create_task(daemon._run_with_dense_refresh(lambda: {"committed": True}))
    close_task: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        close_task = asyncio.create_task(daemon.close())
        assert await mutation == {"committed": True}
        release.set()
        await close_task
    finally:
        release.set()
        await asyncio.gather(mutation, return_exceptions=True)
        if close_task is not None:
            await asyncio.gather(close_task, return_exceptions=True)
        else:
            await daemon.close()
        socket_parent.rmdir()


@pytest.mark.asyncio
async def test_dense_serves_last_validated_snapshot_until_coalesced_refresh_is_published(tmp_path):
    socket_parent = Path("/tmp") / f"lhs-{uuid4().hex[:8]}"
    socket_parent.mkdir(mode=0o700)
    socket_path = socket_parent / "s"
    daemon = SearchDaemon(database_path=tmp_path / "search.db", socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    assert daemon._dense_index is not None
    original_load = daemon._dense_index.load
    entered = threading.Event()
    release = threading.Event()

    def blocked_load(connection):
        entered.set()
        assert release.wait(timeout=2)
        original_load(connection)

    daemon._dense_index.load = blocked_load
    mutation = asyncio.create_task(daemon._run_with_dense_refresh(lambda: {"committed": True}))
    vector = np.zeros(ACTIVE_EMBEDDING_DIMS, dtype=np.float32)
    vector[0] = 1.0
    query = {
        "model": ACTIVE_EMBEDDING_MODEL,
        "owner_id": "42",
        "dims": ACTIVE_EMBEDDING_DIMS,
        "query_embedding": base64.b64encode(vector.tobytes()).decode("ascii"),
        "limit": 5,
        "project": None,
        "provider": None,
        "environment": None,
        "exclude_environments": None,
        "since_iso": None,
    }
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert daemon._dense_index.coverage.integrity_ready is True
        assert daemon._dense_index.coverage.stale is True
        stale = await client.call("search.embedding.query.v2", query)
        assert stale["coverage"]["integrity_ready"] is True
        assert stale["coverage"]["stale"] is True
        release.set()
        assert await mutation == {"committed": True}
        assert daemon._dense_index.coverage.integrity_ready is True
        assert daemon._dense_index.coverage.stale is False
    finally:
        release.set()
        await asyncio.gather(mutation, return_exceptions=True)
        await client.close()
        await daemon.close()
        socket_parent.rmdir()


@pytest.mark.asyncio
async def test_searchd_publishes_only_complete_generations_and_serves_search_worklog(tmp_path):
    socket_parent = Path("/tmp") / f"lhs-{uuid4().hex[:8]}"
    socket_parent.mkdir(mode=0o700)
    socket_path = socket_parent / "s"
    daemon = SearchDaemon(database_path=tmp_path / "search.db", socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    session_id = str(uuid4())
    generation_id = str(uuid4())
    source_epoch = str(uuid4())
    object_id = hashlib.sha256(b"render-object").hexdigest()
    try:
        ping = await client.call("search.ping.v2")
        assert ping["ready"] is True
        assert str(UUID(ping["store_id"])) == ping["store_id"]
        index_params = {
            "session_id": session_id,
            "generation_id": generation_id,
            "object_id": object_id,
            "desired_revision": "7",
            "provider": "codex",
            "machine_id": "cinder",
            "project": "longhouse",
            "environment": "local",
            "cwd": "/workspace/longhouse",
            "git_repo": "cipher982/longhouse",
            "opaque_source_id": "codex/session.jsonl",
            "source_epoch": source_epoch,
            "records": _records("find the speed database"),
        }
        indexed = await client.call("search.index.object.v2", index_params)
        assert indexed["created"] is True
        replay = await client.call("search.index.object.v2", index_params)
        assert replay["exact_replay"] is True
        with pytest.raises(CatalogRemoteError, match="identity conflicts") as conflict:
            await client.call(
                "search.index.object.v2",
                {**index_params, "records": _records("different projection for the same immutable object")},
            )
        assert conflict.value.code == "invalid_request"
        base_publish = {
            "session_id": session_id,
            "generation_id": generation_id,
            "owner_id": "42",
            "desired_revision": "7",
            "event_count": 2,
            "object_set_hash": object_set_hash([object_id]),
            "project": "longhouse",
            "provider": "codex",
            "environment": "local",
            "cwd": "/workspace/longhouse",
            "git_repo": "cipher982/longhouse",
            "started_at": "2026-07-12T12:00:00+00:00",
        }
        lagged = await client.call("search.index.publish.v2", {**base_publish, "object_count": 2})
        assert lagged["projection_lag"] is True
        wrong_set = await client.call(
            "search.index.publish.v2",
            {**base_publish, "object_count": 1, "object_set_hash": hashlib.sha256(b"wrong-set").hexdigest()},
        )
        assert wrong_set["projection_lag"] is True
        assert (await client.call("search.query.v2", _search_params("speed")))["results"] == []
        published = await client.call("search.index.publish.v2", {**base_publish, "object_count": 1})
        assert published["published"] is True
        search = await client.call("search.query.v2", _search_params("speed"))
        assert search["results"][0]["session_id"] == session_id
        assert search["results"][0]["source_object_id"] == object_id
        assert search["results"][0]["record_ordinal"] == 0
        assert "speed" in search["results"][0]["content_snippet"]
        assert "content_text" not in search["results"][0]
        context = await client.call(
            "search.context.v2",
            {
                "owner_id": "42",
                "session_id": session_id,
                "generation_id": generation_id,
                "search_event_id": search["results"][0]["search_event_id"],
                "start_order_time_us": None,
                "context_turns": 1,
            },
        )
        assert context["evidence_status"] == "complete"
        assert context["total_events"] == 2
        assert [item["role"] for item in context["context"]] == ["user", "assistant"]
        filtered = await client.call("search.query.v2", {**_search_params("speed"), "provider": "claude"})
        assert filtered["results"] == []

        with sqlite3.connect(tmp_path / "search.db") as legacy:
            legacy.execute(
                "UPDATE indexed_objects SET projection_hash = ? WHERE object_id = ?",
                (hashlib.sha256(b"legacy-hash-including-session-metadata").hexdigest(), object_id),
            )
        replay_at_new_revision = await client.call(
            "search.index.object.v2",
            {
                **index_params,
                "desired_revision": "8",
                "project": "renamed-longhouse",
                "environment": "hosted",
                "cwd": "/workspace/renamed-longhouse",
                "git_repo": "cipher982/renamed-longhouse",
            },
        )
        assert replay_at_new_revision["exact_replay"] is True
        assert replay_at_new_revision["identity_upgraded"] is True
        exact_replay = await client.call(
            "search.index.object.v2",
            {
                **index_params,
                "desired_revision": "8",
                "project": "renamed-longhouse",
                "environment": "hosted",
                "cwd": "/workspace/renamed-longhouse",
                "git_repo": "cipher982/renamed-longhouse",
            },
        )
        assert exact_replay["identity_upgraded"] is False
        with pytest.raises(CatalogRemoteError, match="identity conflicts"):
            await client.call(
                "search.index.object.v2",
                {
                    **index_params,
                    "desired_revision": "8",
                    "records": _records("different payload after identity upgrade"),
                },
            )
        republished = await client.call(
            "search.index.publish.v2",
            {
                **base_publish,
                "desired_revision": "8",
                "object_count": 1,
                "project": "renamed-longhouse",
                "environment": "hosted",
                "cwd": "/workspace/renamed-longhouse",
                "git_repo": "cipher982/renamed-longhouse",
            },
        )
        assert republished["published"] is True
        renamed = await client.call(
            "search.query.v2",
            {**_search_params("speed"), "project": "renamed-longhouse", "environment": "hosted"},
        )
        assert renamed["results"][0]["session_id"] == session_id
        worklog_sessions = await client.call(
            "worklog.day.v2",
            {
                "owner_id": "42",
                "window_start_us": 1_720_780_399_000_000,
                "window_end_us": 1_720_780_401_000_000,
                "include_test": False,
                "section": "sessions",
                "snapshot_id": None,
                "offset": 0,
                "limit": 100,
            },
        )
        assert worklog_sessions["items"][0]["message_count"] == 2
        assert worklog_sessions["items"][0]["day_event_count"] == 2
        assert worklog_sessions["items"][0]["user_messages"] == 1
        assert worklog_sessions["items"][0]["assistant_messages"] == 1
        assert worklog_sessions["items"][0]["is_sidechain"] == 1
        first_worklog_page = await client.call(
            "worklog.day.v2",
            {
                "owner_id": "42",
                "window_start_us": 1_720_780_399_000_000,
                "window_end_us": 1_720_780_401_000_000,
                "include_test": False,
                "section": "events",
                "snapshot_id": worklog_sessions["snapshot_id"],
                "offset": 0,
                "limit": 1,
            },
        )
        assert [event["role"] for event in first_worklog_page["items"]] == ["user"]
        assert first_worklog_page["has_more"] is True

        replacement_id = hashlib.sha256(b"replacement-render-object").hexdigest()
        replacement_params = {
            **index_params,
            "object_id": replacement_id,
            "desired_revision": "9",
            "records": _records("replacement projection"),
        }
        await client.call("search.index.object.v2", replacement_params)
        # Staging revision 9 must not disturb the fully published revision 8.
        assert len((await client.call("search.query.v2", _search_params("speed")))["results"]) == 1
        assert (await client.call("search.query.v2", _search_params("replacement")))["results"] == []
        replacement_publish = {
            **base_publish,
            "desired_revision": "9",
            "object_set_hash": object_set_hash([replacement_id]),
            "object_count": 1,
        }
        assert (await client.call("search.index.publish.v2", replacement_publish))["published"] is True

        second_worklog_page = await client.call(
            "worklog.day.v2",
            {
                "owner_id": "42",
                "window_start_us": 1_720_780_399_000_000,
                "window_end_us": 1_720_780_401_000_000,
                "include_test": False,
                "section": "events",
                "snapshot_id": first_worklog_page["snapshot_id"],
                "offset": first_worklog_page["next_offset"],
                "limit": 1,
            },
        )
        assert [event["role"] for event in second_worklog_page["items"]] == ["assistant"]
        assert second_worklog_page["has_more"] is False
        released = await client.call(
            "worklog.snapshot.release.v2",
            {"snapshot_id": worklog_sessions["snapshot_id"], "owner_id": "42"},
        )
        assert released["released"] is True
        assert (await client.call("search.query.v2", _search_params("speed")))["results"] == []
        assert len((await client.call("search.query.v2", _search_params("replacement")))["results"]) == 1
    finally:
        await client.close()
        await daemon.close()
        socket_parent.rmdir()


def test_search_reports_whether_ranking_saw_every_match(tmp_path, monkeypatch):
    """Callers must be able to tell exact ranking from a bounded recent window.

    The interactive lane ranks only the most recent candidates. When the walk
    does not saturate it has seen the whole match set, and the ranking is
    exactly what an unbounded scan would produce. Saying so lets an agent
    distinguish "nothing matched" from "I did not look at everything".
    """

    connection = open_search_database(tmp_path / "search.db")
    store = SearchStore(connection)
    now_us = int(datetime.now(UTC).timestamp() * 1_000_000)
    session_id = str(uuid4())
    generation_id = str(uuid4())
    object_id = hashlib.sha256(b"ranking-scope").hexdigest()
    records = [
        {**record, "content_text": "recurring needle text", "order_time_us": now_us + index}
        for index, record in enumerate(_records("recurring needle text"))
    ]
    store.index_object(
        session_id=session_id,
        generation_id=generation_id,
        object_id=object_id,
        desired_revision=1,
        provider="codex",
        machine_id="cinder",
        project="longhouse",
        environment="local",
        cwd="/workspace/longhouse",
        git_repo="cipher982/longhouse",
        opaque_source_id="codex/session.jsonl",
        source_epoch=str(uuid4()),
        records=records,
    )
    store.publish_generation(
        session_id=session_id,
        generation_id=generation_id,
        owner_id="42",
        desired_revision=1,
        object_count=1,
        object_set_hash=object_set_hash([object_id]),
        event_count=len(records),
        project="longhouse",
        provider="codex",
        environment="local",
        cwd="/workspace/longhouse",
        git_repo="cipher982/longhouse",
        started_at=datetime.now(UTC).isoformat(),
    )

    def search(*, include_snippets=True):
        return store.search(
            owner_id="42",
            query="needle",
            project=None,
            provider=None,
            environment=None,
            window_start_us=now_us - 60_000_000,
            window_end_us=None,
            limit=1,
            include_snippets=include_snippets,
        )

    try:
        exact = search()
        assert exact["results"], "expected the published needle to match"
        assert exact["ranking_scope"] == "exact"
        assert "candidate_count" not in exact["results"][0], "internal bookkeeping must not leak to callers"
        without_snippets = search(include_snippets=False)
        assert [row["search_event_id"] for row in without_snippets["results"]] == [row["search_event_id"] for row in exact["results"]]
        assert without_snippets["results"][0]["content_snippet"] is None
        assert without_snippets["results"][0]["tool_output_snippet"] is None

        archive = store.search(
            owner_id="42",
            query="needle",
            project=None,
            provider=None,
            environment=None,
            window_start_us=now_us - int(timedelta(days=365).total_seconds() * 1_000_000),
            window_end_us=None,
            limit=1,
            include_snippets=False,
        )
        assert archive["search_scope"] == "published_archive"
        assert archive["ranking_scope"] == "exact"
        assert [row["search_event_id"] for row in archive["results"]] == [row["search_event_id"] for row in exact["results"]]

        # A ceiling below the match count forces the honest bounded answer.
        monkeypatch.setattr("zerg.searchd.store._CANDIDATE_CEILING", 1)
        assert search()["ranking_scope"] == "recent_bounded"
        assert (
            store.search(
                owner_id="42",
                query="needle",
                project=None,
                provider=None,
                environment=None,
                window_start_us=now_us - int(timedelta(days=365).total_seconds() * 1_000_000),
                window_end_us=None,
                limit=1,
                include_snippets=False,
            )["ranking_scope"]
            == "recent_bounded"
        )
    finally:
        connection.close()


def test_searchd_semantic_projection_hides_claude_control_from_search_and_counts(tmp_path):
    connection = open_search_database(tmp_path / "search.db")
    store = SearchStore(connection)
    session_id = str(uuid4())
    generation_id = str(uuid4())
    object_id = hashlib.sha256(b"claude-semantic-projection").hexdigest()
    now_us = int(datetime.now(UTC).timestamp() * 1_000_000)
    records = [
        {
            "event_id": "command",
            "record_ordinal": 0,
            "order_time_us": now_us,
            "source_position": 0,
            "event_subordinal": 0,
            "role": "user",
            "content_text": "<command-name>/effort</command-name><command-args>high</command-args>",
            "interaction_kind": "local_control",
            "tool_name": None,
            "tool_output_text": None,
            "tool_call_id": None,
            "thread_id": None,
            "branch_kind": None,
        },
        {
            "event_id": "prompt",
            "record_ordinal": 1,
            "order_time_us": now_us + 1,
            "source_position": 1,
            "event_subordinal": 0,
            "role": "user",
            "content_text": "Build the semantic title projection",
            "tool_name": None,
            "tool_output_text": None,
            "tool_call_id": None,
            "thread_id": None,
            "branch_kind": None,
        },
    ]
    try:
        store.index_object(
            session_id=session_id,
            generation_id=generation_id,
            object_id=object_id,
            desired_revision=1,
            provider="claude",
            machine_id="cinder",
            project="longhouse",
            environment="local",
            cwd="/workspace/longhouse",
            git_repo="cipher982/longhouse",
            opaque_source_id="claude/session.jsonl",
            source_epoch=str(uuid4()),
            records=records,
        )
        store.publish_generation(
            session_id=session_id,
            generation_id=generation_id,
            owner_id="42",
            desired_revision=1,
            object_count=1,
            object_set_hash=object_set_hash([object_id]),
            event_count=len(records),
            project="longhouse",
            provider="claude",
            environment="local",
            cwd="/workspace/longhouse",
            git_repo="cipher982/longhouse",
            started_at=datetime.now(UTC).isoformat(),
        )
        aggregate = connection.execute("SELECT user_messages FROM session_index WHERE session_id = ?", (session_id,)).fetchone()
        assert aggregate[0] == 1
        command_results = store.search(
            owner_id="42",
            query="effort",
            project=None,
            provider=None,
            environment=None,
            window_start_us=None,
            window_end_us=None,
            limit=10,
        )
        prompt_results = store.search(
            owner_id="42",
            query="semantic title projection",
            project=None,
            provider=None,
            environment=None,
            window_start_us=None,
            window_end_us=None,
            limit=10,
        )
        assert command_results["results"] == []
        assert [row["event_id"] for row in prompt_results["results"]] == ["prompt"]
        assert connection.execute("SELECT COUNT(*) FROM events WHERE session_id = ?", (session_id,)).fetchone()[0] == 2
    finally:
        connection.close()


def test_searchd_replays_late_semantic_correction_without_identity_conflict(tmp_path):
    connection = open_search_database(tmp_path / "search-semantic-repair.db")
    store = SearchStore(connection)
    session_id = str(uuid4())
    generation_id = str(uuid4())
    object_id = hashlib.sha256(b"claude-late-semantic-repair").hexdigest()
    source_epoch = str(uuid4())
    now_us = int(datetime.now(UTC).timestamp() * 1_000_000)
    initial_records = [
        {
            "event_id": "command",
            "record_ordinal": 0,
            "order_time_us": now_us,
            "source_position": 0,
            "event_subordinal": 0,
            "role": "user",
            "content_text": "<command-name>/effort</command-name>",
            "interaction_kind": "durable_user_message",
            "tool_name": None,
            "tool_output_text": None,
            "tool_call_id": None,
            "thread_id": None,
            "branch_kind": None,
        }
    ]
    corrected_records = [{**initial_records[0], "interaction_kind": "local_control"}]

    def index_and_publish(records: list[dict], revision: int) -> None:
        store.index_object(
            session_id=session_id,
            generation_id=generation_id,
            object_id=object_id,
            desired_revision=revision,
            provider="claude",
            machine_id="cinder",
            project="longhouse",
            environment="local",
            cwd="/workspace/longhouse",
            git_repo="cipher982/longhouse",
            opaque_source_id="claude/session.jsonl",
            source_epoch=source_epoch,
            records=records,
        )
        store.publish_generation(
            session_id=session_id,
            generation_id=generation_id,
            owner_id="42",
            desired_revision=revision,
            object_count=1,
            object_set_hash=object_set_hash([object_id]),
            event_count=1,
            project="longhouse",
            provider="claude",
            environment="local",
            cwd="/workspace/longhouse",
            git_repo="cipher982/longhouse",
            started_at=datetime.now(UTC).isoformat(),
        )

    try:
        index_and_publish(initial_records, 1)
        assert store.search(**_search_params("effort"))["results"]
        assert connection.execute("SELECT user_messages FROM session_index WHERE session_id = ?", (session_id,)).fetchone()[0] == 1

        index_and_publish(corrected_records, 2)
        assert store.search(**_search_params("effort"))["results"] == []
        assert connection.execute("SELECT user_messages FROM session_index WHERE session_id = ?", (session_id,)).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM events WHERE source_object_id = ?", (object_id,)).fetchone()[0] == 1
    finally:
        connection.close()


def test_searchd_searches_only_published_recent_events_and_falls_back_for_archive(tmp_path):
    connection = open_search_database(tmp_path / "search.db")
    store = SearchStore(connection)
    now_us = int(datetime.now(UTC).timestamp() * 1_000_000)

    def publish(*, session_id: str, generation_id: str, object_id: str, revision: int, text: str, order_time_us: int):
        source_epoch = str(uuid4())
        records = [
            {
                **record,
                "content_text": text if index == 0 else record["content_text"],
                "order_time_us": order_time_us + index,
            }
            for index, record in enumerate(_records(text))
        ]
        store.index_object(
            session_id=session_id,
            generation_id=generation_id,
            object_id=object_id,
            desired_revision=revision,
            provider="codex",
            machine_id="cinder",
            project="longhouse",
            environment="local",
            cwd="/workspace/longhouse",
            git_repo="cipher982/longhouse",
            opaque_source_id="codex/session.jsonl",
            source_epoch=source_epoch,
            records=records,
        )
        return store.publish_generation(
            session_id=session_id,
            generation_id=generation_id,
            owner_id="42",
            desired_revision=revision,
            object_count=1,
            object_set_hash=object_set_hash([object_id]),
            event_count=len(records),
            project="longhouse",
            provider="codex",
            environment="local",
            cwd="/workspace/longhouse",
            git_repo="cipher982/longhouse",
            started_at=datetime.now(UTC).isoformat(),
        )

    def search(query: str, *, window_start_us: int):
        return store.search(
            owner_id="42",
            query=query,
            project=None,
            provider=None,
            environment=None,
            window_start_us=window_start_us,
            window_end_us=None,
            limit=10,
        )

    session_id = str(uuid4())
    first_generation = str(uuid4())
    first_object = hashlib.sha256(b"published-recent").hexdigest()
    assert (
        publish(
            session_id=session_id,
            generation_id=first_generation,
            object_id=first_object,
            revision=1,
            text="published recent recall needle",
            order_time_us=now_us,
        )["published"]
        is True
    )
    recent = search("published recent recall needle", window_start_us=now_us - 60_000_000)
    assert recent["search_scope"] == "published_recent"
    assert [row["session_id"] for row in recent["results"]] == [session_id]
    normal_window = search(
        "published recent recall needle",
        window_start_us=int((datetime.now(UTC) - timedelta(days=90)).timestamp() * 1_000_000),
    )
    assert normal_window["search_scope"] == "published_recent"
    assert [row["session_id"] for row in normal_window["results"]] == [session_id]

    staged_object = hashlib.sha256(b"staged-replacement").hexdigest()
    staged_generation = str(uuid4())
    store.index_object(
        session_id=session_id,
        generation_id=staged_generation,
        object_id=staged_object,
        desired_revision=2,
        provider="codex",
        machine_id="cinder",
        project="longhouse",
        environment="local",
        cwd="/workspace/longhouse",
        git_repo="cipher982/longhouse",
        opaque_source_id="codex/session.jsonl",
        source_epoch=str(uuid4()),
        records=[
            {**record, "content_text": "staged replacement needle", "order_time_us": now_us + index}
            for index, record in enumerate(_records("staged"))
        ],
    )
    assert search("staged replacement needle", window_start_us=now_us - 60_000_000)["results"] == []

    assert (
        store.publish_generation(
            session_id=session_id,
            generation_id=staged_generation,
            owner_id="42",
            desired_revision=2,
            object_count=1,
            object_set_hash=object_set_hash([staged_object]),
            event_count=2,
            project="longhouse",
            provider="codex",
            environment="local",
            cwd="/workspace/longhouse",
            git_repo="cipher982/longhouse",
            started_at=datetime.now(UTC).isoformat(),
        )["published"]
        is True
    )
    assert search("published recent recall needle", window_start_us=now_us - 60_000_000)["results"] == []
    assert {row["session_id"] for row in search("staged replacement needle", window_start_us=now_us - 60_000_000)["results"]} == {
        session_id
    }

    archived_session = str(uuid4())
    archived_generation = str(uuid4())
    archived_object = hashlib.sha256(b"archived-recall").hexdigest()
    old_us = int((datetime.now(UTC) - timedelta(days=100)).timestamp() * 1_000_000)
    assert (
        publish(
            session_id=archived_session,
            generation_id=archived_generation,
            object_id=archived_object,
            revision=1,
            text="archived recall needle",
            order_time_us=old_us,
        )["published"]
        is True
    )
    archive = search("archived recall needle", window_start_us=old_us - 60_000_000)
    assert archive["search_scope"] == "published_archive"
    assert [row["session_id"] for row in archive["results"]] == [archived_session]
    assert connection.execute("SELECT COUNT(*) FROM searchable_events WHERE session_id = ?", (archived_session,)).fetchone()[0] == 0
    assert search("archived recall needle", window_start_us=now_us - 60_000_000)["results"] == []


def test_searchd_upgrades_legacy_empty_object_for_same_subject_only(tmp_path):
    connection = open_search_database(tmp_path / "search.db")
    store = SearchStore(connection)
    session_id = str(uuid4())
    generation_id = str(uuid4())
    object_id = hashlib.sha256(b"empty-render-object").hexdigest()
    source_epoch = str(uuid4())
    params = {
        "session_id": session_id,
        "generation_id": generation_id,
        "object_id": object_id,
        "desired_revision": 1,
        "provider": "claude",
        "machine_id": "cinder",
        "project": "longhouse",
        "environment": "local",
        "cwd": "/workspace/longhouse",
        "git_repo": "cipher982/longhouse",
        "opaque_source_id": "claude/session.jsonl",
        "source_epoch": source_epoch,
        "records": [],
    }
    try:
        assert store.index_object(**params)["created"] is True
        connection.execute(
            "UPDATE indexed_objects SET projection_hash = ? WHERE object_id = ?",
            (hashlib.sha256(b"legacy-empty-object-hash").hexdigest(), object_id),
        )
        upgraded = store.index_object(**{**params, "desired_revision": 2, "project": "renamed-longhouse"})
        assert upgraded["identity_upgraded"] is True
        assert store.index_object(**{**params, "desired_revision": 2})["identity_upgraded"] is False
        with pytest.raises(ValueError, match="identity conflicts"):
            store.index_object(**{**params, "session_id": str(uuid4()), "desired_revision": 3})
    finally:
        connection.close()
