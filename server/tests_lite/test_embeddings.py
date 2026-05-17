"""Tests for embedding utilities: round-trip serialization, sanitization, chunking, and upsert."""

import os
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

import numpy as np
import pytest
from sqlalchemy.orm import sessionmaker

from zerg.database import Base
from zerg.database import make_engine
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionEmbedding
from zerg.models.work import Insight  # noqa: F401
from zerg.services.session_processing.embeddings import bytes_to_embedding
from zerg.services.session_processing.embeddings import content_hash
from zerg.services.session_processing.embeddings import embed_session
from zerg.services.session_processing.embeddings import embedding_to_bytes
from zerg.services.session_processing.embeddings import generate_embeddings
from zerg.services.session_processing.embeddings import mark_session_embedding_complete
from zerg.services.session_processing.embeddings import prepare_session_chunk
from zerg.services.session_processing.embeddings import prepare_turn_chunks
from zerg.services.session_processing.embeddings import sanitize_for_embedding


def _make_db(tmp_path):
    db_path = tmp_path / "test_emb.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal


def test_embedding_roundtrip():
    """Serialize and deserialize a numpy array through bytes."""
    original = np.array([0.1, 0.2, 0.3, 0.4], dtype=np.float32)
    encoded = embedding_to_bytes(original)
    decoded = bytes_to_embedding(encoded, 4)
    np.testing.assert_array_almost_equal(original, decoded)


@pytest.mark.asyncio
async def test_generate_embeddings_preserves_provider_index_order(monkeypatch):
    """Batched provider responses are returned in input order."""

    class _FakeEmbedding:
        def __init__(self, index, embedding):
            self.index = index
            self.embedding = embedding

    class _FakeEmbeddings:
        async def create(self, **_kwargs):
            return SimpleNamespace(
                data=[
                    _FakeEmbedding(1, [0.0, 1.0, 0.0, 0.0]),
                    _FakeEmbedding(0, [1.0, 0.0, 0.0, 0.0]),
                ]
            )

    class _FakeClient:
        def __init__(self, **_kwargs):
            self.embeddings = _FakeEmbeddings()

        async def close(self):
            return None

    monkeypatch.setattr("openai.AsyncOpenAI", _FakeClient)

    config = SimpleNamespace(provider="openai", model="test-model", dims=4, api_key="test-key", base_url=None)
    vectors = await generate_embeddings(["first", "second"], config)

    assert len(vectors) == 2
    np.testing.assert_array_equal(vectors[0], np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
    np.testing.assert_array_equal(vectors[1], np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32))


def test_sanitize_for_embedding():
    """Noise and secrets are stripped from embedding input text."""
    text = "<system-reminder>ignored</system-reminder>Hello world sk-abc123456789012345678901234567890123456789012345"
    cleaned = sanitize_for_embedding(text)
    assert "<system-reminder>" not in cleaned
    assert "Hello world" in cleaned
    # The secret key should be redacted
    assert "sk-abc123456789012345678901234567890123456789012345" not in cleaned


def test_session_chunk_from_summary(tmp_path):
    """When a session has a summary, the session chunk uses it."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        session_id = str(uuid4())
        session = AgentSession(
            id=session_id,
            provider="claude",
            environment="test",
            project="zerg",
            started_at=datetime.now(timezone.utc),
            summary="Fixed auth bug in login flow",
            summary_title="Auth bug fix",
        )
        db.add(session)
        db.commit()

        # prepare_session_chunk uses the summary
        chunk = prepare_session_chunk(session, [])
        assert chunk is not None
        assert chunk.kind == "session"
        assert "Auth bug fix" in chunk.text
        assert "Fixed auth bug" in chunk.text


def test_session_chunk_from_transcript(tmp_path):
    """When no summary is available, session chunk is built from transcript."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        session_id = str(uuid4())
        session = AgentSession(
            id=session_id,
            provider="claude",
            environment="test",
            project="zerg",
            started_at=datetime.now(timezone.utc),
            summary=None,
            summary_title=None,
        )
        db.add(session)
        db.commit()

        events = [
            {
                "role": "user",
                "content_text": "Please fix the login bug",
                "tool_name": None,
                "tool_input_json": None,
                "tool_output_text": None,
                "timestamp": datetime.now(timezone.utc),
                "session_id": session_id,
            },
            {
                "role": "assistant",
                "content_text": "I will fix the login bug now",
                "tool_name": None,
                "tool_input_json": None,
                "tool_output_text": None,
                "timestamp": datetime.now(timezone.utc),
                "session_id": session_id,
            },
        ]

        chunk = prepare_session_chunk(session, events)
        assert chunk is not None
        assert chunk.kind == "session"
        assert "login bug" in chunk.text.lower()


