import asyncio
import sqlite3
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import inspect

from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.routers.agents_search import index_recall_sessions
from zerg.routers.agents_search import recall_sessions
from zerg.routers.agents_search import recall_index_status
from zerg.services.retrieval_recall_subprocess import retrieval_recall_payload
from zerg.services.retrieval_index import RetrievalChunk
from zerg.services.retrieval_index import check_fts_integrity
from zerg.services.retrieval_index import connect_retrieval_db
from zerg.services.retrieval_index import connect_retrieval_db_readonly
from zerg.services.retrieval_index import initialize_retrieval_db
from zerg.services.retrieval_index import project_session_chunks
from zerg.services.retrieval_index import rebuild_fts
from zerg.services.retrieval_index import replace_session_chunks
from zerg.services.retrieval_index import resolve_retrieval_db_path
from zerg.services.retrieval_index import search_lexical_chunks
from zerg.services.retrieval_index_jobs import claim_next_recall_index_job
from zerg.services.retrieval_index_jobs import enqueue_recall_index_job
from zerg.services.retrieval_index_jobs import get_active_recall_index_job
from zerg.services.retrieval_index_jobs import get_recall_index_job
from zerg.services.retrieval_index_jobs import requeue_stale_recall_index_jobs
from zerg.services.retrieval_index_jobs import request_recall_index_cancel
from zerg.services.retrieval_index_jobs import run_recall_index_job_once


def _open_index(tmp_path):
    path = tmp_path / "retrieval.db"
    conn = connect_retrieval_db(path)
    initialize_retrieval_db(conn)
    return conn


def _request():
    return SimpleNamespace(state=SimpleNamespace())


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
    started_at: str = "2026-07-08T00:00:00+00:00",
    last_activity_at: str | None = None,
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
        started_at=started_at,
        last_activity_at=last_activity_at,
        content=content,
        intent_text=content if kind == "intent" else None,
        evidence_text=content if kind != "intent" else None,
        structured_text=content,
    )


