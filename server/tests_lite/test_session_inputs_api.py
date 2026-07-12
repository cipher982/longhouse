from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-1234")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-1234")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-google-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-google-client-secret")

import pytest

from tests_lite._kernel_test_helpers import seed_managed_kernel_rows
from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import initialize_live_database
from zerg.database import make_engine
from zerg.database import make_live_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.browser_route_auth import get_current_browser_route_user
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionInput
from zerg.models.agents import SessionInputAttachment
from zerg.models.agents import SessionInputDeliveryAttempt
from zerg.models.agents import SessionTurn
from zerg.models.enums import UserRole
from zerg.models.live_store import LiveArchiveOutbox
from zerg.models.live_store import LiveSessionInputReceipt
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.routers.session_chat import SessionInputRequest
from zerg.routers.session_chat import _create_session_input_response
from zerg.routers.session_chat import _project_live_input_to_archive
from zerg.services.live_archive_outbox import SESSION_INPUT_RECEIPT_KIND
from zerg.services.live_archive_outbox import drain_live_archive_outbox
from zerg.services.agents import AgentsStore
from zerg.services.agents import EventIngest
from zerg.services.agents import SessionIngest
from zerg.services.machine_control_channel import get_machine_control_channel_registry
from zerg.services.runner_connection_manager import get_runner_connection_manager
from zerg.services.session_locks import session_lock_manager
from zerg.services.session_inputs import INPUT_STATUS_CANCELLED
from zerg.services.session_inputs import INPUT_STATUS_DELIVERED
from zerg.services.session_inputs import INPUT_STATUS_DELIVERING
from zerg.services.session_inputs import INPUT_STATUS_FAILED
from zerg.services.session_inputs import INPUT_STATUS_QUEUED
from zerg.services.session_inputs import create_session_input
from zerg.services.live_session_inputs import LiveInputReceiptSnapshot
from zerg.services.session_kernel_projection import project_session_control_fields
from zerg.services.session_runtime import phase_freshness_ms
from zerg.services.session_runtime import runtime_key_for_session


def _make_db(tmp_path):
    db_path = tmp_path / "test_session_inputs.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _enable_live_input_store(monkeypatch, tmp_path):
    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live-inputs.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    class LiveSerializer:
        is_configured = True

        async def execute(self, fn, *, auto_commit=True, **_kwargs):
            with LiveSession() as live_db:
                result = fn(live_db)
                if auto_commit:
                    live_db.commit()
                return result

    serializer = LiveSerializer()
    monkeypatch.setattr("zerg.database.live_store_configured", lambda: True)
    monkeypatch.setattr("zerg.database.get_live_session_factory", lambda: LiveSession)
    monkeypatch.setattr("zerg.services.live_session_inputs.get_live_write_serializer", lambda: serializer)
    monkeypatch.setattr("zerg.routers.session_chat.get_live_write_serializer", lambda: serializer)
    return LiveSession, live_engine


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
        request_id=None,
        timeout_secs=15,
        verify_turn_started=False,
        verification_timeout_secs=None,
        attachments=None,
    ):
        calls.append({"session_id": str(session.id), "text": text, "request_id": request_id})
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


class _AutoCompletingMachineWebSocket:
    def __init__(self):
        self.sent: list[dict[str, object]] = []

    async def send_json(self, message):
        self.sent.append(message)
        await get_machine_control_channel_registry().complete_command(
            {
                "type": "command_result",
                "command_id": message["command_id"],
                "ok": True,
                "result": {
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "turn_id": "machine-control-turn-1",
                },
            }
        )


async def _clear_machine_control_registry() -> None:
    await get_machine_control_channel_registry().clear_for_tests()


async def _register_fake_machine_control(
    *,
    owner_id: int,
    supports: list[str],
    device_id: str = "cinder",
) -> _AutoCompletingMachineWebSocket:
    websocket = _AutoCompletingMachineWebSocket()
    await get_machine_control_channel_registry().register(
        owner_id=owner_id,
        device_id=device_id,
        machine_name=device_id,
        engine_build="test-engine",
        supports=supports,
        websocket=websocket,
    )
    return websocket


def _seed_machine_control_session(
    session_local,
    *,
    provider: str,
    control_plane: str,
    managed_transport: str | None = None,
    can_interrupt: bool = True,
    device_id: str = "cinder",
    phase: str = "idle",
):
    from zerg.models.agents import SessionConnection
    from zerg.models.agents import SessionRun
    from zerg.models.agents import SessionRuntimeState
    from zerg.models.agents import SessionThread

    session_id, user_id = _seed_live_session(session_local)
    with session_local() as db:
        session = db.query(AgentSession).filter_by(id=session_id).one()
        session.provider = provider
        session.device_id = device_id
        db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session.id).delete(
            synchronize_session=False
        )
        thread = (
            db.query(SessionThread).filter(SessionThread.session_id == session.id, SessionThread.is_primary == 1).one()
        )
        thread.provider = provider
        run = db.query(SessionRun).filter(SessionRun.thread_id == thread.id, SessionRun.ended_at.is_(None)).one()
        run.provider = provider
        conn = db.query(SessionConnection).filter(SessionConnection.run_id == run.id).one()
        conn.control_plane = control_plane
        conn.can_send_input = 1
        conn.can_interrupt = int(can_interrupt)
        conn.can_terminate = 0
        conn.can_tail_output = 1
        conn.can_resume = 0
        db.commit()
        _seed_live_runtime_state(db, session, phase=phase)
    return session_id, user_id


def _seed_antigravity_session(session_local):
    return _seed_machine_control_session(
        session_local,
        provider="antigravity",
        control_plane="antigravity_hook_inbox",
        can_interrupt=False,
    )


def _seed_codex_machine_control_session(session_local, *, phase: str = "idle"):
    return _seed_machine_control_session(
        session_local,
        provider="codex",
        control_plane="codex_bridge",
        managed_transport="codex_app_server",
        device_id="codex-machine-control",
        phase=phase,
    )


def _wait_for_turn_input_link(session_local, *, session_id, request_id: str, timeout_secs: float = 1.0):
    deadline = time.monotonic() + timeout_secs
    last_turn = None
    while time.monotonic() < deadline:
        with session_local() as db:
            turn = (
                db.query(SessionTurn)
                .filter(SessionTurn.session_id == session_id, SessionTurn.request_id == request_id)
                .one_or_none()
            )
            if turn is not None:
                last_turn = SimpleNamespace(
                    session_input_id=turn.session_input_id,
                    user_event_id=turn.user_event_id,
                )
                if turn.user_event_id is not None:
                    return last_turn
        time.sleep(0.01)
    return last_turn


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


def test_json_input_rejects_empty_text_by_contract(tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "", "intent": "auto", "client_request_id": "empty-json-1"},
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert any(item["loc"] == ["body", "text"] for item in detail)
    finally:
        api_app_ref.dependency_overrides = {}


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


