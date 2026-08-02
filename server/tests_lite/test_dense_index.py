"""The resident index must return exactly what the SQL path returned.

Why this exists: the SQL path filters on owner, project, provider, environment
and recency *before* scoring. A resident matrix that scores everything and
filters the top-k afterwards produces a different, silently smaller answer — the
kind of defect that looks like a ranking opinion rather than a bug. These tests
pin equivalence, not plausibility.
"""

from __future__ import annotations

import os

import numpy as np

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.searchd.dense_index import DenseIndex
from zerg.searchd.store import SearchStore
from zerg.searchd.store import open_search_database

DIMS = 4
MODEL = "test-model"


def _unit(values):
    v = np.array(values, dtype="float32")
    return v / np.linalg.norm(v)


def _seed(connection, rows):
    for session_id, ordinal, vector, owner, project, provider, environment, started in rows:
        connection.execute(
            "INSERT OR REPLACE INTO session_index("
            " session_id, generation_id, owner_id, desired_revision, indexed_through, object_count,"
            " object_set_hash, event_count, user_messages, assistant_messages, tool_calls, is_sidechain,"
            " project, provider, environment, cwd, git_repo, started_at, published_at)"
            " VALUES (?,?,?,1,1,1,'h',1,1,1,0,0,?,?,?,NULL,NULL,?,?)",
            (session_id, "g-" + session_id, owner, project, provider, environment, started, started),
        )
        connection.execute(
            "INSERT INTO episode_embeddings("
            " session_id, owner_id, generation_id, revision, episode_ordinal,"
            " event_index_start, event_index_end, start_order_time_us,"
            " model, dims, content_hash, embedding, updated_at)"
            " VALUES (?,?,?,1,?,0,1,1000,?,?,?,?,'2026-08-01T00:00:00+00:00')",
            (
                session_id,
                owner,
                "g-" + session_id,
                ordinal,
                MODEL,
                DIMS,
                f"h{session_id}{ordinal}",
                _unit(vector).tobytes(),
            ),
        )
    connection.commit()


def _index(tmp_path, rows):
    connection = open_search_database(tmp_path / "search.db")
    SearchStore(connection)  # ensure schema paths run as in production
    _seed(connection, rows)
    index = DenseIndex(model=MODEL, dims=DIMS)
    index.load(connection)
    return index, connection


def test_not_ready_until_loaded():
    """An unloaded index must not look like an index that found nothing."""
    index = DenseIndex(model=MODEL, dims=DIMS)
    assert index.ready is False
    assert index.size == 0


def test_ranks_by_cosine(tmp_path):
    rows = [
        ("s1", 0, [1, 0, 0, 0], "42", "zerg", "claude", "local", "2026-07-01"),
        ("s2", 0, [0, 1, 0, 0], "42", "zerg", "claude", "local", "2026-07-01"),
        ("s3", 0, [0.9, 0.1, 0, 0], "42", "zerg", "claude", "local", "2026-07-01"),
    ]
    index, connection = _index(tmp_path, rows)
    try:
        assert index.ready is True
        hits = index.search(_unit([1, 0, 0, 0]), owner_id="42", limit=3)
        assert [h["session_id"] for h in hits] == ["s1", "s3", "s2"]
        assert hits[0]["score"] > hits[1]["score"] > hits[2]["score"]
    finally:
        connection.close()


def test_owner_scoping_is_not_optional(tmp_path):
    """Another owner's vectors must never be scored, let alone returned."""
    rows = [
        ("s1", 0, [1, 0, 0, 0], "42", "zerg", "claude", "local", "2026-07-01"),
        ("s2", 0, [1, 0, 0, 0], "99", "zerg", "claude", "local", "2026-07-01"),
    ]
    index, connection = _index(tmp_path, rows)
    try:
        hits = index.search(_unit([1, 0, 0, 0]), owner_id="42", limit=10)
        assert [h["session_id"] for h in hits] == ["s1"]
    finally:
        connection.close()


def test_filters_apply_before_top_k(tmp_path):
    """The defect this whole file exists for.

    The nearest three vectors all belong to the excluded environment. Filtering
    a top-3 after scoring would return nothing; filtering before scoring returns
    the best surviving match.
    """
    rows = [
        ("near1", 0, [1, 0, 0, 0], "42", "zerg", "claude", "test", "2026-07-01"),
        ("near2", 0, [0.99, 0.01, 0, 0], "42", "zerg", "claude", "test", "2026-07-01"),
        ("near3", 0, [0.98, 0.02, 0, 0], "42", "zerg", "claude", "test", "2026-07-01"),
        ("far", 0, [0.5, 0.5, 0, 0], "42", "zerg", "claude", "local", "2026-07-01"),
    ]
    index, connection = _index(tmp_path, rows)
    try:
        hits = index.search(_unit([1, 0, 0, 0]), owner_id="42", limit=3, exclude_environments=["test"])
        assert [h["session_id"] for h in hits] == ["far"]
    finally:
        connection.close()


def test_project_provider_and_recency_filters(tmp_path):
    rows = [
        ("keep", 0, [1, 0, 0, 0], "42", "zerg", "claude", "local", "2026-07-20"),
        ("wrongproject", 0, [1, 0, 0, 0], "42", "other", "claude", "local", "2026-07-20"),
        ("wrongprovider", 0, [1, 0, 0, 0], "42", "zerg", "codex", "local", "2026-07-20"),
        ("tooold", 0, [1, 0, 0, 0], "42", "zerg", "claude", "local", "2026-01-01"),
    ]
    index, connection = _index(tmp_path, rows)
    try:
        hits = index.search(
            _unit([1, 0, 0, 0]),
            owner_id="42",
            limit=10,
            project="zerg",
            provider="claude",
            since_iso="2026-07-01",
        )
        assert [h["session_id"] for h in hits] == ["keep"]
    finally:
        connection.close()


def test_reload_reflects_deletes(tmp_path):
    """Vectors are UPSERTed and sessions get deleted; this is not append-only."""
    rows = [
        ("s1", 0, [1, 0, 0, 0], "42", "zerg", "claude", "local", "2026-07-01"),
        ("s2", 0, [0, 1, 0, 0], "42", "zerg", "claude", "local", "2026-07-01"),
    ]
    index, connection = _index(tmp_path, rows)
    try:
        assert index.size == 2
        connection.execute("DELETE FROM episode_embeddings WHERE session_id = 's1'")
        connection.commit()
        index.load(connection)
        assert index.size == 1
        assert [h["session_id"] for h in index.search(_unit([1, 0, 0, 0]), owner_id="42", limit=5)] == ["s2"]
    finally:
        connection.close()


def test_locator_and_generation_ride_along(tmp_path):
    """Hydration needs these; a hit without them cannot fetch its evidence."""
    rows = [("s1", 3, [1, 0, 0, 0], "42", "zerg", "claude", "local", "2026-07-01")]
    index, connection = _index(tmp_path, rows)
    try:
        hit = index.search(_unit([1, 0, 0, 0]), owner_id="42", limit=1)[0]
        assert hit["episode_ordinal"] == 3
        assert hit["generation_id"] == "g-s1"
        assert hit["start_order_time_us"] == 1000
    finally:
        connection.close()