def test_turn_chunks_event_indices(tmp_path):
    """Turn chunks track correct event start/end indices."""
    events = [
        {
            "role": "user",
            "content_text": "What is the capital of France?",
            "tool_name": None,
            "tool_input_json": None,
            "tool_output_text": None,
            "timestamp": datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
            "session_id": "test",
        },
        {
            "role": "assistant",
            "content_text": "The capital of France is Paris.",
            "tool_name": None,
            "tool_input_json": None,
            "tool_output_text": None,
            "timestamp": datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
            "session_id": "test",
        },
        {
            "role": "user",
            "content_text": "What about Germany?",
            "tool_name": None,
            "tool_input_json": None,
            "tool_output_text": None,
            "timestamp": datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc),
            "session_id": "test",
        },
        {
            "role": "assistant",
            "content_text": "The capital of Germany is Berlin.",
            "tool_name": None,
            "tool_input_json": None,
            "tool_output_text": None,
            "timestamp": datetime(2026, 1, 1, 0, 3, tzinfo=timezone.utc),
            "session_id": "test",
        },
    ]

    chunks = prepare_turn_chunks(events)
    assert len(chunks) == 2

    # First turn: user(0) + assistant(1)
    assert chunks[0].chunk_index == 0
    assert chunks[0].event_index_start == 0
    assert chunks[0].event_index_end == 1

    # Second turn: user(2) + assistant(3)
    assert chunks[1].chunk_index == 1
    assert chunks[1].event_index_start == 2
    assert chunks[1].event_index_end == 3


def test_turn_chunks_break_equal_timestamps_by_event_id(tmp_path):
    """Equal event timestamps use the durable row id for stable transcript order."""
    events = [
        {
            "id": 2,
            "role": "assistant",
            "content_text": "Then the answer.",
            "tool_name": None,
            "tool_input_json": None,
            "tool_output_text": None,
            "timestamp": "2026-01-01T00:00:00Z",
            "session_id": "test",
        },
        {
            "id": 1,
            "role": "user",
            "content_text": "First the question.",
            "tool_name": None,
            "tool_input_json": None,
            "tool_output_text": None,
            "timestamp": datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
            "session_id": "test",
        },
    ]

    chunks = prepare_turn_chunks(events)

    assert len(chunks) == 1
    assert chunks[0].text.index("First the question.") < chunks[0].text.index("Then the answer.")


def test_embedding_upsert(tmp_path):
    """SessionEmbedding can be inserted and queried back."""
    SessionLocal = _make_db(tmp_path)
    session_id = str(uuid4())

    with SessionLocal() as db:
        # Create a session first (FK constraint)
        db.add(AgentSession(
            id=session_id,
            provider="claude",
            environment="test",
            project="zerg",
            started_at=datetime.now(timezone.utc),
        ))
        db.commit()

        # Insert an embedding
        vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        db.add(SessionEmbedding(
            session_id=session_id,
            kind="session",
            chunk_index=-1,
            model="test-model",
            dims=4,
            embedding=embedding_to_bytes(vec),
            content_hash=content_hash("test content"),
        ))
        db.commit()

        # Query it back
        emb = (
            db.query(SessionEmbedding)
            .filter(
                SessionEmbedding.session_id == session_id,
                SessionEmbedding.kind == "session",
            )
            .first()
        )
        assert emb is not None
        assert emb.dims == 4
        assert emb.model == "test-model"

        decoded = bytes_to_embedding(emb.embedding, 4)
        np.testing.assert_array_almost_equal(vec, decoded)


