from __future__ import annotations

import asyncio
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-1234")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-1234")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-google-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-google-client-secret")

from tests_lite._kernel_test_helpers import seed_managed_kernel_rows
from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.browser_route_auth import get_current_browser_route_user
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionInput
from zerg.models.agents import SessionTurn
from zerg.models.enums import UserRole
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest
from zerg.services.runner_connection_manager import get_runner_connection_manager
from zerg.services.session_continuity import session_lock_manager
from zerg.services.session_inputs import INPUT_STATUS_CANCELLED
from zerg.services.session_inputs import INPUT_STATUS_DELIVERED
from zerg.services.session_inputs import INPUT_STATUS_DELIVERING
from zerg.services.session_inputs import INPUT_STATUS_FAILED
from zerg.services.session_inputs import INPUT_STATUS_QUEUED
from zerg.services.session_inputs import create_session_input
from zerg.services.session_runtime import phase_freshness_ms
from zerg.services.session_runtime import runtime_key_for_session


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
    api_app.dependency_overrides[get_current_browser_route_user] = override_current_user
    return TestClient(app, backend="asyncio"), api_app


def _seed_live_runtime_state(db, session, *, phase: str = "idle") -> None:
    from zerg.models.agents import SessionRuntimeState

    now = datetime.now(timezone.utc)
    freshness_ms = phase_freshness_ms(phase) or int(timedelta(minutes=5).total_seconds() * 1000)
    key = runtime_key_for_session(str(session.provider or "claude"), str(session.id))
    state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == key).first()
    if state is None:
        state = SessionRuntimeState(
            runtime_key=key,
            session_id=session.id,
            provider=str(session.provider or "claude"),
            device_id=session.device_id,
        )
        db.add(state)
    state.phase = phase
    state.phase_source = "semantic"
    state.phase_started_at = now
    state.last_runtime_signal_at = now
    state.last_progress_at = now
    state.last_live_at = now
    state.timeline_anchor_at = now
    state.freshness_expires_at = now + timedelta(milliseconds=freshness_ms)
    state.terminal_state = None
    state.terminal_at = None
    state.runtime_version = int(getattr(state, "runtime_version", 0) or 0) + 1
    db.commit()


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
        seed_managed_kernel_rows(db, session, control_plane="claude_channel_bridge")
        runner = Runner(
            id=1,
            owner_id=user.id,
            name="cinder",
            status="online",
            auth_secret_hash="test",
        )
        db.merge(runner)
        db.commit()
        get_runner_connection_manager().register(user.id, 1, SimpleNamespace())
        _seed_live_runtime_state(db, session)
        user_id = user.id

    return session_id, user_id


def _stub_dispatch(monkeypatch, *, emit_verified_user_event: bool = False):
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
        attachments=None,
    ):
        calls.append({"session_id": str(session.id), "text": text, "commis_id": commis_id})
        verified_user_event_id = None
        if emit_verified_user_event:
            event = AgentEvent(
                session_id=session.id,
                role="user",
                content_text=text,
                timestamp=datetime.now(timezone.utc),
            )
            db.add(event)
            db.flush()
            verified_user_event_id = int(event.id)
        return SimpleNamespace(
            ok=True,
            exit_code=0,
            error=None,
            verified_turn_started=True,
            verified_user_event_id=verified_user_event_id,
        )

    monkeypatch.setattr("zerg.services.live_session_dispatch.send_text_to_live_session", fake_send_text)
    monkeypatch.setattr("zerg.services.session_chat_impl._schedule_managed_local_lock_release", lambda **_kwargs: None)
    monkeypatch.setattr(
        "zerg.services.session_chat_impl._schedule_managed_local_active_phase_observation",
        lambda **_kwargs: None,
    )
    return calls


