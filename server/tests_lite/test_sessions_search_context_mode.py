from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.services.agents import AgentsStore
from zerg.services.agents import EventIngest
from zerg.services.agents import SessionIngest

BASE_TS = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=1)


def _ts(second: int) -> datetime:
    return BASE_TS + timedelta(seconds=second)


def _make_db(tmp_path):
    db_path = tmp_path / "sessions_context_mode.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _get_client(session_factory):
    from zerg.main import api_app

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id="context-mode", id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    client = TestClient(api_app)
    yield client
    api_app.dependency_overrides.clear()


def _seed_compacted_session(factory):
    db = factory()
    try:
        store = AgentsStore(db)
        source_path = "/tmp/context-mode-session.jsonl"
        store.ingest_session(
            SessionIngest(
                provider="claude",
                environment="production",
                project="zerg",
                device_id="dev-machine",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                started_at=_ts(0),
                events=[
                    EventIngest(
                        role="user",
                        content_text="My favorite color is yellow.",
                        timestamp=_ts(1),
                        source_path=source_path,
                        source_offset=100,
                        raw_json='{"type":"user","timestamp":"2026-01-01T00:00:01Z"}',
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="Noted.",
                        timestamp=_ts(2),
                        source_path=source_path,
                        source_offset=200,
                        raw_json='{"type":"assistant","timestamp":"2026-01-01T00:00:02Z"}',
                    ),
                    EventIngest(
                        role="system",
                        content_text="Conversation compacted [trigger=auto]",
                        timestamp=_ts(3),
                        source_path=source_path,
                        source_offset=300,
                        raw_json='{"type":"system","subtype":"compact_boundary","timestamp":"2026-01-01T00:00:03Z"}',
                    ),
                    EventIngest(
                        role="user",
                        content_text="Please continue the migration.",
                        timestamp=_ts(4),
                        source_path=source_path,
                        source_offset=400,
                        raw_json='{"type":"user","timestamp":"2026-01-01T00:00:04Z"}',
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="Continuing migration now.",
                        timestamp=_ts(5),
                        source_path=source_path,
                        source_offset=500,
                        raw_json='{"type":"assistant","timestamp":"2026-01-01T00:00:05Z"}',
                    ),
                    # Rewind-like stale row after compaction: newer timestamp, older offset.
                    EventIngest(
                        role="user",
                        content_text="yellow stale rewind branch",
                        timestamp=_ts(6),
                        source_path=source_path,
                        source_offset=150,
                        raw_json='{"type":"user","timestamp":"2026-01-01T00:00:06Z"}',
                    ),
                ],
                source_lines=[],
            )
        )
    finally:
        db.close()


def _seed_simple_session(factory, name: str, *, provider: str = "claude") -> str:
    db = factory()
    try:
        store = AgentsStore(db)
        result = store.ingest_session(
            SessionIngest(
                provider=provider,
                environment="production",
                project="zerg",
                device_id=f"{name}-device",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                started_at=_ts(10),
                events=[
                    EventIngest(
                        role="user",
                        content_text=f"{name} question",
                        timestamp=_ts(11),
                        source_path=f"/tmp/{name}.jsonl",
                        source_offset=0,
                        raw_json='{"type":"user"}',
                    ),
                    EventIngest(
                        role="assistant",
                        content_text=f"{name} answer",
                        timestamp=_ts(12),
                        source_path=f"/tmp/{name}.jsonl",
                        source_offset=1,
                        raw_json='{"type":"assistant"}',
                    ),
                ],
            )
        )
        return str(result.session_id)
    finally:
        db.close()


def test_sessions_query_context_mode_filters_pre_compaction_matches(tmp_path):
    factory = _make_db(tmp_path)
    _seed_compacted_session(factory)

    for client in _get_client(factory):
        forensic = client.get(
            "/agents/sessions",
            params={"query": "yellow", "days_back": 90, "context_mode": "forensic"},
        )
        assert forensic.status_code == 200
        forensic_data = forensic.json()
        assert forensic_data["total"] == 1
        assert "yellow" in (forensic_data["sessions"][0]["match_snippet"] or "").lower()

        active = client.get(
            "/agents/sessions",
            params={"query": "yellow", "days_back": 90, "context_mode": "active_context"},
        )
        assert active.status_code == 200
        active_data = active.json()
        assert active_data["total"] == 0


