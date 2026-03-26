from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.oikos_auth import get_current_oikos_user
from zerg.models.enums import UserRole
from zerg.models.user import User
from zerg.routers import session_chat
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest


def _make_db(tmp_path):
    db_path = tmp_path / "test_session_chat.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _make_client(session_local, current_user):
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        db = session_local()
        try:
            yield db
        finally:
            db.close()

    def override_current_user():
        return current_user

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[get_current_oikos_user] = override_current_user
    return TestClient(app, backend="asyncio"), api_app


def test_fake_cloud_continuation_persists_turn_for_follow_up_requests(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    project = "resume-send-test"
    source_session_id = uuid4()
    provider_session_id = f"resume-send-{uuid4().hex[:8]}"

    with session_local() as db:
        user = User(email="session-chat@test.local", role=UserRole.USER.value)
        db.add(user)
        db.commit()
        db.refresh(user)

        store = AgentsStore(db)
        started_at = datetime.now(timezone.utc)
        store.ingest_session(
            SessionIngest(
                id=source_session_id,
                provider="claude",
                environment="Cinder",
                project=project,
                device_id="e2e-device",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                provider_session_id=provider_session_id,
                started_at=started_at,
                ended_at=started_at,
                events=[
                    EventIngest(
                        role="user",
                        content_text="Started on laptop before cloud continuation",
                        timestamp=started_at,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )
        db.commit()
        user_id = user.id

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="session-chat@test.local", role=UserRole.USER.value),
    )
    monkeypatch.setenv("E2E_FAKE_SESSION_CHAT", "1")

    async def fake_resolve(*, original_cwd, git_repo, git_branch, session_id):
        return SimpleNamespace(path=Path("/tmp"), is_temp=False, error=None)

    async def fake_prepare(*, session_id, workspace_path, db):
        return provider_session_id

    monkeypatch.setattr(session_chat.workspace_resolver, "resolve", fake_resolve)
    monkeypatch.setattr(session_chat, "prepare_session_for_resume", fake_prepare)

    try:
        response = client.post(
            f"/api/sessions/{source_session_id}/chat",
            json={"message": "anything else?"},
        )
        assert response.status_code == 200, response.text
        assert '"persisted_events": 2' in response.text
        assert '"created_continuation": true' in response.text

        with session_local() as db:
            store = AgentsStore(db)
            source_session = store.get_session(source_session_id)
            assert source_session is not None
            target_session = store.get_thread_head(source_session)
            assert target_session is not None
            assert target_session.id != source_session_id
            assert target_session.project == project
            assert target_session.user_messages == 1
            assert target_session.assistant_messages == 1

            total, rows = store.list_timeline_thread_page(
                project=project,
                since=started_at.replace(hour=0, minute=0, second=0, microsecond=0),
                limit=20,
                offset=0,
                hide_autonomous=True,
            )
            assert total == 1
            assert len(rows) == 1
            assert rows[0][0] == str(source_session_id)
            assert rows[0][1] == str(target_session.id)
    finally:
        api_app_ref.dependency_overrides = {}
