import sqlite3

from sqlalchemy import inspect

from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.services.retrieval_index import RetrievalChunk
from zerg.services.retrieval_index import check_fts_integrity
from zerg.services.retrieval_index import connect_retrieval_db
from zerg.services.retrieval_index import initialize_retrieval_db
from zerg.services.retrieval_index import rebuild_fts
from zerg.services.retrieval_index import replace_session_chunks
from zerg.services.retrieval_index import resolve_retrieval_db_path
from zerg.services.retrieval_index import search_lexical_chunks


def _open_index(tmp_path):
    path = tmp_path / "retrieval.db"
    conn = connect_retrieval_db(path)
    initialize_retrieval_db(conn)
    return conn


def _chunk(
    uid: str,
    *,
    session_id: str = "session-1",
    role: str = "child",
    kind: str = "intent",
    content: str = "needle",
    parent_uid: str | None = None,
    index: int = 0,
    project: str = "longhouse",
    provider: str = "codex",
    environment: str = "test",
) -> RetrievalChunk:
    return RetrievalChunk(
        chunk_uid=uid,
        session_id=session_id,
        parent_chunk_uid=parent_uid,
        chunk_index=index,
        chunk_kind=kind,
        retrieval_role=role,
        event_index_start=index,
        event_index_end=index,
        first_event_id=100 + index,
        last_event_id=100 + index,
        provider=provider,
        project=project,
        environment=environment,
        started_at="2026-07-08T00:00:00+00:00",
        content=content,
        intent_text=content if kind == "intent" else None,
        evidence_text=content if kind != "intent" else None,
        structured_text=content,
    )


def test_retrieval_db_initializes_separately_from_main_db(tmp_path):
    main_path = tmp_path / "longhouse.db"
    main_url = f"sqlite:///{main_path}"
    main_engine = make_engine(main_url)
    initialize_database(main_engine)

    retrieval_path = resolve_retrieval_db_path(main_url)
    assert retrieval_path == tmp_path / "retrieval.db"

    with connect_retrieval_db(retrieval_path) as conn:
        initialize_retrieval_db(conn)

    main_tables = set(inspect(main_engine).get_table_names())
    assert "sessions" in main_tables
    assert "recall_chunks" not in main_tables

    retrieval = sqlite3.connect(str(retrieval_path))
    try:
        tables = {row[0] for row in retrieval.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual')")}
    finally:
        retrieval.close()
    assert "recall_chunks" in tables
    assert "recall_chunks_fts" in tables
    assert "recall_index_state" in tables


def test_child_chunks_are_searchable_but_parent_rows_are_not_primary_hits(tmp_path):
    with _open_index(tmp_path) as conn:
        replace_session_chunks(
            conn,
            "session-1",
            [
                _chunk("parent", role="parent", kind="trace_parent", content="parent-only-needle"),
                _chunk("child", content="child-only-needle"),
            ],
        )

        assert search_lexical_chunks(conn, "parent-only-needle") == []
        hits = search_lexical_chunks(conn, "child-only-needle")
        assert [hit.chunk_uid for hit in hits] == ["child"]
        assert hits[0].parent_chunk_id is None


def test_child_chunks_can_reference_parent_context(tmp_path):
    with _open_index(tmp_path) as conn:
        replace_session_chunks(
            conn,
            "session-1",
            [
                _chunk("parent", role="parent", kind="trace_parent", content="parent context"),
                _chunk("child", content="specific child evidence", parent_uid="parent"),
            ],
        )

        hits = search_lexical_chunks(conn, "specific")
        assert len(hits) == 1
        assert hits[0].chunk_uid == "child"
        assert hits[0].parent_chunk_id is not None


def test_reprojection_removes_stale_fts_rows(tmp_path):
    with _open_index(tmp_path) as conn:
        replace_session_chunks(conn, "session-1", [_chunk("old", content="oldneedle")])
        assert search_lexical_chunks(conn, "oldneedle")

        replace_session_chunks(conn, "session-1", [_chunk("new", content="newneedle")])

        assert search_lexical_chunks(conn, "oldneedle") == []
        assert [hit.chunk_uid for hit in search_lexical_chunks(conn, "newneedle")] == ["new"]
        assert check_fts_integrity(conn) is True


def test_fts_tokenizer_preserves_code_shaped_terms(tmp_path):
    content = (
        "server/zerg/routers/agents_search.py --no-verify source_lines "
        "feature/recall-index OperationalError"
    )
    with _open_index(tmp_path) as conn:
        replace_session_chunks(conn, "session-1", [_chunk("code", content=content)])

        assert search_lexical_chunks(conn, "server/zerg/routers/agents_search.py")
        assert search_lexical_chunks(conn, "--no-verify")
        assert search_lexical_chunks(conn, "source_lines")
        assert search_lexical_chunks(conn, "feature/recall-index")
        assert search_lexical_chunks(conn, "OperationalError")


def test_bm25_ascending_returns_stronger_match_first(tmp_path):
    with _open_index(tmp_path) as conn:
        replace_session_chunks(
            conn,
            "session-1",
            [_chunk("weak", session_id="session-1", content="timeout")],
        )
        replace_session_chunks(
            conn,
            "session-2",
            [_chunk("strong", session_id="session-2", content="timeout timeout timeout")],
        )

        hits = search_lexical_chunks(conn, "timeout", limit=2)
        assert [hit.chunk_uid for hit in hits] == ["strong", "weak"]
        assert hits[0].score < hits[1].score


def test_filters_apply_before_results_return(tmp_path):
    with _open_index(tmp_path) as conn:
        replace_session_chunks(
            conn,
            "session-1",
            [_chunk("codex", session_id="session-1", content="needle", provider="codex", project="longhouse")],
        )
        replace_session_chunks(
            conn,
            "session-2",
            [_chunk("claude", session_id="session-2", content="needle", provider="claude", project="other")],
        )

        assert [hit.chunk_uid for hit in search_lexical_chunks(conn, "needle", provider="codex")] == ["codex"]
        assert [hit.chunk_uid for hit in search_lexical_chunks(conn, "needle", project="other")] == ["claude"]


def test_rebuild_fts_restores_child_rows_only(tmp_path):
    with _open_index(tmp_path) as conn:
        replace_session_chunks(
            conn,
            "session-1",
            [
                _chunk("parent", role="parent", kind="trace_parent", content="parentneedle"),
                _chunk("child", content="childneedle"),
            ],
        )
        conn.execute("DELETE FROM recall_chunks_fts")
        conn.commit()
        assert check_fts_integrity(conn) is False

        rebuild_fts(conn)

        assert check_fts_integrity(conn) is True
        assert search_lexical_chunks(conn, "parentneedle") == []
        assert search_lexical_chunks(conn, "childneedle")
