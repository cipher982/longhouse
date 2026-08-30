from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest
import zerg.dependencies.auth as _auth_deps  # noqa: F401
import zerg.routers.timeline as timeline_router
from fastapi import HTTPException
from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession


def _make_db(tmp_path, name="timeline_stream.db"):
    engine = make_engine(f"sqlite:///{tmp_path}/{name}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_session(db, *, started_at, ended_at, project):
    session = AgentSession(
        provider="claude",
        project=project,
        started_at=started_at,
        ended_at=ended_at,
        environment="production",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


@pytest.mark.parametrize(
    ("query", "sort", "mode"),
    [
        ("thread-card-needle", None, "lexical"),
        (None, None, "hybrid"),
    ],
)
def test_timeline_stream_rejects_non_threaded_query_contracts(tmp_path, query, sort, mode):
    session_local = _make_db(tmp_path, "timeline_stream_reject_contract.db")
    now = datetime.now(timezone.utc)

    with session_local() as db:
        _seed_session(
            db,
            started_at=now - timedelta(minutes=5),
            ended_at=None,
            project="reject-stream-contract",
        )

        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(
                timeline_router.stream_timeline_sessions(
                    _ConnectedRequest(),
                    project=None,
                    provider=None,
                    environment=None,
                    include_test=False,
                    hide_autonomous=True,
                    device_id=None,
                    days_back=14,
                    query=query,
                    limit=20,
                    offset=0,
                    sort=sort,
                    mode=mode,
                    context_mode="forensic",
                )
            )

    assert excinfo.value.status_code == 400
    assert "default no-query lexical recency contract" in str(excinfo.value.detail)