@pytest.mark.asyncio
async def test_embed_session_routes_write_phase_through_serializer(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    session_id = str(uuid4())
    labels: list[str] = []

    class _FakeSerializer:
        is_configured = True

        async def execute_or_direct(self, fn, fallback_db=None, *, label="", auto_commit=True):
            labels.append(label)
            result = fn(fallback_db)
            if auto_commit:
                fallback_db.commit()
            return result

    async def _fake_generate_embeddings(texts, _config):
        return [np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32) for _ in texts]

    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: _FakeSerializer())
    monkeypatch.setattr("zerg.services.session_processing.embeddings.generate_embeddings", _fake_generate_embeddings)

    config = SimpleNamespace(provider="openai", model="test-model", dims=4, api_key="test-key")

    with SessionLocal() as db:
        db.add(
            AgentSession(
                id=session_id,
                provider="claude",
                environment="test",
                project="zerg",
                started_at=datetime.now(timezone.utc),
                needs_embedding=1,
                transcript_revision=4,
            )
        )
        db.add(
            AgentEvent(
                session_id=session_id,
                role="user",
                content_text="Please fix the login bug",
                timestamp=datetime.now(timezone.utc),
            )
        )
        db.add(
            AgentEvent(
                session_id=session_id,
                role="assistant",
                content_text="I fixed the login bug.",
                timestamp=datetime.now(timezone.utc),
            )
        )
        db.commit()

        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        events = db.query(AgentEvent).filter(AgentEvent.session_id == session_id).order_by(AgentEvent.timestamp).all()

        written, remaining = await embed_session(session_id, session, events, config, db, transcript_revision=4)
        assert (written, remaining) == (2, 0)
        await mark_session_embedding_complete(session_id, transcript_revision=4, db=db)
        assert labels == ["embeddings", "embeddings-complete"]

        db.expire_all()
        stored = (
            db.query(SessionEmbedding)
            .filter(SessionEmbedding.session_id == session_id)
            .order_by(SessionEmbedding.kind.asc(), SessionEmbedding.chunk_index.asc())
            .all()
        )
        assert len(stored) == 2

        refreshed_session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        assert refreshed_session.needs_embedding == 0
        assert refreshed_session.embedding_revision == 4


@pytest.mark.asyncio
async def test_embed_session_batches_turn_embeddings(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    session_id = str(uuid4())
    batch_sizes: list[int] = []

    class _FakeSerializer:
        is_configured = True

        async def execute_or_direct(self, fn, fallback_db=None, *, label="", auto_commit=True):
            result = fn(fallback_db)
            if auto_commit:
                fallback_db.commit()
            return result

    async def _fake_generate_embeddings(texts, _config):
        batch_sizes.append(len(texts))
        return [np.array([float(i + 1), 0.0, 0.0, 0.0], dtype=np.float32) for i, _text in enumerate(texts)]

    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: _FakeSerializer())
    monkeypatch.setattr("zerg.services.session_processing.embeddings.generate_embeddings", _fake_generate_embeddings)

    config = SimpleNamespace(provider="openai", model="test-model", dims=4, api_key="test-key")

    with SessionLocal() as db:
        db.add(
            AgentSession(
                id=session_id,
                provider="claude",
                environment="test",
                project="zerg",
                started_at=datetime.now(timezone.utc),
                needs_embedding=1,
                summary="A session summary gives us a session-level embedding.",
                transcript_revision=2,
            )
        )
        for idx in range(3):
            db.add(
                AgentEvent(
                    session_id=session_id,
                    role="user",
                    content_text=f"Question {idx}",
                    timestamp=datetime(2026, 1, 1, 0, idx * 2, tzinfo=timezone.utc),
                )
            )
            db.add(
                AgentEvent(
                    session_id=session_id,
                    role="assistant",
                    content_text=f"Answer {idx}",
                    timestamp=datetime(2026, 1, 1, 0, idx * 2 + 1, tzinfo=timezone.utc),
                )
            )
        db.commit()

        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        events = db.query(AgentEvent).filter(AgentEvent.session_id == session_id).order_by(AgentEvent.timestamp).all()

        written, remaining = await embed_session(session_id, session, events, config, db, transcript_revision=2)
        assert (written, remaining) == (4, 0)
        assert batch_sizes == [4]