def test_auto_input_response_includes_live_input_id(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    _stub_dispatch(monkeypatch)
    receipt_calls: list[dict[str, object]] = []

    async def fake_live_receipt(**kwargs):
        receipt_calls.append(kwargs)
        return "live-input-1"

    monkeypatch.setattr("zerg.routers.session_chat.record_live_input_receipt_best_effort", fake_live_receipt)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "hello hot lane", "intent": "auto", "client_request_id": "ios-live-1"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["outcome"] == "sent"
        assert body["live_input_id"] == "live-input-1"
        assert body["input_id"] is None
        assert len(receipt_calls) == 2
        assert receipt_calls[0]["client_request_id"] == "ios-live-1"
        assert receipt_calls[0]["status"] == INPUT_STATUS_DELIVERING
        assert receipt_calls[1]["client_request_id"] == "ios-live-1"
        assert receipt_calls[1]["status"] == INPUT_STATUS_DELIVERED
        assert receipt_calls[1]["enqueue_archive_projection"] is True
        assert receipt_calls[1]["delivery_request_id"]
        with session_local() as db:
            assert db.query(SessionInput).filter(SessionInput.session_id == session_id).count() == 0
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        api_app_ref.dependency_overrides = {}


def test_auto_input_dedupes_existing_live_receipt(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    calls = _stub_dispatch(monkeypatch)

    async def fake_live_lookup(**_kwargs):
        return LiveInputReceiptSnapshot(
            id="live-input-existing",
            owner_id=user_id,
            session_id=str(session_id),
            provider="codex",
            text="already sent",
            intent="auto",
            status=INPUT_STATUS_DELIVERED,
            client_request_id="ios-live-repeat",
            archive_session_input_id=None,
            delivery_request_id="delivery-live-repeat",
        )

    monkeypatch.setattr("zerg.routers.session_chat.load_live_input_receipt_by_client_request_best_effort", fake_live_lookup)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "already sent", "intent": "auto", "client_request_id": "ios-live-repeat"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["outcome"] == "sent"
        assert body["live_input_id"] == "live-input-existing"
        assert body["input_id"] is None
        assert len(calls) == 0
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        api_app_ref.dependency_overrides = {}


def test_live_input_projection_creates_archive_row_and_links_turn(tmp_path):
    from zerg.services.session_turns import create_session_turn

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    with session_local() as db:
        create_session_turn(db, session_id=session_id, request_id="req-live-project")

        input_id = _project_live_input_to_archive(
            db,
            source_session_id=session_id,
            owner_id=user_id,
            text="project me later",
            intent="auto",
            client_request_id="ios-live-project",
            delivery_request_id="req-live-project",
        )

        row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
        assert row.status == INPUT_STATUS_DELIVERED
        assert row.client_request_id == "ios-live-project"
        assert row.delivery_request_id == "req-live-project"

        turn = db.query(SessionTurn).filter(SessionTurn.session_id == session_id, SessionTurn.request_id == "req-live-project").one()
        assert turn.session_input_id == input_id


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


def test_cancelled_auto_input_marks_failed_and_releases_lock(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    async def cancelled_dispatch(**_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr("zerg.routers.session_chat._build_managed_local_chat_response", cancelled_dispatch)

    with session_local() as db:
        source_session = AgentsStore(db).get_session(session_id)
        assert source_session is not None
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                _create_session_input_response(
                    source_session=source_session,
                    owner_id=user_id,
                    body=SessionInputRequest(
                        text="will timeout",
                        intent="auto",
                        client_request_id="ios-timeout-regression",
                    ),
                    db=db,
                )
            )

        row = db.query(SessionInput).filter(SessionInput.session_id == session_id).one()
        assert row.status == INPUT_STATUS_FAILED
        assert row.last_error == "request timed out"
        assert asyncio.run(session_lock_manager.is_locked(str(session_id))) is False


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


def test_antigravity_auto_input_routes_through_machine_control(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_antigravity_session(session_local)
    websocket = asyncio.run(_register_fake_machine_control(owner_id=user_id, supports=["antigravity.send"]))

    monkeypatch.setattr("zerg.services.session_chat_impl._schedule_managed_local_lock_release", lambda **_kwargs: None)
    monkeypatch.setattr(
        "zerg.services.session_chat_impl._schedule_managed_local_active_phase_observation",
        lambda **_kwargs: None,
    )

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "ship through agy hooks", "intent": "auto", "client_request_id": "agy-send-1"},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["outcome"] == "sent"
        assert body["intent"] == "auto"
        assert len(websocket.sent) == 1
        frame = websocket.sent[0]
        assert frame["command_type"] == "session.send_text"
        assert frame["session_id"] == str(session_id)
        assert str(frame["command_id"]).startswith(f"managed-control:{session_id}:session.send_text:")
        assert frame["payload"] == {
            "provider": "antigravity",
            "text": "ship through agy hooks",
        }

        with session_local() as db:
            session = db.query(AgentSession).filter_by(id=session_id).one()
            assert project_session_control_fields(db, session).source_runner_id is None
            row = db.query(SessionInput).filter(SessionInput.session_id == session_id).one()
            assert row.status == INPUT_STATUS_DELIVERED
            assert row.client_request_id == "agy-send-1"
            turn = (
                db.query(SessionTurn)
                .filter(SessionTurn.session_id == session_id, SessionTurn.request_id == row.delivery_request_id)
                .one()
            )
            assert turn.session_input_id == row.id
            assert turn.send_accepted_at is not None
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        asyncio.run(_clear_machine_control_registry())
        api_app_ref.dependency_overrides = {}


def _assert_provider_auto_input_routes_through_machine_control(
    monkeypatch,
    tmp_path,
    *,
    provider: str,
    control_plane: str,
    support: str,
    managed_transport: str | None = None,
) -> None:
    session_local = _make_db(tmp_path)
    device_id = f"{provider}-machine-control"
    session_id, user_id = _seed_machine_control_session(
        session_local,
        provider=provider,
        control_plane=control_plane,
        managed_transport=managed_transport,
        device_id=device_id,
    )
    websocket = asyncio.run(_register_fake_machine_control(owner_id=user_id, supports=[support], device_id=device_id))

    monkeypatch.setattr("zerg.services.session_chat_impl._schedule_managed_local_lock_release", lambda **_kwargs: None)
    monkeypatch.setattr(
        "zerg.services.session_chat_impl._schedule_managed_local_active_phase_observation",
        lambda **_kwargs: None,
    )

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": f"ship through {provider}", "intent": "auto", "client_request_id": f"{provider}-send-1"},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["outcome"] == "sent"
        assert body["intent"] == "auto"
        assert len(websocket.sent) == 1
        frame = websocket.sent[0]
        assert frame["command_type"] == "session.send_text"
        assert frame["session_id"] == str(session_id)
        assert str(frame["command_id"]).startswith(f"managed-control:{session_id}:session.send_text:")
        assert frame["payload"] == {
            "provider": provider,
            "text": f"ship through {provider}",
        }

        with session_local() as db:
            session = db.query(AgentSession).filter_by(id=session_id).one()
            assert project_session_control_fields(db, session).source_runner_id is None
            row = db.query(SessionInput).filter(SessionInput.session_id == session_id).one()
            assert row.status == INPUT_STATUS_DELIVERED
            assert row.client_request_id == f"{provider}-send-1"
            turn = (
                db.query(SessionTurn)
                .filter(SessionTurn.session_id == session_id, SessionTurn.request_id == row.delivery_request_id)
                .one()
            )
            assert turn.session_input_id == row.id
            assert turn.send_accepted_at is not None
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        asyncio.run(_clear_machine_control_registry())
        api_app_ref.dependency_overrides = {}


def test_claude_auto_input_routes_through_machine_control(monkeypatch, tmp_path):
    _assert_provider_auto_input_routes_through_machine_control(
        monkeypatch,
        tmp_path,
        provider="claude",
        control_plane="claude_channel_bridge",
        support="claude.send",
    )


def test_opencode_auto_input_routes_through_machine_control(monkeypatch, tmp_path):
    _assert_provider_auto_input_routes_through_machine_control(
        monkeypatch,
        tmp_path,
        provider="opencode",
        control_plane="opencode_server_bridge",
        support="opencode.send",
    )


def test_codex_auto_input_routes_through_machine_control(monkeypatch, tmp_path):
    _assert_provider_auto_input_routes_through_machine_control(
        monkeypatch,
        tmp_path,
        provider="codex",
        control_plane="codex_bridge",
        managed_transport="codex_app_server",
        support="codex.send",
    )


class _DisconnectOnSendMachineWebSocket:
    """Fake Machine Agent that drops its control channel as the command goes out.

    Mimics the most plausible "no babysitting" steer-loop failure: the engine's
    control WebSocket disconnects while a send_text is in flight. The frame is
    recorded, then the connection unregisters itself, which fails the pending
    command via the registry's disconnect path.
    """

    def __init__(self, *, owner_id: int, device_id: str):
        self.sent: list[dict[str, object]] = []
        self._owner_id = owner_id
        self._device_id = device_id

    async def send_json(self, message):
        self.sent.append(message)
        await get_machine_control_channel_registry().unregister(
            owner_id=self._owner_id,
            device_id=self._device_id,
            websocket=self,
        )


def _assert_provider_inflight_disconnect_fails_cleanly(
    monkeypatch,
    tmp_path,
    *,
    provider: str,
    control_plane: str,
    support: str,
    managed_transport: str | None = None,
) -> None:
    session_local = _make_db(tmp_path)
    device_id = f"{provider}-machine-control"
    session_id, user_id = _seed_machine_control_session(
        session_local,
        provider=provider,
        control_plane=control_plane,
        managed_transport=managed_transport,
        device_id=device_id,
    )
    websocket = _DisconnectOnSendMachineWebSocket(owner_id=user_id, device_id=device_id)
    asyncio.run(
        get_machine_control_channel_registry().register(
            owner_id=user_id,
            device_id=device_id,
            machine_name=device_id,
            engine_build="test-engine",
            supports=[support],
            websocket=websocket,
        )
    )

    monkeypatch.setattr("zerg.services.session_chat_impl._schedule_managed_local_lock_release", lambda **_kwargs: None)
    monkeypatch.setattr(
        "zerg.services.session_chat_impl._schedule_managed_local_active_phase_observation",
        lambda **_kwargs: None,
    )

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/input",
            json={
                "text": f"steer through {provider}",
                "intent": "auto",
                "client_request_id": f"{provider}-disconnect-1",
            },
        )

        # The engine dropped mid-command: the client must see a clean gateway
        # error, never a false "sent".
        assert resp.status_code == 502, resp.text
        assert resp.json()["detail"]["error_code"] == "send_failed"
        assert len(websocket.sent) == 1
        assert websocket.sent[0]["command_type"] == "session.send_text"

        with session_local() as db:
            row = db.query(SessionInput).filter(SessionInput.session_id == session_id).one()
            # The crucial "no babysitting" guarantee: a dropped send is NOT
            # silently marked delivered.
            assert row.status == INPUT_STATUS_FAILED
            assert row.status != INPUT_STATUS_DELIVERED
            assert row.last_error
            # The turn for this dropped input must not claim a send_accepted milestone.
            turn = (
                db.query(SessionTurn)
                .filter(
                    SessionTurn.session_id == session_id,
                    SessionTurn.session_input_id == row.id,
                )
                .one_or_none()
            )
            if turn is not None:
                assert turn.send_accepted_at is None
        # Lock must be released so the next steer attempt is not wedged.
        assert asyncio.run(session_lock_manager.is_locked(str(session_id))) is False
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        asyncio.run(_clear_machine_control_registry())
        api_app_ref.dependency_overrides = {}


def test_claude_inflight_disconnect_fails_cleanly(monkeypatch, tmp_path):
    _assert_provider_inflight_disconnect_fails_cleanly(
        monkeypatch,
        tmp_path,
        provider="claude",
        control_plane="claude_channel_bridge",
        support="claude.send",
    )


def test_codex_inflight_disconnect_fails_cleanly(monkeypatch, tmp_path):
    _assert_provider_inflight_disconnect_fails_cleanly(
        monkeypatch,
        tmp_path,
        provider="codex",
        control_plane="codex_bridge",
        managed_transport="codex_app_server",
        support="codex.send",
    )


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


def test_queue_input_acks_from_live_receipt_without_archive_row(monkeypatch, tmp_path):
    LiveSession, live_engine = _enable_live_input_store(monkeypatch, tmp_path)
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
            json={"text": "queued hot", "intent": "queue", "client_request_id": "live-queue-1"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["outcome"] == "queued"
        assert body["input_id"] is None
        assert body["live_input_id"]
        assert body["queued"] == [
            {
                "id": None,
                "live_input_id": body["live_input_id"],
                "text": "queued hot",
                "intent": "queue",
                "status": "queued",
                "last_error": None,
                "created_at": body["queued"][0]["created_at"],
            }
        ]
        assert calls == []

        with session_local() as db:
            assert db.query(SessionInput).filter(SessionInput.session_id == session_id).count() == 0
        with LiveSession() as live_db:
            receipt = live_db.query(LiveSessionInputReceipt).filter_by(id=body["live_input_id"]).one()
            assert receipt.status == INPUT_STATUS_QUEUED
            assert receipt.client_request_id == "live-queue-1"
    finally:
        api_app_ref.dependency_overrides = {}
        live_engine.dispose()


def test_cancel_live_queued_input_uses_live_receipt(monkeypatch, tmp_path):
    LiveSession, live_engine = _enable_live_input_store(monkeypatch, tmp_path)
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    _stub_dispatch(monkeypatch)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        queued = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "cancel hot", "intent": "queue", "client_request_id": "live-cancel-1"},
        )
        assert queued.status_code == 200, queued.text
        live_input_id = queued.json()["live_input_id"]

        resp = client.delete(f"/api/sessions/{session_id}/inputs/live/{live_input_id}")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"cancelled": True, "live_input_id": live_input_id, "input_id": None}

        listed = client.get(f"/api/sessions/{session_id}/inputs")
        assert listed.status_code == 200, listed.text
        assert listed.json() == []
        with LiveSession() as live_db:
            receipt = live_db.query(LiveSessionInputReceipt).filter_by(id=live_input_id).one()
            assert receipt.status == INPUT_STATUS_CANCELLED
    finally:
        api_app_ref.dependency_overrides = {}
        live_engine.dispose()


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
    from zerg.services.session_input_queue import wake_session_input_queue
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
        result = asyncio.run(
            wake_session_input_queue(
                db_bind=db_bind,
                session_id=session_id,
                reason="test_direct_wake",
                lock_scope_id=str(session_id),
            )
        )

        with session_local() as db:
            row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
            assert row.status == INPUT_STATUS_DELIVERED
            assert row.client_request_id == "ios-drain-origin-1"
            assert row.delivery_request_id
            assert row.delivery_request_id.startswith("drain-")

            attempt = db.query(SessionInputDeliveryAttempt).filter(SessionInputDeliveryAttempt.session_input_id == input_id).one()
            assert attempt.status == "accepted"
            assert attempt.request_id == row.delivery_request_id
            assert attempt.lease_expires_at is not None
            delivery_request_id = row.delivery_request_id
        turn = _wait_for_turn_input_link(session_local, session_id=session_id, request_id=delivery_request_id)
        assert turn is not None
        assert turn.session_input_id == input_id
        assert turn.user_event_id is not None
        assert result.dispatched is True
        assert result.input_id == input_id
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))


