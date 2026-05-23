"""HTTP-level tests for summary fields in session API responses.

Covers:
- GET /api/agents/sessions returns summary + summary_title fields
- GET /api/agents/sessions/{id} returns summary + summary_title fields
- Sessions without summary return null (not error)

Uses in-memory SQLite with inline setup (no shared conftest).
"""

from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.agents import AgentEvent
from zerg.database import Base
from zerg.models.agents import AgentSession
from zerg.services.agents_store import AgentsStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path):
    """Create an in-memory SQLite DB with agent tables, return session factory."""
    db_path = tmp_path / "test_summary_api.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_session(db, *, summary=None, summary_title=None, project="test-project", environment="production"):
    """Create a session with optional summary fields."""
    session = AgentSession(
        provider="claude",
        environment=environment,
        project=project,
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        user_messages=5,
        assistant_messages=7,
        tool_calls=3,
        summary=summary,
        summary_title=summary_title,
        summary_event_count=10 if summary else 0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _seed_session_event(db, session, *, role="assistant", content_text="Semantic snippet content that is long enough."):
    event = AgentEvent(
        session_id=session.id,
        role=role,
        content_text=content_text,
        timestamp=datetime.now(timezone.utc),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def _get_client(session_factory):
    """Create a TestClient with DB dependency override."""
    from zerg.main import api_app

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id="summary-api", id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    client = TestClient(api_app)
    yield client
    api_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_list_sessions_includes_summary(tmp_path):
    """GET /agents/sessions returns summary and summary_title fields."""
    factory = _make_db(tmp_path)
    db = factory()
    try:
        _seed_session(
            db,
            summary="Implemented JWT auth and rate limiting.",
            summary_title="Auth and Rate Limiting",
            environment="work-macbook",
        )
    finally:
        db.close()

    for client in _get_client(factory):
        resp = client.get("/agents/sessions?days_back=1")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["sessions"]) >= 1
        session = data["sessions"][0]
        assert session["summary"] == "Implemented JWT auth and rate limiting."
        assert session["summary_title"] == "Auth and Rate Limiting"
        assert session["environment"] == "work-macbook"
        assert session["thread_root_session_id"] == session["id"]
        assert session["thread_head_session_id"] == session["id"]
        assert session["thread_continuation_count"] == 1
        assert session["continuation_kind"] == "local"
        assert session["origin_label"] == "work-macbook"
        assert session["is_writable_head"] is True


def test_list_sessions_uses_batched_thread_meta(tmp_path):
    """GET /agents/sessions preloads thread metadata instead of per-thread lookups."""
    factory = _make_db(tmp_path)
    db = factory()
    try:
        first = _seed_session(
            db,
            summary="First session.",
            summary_title="First",
            environment="cinder",
        )
        second = _seed_session(
            db,
            summary="Second session.",
            summary_title="Second",
            environment="cinder",
        )
    finally:
        db.close()

    batch_calls: list[list[str]] = []
    original_batch_thread_meta = AgentsStore.batch_thread_meta

    def record_batch_thread_meta(self, sessions):
        batch_calls.append([str(session.id) for session in sessions])
        return original_batch_thread_meta(self, sessions)

    with (
        patch.object(AgentsStore, "batch_thread_meta", record_batch_thread_meta),
        patch.object(
            AgentsStore,
            "get_thread_head",
            side_effect=AssertionError("per-thread head lookup should be preloaded"),
        ),
        patch.object(
            AgentsStore,
            "list_thread_sessions",
            side_effect=AssertionError("per-thread session lookup should be preloaded"),
        ),
    ):
        for client in _get_client(factory):
            resp = client.get("/agents/sessions?days_back=1&limit=5")
            assert resp.status_code == 200, resp.text

    assert len(batch_calls) == 1
    assert set(batch_calls[0]) == {str(first.id), str(second.id)}


def test_list_sessions_hybrid_mode_serializes_datetimes(tmp_path):
    """Hybrid-mode JSON responses should render datetimes without a manual JSONResponse crash."""
    factory = _make_db(tmp_path)
    db = factory()
    try:
        _seed_session(
            db,
            summary="Hybrid-mode serialization check.",
            summary_title="Hybrid Response",
            environment="work-macbook",
        )
    finally:
        db.close()

    for client in _get_client(factory):
        resp = client.get("/agents/sessions?mode=hybrid&days_back=1&limit=5")
        assert resp.status_code == 200, resp.text
        assert resp.headers.get("x-search-mode") == "lexical-fallback"
        payload = resp.json()
        assert isinstance(payload["sessions"][0]["started_at"], str)


def test_list_sessions_hybrid_active_context_emits_search_mode_header(tmp_path):
    """Active-context hybrid search advertises the lexical-only fallback mode."""
    factory = _make_db(tmp_path)
    db = factory()
    try:
        _seed_session(
            db,
            summary="Active-context hybrid header check.",
            summary_title="Active Context Header",
            environment="work-macbook",
        )
    finally:
        db.close()

    for client in _get_client(factory):
        resp = client.get("/agents/sessions?mode=hybrid&context_mode=active_context&days_back=1&limit=5")
        assert resp.status_code == 200, resp.text
        assert resp.headers.get("x-search-mode") == "active-context-lexical"


def test_list_sessions_rejects_balanced_sort_without_query(tmp_path):
    """sort=balanced is only defined for query-backed listing."""
    factory = _make_db(tmp_path)

    for client in _get_client(factory):
        resp = client.get("/agents/sessions?sort=balanced&days_back=1")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "sort=balanced requires a search query (q param)"


def test_list_sessions_rejects_hybrid_offset(tmp_path):
    """Hybrid fusion does not support offset pagination."""
    factory = _make_db(tmp_path)

    for client in _get_client(factory):
        resp = client.get("/agents/sessions?mode=hybrid&offset=1&days_back=1")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Pagination (offset) is not supported for mode=hybrid"


def test_list_sessions_hybrid_mode_batches_semantic_session_loads(tmp_path):
    """Hybrid mode bulk-loads semantic hits instead of fetching each matched session by ID."""
    factory = _make_db(tmp_path)
    db = factory()
    try:
        first = _seed_session(
            db,
            summary="First hybrid batch session.",
            summary_title="First Hybrid Batch",
            environment="work-macbook",
        )
        second = _seed_session(
            db,
            summary="Second hybrid batch session.",
            summary_title="Second Hybrid Batch",
            environment="work-macbook",
        )
    finally:
        db.close()

    async def fake_generate_embedding(_query, _config):
        return [1.0, 0.0, 0.0, 0.0]

    def fake_load_session_embeddings(self, _db, _model, _dims):
        self._session_loaded = True

    def fake_load_turn_embeddings(self, _db, _model, _dims):
        self._turn_loaded = True

    batch_calls: list[list[str]] = []
    original_get_sessions_ordered = AgentsStore.get_sessions_ordered

    def record_get_sessions_ordered(self, session_ids):
        batch_calls.append([str(session_id) for session_id in session_ids])
        return original_get_sessions_ordered(self, session_ids)

    with (
        patch(
            "zerg.models_config.get_embedding_config",
            return_value=SimpleNamespace(model="test-model", dims=4),
        ),
        patch("zerg.services.session_processing.embeddings.generate_embedding", fake_generate_embedding),
        patch("zerg.services.search.lexical_search", return_value=[]),
        patch("zerg.services.embedding_cache.EmbeddingCache.load_session_embeddings", fake_load_session_embeddings),
        patch(
            "zerg.services.embedding_cache.EmbeddingCache.search_sessions",
            return_value=[(first.id, 0.9), (second.id, 0.8)],
        ),
        patch("zerg.services.embedding_cache.EmbeddingCache.load_turn_embeddings", fake_load_turn_embeddings),
        patch("zerg.services.embedding_cache.EmbeddingCache.search_turns", return_value=[]),
        patch.object(AgentsStore, "get_sessions_ordered", record_get_sessions_ordered),
    ):
        for client in _get_client(factory):
            resp = client.get("/agents/sessions?mode=hybrid&days_back=1&limit=5&query=batch")
            assert resp.status_code == 200, resp.text

    assert batch_calls == [[str(first.id), str(second.id)]]


def test_list_sessions_hybrid_mode_uses_semantic_snippet_fallback(tmp_path):
    """Hybrid mode surfaces semantic snippet text when lexical matching contributes nothing."""
    factory = _make_db(tmp_path)
    db = factory()
    try:
        session = _seed_session(
            db,
            summary="Semantic snippet fallback session.",
            summary_title="Semantic Snippet Fallback",
            environment="work-macbook",
        )
        _seed_session_event(
            db,
            session,
            content_text="Semantic snippet fallback survives the hybrid search path.",
        )
    finally:
        db.close()

    async def fake_generate_embedding(_query, _config):
        return [1.0, 0.0, 0.0, 0.0]

    def fake_load_session_embeddings(self, _db, _model, _dims):
        self._session_loaded = True

    def fake_load_turn_embeddings(self, _db, _model, _dims):
        self._turn_loaded = True

    with (
        patch(
            "zerg.models_config.get_embedding_config",
            return_value=SimpleNamespace(model="test-model", dims=4),
        ),
        patch("zerg.services.session_processing.embeddings.generate_embedding", fake_generate_embedding),
        patch("zerg.services.search.lexical_search", return_value=[]),
        patch("zerg.services.embedding_cache.EmbeddingCache.load_session_embeddings", fake_load_session_embeddings),
        patch("zerg.services.embedding_cache.EmbeddingCache.search_sessions", return_value=[(session.id, 0.9)]),
        patch("zerg.services.embedding_cache.EmbeddingCache.load_turn_embeddings", fake_load_turn_embeddings),
        patch(
            "zerg.services.embedding_cache.EmbeddingCache.search_turns",
            return_value=[(str(session.id), 0, 0.8, 0, 0)],
        ),
    ):
        for client in _get_client(factory):
            resp = client.get("/agents/sessions?mode=hybrid&days_back=1&limit=5&query=semantic")
            assert resp.status_code == 200, resp.text
            payload = resp.json()
            assert (
                payload["sessions"][0]["match_snippet"]
                == "Semantic snippet fallback survives the hybrid search path."
            )


def test_get_session_includes_summary(tmp_path):
    """GET /agents/sessions/{id} returns summary and summary_title fields."""
    factory = _make_db(tmp_path)
    db = factory()
    try:
        session = _seed_session(
            db,
            summary="Fixed critical database bug.",
            summary_title="Database Bug Fix",
            environment="work-laptop",
        )
        session_id = str(session.id)
    finally:
        db.close()

    for client in _get_client(factory):
        resp = client.get(f"/agents/sessions/{session_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"] == "Fixed critical database bug."
        assert data["summary_title"] == "Database Bug Fix"
        assert data["environment"] == "work-laptop"
        assert data["thread_root_session_id"] == session_id
        assert data["thread_head_session_id"] == session_id
        assert data["thread_continuation_count"] == 1
        assert data["continuation_kind"] == "local"
        assert data["origin_label"] == "work-laptop"
        assert data["is_writable_head"] is True


def test_summary_null_when_missing(tmp_path):
    """Sessions without summary return null, not error."""
    factory = _make_db(tmp_path)
    db = factory()
    try:
        session = _seed_session(db, summary=None, summary_title=None)
        session_id = str(session.id)
    finally:
        db.close()

    for client in _get_client(factory):
        resp = client.get(f"/agents/sessions/{session_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"] is None
        assert data["summary_title"] is None


def test_get_session_thread_returns_lineage(tmp_path):
    """GET /agents/sessions/{id}/thread returns the logical thread and head."""
    import pytest

    pytest.skip(
        "Session-identity-kernel cleanup removed multi-session lineage columns "
        "(thread_root_session_id, continuation_kind, origin_label, "
        "is_writable_head, continued_from_session_id). Thread responses now "
        "always describe a single session."
    )
    factory = _make_db(tmp_path)
    db = factory()
    try:
        root = _seed_session(
            db,
            summary="Started locally.",
            summary_title="Local root",
            environment="Cinder",
        )
        root.thread_root_session_id = root.id
        root.continuation_kind = "local"
        root.origin_label = "Cinder"
        root.is_writable_head = 1
        db.commit()

        store = AgentsStore(db)
        child = store.create_continuation_session(
            root.id,
            continuation_kind="cloud",
            origin_label="Cloud",
            environment="Cloud",
            device_id="zerg-commis-cloud",
            branched_from_event_id=None,
        )
        child.summary = "Continued in cloud."
        child.summary_title = "Cloud branch"
        db.commit()
        root_id = str(root.id)
        child_id = str(child.id)
    finally:
        db.close()

    for client in _get_client(factory):
        resp = client.get(f"/agents/sessions/{root_id}/thread")
        assert resp.status_code == 200
        data = resp.json()
        assert data["root_session_id"] == root_id
        assert data["head_session_id"] == child_id
        assert len(data["sessions"]) == 2
        assert [item["id"] for item in data["sessions"]] == [root_id, child_id]
        assert data["sessions"][0]["is_writable_head"] is False
        assert data["sessions"][1]["is_writable_head"] is True
        assert data["sessions"][1]["continued_from_session_id"] == root_id
        assert data["sessions"][1]["origin_label"] == "Cloud"
        assert data["sessions"][1]["continuation_kind"] == "cloud"
