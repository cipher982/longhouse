from __future__ import annotations

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-1234")
os.environ.setdefault("INTERNAL_API_SECRET", Fernet.generate_key().decode())

from tests_lite._kernel_test_helpers import seed_managed_kernel_rows
from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.browser_route_auth import get_current_browser_route_user
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPauseRequest
from zerg.models.agents import SessionRuntimeState
from zerg.models.enums import UserRole
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.routers import session_chat
from zerg.services.managed_local_control import ManagedLocalSendResult
from zerg.services.session_pause_requests import PAUSE_KIND_STRUCTURED_QUESTION
from zerg.services.session_pause_requests import apply_pause_runtime_event
from zerg.services.session_pause_requests import upsert_pause_request
from zerg.services.session_runtime import phase_freshness_ms
from zerg.services.session_runtime import runtime_key_for_session


def _make_db(tmp_path):
    db_path = tmp_path / "test_pause_request_routes.db"
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


def _seed_live_runtime_state(db, session, *, phase: str = "needs_user") -> None:
    now = datetime.now(timezone.utc)
    freshness_ms = phase_freshness_ms(phase) or int(timedelta(minutes=5).total_seconds() * 1000)
    key = runtime_key_for_session(str(session.provider or "codex"), str(session.id))
    state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == key).first()
    if state is None:
        state = SessionRuntimeState(
            runtime_key=key,
            session_id=session.id,
            provider=str(session.provider or "codex"),
            device_id=session.device_id,
        )
        db.add(state)
    state.phase = phase
    state.phase_source = "codex_bridge"
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


def _seed_codex_session(session_local):
    session_id = uuid4()
    with session_local() as db:
        user = User(email=f"pause-{uuid4().hex[:6]}@test.local", role=UserRole.USER.value)
        db.add(user)
        db.flush()

        session = AgentSession(
            id=session_id,
            provider="codex",
            environment="Cinder",
            project="pause-routes",
            device_id="cinder",
            cwd="/tmp/pause-routes",
            started_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                                                                                )
        db.add(session)
        seed_managed_kernel_rows(db, session, control_plane="codex_bridge")
        runner = Runner(
            id=1,
            owner_id=user.id,
            name="cinder",
            status="online",
            auth_secret_hash="test",
        )
        db.merge(runner)
        db.commit()
        _seed_live_runtime_state(db, session)
        return session.id, user.id


def _seed_pause_request(
    session_local,
    session_id,
    *,
    can_respond: bool,
    request_key: str = "codex:pause-routes:req-1",
):
    with session_local() as db:
        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        row, _changed = upsert_pause_request(
            db,
            session_id=session.id,
            runtime_key=runtime_key_for_session(str(session.provider), str(session.id)),
            provider="codex",
            request_key=request_key,
            provider_request_id=request_key.rsplit(":", 1)[-1],
            kind=PAUSE_KIND_STRUCTURED_QUESTION,
            title="Choose storage",
            summary="The agent needs a product decision.",
            request_payload={
                "questions": [
                    {
                        "id": "storage",
                        "header": "Storage",
                        "question": "Which storage backend should I use?",
                        "multiSelect": False,
                        "options": [
                            {"label": "SQLite", "description": "Keep it local."},
                            {"label": "Postgres", "description": "Use a service."},
                        ],
                    }
                ]
            },
            can_respond=can_respond,
            occurred_at=datetime.now(timezone.utc),
        )
        db.commit()
        return row.id