def test_session_input_api_schema_exposes_typed_lifecycle_contract():
    from zerg.routers.session_chat import QueuedInputSummary
    from zerg.routers.session_chat import SessionInputRequest
    from zerg.routers.session_chat import SessionInputResponse

    request_schema = SessionInputRequest.model_json_schema()
    response_schema = SessionInputResponse.model_json_schema()
    queued_schema = QueuedInputSummary.model_json_schema()

    assert request_schema["properties"]["intent"]["enum"] == ["auto", "queue", "steer"]
    assert response_schema["properties"]["outcome"]["enum"] == ["sent", "queued"]
    assert response_schema["properties"]["intent"]["enum"] == ["auto", "queue", "steer"]
    assert queued_schema["properties"]["intent"]["enum"] == ["auto", "queue", "steer"]
    assert queued_schema["properties"]["status"]["enum"] == [
        "queued",
        "delivering",
        "delivered",
        "cancelled",
        "failed",
    ]


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


def test_client_request_id_dedupes_delivered_auto(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    calls = _stub_dispatch(monkeypatch)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        payload = {"text": "hello once", "intent": "auto", "client_request_id": "ios-request-1"}
        first = client.post(f"/api/sessions/{session_id}/input", json=payload)
        second = client.post(f"/api/sessions/{session_id}/input", json=payload)

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["input_id"] == second.json()["input_id"]
        assert second.json()["outcome"] == "sent"
        assert len(calls) == 1
        with session_local() as db:
            rows = db.query(SessionInput).filter(SessionInput.session_id == session_id).all()
            assert len(rows) == 1
            assert rows[0].client_request_id == "ios-request-1"
            assert rows[0].delivery_request_id
            assert rows[0].status == INPUT_STATUS_DELIVERED
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        api_app_ref.dependency_overrides = {}


def test_auto_input_links_session_turn_to_verified_user_event(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    _stub_dispatch(monkeypatch, emit_verified_user_event=True)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "linked from ios", "intent": "auto", "client_request_id": "ios-link-1"},
        )
        assert resp.status_code == 200, resp.text

        with session_local() as db:
            row = db.query(SessionInput).filter(SessionInput.session_id == session_id).one()
            assert row.status == INPUT_STATUS_DELIVERED
            assert row.client_request_id == "ios-link-1"
            assert row.delivery_request_id

            turn = (
                db.query(SessionTurn)
                .filter(SessionTurn.session_id == session_id, SessionTurn.request_id == row.delivery_request_id)
                .one()
            )
            assert turn.session_input_id == row.id
            assert turn.user_event_id is not None

            event = db.query(AgentEvent).filter(AgentEvent.id == turn.user_event_id).one()
            assert event.content_text == "linked from ios"
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