@pytest.mark.asyncio
async def test_embed_session_writes_bounded_slice_and_leaves_session_stale(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    session_id = str(uuid4())

    class _FakeSerializer:
        is_configured = True

        async def execute_or_direct(self, fn, fallback_db=None, *, label="", auto_commit=True):
            result = fn(fallback_db)
            if auto_commit:
                fallback_db.commit()
            return result

    async def _fake_generate_embeddings(texts, _config):
        return [np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32) for _ in texts]

    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: _FakeSerializer())
    monkeypatch.setattr("zerg.services.session_processing.embeddings.generate_embeddings", _fake_generate_embeddings)
    monkeypatch.setattr("zerg.services.session_processing.embeddings.EMBEDDING_MAX_CHUNKS_PER_PASS", 2)

    config = SimpleNamespace(provider="openai", model="test-model", dims=4, api_key="test-key")

    with SessionLocal() as db:
        db.add(
            AgentSession(
                id=session_id,
                provider="claude",
                environment="test",
                project="zerg",
                started_at=datetime.now(timezone.utc),
                needs_embedding=1,
                summary="Sliceable session summary.",
                transcript_revision=5,
            )
        )
        for idx in range(3):
            db.add(
                AgentEvent(
                    session_id=session_id,
                    role="user",
                    content_text=f"Question {idx}",
                    timestamp=datetime(2026, 1, 1, 0, idx * 2, tzinfo=timezone.utc),
                )
            )
            db.add(
                AgentEvent(
                    session_id=session_id,
                    role="assistant",
                    content_text=f"Answer {idx}",
                    timestamp=datetime(2026, 1, 1, 0, idx * 2 + 1, tzinfo=timezone.utc),
                )
            )
        db.commit()

        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        events = db.query(AgentEvent).filter(AgentEvent.session_id == session_id).order_by(AgentEvent.timestamp).all()

        written, remaining = await embed_session(session_id, session, events, config, db, transcript_revision=5)
        assert written == 2
        assert remaining > 0

        stored = db.query(SessionEmbedding).filter(SessionEmbedding.session_id == session_id).all()
        assert len(stored) == 2

        db.expire_all()
        refreshed = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        assert refreshed.needs_embedding == 1
        assert refreshed.embedding_revision == 0


@pytest.mark.asyncio
async def test_embed_session_resumes_from_existing_content_hashes(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    session_id = str(uuid4())
    generated_texts: list[str] = []

    class _FakeSerializer:
        is_configured = True

        async def execute_or_direct(self, fn, fallback_db=None, *, label="", auto_commit=True):
            result = fn(fallback_db)
            if auto_commit:
                fallback_db.commit()
            return result

    async def _fake_generate_embeddings(texts, _config):
        generated_texts.extend(texts)
        return [np.array([float(len(generated_texts)), 0.0, 0.0, 0.0], dtype=np.float32) for _ in texts]

    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: _FakeSerializer())
    monkeypatch.setattr("zerg.services.session_processing.embeddings.generate_embeddings", _fake_generate_embeddings)
    monkeypatch.setattr("zerg.services.session_processing.embeddings.EMBEDDING_MAX_CHUNKS_PER_PASS", 2)

    config = SimpleNamespace(provider="openai", model="test-model", dims=4, api_key="test-key")

    with SessionLocal() as db:
        db.add(
            AgentSession(
                id=session_id,
                provider="claude",
                environment="test",
                project="zerg",
                started_at=datetime.now(timezone.utc),
                needs_embedding=1,
                summary="Resume session summary.",
                transcript_revision=3,
            )
        )
        for idx in range(3):
            db.add(
                AgentEvent(
                    session_id=session_id,
                    role="user",
                    content_text=f"Resume question {idx}",
                    timestamp=datetime(2026, 1, 1, 0, idx * 2, tzinfo=timezone.utc),
                )
            )
            db.add(
                AgentEvent(
                    session_id=session_id,
                    role="assistant",
                    content_text=f"Resume answer {idx}",
                    timestamp=datetime(2026, 1, 1, 0, idx * 2 + 1, tzinfo=timezone.utc),
                )
            )
        db.commit()

        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        events = db.query(AgentEvent).filter(AgentEvent.session_id == session_id).order_by(AgentEvent.timestamp).all()

        first = await embed_session(session_id, session, events, config, db, transcript_revision=3)
        second = await embed_session(session_id, session, events, config, db, transcript_revision=3)

        assert first == (2, 1)
        assert second == (2, 0)
        assert len(generated_texts) == 4
        assert len(set(generated_texts)) == 4