def test_browser_lists_pending_pause_requests(tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_codex_session(session_local)
    pause_id = _seed_pause_request(session_local, session_id, can_respond=True)
    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.get(f"/api/sessions/{session_id}/pause-requests")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 1
        request = body["requests"][0]
        assert request["id"] == str(pause_id)
        assert request["status"] == "pending"
        assert request["can_respond"] is True
        assert request["questions"][0]["id"] == "storage"
    finally:
        api_app_ref.dependency_overrides = {}


def test_machine_lists_pending_pause_requests_with_auth_disabled(tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_codex_session(session_local)
    _seed_pause_request(session_local, session_id, can_respond=True)
    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.get(f"/api/agents/sessions/{session_id}/pause-requests")
        assert resp.status_code == 200, resp.text
        assert resp.json()["total"] == 1
    finally:
        api_app_ref.dependency_overrides = {}


def test_non_answerable_pause_response_returns_structured_conflict(tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_codex_session(session_local)
    pause_id = _seed_pause_request(session_local, session_id, can_respond=False)
    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/pause-requests/{pause_id}/response",
            json={"decision": "answer", "answers": {"storage": ["SQLite"]}},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "pause_request_not_answerable"
        assert resp.json()["detail"]["pause_request_id"] == str(pause_id)
    finally:
        api_app_ref.dependency_overrides = {}


def test_answerable_pause_response_dispatches_and_resolves(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_codex_session(session_local)
    pause_id = _seed_pause_request(session_local, session_id, can_respond=True)
    calls: list[dict[str, object]] = []

    async def fake_answer(**kwargs):
        calls.append(kwargs)
        return ManagedLocalSendResult(
            ok=True,
            exit_code=0,
            response_data={
                "request_key": "codex:pause-routes:req-1",
                "provider_request_id": "req-1",
                "status": "resolved",
                "response_payload": {
                    "request": {
                        "decision": "answer",
                        "answers": {"storage": ["SQLite"]},
                    },
                    "provider_result": {
                        "answers": {"storage": {"answers": ["SQLite"]}},
                    },
                },
                "response_text": "Use SQLite.",
            },
        )

    monkeypatch.setattr(session_chat, "answer_pause_request_on_managed_local_session", fake_answer)
    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/pause-requests/{pause_id}/response",
            json={"decision": "answer", "answers": {"storage": ["SQLite"]}, "message": "Use SQLite."},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "resolved"
        assert body["pause_request"]["status"] == "resolved"
        assert len(calls) == 1
        assert calls[0]["request_key"] == "codex:pause-routes:req-1"
        assert calls[0]["answers"] == {"storage": ["SQLite"]}

        with session_local() as db:
            row = db.query(SessionPauseRequest).filter(SessionPauseRequest.id == pause_id).one()
            assert row.status == "resolved"
            assert row.response_text == "Use SQLite."
            assert row.response_payload_json["provider_result"]["answers"]["storage"]["answers"] == ["SQLite"]
    finally:
        api_app_ref.dependency_overrides = {}


def test_pause_response_builds_message_from_structured_answers(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_codex_session(session_local)
    pause_id = _seed_pause_request(session_local, session_id, can_respond=True)
    calls: list[dict[str, object]] = []

    async def fake_answer(**kwargs):
        calls.append(kwargs)
        return ManagedLocalSendResult(
            ok=True,
            exit_code=0,
            response_data={
                "status": "resolved",
                "response_payload": {"source": "fake"},
            },
        )

    monkeypatch.setattr(session_chat, "answer_pause_request_on_managed_local_session", fake_answer)
    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/pause-requests/{pause_id}/response",
            json={"decision": "answer", "answers": {"storage": ["SQLite"]}},
        )
        assert resp.status_code == 200, resp.text
        assert len(calls) == 1
        assert calls[0]["message"] == "Storage: SQLite"
        with session_local() as db:
            row = db.query(SessionPauseRequest).filter(SessionPauseRequest.id == pause_id).one()
            assert row.response_text == "Storage: SQLite"
    finally:
        api_app_ref.dependency_overrides = {}


def test_route_response_converges_with_pause_resolution_event(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_codex_session(session_local)
    pause_id = _seed_pause_request(session_local, session_id, can_respond=True)
    response_payload = {
        "request": {
            "decision": "answer",
            "answers": {"storage": ["SQLite"]},
        },
        "provider_result": {
            "answers": {"storage": {"answers": ["SQLite"]}},
        },
    }

    async def fake_answer(**_kwargs):
        return ManagedLocalSendResult(
            ok=True,
            exit_code=0,
            response_data={
                "request_key": "codex:pause-routes:req-1",
                "provider_request_id": "req-1",
                "status": "resolved",
                "response_payload": response_payload,
                "response_text": "Use SQLite.",
            },
        )

    monkeypatch.setattr(session_chat, "answer_pause_request_on_managed_local_session", fake_answer)
    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/pause-requests/{pause_id}/response",
            json={"decision": "answer", "answers": {"storage": ["SQLite"]}, "message": "Use SQLite."},
        )
        assert resp.status_code == 200, resp.text

        with session_local() as db:
            row = db.query(SessionPauseRequest).filter(SessionPauseRequest.id == pause_id).one()
            original_resolved_at = row.resolved_at
            event = SimpleNamespace(
                kind="pause_resolution",
                session_id=session_id,
                runtime_key=row.runtime_key,
                provider="codex",
                tool_name=None,
                occurred_at=datetime.now(timezone.utc),
                dedupe_key="pause-resolution-1",
                payload={
                    "request_key": row.request_key,
                    "provider_request_id": row.provider_request_id,
                    "status": "resolved",
                    "response_payload": response_payload,
                    "response_text": "Use SQLite.",
                },
            )
            assert apply_pause_runtime_event(db, event) is True
            db.commit()
            db.refresh(row)
            assert row.status == "resolved"
            assert row.resolved_at == original_resolved_at
            assert row.response_text == "Use SQLite."
            assert row.response_payload_json == response_payload
    finally:
        api_app_ref.dependency_overrides = {}


@pytest.mark.parametrize("decision", ["reject", "cancel"])
def test_pause_response_reject_and_cancel_persist_rejected(monkeypatch, tmp_path, decision):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_codex_session(session_local)
    pause_id = _seed_pause_request(session_local, session_id, can_respond=True)

    async def fake_answer(**kwargs):
        assert kwargs["decision"] == decision
        return ManagedLocalSendResult(
            ok=True,
            exit_code=0,
            response_data={
                "request_key": "codex:pause-routes:req-1",
                "provider_request_id": "req-1",
                "status": "rejected",
                "response_payload": {
                    "request": {"decision": decision},
                    "provider_result": {"status": "rejected"},
                },
                "response_text": f"{decision}ed",
            },
        )

    monkeypatch.setattr(session_chat, "answer_pause_request_on_managed_local_session", fake_answer)
    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/pause-requests/{pause_id}/response",
            json={"decision": decision},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "rejected"
        assert body["pause_request"]["status"] == "rejected"

        with session_local() as db:
            row = db.query(SessionPauseRequest).filter(SessionPauseRequest.id == pause_id).one()
            assert row.status == "rejected"
            assert row.response_payload_json["request"]["decision"] == decision
    finally:
        api_app_ref.dependency_overrides = {}


def test_pause_response_dispatch_failure_leaves_request_pending(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_codex_session(session_local)
    pause_id = _seed_pause_request(session_local, session_id, can_respond=True)

    async def fake_answer(**_kwargs):
        return ManagedLocalSendResult(ok=False, exit_code=12, error="bridge offline")

    monkeypatch.setattr(session_chat, "answer_pause_request_on_managed_local_session", fake_answer)
    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/pause-requests/{pause_id}/response",
            json={"decision": "answer", "answers": {"storage": ["SQLite"]}},
        )
        assert resp.status_code == 502, resp.text
        assert resp.json()["detail"]["code"] == "pause_response_dispatch_failed"
        assert resp.json()["detail"]["retryable"] is True
        assert resp.json()["detail"]["refetch_required"] is True
        with session_local() as db:
            row = db.query(SessionPauseRequest).filter(SessionPauseRequest.id == pause_id).one()
            assert row.status == "pending"
            assert row.response_text is None
    finally:
        api_app_ref.dependency_overrides = {}


def test_normal_input_conflicts_with_pending_answerable_pause_request(tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_codex_session(session_local)
    pause_id = _seed_pause_request(session_local, session_id, can_respond=True)
    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "Use SQLite", "intent": "auto", "client_request_id": "pause-conflict-1"},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "pause_request_pending"
        assert resp.json()["detail"]["pause_request_id"] == str(pause_id)
    finally:
        api_app_ref.dependency_overrides = {}