def test_live_queue_drain_dispatches_catalog_receipt_without_archive_projection(monkeypatch, tmp_path):
    from zerg.services.session_input_queue import wake_session_input_queue

    LiveSession, live_engine = _enable_live_input_store(monkeypatch, tmp_path)
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    _stub_dispatch(monkeypatch, emit_verified_user_event=True)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        queued = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "drain hot receipt", "intent": "queue", "client_request_id": "live-drain-1"},
        )
        assert queued.status_code == 200, queued.text
        live_input_id = queued.json()["live_input_id"]
        with session_local() as db:
            db_bind = db.get_bind()

        result = asyncio.run(
            wake_session_input_queue(
                db_bind=db_bind,
                session_id=session_id,
                reason="test_live_queue",
                lock_scope_id=str(session_id),
            )
        )

        assert result.dispatched is True
        assert result.input_id is None
        assert result.live_input_id == live_input_id
        with session_local() as db:
            assert db.query(SessionInput).filter(SessionInput.session_id == session_id).count() == 0
        with LiveSession() as live_db:
            receipt = live_db.query(LiveSessionInputReceipt).filter_by(id=live_input_id).one()
            assert receipt.status == INPUT_STATUS_DELIVERED
            assert receipt.delivery_request_id
            assert live_db.query(LiveArchiveOutbox).count() == 0
            assert receipt.archive_session_input_id is None
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        api_app_ref.dependency_overrides = {}
        live_engine.dispose()


