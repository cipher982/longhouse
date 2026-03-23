"""Regression tests for semantic search autonomous-session filtering."""

from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentsBase
from zerg.models.agents import SessionEmbedding
from zerg.services.embedding_cache import EmbeddingCache
from zerg.services.search import SessionFilters
from zerg.services.search import semantic_search
from zerg.services.session_processing.embeddings import embedding_to_bytes


def _make_db(tmp_path):
    db_path = tmp_path / "test_semantic_search_autonomous.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _create_session_with_embedding(
    db,
    *,
    session_id: str,
    user_messages: int,
    is_sidechain: int,
    vec: list[float],
):
    db.add(
        AgentSession(
            id=session_id,
            provider="claude",
            environment="production",
            project="zerg",
            started_at=datetime.now(timezone.utc),
            user_messages=user_messages,
            is_sidechain=is_sidechain,
        )
    )
    db.add(
        SessionEmbedding(
            session_id=session_id,
            kind="session",
            chunk_index=-1,
            model="test-model",
            dims=len(vec),
            embedding=embedding_to_bytes(np.array(vec, dtype=np.float32)),
        )
    )
    db.commit()


async def _fake_generate_embedding(_query, _config):
    return np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)


def test_semantic_search_hides_autonomous_sessions(monkeypatch, tmp_path):
    """hide_autonomous=True excludes sidechain and zero-user sessions."""
    EmbeddingCache.reset()
    SessionLocal = _make_db(tmp_path)

    normal_id = str(uuid4())
    sidechain_id = str(uuid4())
    zero_user_id = str(uuid4())

    with SessionLocal() as db:
        # Intentionally make autonomous sessions highly similar so this test
        # fails loudly if filtering regresses.
        _create_session_with_embedding(db, session_id=normal_id, user_messages=1, is_sidechain=0, vec=[0.8, 0.2, 0.0, 0.0])
        _create_session_with_embedding(db, session_id=sidechain_id, user_messages=3, is_sidechain=1, vec=[1.0, 0.0, 0.0, 0.0])
        _create_session_with_embedding(db, session_id=zero_user_id, user_messages=0, is_sidechain=0, vec=[0.99, 0.01, 0.0, 0.0])

        monkeypatch.setattr(
            "zerg.models_config.get_embedding_config_with_db_fallback",
            lambda db: SimpleNamespace(model="test-model", dims=4),
        )
        monkeypatch.setattr(
            "zerg.services.session_processing.embeddings.generate_embedding",
            _fake_generate_embedding,
        )

        results = semantic_search(
            "find similar sessions",
            db,
            SessionFilters(project="zerg", hide_autonomous=True),
            limit=10,
        )
        result_ids = [str(session.id) for session, _score in results]

        assert result_ids == [normal_id]

    EmbeddingCache.reset()


def test_semantic_search_can_include_autonomous_when_requested(monkeypatch, tmp_path):
    """hide_autonomous=False allows sidechain/zero-user sessions in semantic mode."""
    EmbeddingCache.reset()
    SessionLocal = _make_db(tmp_path)

    normal_id = str(uuid4())
    sidechain_id = str(uuid4())
    zero_user_id = str(uuid4())

    with SessionLocal() as db:
        _create_session_with_embedding(db, session_id=normal_id, user_messages=1, is_sidechain=0, vec=[0.8, 0.2, 0.0, 0.0])
        _create_session_with_embedding(db, session_id=sidechain_id, user_messages=3, is_sidechain=1, vec=[1.0, 0.0, 0.0, 0.0])
        _create_session_with_embedding(db, session_id=zero_user_id, user_messages=0, is_sidechain=0, vec=[0.99, 0.01, 0.0, 0.0])

        monkeypatch.setattr(
            "zerg.models_config.get_embedding_config_with_db_fallback",
            lambda db: SimpleNamespace(model="test-model", dims=4),
        )
        monkeypatch.setattr(
            "zerg.services.session_processing.embeddings.generate_embedding",
            _fake_generate_embedding,
        )

        results = semantic_search(
            "find similar sessions",
            db,
            SessionFilters(project="zerg", hide_autonomous=False),
            limit=10,
        )
        result_ids = [str(session.id) for session, _score in results]

        assert set(result_ids) == {normal_id, sidechain_id, zero_user_id}
        assert len(result_ids) == 3

    EmbeddingCache.reset()