def test_client_request_id_dedupes_queued_input(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    _stub_dispatch(monkeypatch)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        payload = {"text": "queued once", "intent": "queue", "client_request_id": "ios-queued-1"}
        first = client.post(f"/api/sessions/{session_id}/input", json=payload)
        second = client.post(f"/api/sessions/{session_id}/input", json=payload)

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["input_id"] == second.json()["input_id"]
        assert second.json()["outcome"] == "queued"
        with session_local() as db:
            rows = db.query(SessionInput).filter(SessionInput.session_id == session_id).all()
            assert len(rows) == 1
            assert rows[0].client_request_id == "ios-queued-1"
            assert rows[0].delivery_request_id is None
            assert rows[0].status == INPUT_STATUS_QUEUED
    finally:
        api_app_ref.dependency_overrides = {}


def test_client_request_id_unique_constraint_blocks_duplicate_rows(tmp_path):
    from sqlalchemy.exc import IntegrityError

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    with session_local() as db:
        create_session_input(
            db,
            session_id=session_id,
            text="once",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-unique-1",
        )
        try:
            create_session_input(
                db,
                session_id=session_id,
                text="twice",
                owner_id=user_id,
                intent="queue",
                status=INPUT_STATUS_QUEUED,
                client_request_id="ios-unique-1",
            )
        except IntegrityError:
            db.rollback()
        else:
            raise AssertionError("duplicate client request id inserted")

        rows = db.query(SessionInput).filter(SessionInput.session_id == session_id).all()
        assert len(rows) == 1
        assert rows[0].body == "once"


def test_client_request_id_same_key_different_owner_creates_separate_inputs(tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    with session_local() as db:
        second_user = User(email="second-owner@test.local", role=UserRole.USER.value)
        db.add(second_user)
        db.flush()
        first = create_session_input(
            db,
            session_id=session_id,
            text="same owner scoped id",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="shared-client-key",
        )
        second = create_session_input(
            db,
            session_id=session_id,
            text="same owner scoped id",
            owner_id=second_user.id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="shared-client-key",
        )
        db.commit()

        assert first.id != second.id
        rows = (
            db.query(SessionInput)
            .filter(SessionInput.session_id == session_id, SessionInput.client_request_id == "shared-client-key")
            .all()
        )
        assert {row.owner_id for row in rows} == {user_id, second_user.id}


def test_duplicate_integrity_retry_path_reuses_failed_input(tmp_path):
    from zerg.routers.session_chat import SessionInputRequest
    from zerg.routers.session_chat import _create_session_input_or_existing

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    with session_local() as db:
        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        failed = create_session_input(
            db,
            session_id=session_id,
            text="retry after failed race",
            owner_id=user_id,
            intent="auto",
            status=INPUT_STATUS_FAILED,
            client_request_id="race-client-key",
            delivery_request_id="old-delivery",
        )
        db.commit()
        failed_id = int(failed.id)

        row = _create_session_input_or_existing(
            db=db,
            source_session=session,
            owner_id=user_id,
            body=SessionInputRequest(
                text="retry after failed race",
                intent="auto",
                client_request_id="race-client-key",
            ),
            intent="auto",
            status_value=INPUT_STATUS_DELIVERING,
            client_request_id="race-client-key",
            delivery_request_id="new-delivery",
        )
        db.commit()

        assert isinstance(row, SessionInput)
        assert int(row.id) == failed_id
        assert row.status == INPUT_STATUS_DELIVERING
        assert row.delivery_request_id == "new-delivery"


def test_queue_drain_preserves_client_request_id(tmp_path):
    from zerg.services.session_inputs import claim_next_queued

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="drain me",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-drain-1",
        )

        claimed = claim_next_queued(db, session_id, delivery_request_id="drain-delivery-1")

        assert claimed is not None
        assert claimed.id == row.id
        assert claimed.client_request_id == "ios-drain-1"
        assert claimed.delivery_request_id == "drain-delivery-1"


def test_queue_drain_links_session_turn_to_session_input(monkeypatch, tmp_path):
    from zerg.services.session_chat_impl import _drain_next_queued_input
    from zerg.services.session_inputs import create_session_input

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    _stub_dispatch(monkeypatch, emit_verified_user_event=True)

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="drained from ios",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-drain-origin-1",
        )
        db.commit()
        input_id = int(row.id)
        db_bind = db.get_bind()

    try:
        asyncio.run(
            _drain_next_queued_input(
                db_bind=db_bind,
                session_id=session_id,
                lock_scope_id=str(session_id),
            )
        )

        with session_local() as db:
            row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
            assert row.status == INPUT_STATUS_DELIVERED
            assert row.client_request_id == "ios-drain-origin-1"
            assert row.delivery_request_id
            assert row.delivery_request_id.startswith("drain-")

            turn = (
                db.query(SessionTurn)
                .filter(SessionTurn.session_id == session_id, SessionTurn.request_id == row.delivery_request_id)
                .one()
            )
            assert turn.session_input_id == input_id
            assert turn.user_event_id is not None
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))


def test_client_request_id_different_text_conflicts(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    _stub_dispatch(monkeypatch)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        first = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "original", "intent": "queue", "client_request_id": "ios-conflict-1"},
        )
        second = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "edited", "intent": "queue", "client_request_id": "ios-conflict-1"},
        )

        assert first.status_code == 200, first.text
        assert second.status_code == 409, second.text
        assert second.json()["detail"] == {
            "error_code": "input_conflict",
            "existing_input_id": first.json()["input_id"],
            "reason": "different_text",
        }
    finally:
        api_app_ref.dependency_overrides = {}