def test_queue_wake_defers_behind_active_turn(monkeypatch, tmp_path):
    from zerg.services.session_input_queue import wake_session_input_queue
    from zerg.services.session_inputs import create_session_input
    from zerg.services.session_turns import create_session_turn
    from zerg.services.session_turns import mark_session_turn_send_accepted

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    dispatch_calls = _stub_dispatch(monkeypatch, emit_verified_user_event=True)

    with session_local() as db:
        create_session_turn(db, session_id=session_id, request_id="req-active-prior")
        mark_session_turn_send_accepted(db, session_id=session_id, request_id="req-active-prior")
        row = create_session_input(
            db,
            session_id=session_id,
            text="wait behind active turn",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-active-gate-1",
        )
        input_id = int(row.id)
        db_bind = db.get_bind()
        db.commit()

    result = asyncio.run(
        wake_session_input_queue(
            db_bind=db_bind,
            session_id=session_id,
            reason="test_active_turn",
            lock_scope_id=str(session_id),
        )
    )

    with session_local() as db:
        row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
        assert row.status == INPUT_STATUS_QUEUED
    assert result.dispatched is False
    assert result.reason == "active_turn"
    assert dispatch_calls == []


def test_queue_wake_drains_after_prior_turn_terminal(monkeypatch, tmp_path):
    from zerg.services.session_input_queue import wake_session_input_queue
    from zerg.services.session_inputs import create_session_input
    from zerg.services.session_turns import create_session_turn
    from zerg.services.session_turns import mark_session_turn_send_accepted
    from zerg.services.session_turns import mark_session_turn_terminal

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    _stub_dispatch(monkeypatch, emit_verified_user_event=True)

    with session_local() as db:
        create_session_turn(db, session_id=session_id, request_id="req-terminal-prior")
        mark_session_turn_send_accepted(db, session_id=session_id, request_id="req-terminal-prior")
        mark_session_turn_terminal(db, session_id=session_id, request_id="req-terminal-prior", phase="idle")
        row = create_session_input(
            db,
            session_id=session_id,
            text="drain after terminal prior turn",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-terminal-gate-1",
        )
        input_id = int(row.id)
        db_bind = db.get_bind()
        db.commit()

    try:
        result = asyncio.run(
            wake_session_input_queue(
                db_bind=db_bind,
                session_id=session_id,
                reason="test_terminal_turn",
                lock_scope_id=str(session_id),
            )
        )

        with session_local() as db:
            row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
            assert row.status == INPUT_STATUS_DELIVERED
            assert row.delivery_request_id
            turn = db.query(SessionTurn).filter(SessionTurn.request_id == row.delivery_request_id).one()
            assert turn.session_input_id == input_id
        assert result.dispatched is True
        assert result.input_id == input_id
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))


def test_queue_wake_drains_needs_user_phase(monkeypatch, tmp_path):
    from zerg.services.session_input_queue import wake_session_input_queue
    from zerg.services.session_inputs import create_session_input

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    _stub_dispatch(monkeypatch, emit_verified_user_event=True)

    with session_local() as db:
        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        _seed_live_runtime_state(db, session, phase="needs_user")
        row = create_session_input(
            db,
            session_id=session_id,
            text="answer needs user",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-needs-user-gate-1",
        )
        input_id = int(row.id)
        db_bind = db.get_bind()
        db.commit()

    try:
        result = asyncio.run(
            wake_session_input_queue(
                db_bind=db_bind,
                session_id=session_id,
                reason="test_needs_user",
                lock_scope_id=str(session_id),
            )
        )

        with session_local() as db:
            row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
            assert row.status == INPUT_STATUS_DELIVERED
        assert result.dispatched is True
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))