def _session(**overrides):
    values = {
        "id": "session-1",
        "provider": "codex",
        "environment": "test",
        "project": "longhouse",
        "device_id": "device-1",
        "cwd": "/Users/davidrose/git/zerg/longhouse",
        "git_repo": "cipher982/longhouse",
        "git_branch": "feature/recall-index",
        "started_at": datetime(2026, 7, 8, tzinfo=timezone.utc),
        "last_activity_at": datetime(2026, 7, 8, 0, 1, tzinfo=timezone.utc),
        "transcript_revision": 7,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


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
    assert "recall_index_jobs" in tables


def test_retrieval_db_readonly_connection_cannot_write(tmp_path):
    retrieval_path = tmp_path / "retrieval.db"
    with connect_retrieval_db(retrieval_path) as conn:
        initialize_retrieval_db(conn)

    with connect_retrieval_db_readonly(retrieval_path) as conn:
        assert conn.execute("SELECT count(*) FROM recall_chunks").fetchone()[0] == 0
        try:
            conn.execute("INSERT INTO recall_index_state(key, value_json, updated_at) VALUES('x', '{}', 'now')")
        except sqlite3.OperationalError as exc:
            assert "readonly" in str(exc).lower() or "read-only" in str(exc).lower()
        else:  # pragma: no cover - proves the connection is actually read-only
            raise AssertionError("read-only retrieval connection accepted a write")


def test_retrieval_index_jobs_allow_one_active_job(tmp_path):
    with _open_index(tmp_path) as conn:
        first, first_created = enqueue_recall_index_job(
            conn,
            project=None,
            provider=None,
            since_days=90,
            limit=100,
        )
        second, second_created = enqueue_recall_index_job(
            conn,
            project="longhouse",
            provider=None,
            since_days=30,
            limit=10,
        )

        active = get_active_recall_index_job(conn)

    assert first_created is True
    assert second_created is False
    assert second.id == first.id
    assert active is not None
    assert active.id == first.id


def test_retrieval_index_requeues_stale_running_job(tmp_path):
    with _open_index(tmp_path) as conn:
        queued, _created = enqueue_recall_index_job(
            conn,
            project=None,
            provider=None,
            since_days=90,
            limit=100,
        )
        running = claim_next_recall_index_job(conn)
        assert running is not None
        stale_heartbeat = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        with conn:
            conn.execute(
                "UPDATE recall_index_jobs SET heartbeat_at = ? WHERE id = ?",
                (stale_heartbeat, queued.id),
            )

        requeued = requeue_stale_recall_index_jobs(conn, stale_after_seconds=300)
        job = get_recall_index_job(conn, queued.id)

    assert requeued == 1
    assert job is not None
    assert job.status == "queued"


def test_canceled_recall_index_job_finishes_canceled_without_indexing(tmp_path):
    main_path = tmp_path / "longhouse.db"
    database_url = f"sqlite:///{main_path}"
    engine = make_engine(database_url)
    initialize_database(engine)
    SessionLocal = make_sessionmaker(engine)
    retrieval_path = resolve_retrieval_db_path(database_url)
    assert retrieval_path is not None

    with connect_retrieval_db(retrieval_path) as conn:
        initialize_retrieval_db(conn)
        queued, _created = enqueue_recall_index_job(
            conn,
            project=None,
            provider=None,
            since_days=90,
            limit=100,
        )
        request_recall_index_cancel(conn, queued.id)

    result = run_recall_index_job_once(database_url=database_url, session_factory=SessionLocal)

    assert result is not None
    assert result.status == "canceled"
    assert result.sessions_indexed == 0


def test_recall_index_worker_idle_tick_does_not_create_retrieval_db(tmp_path):
    main_path = tmp_path / "longhouse.db"
    database_url = f"sqlite:///{main_path}"
    engine = make_engine(database_url)
    initialize_database(engine)
    SessionLocal = make_sessionmaker(engine)
    retrieval_path = resolve_retrieval_db_path(database_url)
    assert retrieval_path is not None
    assert not retrieval_path.exists()

    result = run_recall_index_job_once(database_url=database_url, session_factory=SessionLocal)

    assert result is None
    assert not retrieval_path.exists()


def test_project_session_chunks_creates_parent_and_child_evidence():
    chunks = project_session_chunks(
        _session(),
        [
            {
                "id": 1,
                "role": "user",
                "content_text": "Find the recall timeout in server/zerg/routers/agents_search.py",
            },
            {
                "id": 2,
                "role": "tool",
                "tool_name": "exec_command",
                "tool_output_text": "OperationalError while scanning source_lines",
            },
            {
                "id": 3,
                "role": "assistant",
                "content_text": "The issue is the full turn embedding matrix load.",
            },
        ],
    )

    kinds = [chunk.chunk_kind for chunk in chunks]
    assert kinds == ["trace_parent", "intent", "assistant_conclusion", "tool_result"]
    parent = chunks[0]
    assert parent.retrieval_role == "parent"
    assert parent.event_index_start == 0
    assert parent.event_index_end == 2
    assert parent.first_event_id == 1
    assert parent.last_event_id == 3

    children = chunks[1:]
    assert all(chunk.retrieval_role == "child" for chunk in children)
    assert {chunk.parent_chunk_uid for chunk in children} == {parent.chunk_uid}
    assert children[0].intent_text
    assert children[1].evidence_text
    assert "file:server/zerg/routers/agents_search.py" in (children[0].structured_text or "")
    assert "tool:exec_command" in (children[2].structured_text or "")
    assert "error:OperationalError" in (children[2].structured_text or "")
    assert children[0].transcript_revision == 7
    assert children[0].git_branch == "feature/recall-index"


def test_projected_chunks_round_trip_into_search_index(tmp_path):
    session = _session()
    chunks = project_session_chunks(
        session,
        [
            {"id": 1, "role": "user", "content_text": "Need launchctl dogfood refresh command"},
            {"id": 2, "role": "assistant", "content_text": "Run make dogfood-refresh then launchctl kickstart."},
        ],
    )

    with _open_index(tmp_path) as conn:
        replace_session_chunks(conn, session.id, chunks)

        hits = search_lexical_chunks(conn, "kickstart", limit=2)
        assert [hit.chunk_kind for hit in hits] == ["assistant_conclusion"]
        assert hits[0].parent_chunk_id is not None
        assert hits[0].first_event_id == 2


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


def test_lexical_since_filter_uses_recent_activity_for_old_sessions(tmp_path):
    with _open_index(tmp_path) as conn:
        replace_session_chunks(
            conn,
            "session-old-active",
            [
                _chunk(
                    "old-active",
                    session_id="session-old-active",
                    content="ancient started recent activity needle",
                    started_at="2026-01-01T00:00:00+00:00",
                    last_activity_at="2026-07-08T00:00:00+00:00",
                )
            ],
        )
        replace_session_chunks(
            conn,
            "session-old-inactive",
            [
                _chunk(
                    "old-inactive",
                    session_id="session-old-inactive",
                    content="ancient inactive needle",
                    started_at="2026-01-01T00:00:00+00:00",
                    last_activity_at="2026-01-02T00:00:00+00:00",
                )
            ],
        )

        hits = search_lexical_chunks(conn, "ancient", since="2026-07-01T00:00:00+00:00", limit=5)

    assert [hit.chunk_uid for hit in hits] == ["old-active"]


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
        conn.execute("CREATE VIRTUAL TABLE recall_chunks_vocab USING fts5vocab(recall_chunks_fts, 'row')")
        terms = {row[0] for row in conn.execute("SELECT term FROM recall_chunks_vocab")}

        assert "server/zerg/routers/agents_search" in terms
        assert "py" in terms
        assert "--no-verify" in terms
        assert "source_lines" in terms
        assert "feature/recall-index" in terms
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


def test_internal_canary_chunks_are_hidden_by_default(tmp_path):
    with _open_index(tmp_path) as conn:
        replace_session_chunks(
            conn,
            "visible",
            [_chunk("visible", session_id="visible", content="needle", provider="codex", project="longhouse")],
        )
        replace_session_chunks(
            conn,
            "canary",
            [_chunk("canary", session_id="canary", content="needle", provider="canary", project="canary")],
        )

        assert [hit.chunk_uid for hit in search_lexical_chunks(conn, "needle")] == ["visible"]
        assert [hit.chunk_uid for hit in search_lexical_chunks(conn, "needle", hide_internal_canary=False, limit=2)] == [
            "visible",
            "canary",
        ]


def test_recall_fast_path_uses_retrieval_db_without_embedding_cache(monkeypatch, tmp_path):
    main_path = tmp_path / "longhouse.db"
    database_url = f"sqlite:///{main_path}"
    engine = make_engine(f"sqlite:///{main_path}")
    initialize_database(engine)
    SessionLocal = make_sessionmaker(engine)

    retrieval_path = resolve_retrieval_db_path(database_url)
    monkeypatch.setenv("LONGHOUSE_RETRIEVAL_DB_PATH", str(retrieval_path))
    with connect_retrieval_db(retrieval_path) as conn:
        initialize_retrieval_db(conn)
        replace_session_chunks(
            conn,
            "session-1",
            [
                _chunk("parent", role="parent", kind="trace_parent", content="parent trace with timeout"),
                _chunk("child", content="specific timeout evidence", parent_uid="parent"),
            ],
        )

    def fail_embedding_config():
        raise AssertionError("fast retrieval recall must not request embedding config")

    monkeypatch.setattr("zerg.models_config.get_embedding_config", fail_embedding_config)

    response = asyncio.run(
        recall_sessions(
            request=_request(),
            query="timeout",
            project=None,
            provider=None,
            since_days=90,
            max_results=5,
            context_turns=2,
            context_mode="forensic",
            mode="auto",
            database_url=database_url,
            session_factory=SessionLocal,
            _auth=None,
            _single=None,
        )
    )

    assert response.total == 1
    match = response.matches[0]
    assert match.session_id == "session-1"
    assert match.chunk_uid == "child"
    assert match.parent_chunk_id is not None
    assert match.context_text == "parent trace with timeout"
    assert match.context == [
        {
            "index": 0,
            "role": "user",
            "content": "specific timeout evidence",
            "tool_name": None,
            "is_match": True,
        }
    ]
    assert match.diagnostics == {"mode": "lexical", "source": "retrieval_db"}


def test_retrieval_recall_payload_projects_indexed_matches(monkeypatch, tmp_path):
    main_path = tmp_path / "longhouse.db"
    database_url = f"sqlite:///{main_path}"
    retrieval_path = resolve_retrieval_db_path(database_url)
    monkeypatch.setenv("LONGHOUSE_RETRIEVAL_DB_PATH", str(retrieval_path))
    with connect_retrieval_db(retrieval_path) as conn:
        initialize_retrieval_db(conn)
        replace_session_chunks(
            conn,
            "session-1",
            [
                _chunk("parent", role="parent", kind="trace_parent", content="parent trace with timeout"),
                _chunk("child", content="specific timeout evidence", parent_uid="parent"),
            ],
        )

    payload = retrieval_recall_payload(
        database_url,
        query="timeout",
        project=None,
        provider=None,
        since_days=90,
        max_results=5,
        context_turns=2,
        hide_internal_canary=True,
    )

    assert payload is not None
    assert payload["total"] == 1
    assert payload["matches"][0]["session_id"] == "session-1"
    assert payload["matches"][0]["chunk_uid"] == "child"
    assert payload["matches"][0]["context_text"] == "parent trace with timeout"
    assert payload["matches"][0]["diagnostics"] == {"mode": "lexical", "source": "retrieval_db"}


def test_recall_auto_ready_index_miss_does_not_fall_back_to_embeddings(monkeypatch, tmp_path):
    main_path = tmp_path / "longhouse.db"
    database_url = f"sqlite:///{main_path}"
    engine = make_engine(f"sqlite:///{main_path}")
    initialize_database(engine)
    SessionLocal = make_sessionmaker(engine)

    retrieval_path = resolve_retrieval_db_path(database_url)
    monkeypatch.setenv("LONGHOUSE_RETRIEVAL_DB_PATH", str(retrieval_path))
    with connect_retrieval_db(retrieval_path) as conn:
        initialize_retrieval_db(conn)
        replace_session_chunks(conn, "session-1", [_chunk("child", content="specific timeout evidence")])

    def fail_embedding_config():
        raise AssertionError("ready retrieval index misses must not fall back to embeddings in auto mode")

    monkeypatch.setattr("zerg.models_config.get_embedding_config", fail_embedding_config)

    response = asyncio.run(
        recall_sessions(
            request=_request(),
            query="semantic-only-miss",
            project=None,
            provider=None,
            since_days=90,
            max_results=5,
            context_turns=2,
            context_mode="forensic",
            mode="auto",
            database_url=database_url,
            session_factory=SessionLocal,
            _auth=None,
            _single=None,
        )
    )

    assert response.total == 0
    assert response.matches == []


def test_recall_index_endpoint_projects_recent_sessions(monkeypatch, tmp_path):
    main_path = tmp_path / "longhouse.db"
    database_url = f"sqlite:///{main_path}"
    engine = make_engine(f"sqlite:///{main_path}")
    initialize_database(engine)
    SessionLocal = make_sessionmaker(engine)
    session_id = uuid4()
    retrieval_path = resolve_retrieval_db_path(database_url)
    monkeypatch.setenv("LONGHOUSE_RETRIEVAL_DB_PATH", str(retrieval_path))

    with SessionLocal() as db:
        db.add(
            AgentSession(
                id=session_id,
                provider="codex",
                environment="production",
                project="longhouse",
                device_id="device-1",
                cwd="/Users/davidrose/git/zerg/longhouse",
                git_repo="cipher982/longhouse",
                git_branch="feature/recall-index",
                started_at=datetime.now(timezone.utc),
                last_activity_at=datetime.now(timezone.utc),
                user_messages=1,
            )
        )
        db.add(
            AgentEvent(
                session_id=session_id,
                role="user",
                content_text="Find the request timeout middleware note",
                timestamp=datetime.now(timezone.utc),
            )
        )
        db.add(
            AgentEvent(
                session_id=session_id,
                role="assistant",
                content_text="The request timeout middleware was fixed by avoiding whole-session hydration.",
                timestamp=datetime.now(timezone.utc),
            )
        )
        db.commit()

        index_response = asyncio.run(
            index_recall_sessions(
                project=None,
                provider=None,
                since_days=90,
                limit=10,
                database_url=database_url,
                _auth=None,
                _single=None,
            )
        )
        job = run_recall_index_job_once(
            database_url=database_url,
            session_factory=SessionLocal,
        )
        status_response = asyncio.run(recall_index_status(database_url=database_url, _auth=None, _single=None))

        def fail_embedding_config():
            raise AssertionError("indexed recall must not request embedding config")

        monkeypatch.setattr("zerg.models_config.get_embedding_config", fail_embedding_config)
        recall_response = asyncio.run(
            recall_sessions(
                request=_request(),
                query="whole-session hydration",
                project=None,
                provider=None,
                since_days=90,
                max_results=5,
                context_turns=2,
                context_mode="forensic",
                mode="auto",
                database_url=database_url,
                session_factory=SessionLocal,
                _auth=None,
                _single=None,
            )
        )

    assert index_response["status"] == "queued"
    assert index_response["created"] is True
    assert job is not None
    assert job.status == "done"
    assert job.sessions_indexed == 1
    assert job.child_chunk_count > 0
    assert status_response["status"] == "ready"
    assert recall_response.total == 1
    assert recall_response.matches[0].session_id == str(session_id)
    assert recall_response.matches[0].chunk_kind == "assistant_conclusion"
    assert [item["role"] for item in recall_response.matches[0].context] == ["user", "assistant"]


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
