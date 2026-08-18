"""An empty search must say what it searched.

A zero-hit answer is ambiguous on its own. An agent that receives only
"0 results" has to guess why, and the cheap guess -- "this provider must not
be indexed" -- produced a confident, false claim about a session that had
simply never been ingested. Reporting the scope alongside the zero makes that
guess unnecessary: an agent reading `providers: [claude, codex, ...]` cannot
honestly conclude those providers are missing from the corpus.
"""

from __future__ import annotations

from uuid import uuid4

from zerg.searchd.store import SearchStore
from zerg.searchd.store import open_search_database


def _index_session(
    connection,
    *,
    owner_id: str,
    provider: str,
    started_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO session_index (
            session_id, generation_id, owner_id, desired_revision, indexed_through,
            object_count, object_set_hash, event_count, user_messages, assistant_messages,
            tool_calls, is_sidechain, project, provider, environment, cwd, git_repo,
            started_at, published_at
        ) VALUES (?, ?, ?, 1, 1, 1, 'hash', 2, 1, 1, 0, 0, 'proj', ?, 'local', NULL, NULL, ?, ?)
        """,
        (str(uuid4()), str(uuid4()), owner_id, provider, started_at, started_at),
    )


def test_coverage_reports_indexed_scope(tmp_path):
    connection = open_search_database(tmp_path / "search.db")
    store = SearchStore(connection)
    try:
        _index_session(connection, owner_id="owner-1", provider="claude", started_at="2026-01-05T00:00:00+00:00")
        _index_session(connection, owner_id="owner-1", provider="codex", started_at="2026-03-09T00:00:00+00:00")
        _index_session(connection, owner_id="owner-1", provider="opencode", started_at="2026-02-01T00:00:00+00:00")

        coverage = store.search_coverage(owner_id="owner-1")

        assert coverage["indexed_sessions"] == 3
        # The corpus names the providers it holds, so "opencode is not indexed"
        # is not a conclusion an agent can reach from an empty result.
        assert coverage["providers"] == ["claude", "codex", "opencode"]
        assert coverage["oldest_session_at"] == "2026-01-05T00:00:00+00:00"
        assert coverage["newest_session_at"] == "2026-03-09T00:00:00+00:00"
    finally:
        connection.close()


def test_coverage_is_scoped_to_one_owner(tmp_path):
    connection = open_search_database(tmp_path / "search.db")
    store = SearchStore(connection)
    try:
        _index_session(connection, owner_id="owner-1", provider="claude", started_at="2026-01-05T00:00:00+00:00")
        _index_session(connection, owner_id="owner-2", provider="cursor", started_at="2026-01-06T00:00:00+00:00")

        coverage = store.search_coverage(owner_id="owner-1")

        assert coverage["indexed_sessions"] == 1
        assert coverage["providers"] == ["claude"]
    finally:
        connection.close()


def test_coverage_on_an_empty_corpus_is_honest_not_absent(tmp_path):
    """Zero indexed sessions is a real answer, and distinct from 'unknown'."""

    connection = open_search_database(tmp_path / "search.db")
    store = SearchStore(connection)
    try:
        coverage = store.search_coverage(owner_id="owner-nobody")

        assert coverage["indexed_sessions"] == 0
        assert coverage["providers"] == []
        assert coverage["oldest_session_at"] is None
        assert coverage["newest_session_at"] is None
    finally:
        connection.close()