def test_concurrent_queue_wakes_dispatch_at_most_one_input(monkeypatch, tmp_path):
    from zerg.services.session_input_queue import wake_session_input_queue
    from zerg.services.session_inputs import create_session_input

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    dispatch_calls = _stub_dispatch(monkeypatch, emit_verified_user_event=True)

    with session_local() as db:
        first = create_session_input(
            db,
            session_id=session_id,
            text="first concurrent",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-concurrent-1",
        )
        second = create_session_input(
            db,
            session_id=session_id,
            text="second concurrent",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-concurrent-2",
        )
        input_ids = [int(first.id), int(second.id)]
        db_bind = db.get_bind()

    async def run_wakes():
        return await asyncio.gather(
            wake_session_input_queue(
                db_bind=db_bind,
                session_id=session_id,
                reason="test_concurrent_1",
                lock_scope_id=str(session_id),
            ),
            wake_session_input_queue(
                db_bind=db_bind,
                session_id=session_id,
                reason="test_concurrent_2",
                lock_scope_id=str(session_id),
            ),
        )

    try:
        results = asyncio.run(run_wakes())

        with session_local() as db:
            rows = db.query(SessionInput).filter(SessionInput.id.in_(input_ids)).order_by(SessionInput.id.asc()).all()
            statuses = [row.status for row in rows]
            assert statuses.count(INPUT_STATUS_DELIVERED) == 1
            assert statuses.count(INPUT_STATUS_QUEUED) == 1
        assert sum(1 for result in results if result.dispatched) == 1
        assert len(dispatch_calls) == 1
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))


def test_active_attempt_blocks_queue_readiness(tmp_path):
    from zerg.services.session_input_queue import evaluate_session_input_queue_readiness
    from zerg.services.session_inputs import create_session_input

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    with session_local() as db:
        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        row = create_session_input(
            db,
            session_id=session_id,
            text="held by active lease",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-active-lease-1",
        )
        db.add(
            SessionInputDeliveryAttempt(
                session_input_id=int(row.id),
                session_id=session_id,
                thread_id=row.thread_id,
                owner_id=user_id,
                request_id="active-attempt-1",
                attempt_number=1,
                status="acquired",
                lease_owner="test",
                lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
        )
        db.commit()

        readiness = evaluate_session_input_queue_readiness(db, session=session, owner_id=user_id)

    assert readiness.ready is False
    assert readiness.reason == "lease_active"


def test_concurrent_queue_wakes_different_lock_scopes_create_one_attempt(monkeypatch, tmp_path):
    from zerg.services.session_input_queue import wake_session_input_queue
    from zerg.services.session_inputs import create_session_input

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    dispatch_calls = _stub_dispatch(monkeypatch, emit_verified_user_event=True)

    with session_local() as db:
        first = create_session_input(
            db,
            session_id=session_id,
            text="first durable lease",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-durable-concurrent-1",
        )
        second = create_session_input(
            db,
            session_id=session_id,
            text="second durable lease",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-durable-concurrent-2",
        )
        input_ids = [int(first.id), int(second.id)]
        db_bind = db.get_bind()

    async def run_wakes():
        return await asyncio.gather(
            wake_session_input_queue(
                db_bind=db_bind,
                session_id=session_id,
                reason="test_durable_concurrent_1",
                lock_scope_id=f"scope-a-{uuid4().hex}",
            ),
            wake_session_input_queue(
                db_bind=db_bind,
                session_id=session_id,
                reason="test_durable_concurrent_2",
                lock_scope_id=f"scope-b-{uuid4().hex}",
            ),
        )

    results = asyncio.run(run_wakes())

    with session_local() as db:
        rows = db.query(SessionInput).filter(SessionInput.id.in_(input_ids)).order_by(SessionInput.id.asc()).all()
        statuses = [row.status for row in rows]
        attempts = db.query(SessionInputDeliveryAttempt).filter(SessionInputDeliveryAttempt.session_id == session_id).all()
        assert statuses.count(INPUT_STATUS_DELIVERED) == 1
        assert statuses.count(INPUT_STATUS_QUEUED) == 1
        assert len(attempts) == 1
        assert attempts[0].status == "accepted"
    assert sum(1 for result in results if result.dispatched) == 1
    assert len(dispatch_calls) == 1


def test_expired_attempt_allows_retry(monkeypatch, tmp_path):
    from zerg.services.session_input_queue import wake_session_input_queue
    from zerg.services.session_inputs import create_session_input

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    _stub_dispatch(monkeypatch, emit_verified_user_event=True)

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="retry expired lease",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_DELIVERING,
            client_request_id="ios-expired-attempt-1",
            delivery_request_id="expired-attempt",
        )
        expired = SessionInputDeliveryAttempt(
            session_input_id=int(row.id),
            session_id=session_id,
            thread_id=row.thread_id,
            owner_id=user_id,
            request_id="expired-attempt",
            attempt_number=1,
            status="acquired",
            lease_owner="expired",
            lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        db.add(expired)
        db.commit()
        input_id = int(row.id)
        db_bind = db.get_bind()

    try:
        result = asyncio.run(
            wake_session_input_queue(
                db_bind=db_bind,
                session_id=session_id,
                reason="test_expired_attempt",
                lock_scope_id=str(session_id),
            )
        )

        with session_local() as db:
            row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
            attempts = (
                db.query(SessionInputDeliveryAttempt)
                .filter(SessionInputDeliveryAttempt.session_input_id == input_id)
                .order_by(SessionInputDeliveryAttempt.id.asc())
                .all()
            )
            assert row.status == INPUT_STATUS_DELIVERED
            assert [attempt.status for attempt in attempts] == ["expired", "accepted"]
        assert result.dispatched is True
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))