@pytest.mark.asyncio
async def test_partial_turn_embeddings_are_searchable(monkeypatch, tmp_path):
    from zerg.services.embedding_cache import EmbeddingCache

    EmbeddingCache.reset()
    SessionLocal = _make_db(tmp_path)
    session_id = str(uuid4())

    class _FakeSerializer:
        is_configured = True

        async def execute_or_direct(self, fn, fallback_db=None, *, label="", auto_commit=True):
            result = fn(fallback_db)
            if auto_commit:
                fallback_db.commit()
            return result

    async def _fake_generate_embeddings(texts, _config):
        vectors = []
        for text in texts:
            vectors.append(
                np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
                if "needle" in text
                else np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
            )
        return vectors

    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: _FakeSerializer())
    monkeypatch.setattr("zerg.services.session_processing.embeddings.generate_embeddings", _fake_generate_embeddings)
    monkeypatch.setattr("zerg.services.session_processing.embeddings.EMBEDDING_MAX_CHUNKS_PER_PASS", 2)

    config = SimpleNamespace(provider="openai", model="test-model", dims=4, api_key="test-key")

    with SessionLocal() as db:
        db.add(
            AgentSession(
                id=session_id,
                provider="claude",
                environment="test",
                project="zerg",
                started_at=datetime.now(timezone.utc),
                needs_embedding=1,
                summary="Searchable session summary.",
                transcript_revision=3,
            )
        )
        db.add(
            AgentEvent(
                session_id=session_id,
                role="user",
                content_text="needle question",
                timestamp=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
            )
        )
        db.add(
            AgentEvent(
                session_id=session_id,
                role="assistant",
                content_text="needle answer",
                timestamp=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
            )
        )
        db.add(
            AgentEvent(
                session_id=session_id,
                role="user",
                content_text="later question",
                timestamp=datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc),
            )
        )
        db.commit()

        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        events = db.query(AgentEvent).filter(AgentEvent.session_id == session_id).order_by(AgentEvent.timestamp).all()
        written, remaining = await embed_session(session_id, session, events, config, db, transcript_revision=3)
        assert (written, remaining) == (2, 1)

        cache = EmbeddingCache()
        assert cache.load_turn_embeddings(db, "test-model", 4) == 1
        results = cache.search_turns(np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32), limit=1)
        assert results
        assert results[0][0] == session_id
        assert results[0][1] == 0

    EmbeddingCache.reset()


@pytest.mark.asyncio
async def test_stale_content_hash_is_regenerated(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    session_id = str(uuid4())
    generated_texts: list[str] = []

    class _FakeSerializer:
        is_configured = True

        async def execute_or_direct(self, fn, fallback_db=None, *, label="", auto_commit=True):
            result = fn(fallback_db)
            if auto_commit:
                fallback_db.commit()
            return result

    async def _fake_generate_embeddings(texts, _config):
        generated_texts.extend(texts)
        return [np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32) for _ in texts]

    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: _FakeSerializer())
    monkeypatch.setattr("zerg.services.session_processing.embeddings.generate_embeddings", _fake_generate_embeddings)
    monkeypatch.setattr("zerg.services.session_processing.embeddings.EMBEDDING_MAX_CHUNKS_PER_PASS", 10)

    config = SimpleNamespace(provider="openai", model="test-model", dims=4, api_key="test-key")

    with SessionLocal() as db:
        db.add(
            AgentSession(
                id=session_id,
                provider="claude",
                environment="test",
                project="zerg",
                started_at=datetime.now(timezone.utc),
                needs_embedding=1,
                summary="Current summary.",
                transcript_revision=2,
            )
        )
        db.add(
            AgentEvent(
                session_id=session_id,
                role="user",
                content_text="fresh question",
                timestamp=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
            )
        )
        db.add(
            AgentEvent(
                session_id=session_id,
                role="assistant",
                content_text="fresh answer",
                timestamp=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
            )
        )
        db.add(
            SessionEmbedding(
                session_id=session_id,
                kind="turn",
                chunk_index=0,
                model="test-model",
                dims=4,
                embedding=embedding_to_bytes(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)),
                content_hash="stale",
            )
        )
        db.commit()

        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        events = db.query(AgentEvent).filter(AgentEvent.session_id == session_id).order_by(AgentEvent.timestamp).all()
        written, remaining = await embed_session(session_id, session, events, config, db, transcript_revision=2)

        assert (written, remaining) == (2, 0)
        assert any("fresh question" in text for text in generated_texts)
        turn = (
            db.query(SessionEmbedding)
            .filter(
                SessionEmbedding.session_id == session_id,
                SessionEmbedding.kind == "turn",
                SessionEmbedding.chunk_index == 0,
            )
            .one()
        )
        assert turn.content_hash != "stale"
