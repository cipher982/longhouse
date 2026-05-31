from __future__ import annotations

import asyncio
import os
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.routers.agents_search import recall_sessions
from zerg.services.agents_store import AgentsStore
from zerg.services.session_response_projection import has_real_sessions


def _make_db(tmp_path):
    db_path = tmp_path / "test_session_listing_canary_filter.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_session(
    db,
    *,
    provider: str,
    project: str,
    device_id: str,
    user_messages: int = 0,
    ended: bool = False,
) -> AgentSession:
    session = AgentSession(
        id=uuid4(),
        provider=provider,
        environment="production",
        project=project,
        device_id=device_id,
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc) if ended else None,
        user_messages=user_messages,
        assistant_messages=0,
        tool_calls=0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def test_default_session_listing_hides_internal_canary_sessions(tmp_path):
    factory = _make_db(tmp_path)
    with factory() as db:
        canary = _seed_session(db, provider="canary", project="canary", device_id="demo-machine-canary")
        legacy_typo = _seed_session(db, provider="cnary", project="cnary", device_id="demo-machine-cnary")
        _seed_session(db, provider="codex", project="canary-stress", device_id="demo-machine-canary", user_messages=1)
        visible = _seed_session(db, provider="codex", project="zerg", device_id="cinder", user_messages=1)
        store = AgentsStore(db)

        sessions, total = store.list_sessions(hide_autonomous=False)
        assert total == 1
        assert [session.id for session in sessions] == [visible.id]

        project_sessions, project_total = store.list_sessions(project="canary", hide_autonomous=False)
        assert project_total == 0
        assert project_sessions == []

        typo_sessions, typo_total = store.list_sessions(provider="cnary", hide_autonomous=False)
        assert typo_total == 1
        assert [session.id for session in typo_sessions] == [legacy_typo.id]

        canary_sessions, canary_total = store.list_sessions(provider="canary", hide_autonomous=False)
        assert canary_total == 1
        assert [session.id for session in canary_sessions] == [canary.id]

        codex_canary_sessions, codex_canary_total = store.list_sessions(provider="codex", project="canary", hide_autonomous=False)
        assert codex_canary_total == 0
        assert codex_canary_sessions == []


def test_timeline_thread_listing_hides_internal_canary_sessions_by_default(tmp_path):
    factory = _make_db(tmp_path)
    with factory() as db:
        canary = _seed_session(db, provider="canary", project="canary-stress", device_id="demo-machine-canary")
        typo = _seed_session(db, provider="cnary", project="cnary", device_id="demo-machine-cnary")
        _seed_session(db, provider="codex", project="zerg", device_id="demo-machine-canary", user_messages=1)
        visible = _seed_session(db, provider="claude", project="zerg", device_id="cinder", user_messages=1)
        store = AgentsStore(db)

        total, rows = store.list_timeline_thread_page(hide_autonomous=False)
        assert total == 1
        assert [(thread_id, session_id) for thread_id, session_id, _anchor in rows] == [(str(visible.id), str(visible.id))]

        canary_total, canary_rows = store.list_timeline_thread_page(provider="canary", hide_autonomous=False)
        assert canary_total == 1
        assert [(thread_id, session_id) for thread_id, session_id, _anchor in canary_rows] == [(str(canary.id), str(canary.id))]

        typo_total, typo_rows = store.list_timeline_thread_page(provider="cnary", hide_autonomous=False)
        assert typo_total == 1
        assert [(thread_id, session_id) for thread_id, session_id, _anchor in typo_rows] == [(str(typo.id), str(typo.id))]


def test_canary_filter_does_not_hide_real_device_names_with_canary_substring(tmp_path):
    factory = _make_db(tmp_path)
    with factory() as db:
        visible = _seed_session(db, provider="claude", project="zerg", device_id="canary-mbp", user_messages=1)
        store = AgentsStore(db)

        sessions, total = store.list_sessions(hide_autonomous=False)

        assert total == 1
        assert [session.id for session in sessions] == [visible.id]


def test_canary_sessions_do_not_make_demo_data_count_as_real(tmp_path):
    factory = _make_db(tmp_path)
    with factory() as db:
        _seed_session(db, provider="claude", project="demo", device_id="demo-mac", user_messages=1)
        _seed_session(db, provider="canary", project="canary", device_id="demo-machine-canary")

        assert has_real_sessions(db, default_when_empty=False) is False

        _seed_session(db, provider="codex", project="zerg", device_id="cinder", user_messages=1)
        assert has_real_sessions(db, default_when_empty=False) is True


def test_recall_hides_internal_canary_sessions(monkeypatch, tmp_path):
    factory = _make_db(tmp_path)

    class FakeEmbeddingCache:
        def __init__(self):
            self._session_loaded = True
            self._turn_loaded = True

        def load_session_embeddings(self, db, model, dims):
            return 0

        def load_turn_embeddings(self, db, model, dims):
            return 0

        def search_turns(self, query_vec, limit, session_filter):
            return [(session_id, 0, 0.9, 0, 0) for session_id in sorted(session_filter)]

    async def fake_generate_embedding(query, config):
        return [1.0]

    monkeypatch.setattr("zerg.models_config.get_embedding_config", lambda: SimpleNamespace(model="test", dims=1))
    monkeypatch.setattr("zerg.services.embedding_cache.EmbeddingCache", FakeEmbeddingCache)
    monkeypatch.setattr("zerg.services.session_processing.embeddings.generate_embedding", fake_generate_embedding)

    with factory() as db:
        canary = _seed_session(db, provider="canary", project="canary", device_id="demo-machine-canary", user_messages=1)
        typo = _seed_session(db, provider="cnary", project="cnary", device_id="demo-machine-cnary", user_messages=1)
        mislabeled = _seed_session(db, provider="codex", project="canary-stress", device_id="demo-machine-canary", user_messages=1)
        visible = _seed_session(db, provider="codex", project="zerg", device_id="cinder", user_messages=1)
        for session in (canary, typo, mislabeled, visible):
            db.add(
                AgentEvent(
                    session_id=session.id,
                    role="user",
                    content_text="launch review",
                    timestamp=datetime.now(timezone.utc),
                )
            )
        db.commit()

        response = asyncio.run(
                recall_sessions(
                    query="launch review",
                    project=None,
                    max_results=10,
                    since_days=14,
                    context_turns=2,
                    context_mode="forensic",
                    db=db,
                    _auth=None,
                _single=None,
            )
        )

    assert [match.session_id for match in response.matches] == [str(visible.id)]