def test_expired_steer_attempt_is_not_silently_requeued(tmp_path):
    from zerg.services.session_input_queue import wake_session_input_queue
    from zerg.services.session_inputs import create_session_input

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="stale steer",
            owner_id=user_id,
            intent="steer",
            status=INPUT_STATUS_DELIVERING,
            client_request_id="ios-stale-steer-1",
            delivery_request_id="expired-steer-attempt",
        )
        db.add(
            SessionInputDeliveryAttempt(
                session_input_id=int(row.id),
                session_id=session_id,
                thread_id=row.thread_id,
                owner_id=user_id,
                request_id="expired-steer-attempt",
                attempt_number=1,
                status="acquired",
                lease_owner="expired",
                lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        db.commit()
        input_id = int(row.id)
        db_bind = db.get_bind()

    result = asyncio.run(
        wake_session_input_queue(
            db_bind=db_bind,
            session_id=session_id,
            reason="test_expired_steer",
            lock_scope_id=str(session_id),
        )
    )

    with session_local() as db:
        row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
        attempt = db.query(SessionInputDeliveryAttempt).filter(SessionInputDeliveryAttempt.session_input_id == input_id).one()
        assert row.status == INPUT_STATUS_FAILED
        assert row.last_error == "steer delivery interrupted before accepted attempt"
        assert row.delivery_request_id == "expired-steer-attempt"
        assert attempt.status == "expired"
    assert result.dispatched is False
    assert result.reason == "no_queued_input"


def test_expired_attachment_attempt_is_failed_not_requeued(tmp_path):
    from zerg.services.session_input_queue import wake_session_input_queue
    from zerg.services.session_inputs import create_session_input

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="stale attachment",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_DELIVERING,
            client_request_id="ios-stale-attachment-1",
            delivery_request_id="expired-attachment-attempt",
        )
        db.add(
            SessionInputAttachment(
                session_input_id=int(row.id),
                session_id=session_id,
                mime_type="image/png",
                byte_size=12,
                sha256="a" * 64,
                blob_path="/tmp/missing-attachment.png",
            )
        )
        db.add(
            SessionInputDeliveryAttempt(
                session_input_id=int(row.id),
                session_id=session_id,
                thread_id=row.thread_id,
                owner_id=user_id,
                request_id="expired-attachment-attempt",
                attempt_number=1,
                status="submitted",
                lease_owner="expired",
                lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        db.commit()
        input_id = int(row.id)
        db_bind = db.get_bind()

    result = asyncio.run(
        wake_session_input_queue(
            db_bind=db_bind,
            session_id=session_id,
            reason="test_expired_attachment",
            lock_scope_id=str(session_id),
        )
    )

    with session_local() as db:
        row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
        attempt = db.query(SessionInputDeliveryAttempt).filter(SessionInputDeliveryAttempt.session_input_id == input_id).one()
        assert row.status == INPUT_STATUS_FAILED
        assert row.last_error == "attachment delivery interrupted before accepted attempt"
        assert row.delivery_request_id == "expired-attachment-attempt"
        assert attempt.status == "expired"
    assert result.dispatched is False
    assert result.reason == "no_queued_input"


def test_queue_drain_requeues_transient_machine_control_unavailable(monkeypatch, tmp_path):
    from fastapi.responses import JSONResponse

    from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_UNAVAILABLE_ERROR
    from zerg.services.session_input_queue import wake_session_input_queue
    from zerg.services.session_inputs import create_session_input
    from zerg.services.session_turns import SESSION_TURN_ERROR_SEND_FAILED

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="wait for control reconnect",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-drain-requeue-1",
        )
        input_id = int(row.id)
        db_bind = db.get_bind()

    async def fake_dispatch(**_kwargs):
        return JSONResponse(
            status_code=502,
            content={
                "accepted": False,
                "error": MANAGED_CONTROL_UNAVAILABLE_ERROR,
                "error_code": SESSION_TURN_ERROR_SEND_FAILED,
            },
        )

    monkeypatch.setattr("zerg.services.session_chat_impl._dispatch_managed_local_text", fake_dispatch)

    result = asyncio.run(
        wake_session_input_queue(
            db_bind=db_bind,
            session_id=session_id,
            reason="test_transient_failure",
            lock_scope_id=str(session_id),
        )
    )

    with session_local() as db:
        row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
        assert row.status == INPUT_STATUS_QUEUED
        assert row.delivery_request_id is None
        assert row.last_error == MANAGED_CONTROL_UNAVAILABLE_ERROR
        assert row.attempt_count == 1
        assert row.next_attempt_at is not None
        attempt = db.query(SessionInputDeliveryAttempt).filter(SessionInputDeliveryAttempt.session_input_id == input_id).one()
        assert attempt.status == "released"
        assert attempt.error_code == SESSION_TURN_ERROR_SEND_FAILED
    probe = asyncio.run(session_lock_manager.acquire(session_id=str(session_id), holder="probe", ttl_seconds=1))
    assert probe is not None
    asyncio.run(session_lock_manager.release(str(session_id), "probe"))
    assert result.dispatched is False
    assert result.reason == "transient_dispatch_failure"


def test_next_attempt_at_is_respected_after_transient_failure(monkeypatch, tmp_path):
    from fastapi.responses import JSONResponse

    from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_UNAVAILABLE_ERROR
    from zerg.services.session_input_queue import wake_session_input_queue
    from zerg.services.session_inputs import create_session_input
    from zerg.services.session_turns import SESSION_TURN_ERROR_SEND_FAILED

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="respect retry time",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-next-attempt-1",
        )
        input_id = int(row.id)
        db_bind = db.get_bind()

    async def fake_dispatch(*, lock_scope_id, request_id, **_kwargs):
        await session_lock_manager.release(lock_scope_id, request_id)
        return JSONResponse(
            status_code=502,
            content={
                "accepted": False,
                "error": MANAGED_CONTROL_UNAVAILABLE_ERROR,
                "error_code": SESSION_TURN_ERROR_SEND_FAILED,
            },
        )

    monkeypatch.setattr("zerg.services.session_chat_impl._dispatch_managed_local_text", fake_dispatch)

    first = asyncio.run(
        wake_session_input_queue(
            db_bind=db_bind,
            session_id=session_id,
            reason="test_next_attempt_first",
            lock_scope_id=str(session_id),
        )
    )
    second = asyncio.run(
        wake_session_input_queue(
            db_bind=db_bind,
            session_id=session_id,
            reason="test_next_attempt_second",
            lock_scope_id=str(session_id),
        )
    )

    with session_local() as db:
        row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
        attempts = db.query(SessionInputDeliveryAttempt).filter(SessionInputDeliveryAttempt.session_input_id == input_id).all()
        assert row.status == INPUT_STATUS_QUEUED
        assert row.attempt_count == 1
        assert row.next_attempt_at is not None
        assert len(attempts) == 1
    assert first.reason == "transient_dispatch_failure"
    assert second.reason == "next_attempt_pending"


def test_attempt_count_increments_on_retry(monkeypatch, tmp_path):
    from fastapi.responses import JSONResponse

    from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_UNAVAILABLE_ERROR
    from zerg.services.session_input_queue import wake_session_input_queue
    from zerg.services.session_inputs import create_session_input
    from zerg.services.session_turns import SESSION_TURN_ERROR_SEND_FAILED

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="retry then succeed",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-retry-count-1",
        )
        input_id = int(row.id)
        db_bind = db.get_bind()

    async def fake_transient(*, lock_scope_id, request_id, **_kwargs):
        await session_lock_manager.release(lock_scope_id, request_id)
        return JSONResponse(
            status_code=502,
            content={
                "accepted": False,
                "error": MANAGED_CONTROL_UNAVAILABLE_ERROR,
                "error_code": SESSION_TURN_ERROR_SEND_FAILED,
            },
        )

    monkeypatch.setattr("zerg.services.session_chat_impl._dispatch_managed_local_text", fake_transient)
    asyncio.run(
        wake_session_input_queue(
            db_bind=db_bind,
            session_id=session_id,
            reason="test_retry_count_first",
            lock_scope_id=str(session_id),
        )
    )

    with session_local() as db:
        db.query(SessionInput).filter(SessionInput.id == input_id).update(
            {"next_attempt_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
            synchronize_session=False,
        )
        db.commit()

    async def fake_success(**_kwargs):
        return JSONResponse(
            status_code=200,
            content={
                "accepted": True,
                "session_id": str(session_id),
                "request_id": "retry-success",
            },
        )

    monkeypatch.setattr("zerg.services.session_chat_impl._dispatch_managed_local_text", fake_success)
    try:
        second = asyncio.run(
            wake_session_input_queue(
                db_bind=db_bind,
                session_id=session_id,
                reason="test_retry_count_second",
                lock_scope_id=str(session_id),
            )
        )

        with session_local() as db:
            row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
            attempts = (
                db.query(SessionInputDeliveryAttempt)
                .filter(SessionInputDeliveryAttempt.session_input_id == input_id)
                .order_by(SessionInputDeliveryAttempt.id.asc())
                .all()
            )
            assert row.status == INPUT_STATUS_DELIVERED
            assert row.attempt_count == 2
            assert [attempt.status for attempt in attempts] == ["released", "accepted"]
        assert second.dispatched is True
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))


