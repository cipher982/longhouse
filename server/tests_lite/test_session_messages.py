from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
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
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionMessage
from zerg.models.agents import SessionPresence
from zerg.services.presence_cache import get_presence_cache


def _make_db(tmp_path):
    db_path = tmp_path / "test_session_messages.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _make_client(session_factory, *, token_device_id: str = "shipper-laptop"):
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id=token_device_id, id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    return TestClient(app, backend="asyncio"), api_app


def _seed_session(
    db,
    *,
    execution_home: str = "legacy",
    managed_transport: str | None = None,
    source_runner_id: int | None = None,
    source_runner_name: str | None = None,
    device_id: str = "shipper-laptop",
    device_name: str | None = "laptop",
):
    session_id = uuid4()
    session = AgentSession(
        id=session_id,
        provider="claude",
        environment="development",
        project="zerg",
        device_id=device_id,
        device_name=device_name,
        cwd="/Users/davidrose/git/zerg",
        git_repo="git@github.com:cipher982/longhouse.git",
        git_branch="main",
        started_at=datetime.now(timezone.utc),
        provider_session_id=str(session_id),
        thread_root_session_id=session_id,
        continuation_kind="local",
        origin_label=device_id,
        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
        is_writable_head=1,
        is_sidechain=0,
        loop_mode="manual",
        execution_home=execution_home,
        managed_transport=managed_transport,
        source_runner_id=source_runner_id,
        source_runner_name=source_runner_name,
        managed_session_name=f"lh-{session_id.hex[:8]}",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _upsert_presence(db, session_id: str, state: str):
    row = db.query(SessionPresence).filter(SessionPresence.session_id == session_id).first()
    now = datetime.now(timezone.utc)
    if row is None:
        row = SessionPresence(
            session_id=session_id,
            state=state,
            tool_name=None,
            device_id="shipper-laptop",
            cwd="/Users/davidrose/git/zerg",
            project="zerg",
            provider="claude",
            updated_at=now,
        )
        db.add(row)
    else:
        row.state = state
        row.updated_at = now
        row.tool_name = None
    db.commit()


def _clear_presence_cache():
    cache = get_presence_cache()
    cache._entries.clear()  # type: ignore[attr-defined]


def test_create_message_delivers_immediately_for_safe_managed_local(monkeypatch, tmp_path):
    _clear_presence_cache()
    session_local = _make_db(tmp_path)
    send_calls: list[dict[str, object]] = []

    with session_local() as db:
        from_session = _seed_session(db, execution_home="legacy")
        to_session = _seed_session(
            db,
            execution_home="managed_local",
            managed_transport="tmux",
            source_runner_id=7,
            source_runner_name="cinder",
            device_id="cinder",
            device_name="cinder",
        )
        _upsert_presence(db, str(to_session.id), "idle")

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
        send_calls.append(
            {
                "owner_id": owner_id,
                "session_id": str(session.id),
                "text": text,
                "verify_turn_started": verify_turn_started,
            }
        )
        return SimpleNamespace(ok=True, error=None)

    monkeypatch.setattr("zerg.services.session_messages.send_text_to_managed_local_session", fake_send_text)

    client, api_app_ref = _make_client(session_local)
    try:
        response = client.post(
            "/api/agents/messages",
            json={
                "from_session_id": str(from_session.id),
                "to_session_id": str(to_session.id),
                "text": "Heads up: auth is broken.",
            },
        )
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["delivery_status"] == "delivered"
        assert len(send_calls) == 1
        assert send_calls[0]["owner_id"] == 1
        assert send_calls[0]["session_id"] == str(to_session.id)
        assert "Heads up: auth is broken." in str(send_calls[0]["text"])

        with session_local() as verify_db:
            message = verify_db.query(SessionMessage).one()
            assert message.delivery_status == "delivered"
            assert message.delivered_at is not None
    finally:
        api_app_ref.dependency_overrides = {}


def test_create_message_queues_when_target_is_running(monkeypatch, tmp_path):
    _clear_presence_cache()
    session_local = _make_db(tmp_path)

    with session_local() as db:
        from_session = _seed_session(db, execution_home="legacy")
        to_session = _seed_session(
            db,
            execution_home="managed_local",
            managed_transport="tmux",
            source_runner_id=7,
            source_runner_name="cinder",
            device_id="cinder",
            device_name="cinder",
        )
        _upsert_presence(db, str(to_session.id), "running")

    async def fail_if_called(**_kwargs):
        raise AssertionError("send_text_to_managed_local_session should not be called while running")

    monkeypatch.setattr("zerg.services.session_messages.send_text_to_managed_local_session", fail_if_called)

    client, api_app_ref = _make_client(session_local)
    try:
        response = client.post(
            "/api/agents/messages",
            json={
                "from_session_id": str(from_session.id),
                "to_session_id": str(to_session.id),
                "text": "Queue this until the current turn ends.",
            },
        )
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["delivery_status"] == "queued"

        with session_local() as verify_db:
            message = verify_db.query(SessionMessage).one()
            assert message.delivery_status == "queued"
            assert message.delivered_at is None
    finally:
        api_app_ref.dependency_overrides = {}


def test_presence_safe_transition_delivers_oldest_queued_message(monkeypatch, tmp_path):
    _clear_presence_cache()
    session_local = _make_db(tmp_path)
    send_calls: list[str] = []

    with session_local() as db:
        from_session = _seed_session(db, execution_home="legacy")
        to_session = _seed_session(
            db,
            execution_home="managed_local",
            managed_transport="tmux",
            source_runner_id=7,
            source_runner_name="cinder",
            device_id="cinder",
            device_name="cinder",
        )
        db.add(
            SessionMessage(
                from_session_id=from_session.id,
                to_session_id=to_session.id,
                body="Deliver this on the next safe boundary.",
                delivery_status="queued",
            )
        )
        db.commit()

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
        send_calls.append(text)
        return SimpleNamespace(ok=True, error=None)

    monkeypatch.setattr("zerg.services.session_messages.send_text_to_managed_local_session", fake_send_text)

    client, api_app_ref = _make_client(session_local)
    try:
        response = client.post(
            "/api/agents/presence",
            json={
                "session_id": str(to_session.id),
                "state": "idle",
                "cwd": "/Users/davidrose/git/zerg",
                "provider": "claude",
            },
        )
        assert response.status_code == 204, response.text
        assert len(send_calls) == 1

        with session_local() as verify_db:
            message = verify_db.query(SessionMessage).one()
            assert message.delivery_status == "delivered"
            assert message.delivered_at is not None
    finally:
        api_app_ref.dependency_overrides = {}


def test_create_message_stored_only_for_unmanaged_target(monkeypatch, tmp_path):
    _clear_presence_cache()
    session_local = _make_db(tmp_path)

    with session_local() as db:
        from_session = _seed_session(db, execution_home="legacy")
        to_session = _seed_session(db, execution_home="legacy", device_id="shipper-cube", device_name="cube")

    async def fail_if_called(**_kwargs):
        raise AssertionError("send_text_to_managed_local_session should not be called for unmanaged sessions")

    monkeypatch.setattr("zerg.services.session_messages.send_text_to_managed_local_session", fail_if_called)

    client, api_app_ref = _make_client(session_local)
    try:
        response = client.post(
            "/api/agents/messages",
            json={
                "from_session_id": str(from_session.id),
                "to_session_id": str(to_session.id),
                "text": "This should store without push delivery.",
            },
        )
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["delivery_status"] == "stored_only"

        with session_local() as verify_db:
            message = verify_db.query(SessionMessage).one()
            assert message.delivery_status == "stored_only"
            assert message.delivered_at is None
    finally:
        api_app_ref.dependency_overrides = {}


def test_list_messages_returns_inbound_rows_without_mutation(tmp_path):
    _clear_presence_cache()
    session_local = _make_db(tmp_path)

    with session_local() as db:
        from_session = _seed_session(db, execution_home="legacy")
        to_session = _seed_session(db, execution_home="legacy", device_id="shipper-cube", device_name="cube")
        db.add(
            SessionMessage(
                from_session_id=from_session.id,
                to_session_id=to_session.id,
                body="Stored message",
                delivery_status="stored_only",
            )
        )
        db.commit()

    client, api_app_ref = _make_client(session_local, token_device_id="shipper-cube")
    try:
        response = client.get("/api/agents/messages", params={"session_id": str(to_session.id)})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["total"] == 1
        assert data["messages"][0]["delivery_status"] == "stored_only"

        with session_local() as verify_db:
            message = verify_db.query(SessionMessage).one()
            assert message.delivery_status == "stored_only"
            assert message.acknowledged_at is None
    finally:
        api_app_ref.dependency_overrides = {}


def test_create_message_uses_current_session_header_when_body_omitted(tmp_path):
    _clear_presence_cache()
    session_local = _make_db(tmp_path)

    with session_local() as db:
        from_session = _seed_session(db, execution_home="legacy")
        to_session = _seed_session(db, execution_home="legacy", device_id="shipper-cube", device_name="cube")

    client, api_app_ref = _make_client(session_local)
    try:
        response = client.post(
            "/api/agents/messages",
            headers={"X-Longhouse-Session-Id": str(from_session.id)},
            json={
                "to_session_id": str(to_session.id),
                "text": "header-derived sender",
            },
        )
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["from_session_id"] == str(from_session.id)
        assert data["delivery_status"] == "stored_only"
    finally:
        api_app_ref.dependency_overrides = {}


def test_create_message_rejects_header_body_mismatch(tmp_path):
    _clear_presence_cache()
    session_local = _make_db(tmp_path)

    with session_local() as db:
        from_session = _seed_session(db, execution_home="legacy")
        other_session = _seed_session(db, execution_home="legacy")
        to_session = _seed_session(db, execution_home="legacy", device_id="shipper-cube", device_name="cube")

    client, api_app_ref = _make_client(session_local)
    try:
        response = client.post(
            "/api/agents/messages",
            headers={"X-Longhouse-Session-Id": str(from_session.id)},
            json={
                "from_session_id": str(other_session.id),
                "to_session_id": str(to_session.id),
                "text": "should fail",
            },
        )
        assert response.status_code == 403, response.text
        assert "does not match" in response.text
    finally:
        api_app_ref.dependency_overrides = {}


def test_list_messages_rejects_device_session_mismatch(tmp_path):
    _clear_presence_cache()
    session_local = _make_db(tmp_path)

    with session_local() as db:
        from_session = _seed_session(db, execution_home="legacy")
        to_session = _seed_session(db, execution_home="legacy", device_id="shipper-cube", device_name="cube")
        db.add(
            SessionMessage(
                from_session_id=from_session.id,
                to_session_id=to_session.id,
                body="Stored message",
                delivery_status="stored_only",
            )
        )
        db.commit()

    client, api_app_ref = _make_client(session_local, token_device_id="shipper-laptop")
    try:
        response = client.get("/api/agents/messages", params={"session_id": str(to_session.id)})
        assert response.status_code == 403, response.text
        assert "Authenticated device cannot act" in response.text
    finally:
        api_app_ref.dependency_overrides = {}


def test_acknowledge_message_sets_acknowledged_at_and_filters_unacknowledged(tmp_path):
    _clear_presence_cache()
    session_local = _make_db(tmp_path)

    with session_local() as db:
        from_session = _seed_session(db, execution_home="legacy")
        to_session = _seed_session(db, execution_home="legacy", device_id="shipper-cube", device_name="cube")
        db.add(
            SessionMessage(
                from_session_id=from_session.id,
                to_session_id=to_session.id,
                body="Stored message",
                delivery_status="stored_only",
            )
        )
        db.commit()
        message = db.query(SessionMessage).one()
        message_id = message.id

    client, api_app_ref = _make_client(session_local, token_device_id="shipper-cube")
    try:
        ack_response = client.post(
            f"/api/agents/messages/{message_id}/ack",
            headers={"X-Longhouse-Session-Id": str(to_session.id)},
        )
        assert ack_response.status_code == 200, ack_response.text
        assert ack_response.json()["acknowledged_at"] is not None

        unacked_response = client.get(
            "/api/agents/messages",
            headers={"X-Longhouse-Session-Id": str(to_session.id)},
            params={"direction": "inbound", "unacknowledged_only": True},
        )
        assert unacked_response.status_code == 200, unacked_response.text
        assert unacked_response.json()["total"] == 0
    finally:
        api_app_ref.dependency_overrides = {}


def test_acknowledge_message_rejects_queued_delivery(tmp_path):
    _clear_presence_cache()
    session_local = _make_db(tmp_path)

    with session_local() as db:
        from_session = _seed_session(db, execution_home="legacy")
        to_session = _seed_session(db, execution_home="managed_local", device_id="shipper-cube", device_name="cube")
        db.add(
            SessionMessage(
                from_session_id=from_session.id,
                to_session_id=to_session.id,
                body="Queued message",
                delivery_status="queued",
            )
        )
        db.commit()
        message = db.query(SessionMessage).one()
        message_id = message.id

    client, api_app_ref = _make_client(session_local, token_device_id="shipper-cube")
    try:
        response = client.post(
            f"/api/agents/messages/{message_id}/ack",
            headers={"X-Longhouse-Session-Id": str(to_session.id)},
        )
        assert response.status_code == 409, response.text
        assert "has not been delivered" in response.text
    finally:
        api_app_ref.dependency_overrides = {}