def test_context_mode_validation_on_search_endpoints(tmp_path):
    factory = _make_db(tmp_path)

    for client in _get_client(factory):
        bad_sessions = client.get("/agents/sessions", params={"query": "test", "context_mode": "bad"})
        assert bad_sessions.status_code == 400
        assert "context_mode" in bad_sessions.json()["detail"]

        bad_semantic = client.get("/agents/sessions/semantic", params={"query": "test", "context_mode": "bad"})
        assert bad_semantic.status_code == 400
        assert "context_mode" in bad_semantic.json()["detail"]

        bad_recall = client.get("/agents/recall", params={"query": "test", "context_mode": "bad"})
        assert bad_recall.status_code == 400
        assert "context_mode" in bad_recall.json()["detail"]


def test_semantic_search_batches_thread_meta_for_result_set(tmp_path):
    factory = _make_db(tmp_path)
    first_id = _seed_simple_session(factory, "first")
    second_id = _seed_simple_session(factory, "second")

    batch_calls: list[list[str]] = []
    original_batch_thread_meta = AgentsStore.batch_thread_meta

    class FakeEmbeddingCache:
        def __init__(self):
            self._session_loaded = False

        def load_session_embeddings(self, db, model, dims):
            self._session_loaded = True

        @property
        def session_embedding_count(self):
            return 2

        def search_sessions(self, query_vec, limit, session_filter):
            assert first_id in session_filter
            assert second_id in session_filter
            return [(first_id, 0.91), (second_id, 0.82)]

    async def fake_generate_embedding(query, config):
        return [0.1, 0.2, 0.3]

    def record_batch_thread_meta(self, sessions):
        batch_calls.append([str(session.id) for session in sessions])
        return original_batch_thread_meta(self, sessions)

    with (
        patch("zerg.models_config.get_embedding_config", return_value=SimpleNamespace(model="fake", dims=3)),
        patch("zerg.services.embedding_cache.EmbeddingCache", FakeEmbeddingCache),
        patch("zerg.services.session_processing.embeddings.generate_embedding", fake_generate_embedding),
        patch.object(AgentsStore, "batch_thread_meta", record_batch_thread_meta),
        patch.object(
            AgentsStore,
            "get_thread_head",
            side_effect=AssertionError("semantic search should preload thread metadata"),
        ),
        patch.object(
            AgentsStore,
            "list_thread_sessions",
            side_effect=AssertionError("semantic search should preload thread metadata"),
        ),
    ):
        for client in _get_client(factory):
            resp = client.get(
                "/agents/sessions/semantic",
                params={"query": "anything", "days_back": 90, "context_mode": "forensic", "limit": 5},
            )
            assert resp.status_code == 200, resp.text
            payload = resp.json()
            assert payload["total"] == 2

    assert len(batch_calls) == 1
    assert set(batch_calls[0]) == {first_id, second_id}


def test_recall_active_context_dedupes_session_boundary_lookups(tmp_path):
    factory = _make_db(tmp_path)
    _seed_compacted_session(factory)

    boundary_calls: list[str] = []
    original_get_active_context_boundary = AgentsStore.get_active_context_boundary

    class FakeEmbeddingCache:
        def __init__(self):
            self._session_loaded = False
            self._turn_loaded = False

        def load_session_embeddings(self, db, model, dims):
            self._session_loaded = True

        def load_turn_embeddings(self, db, model, dims):
            self._turn_loaded = True

        @property
        def turn_embedding_count(self):
            return 2

        def search_turns(self, query_vec, limit, session_filter):
            assert len(session_filter) == 1
            session_id = next(iter(session_filter))
            return [
                (session_id, 0, 0.91, 3, 3),
                (session_id, 1, 0.82, 4, 4),
            ]

    async def fake_generate_embedding(query, config):
        return [0.1, 0.2, 0.3]

    def record_get_active_context_boundary(self, session_id, *, branch_mode="head"):
        boundary_calls.append(str(session_id))
        return original_get_active_context_boundary(self, session_id, branch_mode=branch_mode)

    with (
        patch("zerg.models_config.get_embedding_config", return_value=SimpleNamespace(model="fake", dims=3)),
        patch("zerg.services.embedding_cache.EmbeddingCache", FakeEmbeddingCache),
        patch("zerg.services.session_processing.embeddings.generate_embedding", fake_generate_embedding),
        patch.object(AgentsStore, "get_active_context_boundary", record_get_active_context_boundary),
    ):
        for client in _get_client(factory):
            resp = client.get(
                "/agents/recall",
                params={
                    "query": "continue migration",
                    "days_back": 90,
                    "context_mode": "active_context",
                    "max_results": 5,
                },
            )
            assert resp.status_code == 200, resp.text
            payload = resp.json()
            assert payload["total"] == 2

    assert len(boundary_calls) == 1