def test_permanent_dispatch_failure_marks_input_and_attempt_failed(monkeypatch, tmp_path):
    from fastapi.responses import JSONResponse

    from zerg.services.session_input_queue import wake_session_input_queue
    from zerg.services.session_inputs import create_session_input

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="permanent failure",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-permanent-failure-1",
        )
        input_id = int(row.id)
        db_bind = db.get_bind()

    async def fake_permanent(*, lock_scope_id, request_id, **_kwargs):
        await session_lock_manager.release(lock_scope_id, request_id)
        return JSONResponse(
            status_code=502,
            content={
                "accepted": False,
                "error": "session is closed",
                "error_code": "session_closed",
            },
        )

    monkeypatch.setattr("zerg.services.session_chat_impl._dispatch_managed_local_text", fake_permanent)
    result = asyncio.run(
        wake_session_input_queue(
            db_bind=db_bind,
            session_id=session_id,
            reason="test_permanent_failure",
            lock_scope_id=str(session_id),
        )
    )

    with session_local() as db:
        row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
        attempt = db.query(SessionInputDeliveryAttempt).filter(SessionInputDeliveryAttempt.session_input_id == input_id).one()
        assert row.status == INPUT_STATUS_FAILED
        assert row.last_error == "session is closed"
        assert attempt.status == "failed"
        assert attempt.error_code == "session_closed"
    assert result.dispatched is False
    assert result.reason == "dispatch_failed"