def test_cancelled_client_request_id_conflicts_on_retry(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    _stub_dispatch(monkeypatch)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        first = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "cancel me", "intent": "queue", "client_request_id": "ios-cancelled-1"},
        )
        assert first.status_code == 200, first.text

        with session_local() as db:
            row = db.query(SessionInput).filter(SessionInput.id == first.json()["input_id"]).one()
            row.status = INPUT_STATUS_CANCELLED
            db.commit()

        retry = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "cancel me", "intent": "queue", "client_request_id": "ios-cancelled-1"},
        )
        assert retry.status_code == 409, retry.text
        assert retry.json()["detail"] == {
            "error_code": "input_conflict",
            "existing_input_id": first.json()["input_id"],
            "reason": "cancelled",
        }
    finally:
        api_app_ref.dependency_overrides = {}


def test_client_request_id_failed_retry_reuses_row(monkeypatch, tmp_path):
    from zerg.services.session_inputs import create_session_input

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    _stub_dispatch(monkeypatch)

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="failed once",
            owner_id=user_id,
            intent="auto",
            status=INPUT_STATUS_FAILED,
            client_request_id="ios-failed-1",
            delivery_request_id="old-delivery",
        )
        row.last_error = "provider disconnected"
        input_id = int(row.id)
        db.commit()

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        retry = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "failed once", "intent": "auto", "client_request_id": "ios-failed-1"},
        )
        assert retry.status_code == 200, retry.text
        assert retry.json()["outcome"] == "sent"
        with session_local() as db:
            rows = db.query(SessionInput).filter(SessionInput.session_id == session_id).all()
            assert len(rows) == 1
            assert rows[0].id == input_id
            assert rows[0].client_request_id == "ios-failed-1"
            assert rows[0].delivery_request_id != "old-delivery"
            assert rows[0].status == INPUT_STATUS_DELIVERED
    finally:
        api_app_ref.dependency_overrides = {}


def test_retry_failed_input_rejects_terminal_rows(tmp_path):
    from zerg.services.session_inputs import retry_failed_input

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="already sent",
            owner_id=user_id,
            intent="auto",
            status=INPUT_STATUS_DELIVERED,
            client_request_id="ios-delivered-1",
            delivery_request_id="old-delivery",
        )
        input_id = int(row.id)

        retried = retry_failed_input(
            db,
            input_id,
            intent="auto",
            status=INPUT_STATUS_DELIVERING,
            delivery_request_id="new-delivery",
        )

        db.expire_all()
        refreshed = db.query(SessionInput).filter(SessionInput.id == input_id).one()
        assert retried is None
        assert refreshed.status == INPUT_STATUS_DELIVERED
        assert refreshed.client_request_id == "ios-delivered-1"
        assert refreshed.delivery_request_id == "old-delivery"


def test_intent_auto_locked_returns_queued(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    _stub_dispatch(monkeypatch)

    # Pre-acquire the lock on the session scope.
    lock_scope_id = str(session_id)
    acquired = asyncio.run(session_lock_manager.acquire(session_id=lock_scope_id, holder="other", ttl_seconds=60))
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


def _seed_codex_session(session_local):
    """Seed a managed-local session on codex_app_server transport so the
    capability gate for steer is satisfied."""
    from zerg.models.agents import SessionConnection
    from zerg.models.agents import SessionRun
    from zerg.models.agents import SessionRuntimeState
    from zerg.models.agents import SessionThread

    session_id, user_id = _seed_live_session(session_local)
    with session_local() as db:
        session = db.query(AgentSession).filter_by(id=session_id).one()
        session.provider = "codex"
        session.managed_transport = "codex_app_server"
        db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session.id).delete(
            synchronize_session=False
        )
        thread = (
            db.query(SessionThread).filter(SessionThread.session_id == session.id, SessionThread.is_primary == 1).one()
        )
        thread.provider = "codex"
        run = db.query(SessionRun).filter(SessionRun.thread_id == thread.id, SessionRun.ended_at.is_(None)).one()
        run.provider = "codex"
        conn = db.query(SessionConnection).filter(SessionConnection.run_id == run.id).one()
        conn.control_plane = "codex_bridge"
        db.commit()
        _seed_live_runtime_state(db, session, phase="running")
    return session_id, user_id


