"""Tests for the embedding cache and semantic search logic."""

from datetime import datetime
from datetime import timezone
from uuid import uuid4

import numpy as np
from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentsBase
from zerg.models.agents import SessionEmbedding
from zerg.models.work import FileReservation  # noqa: F401
from zerg.models.work import Insight  # noqa: F401
from zerg.services.embedding_cache import EmbeddingCache
from zerg.services.session_processing.embeddings import embedding_to_bytes


def _make_db(tmp_path):
    db_path = tmp_path / "test_search.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal


def _create_session(db, session_id, project="zerg"):
    """Helper to create a session in the DB."""
    db.add(AgentSession(
        id=session_id,
        provider="claude",
        environment="test",
        project=project,
        started_at=datetime.now(timezone.utc),
    ))
    db.commit()


def _add_session_embedding(db, session_id, vec, model="test-model"):
    """Helper to add a session-level embedding."""
    db.add(SessionEmbedding(
        session_id=session_id,
        kind="session",
        chunk_index=-1,
        model=model,
        dims=len(vec),
        embedding=embedding_to_bytes(np.array(vec, dtype=np.float32)),
    ))
    db.commit()


def _add_turn_embedding(db, session_id, chunk_index, vec, event_start, event_end, model="test-model"):
    """Helper to add a turn-level embedding."""
    db.add(SessionEmbedding(
        session_id=session_id,
        kind="turn",
        chunk_index=chunk_index,
        model=model,
        dims=len(vec),
        embedding=embedding_to_bytes(np.array(vec, dtype=np.float32)),
        event_index_start=event_start,
        event_index_end=event_end,
    ))
    db.commit()


def test_cache_load_and_search(tmp_path):
    """Create SessionEmbeddings in DB, load cache, search with a query vector."""
    EmbeddingCache.reset()
    SessionLocal = _make_db(tmp_path)

    sid1 = str(uuid4())
    sid2 = str(uuid4())

    with SessionLocal() as db:
        _create_session(db, sid1)
        _create_session(db, sid2)
        _add_session_embedding(db, sid1, [1.0, 0.0, 0.0, 0.0])
        _add_session_embedding(db, sid2, [0.0, 1.0, 0.0, 0.0])

    with SessionLocal() as db:
        cache = EmbeddingCache()
        count = cache.load_session_embeddings(db, "test-model", 4)
        assert count == 2

        # Search for vector close to sid1
        query = np.array([0.9, 0.1, 0.0, 0.0], dtype=np.float32)
        results = cache.search_sessions(query, limit=2)
        assert len(results) == 2
        assert results[0][0] == sid1  # sid1 should be closest
        assert results[0][1] > results[1][1]  # higher score

    EmbeddingCache.reset()


def test_cosine_similarity_ordering(tmp_path):
    """Closer vectors rank higher in search results."""
    EmbeddingCache.reset()
    SessionLocal = _make_db(tmp_path)

    sid_close = str(uuid4())
    sid_far = str(uuid4())
    sid_medium = str(uuid4())

    with SessionLocal() as db:
        _create_session(db, sid_close)
        _create_session(db, sid_far)
        _create_session(db, sid_medium)
        # Close to [1, 0, 0]
        _add_session_embedding(db, sid_close, [0.95, 0.05, 0.0])
        # Far from [1, 0, 0] but still has some positive similarity
        _add_session_embedding(db, sid_far, [0.1, 0.1, 0.98])
        # Medium
        _add_session_embedding(db, sid_medium, [0.5, 0.5, 0.0])

    with SessionLocal() as db:
        cache = EmbeddingCache()
        cache.load_session_embeddings(db, "test-model", 3)

        query = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        results = cache.search_sessions(query, limit=3)

        assert len(results) == 3
        ids = [r[0] for r in results]
        scores = [r[1] for r in results]

        # sid_close should be first (most similar)
        assert ids[0] == sid_close
        # sid_medium should be second
        assert ids[1] == sid_medium
        # sid_far should be last (least similar)
        assert ids[2] == sid_far
        # Scores should be strictly decreasing
        assert scores[0] > scores[1] > scores[2]

    EmbeddingCache.reset()


def test_turn_search_returns_event_ranges(tmp_path):
    """Turn search returns correct event index ranges."""
    EmbeddingCache.reset()
    SessionLocal = _make_db(tmp_path)

    sid = str(uuid4())

    with SessionLocal() as db:
        _create_session(db, sid)
        _add_turn_embedding(db, sid, 0, [1.0, 0.0, 0.0, 0.0], event_start=0, event_end=3)
        _add_turn_embedding(db, sid, 1, [0.0, 1.0, 0.0, 0.0], event_start=4, event_end=7)

    with SessionLocal() as db:
        cache = EmbeddingCache()
        cache.load_turn_embeddings(db, "test-model", 4)

        # Search for first turn
        query = np.array([0.9, 0.1, 0.0, 0.0], dtype=np.float32)
        results = cache.search_turns(query, limit=2)

        assert len(results) == 2
        # First result should be turn 0 (closest to query)
        session_id, chunk_index, score, event_start, event_end = results[0]
        assert session_id == sid
        assert chunk_index == 0
        assert event_start == 0
        assert event_end == 3

        # Second result should be turn 1
        session_id2, chunk_index2, score2, event_start2, event_end2 = results[1]
        assert session_id2 == sid
        assert chunk_index2 == 1
        assert event_start2 == 4
        assert event_end2 == 7

    EmbeddingCache.reset()


def test_fallback_when_no_embeddings(tmp_path):
    """Empty cache returns empty results."""
    EmbeddingCache.reset()
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        cache = EmbeddingCache()
        cache.load_session_embeddings(db, "test-model", 4)

        query = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        results = cache.search_sessions(query, limit=10)
        assert results == []

        cache.load_turn_embeddings(db, "test-model", 4)
        turn_results = cache.search_turns(query, limit=10)
        assert turn_results == []

    EmbeddingCache.reset()


def test_session_filter(tmp_path):
    """Session filter restricts search to specified session IDs."""
    EmbeddingCache.reset()
    SessionLocal = _make_db(tmp_path)

    sid1 = str(uuid4())
    sid2 = str(uuid4())

    with SessionLocal() as db:
        _create_session(db, sid1)
        _create_session(db, sid2)
        # Both have similar embeddings but we filter to only sid2
        _add_session_embedding(db, sid1, [1.0, 0.0, 0.0, 0.0])
        _add_session_embedding(db, sid2, [0.9, 0.1, 0.0, 0.0])

    with SessionLocal() as db:
        cache = EmbeddingCache()
        cache.load_session_embeddings(db, "test-model", 4)

        query = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        results = cache.search_sessions(query, limit=10, session_filter={sid2})

        # Only sid2 should be returned
        assert len(results) == 1
        assert results[0][0] == sid2

    EmbeddingCache.reset()


def test_zero_vector_query(tmp_path):
    """Zero-vector query returns empty results (no division by zero)."""
    EmbeddingCache.reset()
    SessionLocal = _make_db(tmp_path)

    sid = str(uuid4())

    with SessionLocal() as db:
        _create_session(db, sid)
        _add_session_embedding(db, sid, [1.0, 0.0, 0.0, 0.0])

    with SessionLocal() as db:
        cache = EmbeddingCache()
        cache.load_session_embeddings(db, "test-model", 4)

        query = np.array([0.0, 0.0, 0.0, 0.0], dtype=np.float32)
        results = cache.search_sessions(query, limit=10)
        assert results == []

    EmbeddingCache.reset()