def test_lock_watcher_timeout_recovers_from_fresh_runtime_idle_and_drains_queue(monkeypatch, tmp_path):
    from zerg.services.managed_local_control import ManagedLocalTerminalResult
    from zerg.services.session_chat_impl import _release_managed_local_lock_after_terminal
    from zerg.services.session_turns import create_session_turn
    from zerg.services.session_turns import mark_session_turn_send_accepted

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    _stub_dispatch(monkeypatch, emit_verified_user_event=True)

    with session_local() as db:
        create_session_turn(db, session_id=session_id, request_id="req-timeout-recover")
        mark_session_turn_send_accepted(db, session_id=session_id, request_id="req-timeout-recover")
        queued = create_session_input(
            db,
            session_id=session_id,
            text="send after recovered idle",
            owner_id=user_id,
            intent="auto",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-timeout-recover-1",
        )
        prior_input = create_session_input(
            db,
            session_id=session_id,
            text="prior accepted input",
            owner_id=user_id,
            intent="auto",
            status=INPUT_STATUS_DELIVERED,
            client_request_id="prior-timeout-recover-1",
            delivery_request_id="req-timeout-recover",
        )
        db.add(
            SessionInputDeliveryAttempt(
                session_input_id=int(prior_input.id),
                session_id=session_id,
                thread_id=prior_input.thread_id,
                owner_id=user_id,
                request_id="req-timeout-recover",
                attempt_number=1,
                status="accepted",
                lease_owner="req-timeout-recover",
                lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
        )
        db.commit()
        queued_id = int(queued.id)
        db_bind = db.get_bind()

    async def fake_wait_terminal(**_kwargs):
        return None

    monkeypatch.setattr("zerg.services.session_chat_impl.await_managed_local_turn_terminal", fake_wait_terminal)
    monkeypatch.setattr(
        "zerg.services.session_chat_impl._runtime_terminal_result_after",
        lambda **_kwargs: ManagedLocalTerminalResult(
            phase="idle",
            control_status="completed",
            observation_id=0,
            occurred_at=datetime.now(timezone.utc),
        ),
    )

    asyncio.run(session_lock_manager.acquire(str(session_id), holder="req-timeout-recover", ttl_seconds=300))
    try:
        asyncio.run(
            _release_managed_local_lock_after_terminal(
                lock_scope_id=str(session_id),
                request_id="req-timeout-recover",
                session_id=session_id,
                provider="claude",
                db_bind=db_bind,
                after_observation_id=0,
            )
        )

        with session_local() as db:
            queued = db.query(SessionInput).filter(SessionInput.id == queued_id).one()
            assert queued.status == INPUT_STATUS_DELIVERED
            turn = db.query(SessionTurn).filter(SessionTurn.request_id == "req-timeout-recover").one()
            assert turn.terminal_phase == "idle"
            assert turn.terminal_at is not None
            attempt = (
                db.query(SessionInputDeliveryAttempt)
                .filter(SessionInputDeliveryAttempt.request_id == "req-timeout-recover")
                .one()
            )
            assert attempt.status == "completed"
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
    return _seed_codex_machine_control_session(session_local, phase="running")


def test_intent_steer_requires_steerable_capability(monkeypatch, tmp_path):
    """Live send-capable transports without live-injection support return 409 steer_unsupported."""
    from zerg.models.agents import SessionConnection
    from zerg.models.agents import SessionRun
    from zerg.models.agents import SessionThread

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    with session_local() as db:
        thread = (
            db.query(SessionThread).filter(SessionThread.session_id == session_id, SessionThread.is_primary == 1).one()
        )
        run = db.query(SessionRun).filter(SessionRun.thread_id == thread.id, SessionRun.ended_at.is_(None)).one()
        conn = db.query(SessionConnection).filter(SessionConnection.run_id == run.id).one()
        conn.control_plane = "opencode_process"
        conn.acquisition_kind = "spawned_control"
        db.commit()
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


def test_antigravity_steer_intent_is_rejected_before_machine_control(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_antigravity_session(session_local)
    websocket = asyncio.run(_register_fake_machine_control(owner_id=user_id, supports=["antigravity.send"]))

    async def fail_steer(**_kwargs):
        raise AssertionError("Antigravity does not advertise steer and must reject before dispatch")

    monkeypatch.setattr(
        "zerg.services.managed_local_control.steer_text_to_managed_local_session",
        fail_steer,
    )

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "mid-turn change", "intent": "steer"},
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error_code"] == "steer_unsupported"
        assert websocket.sent == []
    finally:
        asyncio.run(_clear_machine_control_registry())
        api_app_ref.dependency_overrides = {}


def test_intent_steer_success_returns_sent_for_claude_channel(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    with session_local() as db:
        session = db.query(AgentSession).filter_by(id=session_id).one()
        _seed_live_runtime_state(db, session, phase="running")

    async def fake_steer(*, db, owner_id, session, text, request_id=None, timeout_secs=15):
        from zerg.services.managed_local_control import ManagedLocalSendResult

        assert session.provider == "claude"
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


def test_intent_steer_acks_from_live_receipt_without_archive_row(monkeypatch, tmp_path):
    LiveSession, live_engine = _enable_live_input_store(monkeypatch, tmp_path)
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    with session_local() as db:
        session = db.query(AgentSession).filter_by(id=session_id).one()
        _seed_live_runtime_state(db, session, phase="running")

    async def fake_steer(*, db, owner_id, session, text, request_id=None, timeout_secs=15):
        from zerg.services.managed_local_control import ManagedLocalSendResult

        assert request_id
        assert text == "redirect hot"
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
            json={"text": "redirect hot", "intent": "steer", "client_request_id": "live-steer-1"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["outcome"] == "sent"
        assert body["intent"] == "steer"
        assert body["input_id"] is None
        assert body["live_input_id"]

        with session_local() as db:
            assert db.query(SessionInput).filter(SessionInput.session_id == session_id).count() == 0
        with LiveSession() as live_db:
            receipt = live_db.query(LiveSessionInputReceipt).filter_by(id=body["live_input_id"]).one()
            assert receipt.status == INPUT_STATUS_DELIVERED
            assert receipt.intent == "steer"
            assert receipt.client_request_id == "live-steer-1"
            assert live_db.query(LiveArchiveOutbox).filter_by(kind=SESSION_INPUT_RECEIPT_KIND).count() == 0
    finally:
        api_app_ref.dependency_overrides = {}
        live_engine.dispose()


def test_intent_steer_requires_active_turn_for_claude_channel(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    async def fake_steer(**_kwargs):
        raise AssertionError("steer dispatch should not run when Claude is idle")

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
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error_code"] == "turn_not_active"
    finally:
        api_app_ref.dependency_overrides = {}


def test_intent_steer_rejects_stale_active_turn_for_claude_channel(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    with session_local() as db:
        session = db.query(AgentSession).filter_by(id=session_id).one()
        _seed_live_runtime_state(db, session, phase="running")
        from zerg.models.agents import SessionRuntimeState

        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session.id).one()
        stale_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        state.freshness_expires_at = stale_at
        state.last_runtime_signal_at = stale_at
        state.last_live_at = stale_at
        db.commit()

    async def fake_steer(**_kwargs):
        raise AssertionError("stale intent=steer must be rejected before dispatch")

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
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error_code"] == "turn_not_active"
    finally:
        api_app_ref.dependency_overrides = {}


def test_intent_steer_failure_returns_structured_502_for_claude_channel(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    with session_local() as db:
        session = db.query(AgentSession).filter_by(id=session_id).one()
        _seed_live_runtime_state(db, session, phase="running")

    async def fake_steer(*, db, owner_id, session, text, request_id=None, timeout_secs=15):
        from zerg.services.managed_local_control import ManagedLocalSendResult

        return ManagedLocalSendResult(ok=False, exit_code=1, error="Claude channel bridge is unavailable")

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
        assert resp.status_code == 502, resp.text
        detail = resp.json()["detail"]
        assert detail["error_code"] == "steer_failed"
        assert detail["message"] == "Claude channel bridge is unavailable"
    finally:
        api_app_ref.dependency_overrides = {}


def test_intent_steer_success_returns_sent_for_codex_bridge(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_codex_session(session_local)

    async def fake_steer(*, db, owner_id, session, text, request_id=None, timeout_secs=15):
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


def test_codex_steer_intent_routes_through_machine_control(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_codex_machine_control_session(session_local, phase="running")
    websocket = asyncio.run(
        _register_fake_machine_control(
            owner_id=user_id,
            supports=["codex.steer"],
            device_id="codex-machine-control",
        )
    )

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "steer through codex bridge", "intent": "steer", "client_request_id": "codex-steer-1"},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["outcome"] == "sent"
        assert body["intent"] == "steer"
        assert len(websocket.sent) == 1
        frame = websocket.sent[0]
        assert frame["command_type"] == "session.steer_text"
        assert frame["session_id"] == str(session_id)
        assert str(frame["command_id"]).startswith(f"managed-control:{session_id}:session.steer_text:")
        assert frame["payload"] == {
            "provider": "codex",
            "text": "steer through codex bridge",
            "intent": "steer",
        }

        with session_local() as db:
            session = db.query(AgentSession).filter_by(id=session_id).one()
            assert project_session_control_fields(db, session).source_runner_id is None
            row = db.query(SessionInput).filter(SessionInput.session_id == session_id).one()
            assert row.status == INPUT_STATUS_DELIVERED
            assert row.intent == "steer"
            assert row.client_request_id == "codex-steer-1"
    finally:
        asyncio.run(_clear_machine_control_registry())
        api_app_ref.dependency_overrides = {}


def test_intent_steer_turn_ended_returns_structured_409(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_codex_session(session_local)

    async def fake_steer(*, db, owner_id, session, text, request_id=None, timeout_secs=15):
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


def test_startup_reconciliation_returns_queued_sessions_for_boot_drain_idempotently(tmp_path):
    from zerg.services.session_inputs import reconcile_startup_session_inputs

    session_local = _make_db(tmp_path)
    queued_session_id, _ = _seed_live_session(session_local)
    retry_session_id, _ = _seed_live_session(session_local)
    steer_session_id, _ = _seed_live_session(session_local)
    delivered_session_id, _ = _seed_live_session(session_local)
    stale_at = datetime.now(timezone.utc) - timedelta(seconds=300)

    with session_local() as db:
        create_session_input(
            db,
            session_id=queued_session_id,
            text="already queued",
            intent="queue",
            status="queued",
            client_request_id="boot-queued",
        )
        retry_row = create_session_input(
            db,
            session_id=retry_session_id,
            text="retry at boot",
            intent="auto",
            status="delivering",
            client_request_id="boot-auto",
            delivery_request_id="boot-auto-delivery",
        )
        steer_row = create_session_input(
            db,
            session_id=steer_session_id,
            text="too late to steer",
            intent="steer",
            status="delivering",
            client_request_id="boot-steer",
            delivery_request_id="boot-steer-delivery",
        )
        create_session_input(
            db,
            session_id=delivered_session_id,
            text="already delivered",
            intent="auto",
            status="delivered",
            client_request_id="boot-delivered",
        )
        retry_row.updated_at = stale_at
        steer_row.updated_at = stale_at
        db.commit()

        first_boot = reconcile_startup_session_inputs(db)
        second_boot = reconcile_startup_session_inputs(db)

        expected = {str(queued_session_id), str(retry_session_id)}
        assert {str(session_id) for session_id in first_boot} == expected
        assert {str(session_id) for session_id in second_boot} == expected

        db.expire_all()
        retry_refreshed = db.query(SessionInput).filter(SessionInput.id == retry_row.id).one()
        steer_refreshed = db.query(SessionInput).filter(SessionInput.id == steer_row.id).one()
        assert retry_refreshed.status == INPUT_STATUS_QUEUED
        assert retry_refreshed.delivery_request_id is None
        assert steer_refreshed.status == INPUT_STATUS_FAILED
        assert steer_refreshed.last_error == "steer interrupted by restart"
