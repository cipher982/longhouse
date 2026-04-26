from __future__ import annotations

import asyncio
import os
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.oikos_auth import get_current_oikos_user
from zerg.models.agents import SessionInput
from zerg.models.enums import UserRole
from zerg.models.user import User
from zerg.routers import session_chat
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest
from zerg.services.session_continuity import session_lock_manager
from zerg.services.session_inputs import INPUT_STATUS_CANCELLED
from zerg.services.session_inputs import INPUT_STATUS_DELIVERED
from zerg.services.session_inputs import INPUT_STATUS_QUEUED


def _make_db(tmp_path):
    db_path = tmp_path / "test_session_inputs.db"
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


def _seed_live_session(session_local):
    session_id = uuid4()
    provider_session_id = f"session-input-{uuid4().hex[:8]}"
    with session_local() as db:
        user = User(email=f"input-{uuid4().hex[:6]}@test.local", role=UserRole.USER.value)
        db.add(user)
        db.commit()
        db.refresh(user)

        store = AgentsStore(db)
        started_at = datetime.now(timezone.utc)
        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="claude",
                environment="Cinder",
                project="session-input-api",
                device_id="cinder",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                provider_session_id=provider_session_id,
                started_at=started_at,
                ended_at=started_at,
                events=[
                    EventIngest(
                        role="user",
                        content_text="seed",
                        timestamp=started_at,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )
        session = store.get_session(session_id)
        assert session is not None
        session.execution_home = "managed_local"
        session.managed_transport = "claude_channel_bridge"
        session.source_runner_id = 1
        session.source_runner_name = "cinder"
        session.managed_session_name = "lh-input"
        db.commit()
        user_id = user.id

    return session_id, user_id


def _stub_dispatch(monkeypatch):
    """Happy-path fake for live_session_dispatch + skip background tasks."""
    calls: list[dict] = []

    async def fake_send_text(
        *,
        db,
        owner_id,
        session,
        text,
        commis_id=None,
        timeout_secs=15,
        verify_turn_started=False,
        verification_timeout_secs=None,
    ):
        calls.append({"session_id": str(session.id), "text": text, "commis_id": commis_id})
        return SimpleNamespace(ok=True, exit_code=0, error=None, verified_turn_started=True)

    monkeypatch.setattr("zerg.services.live_session_dispatch.send_text_to_live_session", fake_send_text)
    monkeypatch.setattr("zerg.services.session_chat_impl._schedule_managed_local_lock_release", lambda **_kwargs: None)
    monkeypatch.setattr(
        "zerg.services.session_chat_impl._schedule_managed_local_active_phase_observation",
        lambda **_kwargs: None,
    )
    return calls


def test_intent_auto_not_locked_returns_sent(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    calls = _stub_dispatch(monkeypatch)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "hello", "intent": "auto"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["outcome"] == "sent"
        assert body["intent"] == "auto"
        assert body["queued"] == []
        assert len(calls) == 1
        assert calls[0]["text"] == "hello"

        # Row is marked delivered
        with session_local() as db:
            row = db.query(SessionInput).filter(SessionInput.session_id == session_id).one()
            assert row.status == INPUT_STATUS_DELIVERED
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        api_app_ref.dependency_overrides = {}


def test_intent_queue_always_persists_queued(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    calls = _stub_dispatch(monkeypatch)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "queued message", "intent": "queue"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["outcome"] == "queued"
        assert len(body["queued"]) == 1
        assert body["queued"][0]["text"] == "queued message"
        # No dispatch happens for queue intent
        assert calls == []

        with session_local() as db:
            row = db.query(SessionInput).filter(SessionInput.session_id == session_id).one()
            assert row.status == INPUT_STATUS_QUEUED
            assert row.intent == "queue"
    finally:
        api_app_ref.dependency_overrides = {}


def test_intent_auto_locked_returns_queued(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    _stub_dispatch(monkeypatch)

    # Pre-acquire the lock on the session scope.
    lock_scope_id = str(session_id)
    acquired = asyncio.run(
        session_lock_manager.acquire(session_id=lock_scope_id, holder="other", ttl_seconds=60)
    )
    assert acquired

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "send if free", "intent": "auto"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["outcome"] == "queued"
        assert body["intent"] == "auto"
        assert len(body["queued"]) == 1

        with session_local() as db:
            row = db.query(SessionInput).filter(SessionInput.session_id == session_id).one()
            assert row.status == INPUT_STATUS_QUEUED
            assert row.intent == "auto"
    finally:
        asyncio.run(session_lock_manager.release(lock_scope_id, "other"))
        api_app_ref.dependency_overrides = {}


def test_list_and_cancel_queued(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    _stub_dispatch(monkeypatch)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        post = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "wait your turn", "intent": "queue"},
        )
        assert post.status_code == 200
        input_id = post.json()["input_id"]

        listed = client.get(f"/api/sessions/{session_id}/inputs")
        assert listed.status_code == 200
        rows = listed.json()
        assert len(rows) == 1
        assert rows[0]["id"] == input_id

        cancelled = client.delete(f"/api/sessions/{session_id}/inputs/{input_id}")
        assert cancelled.status_code == 200
        assert cancelled.json()["cancelled"] is True

        listed2 = client.get(f"/api/sessions/{session_id}/inputs")
        assert listed2.status_code == 200
        assert listed2.json() == []

        with session_local() as db:
            row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
            assert row.status == INPUT_STATUS_CANCELLED
    finally:
        api_app_ref.dependency_overrides = {}


def test_intent_steer_is_not_implemented(tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "steer please", "intent": "steer"},
        )
        assert resp.status_code == 501, resp.text
    finally:
        api_app_ref.dependency_overrides = {}


def test_capability_includes_can_queue_next_input():
    from zerg.services.session_capabilities import build_session_capabilities

    session = SimpleNamespace(
        execution_home="managed_local",
        managed_transport="claude_channel_bridge",
        source_runner_id=1,
        continuation_kind=None,
        origin_label=None,
        environment=None,
    )
    caps = build_session_capabilities(session)
    assert caps.live_control_available is True
    assert caps.can_queue_next_input is True

    session_no_runner = SimpleNamespace(
        execution_home="managed_local",
        managed_transport="claude_channel_bridge",
        source_runner_id=None,
        continuation_kind=None,
        origin_label=None,
        environment=None,
    )
    caps2 = build_session_capabilities(session_no_runner)
    assert caps2.live_control_available is False
    assert caps2.can_queue_next_input is False


def test_startup_reconciliation_rewinds_stuck_delivering(tmp_path):
    from datetime import timedelta

    from zerg.services.session_inputs import create_session_input
    from zerg.services.session_inputs import requeue_stuck_delivering

    session_local = _make_db(tmp_path)
    session_id, _ = _seed_live_session(session_local)

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="stuck",
            intent="auto",
            status="delivering",
            request_id="old",
        )
        row.updated_at = datetime.now(timezone.utc) - timedelta(seconds=300)
        db.commit()
        requeued = requeue_stuck_delivering(db)
        assert requeued == 1
        db.expire_all()
        refreshed = db.query(SessionInput).filter(SessionInput.id == row.id).one()
        assert refreshed.status == INPUT_STATUS_QUEUED
