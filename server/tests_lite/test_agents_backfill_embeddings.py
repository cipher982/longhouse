"""Tests for admin embedding backfill convergence."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.routers import agents_backfill


def test_embedding_backfill_drains_all_slices_for_session(tmp_path, monkeypatch):
    db_path = tmp_path / "embedding_backfill.db"
    database_url = f"sqlite:///{db_path}"
    engine = make_engine(database_url)
    Base.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)

    db = factory()
    session = AgentSession(
        provider="codex",
        environment="test",
        project="zerg",
        started_at=datetime.now(timezone.utc),
        transcript_revision=7,
        embedding_revision=0,
        needs_embedding=1,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    db.add(
        AgentEvent(
            session_id=session.id,
            role="user",
            content_text="Backfill all embedding slices.",
            timestamp=datetime.now(timezone.utc),
        )
    )
    db.commit()
    session_id = str(session.id)
    db.close()

    calls: list[int] = []
    completed: list[tuple[str, int | None]] = []

    async def _fake_embed_session(*_args, **_kwargs):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            return 2, 3
        return 3, 0

    async def _fake_mark_complete(marked_session_id, *, transcript_revision=None, db=None):
        completed.append((marked_session_id, transcript_revision))

    monkeypatch.setattr(agents_backfill, "get_settings", lambda: SimpleNamespace(database_url=database_url))
    monkeypatch.setattr("zerg.services.session_processing.embeddings.embed_session", _fake_embed_session)
    monkeypatch.setattr(
        "zerg.services.session_processing.embeddings.mark_session_embedding_complete",
        _fake_mark_complete,
    )

    asyncio.run(
        agents_backfill._run_embedding_backfill(
            concurrency=1,
            project=None,
            force=False,
            config=object(),
            total=1,
        )
    )

    assert calls == [1, 2]
    assert completed == [(session_id, 7)]
    assert agents_backfill._embedding_backfill_state["embedded"] == 1
    assert agents_backfill._embedding_backfill_state["skipped"] == 0
    assert agents_backfill._embedding_backfill_state["errors"] == 0
    assert agents_backfill._embedding_backfill_state["remaining"] == 0
