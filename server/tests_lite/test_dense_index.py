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
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.searchd.dense_index import ResidentEpisodeIndex
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
            "INSERT OR REPLACE INTO embedding_publications("
            " session_id, model, dims, generation_id, revision, expected_episode_count, completed_at)"
            " VALUES (?,?,?,?,1,1,'2026-08-01T00:00:00+00:00')",
            (session_id, MODEL, DIMS, "g-" + session_id),
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
    index = ResidentEpisodeIndex(model=MODEL, dims=DIMS)
    index.load(connection)
    return index, connection


def _reference_search(
    connection,
    query,
    *,
    owner_id,
    limit,
    project=None,
    provider=None,
    environment=None,
    exclude_environments=None,
    since_iso=None,
):
    """Independent test oracle for the deleted per-request SQL implementation."""

    sql = (
        "SELECT e.session_id, e.embedding FROM episode_embeddings e "
        "JOIN session_index s ON s.session_id=e.session_id "
        "AND s.generation_id=e.generation_id AND s.indexed_through=e.revision "
        "WHERE e.model=? AND e.dims=? AND e.owner_id=? AND s.owner_id=?"
    )
    params = [MODEL, DIMS, owner_id, owner_id]
    if project:
        sql += " AND s.project=?"
        params.append(project)
    if provider:
        sql += " AND s.provider=?"
        params.append(provider)
    if environment:
        sql += " AND s.environment=?"
        params.append(environment)
    if exclude_environments:
        sql += f" AND s.environment NOT IN ({','.join('?' for _ in exclude_environments)})"
        params.extend(exclude_environments)
    if since_iso:
        sql += " AND s.started_at>=?"
        params.append(since_iso)
    rows = connection.execute(sql, params).fetchall()
    ranked = sorted(
        ((float(np.frombuffer(row["embedding"], dtype="float32") @ query), str(row["session_id"])) for row in rows),
        reverse=True,
    )
    return [session_id for _score, session_id in ranked[:limit]]


def test_not_ready_until_loaded():
    """An unloaded index must not look like an index that found nothing."""
    index = ResidentEpisodeIndex(model=MODEL, dims=DIMS)
    assert index.ready is False
    assert index.size == 0
    with pytest.raises(RuntimeError, match="not loaded"):
        index.search(_unit([1, 0, 0, 0]), owner_id="42", limit=1)


def test_coverage_requires_a_current_complete_publication(tmp_path):
    index, connection = _index(
        tmp_path,
        [("s1", 0, [1, 0, 0, 0], "42", "zerg", "claude", "local", "2026-07-01")],
    )
    try:
        assert index.coverage.ready is True
        connection.execute("DELETE FROM embedding_publications WHERE session_id = 's1'")
        connection.commit()
        index.load(connection)
        assert index.coverage.ready is False
        assert index.coverage.missing_session_ids == ("s1",)
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("mutation", "field"),
    [
        ("UPDATE episode_embeddings SET start_order_time_us = NULL", "unlocatable_episodes"),
        (
            "UPDATE episode_embeddings SET embedding = zeroblob(16)",
            "invalid_vectors",
        ),
    ],
)
def test_coverage_rejects_invalid_episode_rows(tmp_path, mutation, field):
    index, connection = _index(
        tmp_path,
        [("s1", 0, [1, 0, 0, 0], "42", "zerg", "claude", "local", "2026-07-01")],
    )
    try:
        connection.execute(mutation)
        connection.commit()
        index.load(connection)
        assert index.coverage.ready is False
        assert getattr(index.coverage, field) == 1
    finally:
        connection.close()


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


def test_superseded_generation_cannot_rank(tmp_path):
    """A vector from an old generation must not occupy top-k.

    Joining vectors to sessions by id alone let a superseded embedding rank and
    then fail to hydrate — the worst combination, because it displaces a current
    vector with a hit that cannot produce evidence. The fence is on the
    published generation, so a stale row is invisible rather than merely
    deprioritised.
    """
    rows = [("s1", 0, [1, 0, 0, 0], "42", "zerg", "claude", "local", "2026-07-01")]
    index, connection = _index(tmp_path, rows)
    try:
        assert index.size == 1
        # The session republishes under a new generation; the vector still
        # points at the old one.
        connection.execute("UPDATE session_index SET generation_id = 'g-new' WHERE session_id = 's1'")
        connection.commit()
        index.load(connection)
        assert index.size == 0
        assert index.search(_unit([1, 0, 0, 0]), owner_id="42", limit=5) == []
    finally:
        connection.close()


def test_superseded_revision_cannot_rank(tmp_path):
    """Generation identity alone is insufficient when one generation republishes."""

    rows = [("s1", 0, [1, 0, 0, 0], "42", "zerg", "claude", "local", "2026-07-01")]
    index, connection = _index(tmp_path, rows)
    try:
        assert index.size == 1
        connection.execute("UPDATE session_index SET indexed_through = 2, desired_revision = 2 WHERE session_id = 's1'")
        connection.commit()
        index.load(connection)
        assert index.size == 0
    finally:
        connection.close()


@pytest.mark.parametrize(
    "query",
    [
        np.array([1, 0, 0], dtype="float32"),
        np.array([1, 0, 0, 0, 0], dtype="float32"),
        np.array([0, 0, 0, 0], dtype="float32"),
        np.array([np.nan, 0, 0, 0], dtype="float32"),
    ],
)
def test_query_vector_must_match_the_space(query, tmp_path):
    index, connection = _index(
        tmp_path,
        [("s1", 0, [1, 0, 0, 0], "42", "zerg", "claude", "local", "2026-07-01")],
    )
    try:
        with pytest.raises(ValueError):
            index.search(query, owner_id="42", limit=1)
    finally:
        connection.close()


def test_matches_the_sql_path_on_random_corpora(tmp_path):
    """Equivalence against the implementation this replaces, not against my expectations.

    Hand-written expectations only prove the resident index agrees with what I
    imagined; they cannot catch a filter I translated wrongly. This runs both
    implementations over the same generated corpus and every filter combination.
    """
    import itertools
    import random

    rng = random.Random(20260801)
    projects = ["zerg", "other"]
    providers = ["claude", "codex"]
    environments = ["local", "test", "development"]
    rows = []
    for n in range(60):
        rows.append(
            (
                f"s{n}",
                0,
                [rng.random() for _ in range(DIMS)],
                "42" if n % 5 else "99",
                projects[n % 2],
                providers[n % 2],
                environments[n % 3],
                f"2026-0{1 + n % 7}-01",
            )
        )
    index, connection = _index(tmp_path, rows)
    try:
        query = _unit([rng.random() for _ in range(DIMS)])
        for project, provider, environment, excluded, since in itertools.product(
            [None, "zerg"],
            [None, "claude"],
            [None, "local", "development"],
            [None, ["test"], ["test", "development"]],
            [None, "2026-04-01"],
        ):
            resident = index.search(
                query,
                owner_id="42",
                limit=5,
                project=project,
                provider=provider,
                environment=environment,
                exclude_environments=excluded,
                since_iso=since,
            )
            reference = _reference_search(
                connection,
                query,
                owner_id="42",
                limit=5,
                project=project,
                provider=provider,
                environment=environment,
                exclude_environments=excluded,
                since_iso=since,
            )
            assert [r["session_id"] for r in resident] == reference, (
                f"divergence for project={project} provider={provider} environment={environment} excluded={excluded} since={since}"
            )
    finally:
        connection.close()