def test_recall_filters_by_provider(tmp_path):
    factory = _make_db(tmp_path)
    claude_id = _seed_simple_session(factory, "claude-recall", provider="claude")
    codex_id = _seed_simple_session(factory, "codex-recall", provider="codex")

    class FakeEmbeddingCache:
        def __init__(self):
            self._session_loaded = False
            self._turn_loaded = False

        def load_session_embeddings(self, db, model, dims):
            self._session_loaded = True

        def load_turn_embeddings(self, db, model, dims):
            self._turn_loaded = True

        @property
        def turn_embedding_count(self):
            return 2

        def search_turns(self, query_vec, limit, session_filter):
            assert claude_id not in session_filter
            assert codex_id in session_filter
            return [(codex_id, 0, 0.91, 0, 0)]

    async def fake_generate_embedding(query, config):
        return [0.1, 0.2, 0.3]

    with (
        patch("zerg.models_config.get_embedding_config", return_value=SimpleNamespace(model="fake", dims=3)),
        patch("zerg.services.embedding_cache.EmbeddingCache", FakeEmbeddingCache),
        patch("zerg.services.session_processing.embeddings.generate_embedding", fake_generate_embedding),
    ):
        for client in _get_client(factory):
            resp = client.get(
                "/agents/recall",
                params={"query": "provider-specific recall", "since_days": 90, "provider": "codex"},
            )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["total"] == 1
    assert payload["matches"][0]["session_id"] == codex_id


def test_semantic_search_fails_loud_when_embedding_config_unavailable(tmp_path):
    factory = _make_db(tmp_path)
    _seed_simple_session(factory, "missing-config")

    with patch("zerg.models_config.get_embedding_config", return_value=None):
        for client in _get_client(factory):
            resp = client.get(
                "/agents/sessions/semantic",
                params={"query": "anything", "days_back": 90},
            )

    assert resp.status_code == 503
    assert "Embeddings unavailable" in resp.json()["detail"]


def test_semantic_search_fails_loud_when_corpus_has_no_session_embeddings(tmp_path):
    factory = _make_db(tmp_path)
    _seed_simple_session(factory, "empty-session-embeddings")

    class EmptyEmbeddingCache:
        def __init__(self):
            self._session_loaded = False

        def load_session_embeddings(self, db, model, dims):
            self._session_loaded = True
            return 0

        @property
        def session_embedding_count(self):
            return 0

        def search_sessions(self, query_vec, limit, session_filter):
            raise AssertionError("semantic search should fail before searching an empty embedding corpus")

    async def fake_generate_embedding(query, config):
        return [0.1, 0.2, 0.3]

    with (
        patch("zerg.models_config.get_embedding_config", return_value=SimpleNamespace(model="fake", dims=3)),
        patch("zerg.services.embedding_cache.EmbeddingCache", EmptyEmbeddingCache),
        patch("zerg.services.session_processing.embeddings.generate_embedding", fake_generate_embedding),
    ):
        for client in _get_client(factory):
            resp = client.get(
                "/agents/sessions/semantic",
                params={"query": "anything", "days_back": 90},
            )

    assert resp.status_code == 503
    assert "no session embeddings are loaded" in resp.json()["detail"]


def test_recall_fails_loud_when_corpus_has_no_turn_embeddings(tmp_path):
    factory = _make_db(tmp_path)
    _seed_simple_session(factory, "empty-turn-embeddings")

    class EmptyEmbeddingCache:
        def __init__(self):
            self._session_loaded = False
            self._turn_loaded = False

        def load_session_embeddings(self, db, model, dims):
            self._session_loaded = True
            return 0

        def load_turn_embeddings(self, db, model, dims):
            self._turn_loaded = True
            return 0

        @property
        def turn_embedding_count(self):
            return 0

        def search_turns(self, query_vec, limit, session_filter):
            raise AssertionError("recall should fail before searching an empty embedding corpus")

    async def fake_generate_embedding(query, config):
        return [0.1, 0.2, 0.3]

    with (
        patch("zerg.models_config.get_embedding_config", return_value=SimpleNamespace(model="fake", dims=3)),
        patch("zerg.services.embedding_cache.EmbeddingCache", EmptyEmbeddingCache),
        patch("zerg.services.session_processing.embeddings.generate_embedding", fake_generate_embedding),
    ):
        for client in _get_client(factory):
            resp = client.get(
                "/agents/recall",
                params={"query": "anything", "since_days": 90},
            )

    assert resp.status_code == 503
    assert "no turn embeddings are loaded" in resp.json()["detail"]
