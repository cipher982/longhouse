"""Tests for embedding utilities: round-trip serialization, sanitization, chunking, and upsert."""

from datetime import datetime
from datetime import timezone
from uuid import uuid4

import numpy as np
from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentsBase
from zerg.models.agents import SessionEmbedding
from zerg.models.work import FileReservation  # noqa: F401
from zerg.models.work import Insight  # noqa: F401
from zerg.services.session_processing.embeddings import bytes_to_embedding
from zerg.services.session_processing.embeddings import content_hash
from zerg.services.session_processing.embeddings import embedding_to_bytes
from zerg.services.session_processing.embeddings import prepare_session_chunk
from zerg.services.session_processing.embeddings import prepare_turn_chunks
from zerg.services.session_processing.embeddings import sanitize_for_embedding


def _make_db(tmp_path):
    db_path = tmp_path / "test_emb.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal


def test_embedding_roundtrip():
    """Serialize and deserialize a numpy array through bytes."""
    original = np.array([0.1, 0.2, 0.3, 0.4], dtype=np.float32)
    encoded = embedding_to_bytes(original)
    decoded = bytes_to_embedding(encoded, 4)
    np.testing.assert_array_almost_equal(original, decoded)


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
