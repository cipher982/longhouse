"""HTTP-level tests for startup continuity context.

Verifies the machine-facing startup-context endpoint returns a small,
project-scoped cross-provider recap suitable for hook injection.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.database import Base
from zerg.models.agents import AgentSession


def _make_db(tmp_path):
    db_path = tmp_path / "test_startup_context_api.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_session(
    db,
    *,
    provider: str,
    project: str,
    summary: str,
    summary_title: str,
    started_at: datetime,
    last_activity_at: datetime | None = None,
    user_messages: int = 1,
    is_sidechain: int = 0,
    user_state: str = "active",
    thread_root_session_id=None,
    is_writable_head: int = 1,
):
    session_id = uuid4()
    root_id = thread_root_session_id or session_id
    session = AgentSession(
        id=session_id,
        provider=provider,
        environment="development",
        project=project,
        device_id="cinder",
        cwd=f"/tmp/{project}",
        git_repo=None,
        git_branch="main",
        started_at=started_at,
        last_activity_at=last_activity_at,
        provider_session_id=str(session_id),
        thread_root_session_id=root_id,
        user_messages=user_messages,
        assistant_messages=1,
        tool_calls=0,
        summary=summary,
        summary_title=summary_title,
        is_sidechain=is_sidechain,
        user_state=user_state,
        is_writable_head=is_writable_head,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _get_client(session_factory):
    from zerg.main import api_app

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id="startup-context", id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    client = TestClient(api_app)
    yield client
    api_app.dependency_overrides.clear()


def test_startup_context_returns_cross_provider_recent_heads(tmp_path):
    import pytest

    pytest.skip(
        "Session-identity-kernel cleanup dropped is_sidechain, is_writable_head, "
        "and thread_root_session_id from AgentSession; startup-context no longer "
        "filters by sidechain/head writability. Each session is now its own head."
    )


def test_startup_context_respects_limit_and_empty_state(tmp_path):
    factory = _make_db(tmp_path)
    now = datetime.now(timezone.utc)

    db = factory()
    try:
        for index in range(6):
            _seed_session(
                db,
                provider="claude" if index % 2 == 0 else "codex",
                project="zerg",
                summary=f"Summary {index}",
                summary_title=f"Session {index}",
                started_at=now - timedelta(hours=index + 1),
                last_activity_at=now - timedelta(hours=index + 1),
            )
    finally:
        db.close()

    for client in _get_client(factory):
        resp = client.get("/agents/sessions/startup-context", params={"project": "zerg", "limit": 3, "days_back": 14})
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["session_count"] == 3
        assert len(payload["items"]) == 3

        empty_resp = client.get("/agents/sessions/startup-context", params={"project": "missing", "limit": 5, "days_back": 14})
        assert empty_resp.status_code == 200, empty_resp.text
        empty_payload = empty_resp.json()
        assert empty_payload["session_count"] == 0
        assert empty_payload["items"] == []
        assert empty_payload["startup_context"] is None