def test_intent_steer_requires_codex_capability(monkeypatch, tmp_path):
    """Claude-channel sessions cannot steer; must return 409 steer_unsupported."""
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)  # defaults to claude_channel_bridge
    _stub_dispatch(monkeypatch)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "steer now", "intent": "steer"},
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error_code"] == "steer_unsupported"
    finally:
        api_app_ref.dependency_overrides = {}


def test_intent_steer_success_returns_sent(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_codex_session(session_local)

    async def fake_steer(*, db, owner_id, session, text, commis_id=None, timeout_secs=15):
        from zerg.services.managed_local_control import ManagedLocalSendResult

        return ManagedLocalSendResult(ok=True, exit_code=0)

    monkeypatch.setattr(
        "zerg.services.managed_local_control.steer_text_to_managed_local_session",
        fake_steer,
    )

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "redirect to failing test", "intent": "steer"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["outcome"] == "sent"
        assert body["intent"] == "steer"
    finally:
        api_app_ref.dependency_overrides = {}


def test_intent_steer_turn_ended_returns_structured_409(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_codex_session(session_local)

    async def fake_steer(*, db, owner_id, session, text, commis_id=None, timeout_secs=15):
        from zerg.services.managed_local_control import MANAGED_LOCAL_STEER_TURN_ENDED
        from zerg.services.managed_local_control import ManagedLocalSendResult

        return ManagedLocalSendResult(ok=False, exit_code=2, error=MANAGED_LOCAL_STEER_TURN_ENDED)

    monkeypatch.setattr(
        "zerg.services.managed_local_control.steer_text_to_managed_local_session",
        fake_steer,
    )

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "too late", "intent": "steer"},
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error_code"] == "turn_ended"
        # The row persists as failed for audit — no silent recovery.
        with session_local() as db:
            row = db.query(SessionInput).filter(SessionInput.session_id == session_id).one()
            assert row.status == "failed"
            assert row.last_error == "turn_ended"
    finally:
        api_app_ref.dependency_overrides = {}


def test_capability_includes_can_queue_next_input():
    from tests_lite._capability_test_helper import build_session_capabilities

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


def test_intent_auto_stores_owner_id(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    _stub_dispatch(monkeypatch)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "hi", "intent": "queue"},
        )
        assert resp.status_code == 200
        with session_local() as db:
            row = db.query(SessionInput).filter(SessionInput.session_id == session_id).one()
            assert row.owner_id == user_id
    finally:
        api_app_ref.dependency_overrides = {}


def test_queue_cap_rejects_over_limit(monkeypatch, tmp_path):
    from zerg.services.session_inputs import MAX_QUEUED_PER_SESSION

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    _stub_dispatch(monkeypatch)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        for i in range(MAX_QUEUED_PER_SESSION):
            r = client.post(
                f"/api/sessions/{session_id}/input",
                json={"text": f"msg {i}", "intent": "queue"},
            )
            assert r.status_code == 200, r.text
        over = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "one too many", "intent": "queue"},
        )
        assert over.status_code == 409, over.text
    finally:
        api_app_ref.dependency_overrides = {}


def test_cancel_rejects_wrong_session(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id_a, user_id = _seed_live_session(session_local)
    session_id_b, _ = _seed_live_session(session_local)
    _stub_dispatch(monkeypatch)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        post = client.post(
            f"/api/sessions/{session_id_a}/input",
            json={"text": "hi", "intent": "queue"},
        )
        input_id = post.json()["input_id"]

        # Cancel via the wrong session id should 404, not leak cancellation.
        wrong = client.delete(f"/api/sessions/{session_id_b}/inputs/{input_id}")
        assert wrong.status_code == 404

        with session_local() as db:
            row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
            assert row.status == INPUT_STATUS_QUEUED
    finally:
        api_app_ref.dependency_overrides = {}


def test_inputs_etag_returns_304_when_unchanged(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    _stub_dispatch(monkeypatch)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        # Seed one queued row so the list is non-trivial.
        r = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "etag test", "intent": "queue"},
        )
        assert r.status_code == 200

        # First list call: full response + ETag header.
        first = client.get(f"/api/sessions/{session_id}/inputs")
        assert first.status_code == 200
        etag = first.headers.get("etag")
        assert etag, "expected ETag header on /inputs response"

        # Second call with If-None-Match presents the same etag → 304.
        second = client.get(
            f"/api/sessions/{session_id}/inputs",
            headers={"If-None-Match": etag},
        )
        assert second.status_code == 304, second.text
        assert second.headers.get("etag") == etag

        # Mutating state (cancel) invalidates the ETag.
        input_id = first.json()[0]["id"]
        cancel = client.delete(f"/api/sessions/{session_id}/inputs/{input_id}")
        assert cancel.status_code == 200

        third = client.get(
            f"/api/sessions/{session_id}/inputs",
            headers={"If-None-Match": etag},
        )
        assert third.status_code == 200, "cancel should bust the ETag"
        assert third.headers.get("etag") != etag
    finally:
        api_app_ref.dependency_overrides = {}


def test_recent_list_surfaces_failed_rows(monkeypatch, tmp_path):
    from zerg.services.session_inputs import mark_failed

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    _stub_dispatch(monkeypatch)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        r = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "will fail", "intent": "queue"},
        )
        input_id = r.json()["input_id"]
        # Simulate a drain failure.
        with session_local() as db:
            mark_failed(db, input_id, error="provider down")

        listed = client.get(f"/api/sessions/{session_id}/inputs")
        assert listed.status_code == 200
        rows = listed.json()
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        assert rows[0]["last_error"] == "provider down"
    finally:
        api_app_ref.dependency_overrides = {}


def test_startup_reconciliation_fails_stuck_steer_rows_instead_of_requeuing(tmp_path):
    from datetime import timedelta

    from zerg.services.session_inputs import create_session_input
    from zerg.services.session_inputs import requeue_stuck_delivering

    session_local = _make_db(tmp_path)
    session_id, _ = _seed_live_session(session_local)

    with session_local() as db:
        steer_row = create_session_input(
            db,
            session_id=session_id,
            text="redirect now",
            intent="steer",
            status="delivering",
            client_request_id="crash-steer",
            delivery_request_id="crash-steer-delivery",
        )
        steer_row.updated_at = datetime.now(timezone.utc) - timedelta(seconds=300)
        auto_row = create_session_input(
            db,
            session_id=session_id,
            text="retryable",
            intent="auto",
            status="delivering",
            client_request_id="crash-auto",
            delivery_request_id="crash-auto-delivery",
        )
        auto_row.updated_at = datetime.now(timezone.utc) - timedelta(seconds=300)
        db.commit()

        requeued = requeue_stuck_delivering(db)
        # Only the auto row requeues; the steer row is failed so we do not
        # silently turn a corrective intent into a queued message.
        assert requeued == 1
        db.expire_all()
        steer_refreshed = db.query(SessionInput).filter(SessionInput.id == steer_row.id).one()
        auto_refreshed = db.query(SessionInput).filter(SessionInput.id == auto_row.id).one()
        assert steer_refreshed.status == INPUT_STATUS_FAILED
        assert steer_refreshed.last_error == "steer interrupted by restart"
        assert steer_refreshed.delivery_request_id == "crash-steer-delivery"
        assert auto_refreshed.status == INPUT_STATUS_QUEUED
        assert auto_refreshed.client_request_id == "crash-auto"
        assert auto_refreshed.delivery_request_id is None


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
            client_request_id="old",
            delivery_request_id="old-delivery",
        )
        row.updated_at = datetime.now(timezone.utc) - timedelta(seconds=300)
        db.commit()
        requeued = requeue_stuck_delivering(db)
        assert requeued == 1
        db.expire_all()
        refreshed = db.query(SessionInput).filter(SessionInput.id == row.id).one()
        assert refreshed.status == INPUT_STATUS_QUEUED
        assert refreshed.client_request_id == "old"
        assert refreshed.delivery_request_id is None
