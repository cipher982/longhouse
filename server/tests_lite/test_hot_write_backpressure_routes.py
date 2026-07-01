from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app
from zerg.models.device_token import DeviceToken
from zerg.models.user import User
from zerg.services.write_serializer import WriteQueueTimeoutError


class _QueueTimeoutSerializer:
    is_configured = True
    queue_depth = 6
    active_label = "ingest-replay"
    active_age_ms = 4500.0

    async def execute_after_closing_request_session(self, _fn, _fallback_db, **kwargs):
        raise WriteQueueTimeoutError(
            label=str(kwargs.get("label") or ""),
            queue_timeout_seconds=float(kwargs.get("queue_timeout_seconds") or 2.0),
        )


def _make_session_factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/hot_write_backpressure.db")
    Base.metadata.create_all(bind=engine)
    SessionLocal = make_sessionmaker(engine)
    with SessionLocal() as db:
        user = User(email="hot-write@example.test")
        db.add(user)
        db.flush()
        token = DeviceToken(owner_id=user.id, device_id="test-device", token_hash="0" * 64)
        db.add(token)
        db.commit()
        token_id = token.id
    return engine, SessionLocal, token_id


def _override_db(SessionLocal):
    def _inner():
        with SessionLocal() as db:
            yield db

    return _inner


def _assert_hot_backpressure(response, admission_state: str) -> None:
    assert response.status_code == 503, response.text
    assert response.headers["X-Longhouse-Write-Backpressure"] == "hot_write_backpressure"
    assert response.headers["X-Longhouse-Write-Error-Kind"] == "hot_write_backpressure"
    assert response.headers["X-Longhouse-Write-Lane"] == "hot"
    assert response.headers["X-Longhouse-Write-Admission-State"] == admission_state
    assert response.headers["Retry-After"] == "2"
    assert response.headers["X-Longhouse-Writer-Queue-Depth"] == "6"
    assert response.headers["X-Longhouse-Writer-Active-Label"] == "ingest-replay"
    assert response.headers["X-Longhouse-Writer-Active-Age-Ms"] == "4500.0"


def test_runtime_batch_returns_typed_hot_write_backpressure(tmp_path, monkeypatch):
    engine, SessionLocal, _token_id = _make_session_factory(tmp_path)

    monkeypatch.setattr("zerg.routers.runtime.get_write_serializer", lambda: _QueueTimeoutSerializer())
    api_app.dependency_overrides[get_db] = _override_db(SessionLocal)
    api_app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(device_id="test-device", id="token-1", owner_id=1)
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    try:
        with TestClient(api_app) as client:
            response = client.post(
                "/agents/runtime/events/batch",
                json={
                    "events": [
                        {
                            "runtime_key": "codex:hot-write",
                            "provider": "codex",
                            "device_id": "test-device",
                            "source": "codex_bridge",
                            "kind": "phase_signal",
                            "phase": "idle",
                            "occurred_at": "2026-01-01T00:00:00Z",
                            "freshness_ms": 60000,
                            "dedupe_key": "hot-write-runtime",
                            "payload": {},
                        }
                    ]
                },
                headers={"X-Agents-Token": "dev"},
            )
    finally:
        api_app.dependency_overrides.clear()
        engine.dispose()

    _assert_hot_backpressure(response, "runtime_queue_timeout")


def test_presence_returns_typed_hot_write_backpressure(tmp_path, monkeypatch):
    engine, SessionLocal, _token_id = _make_session_factory(tmp_path)

    monkeypatch.setattr("zerg.routers.presence.get_write_serializer", lambda: _QueueTimeoutSerializer())
    api_app.dependency_overrides[get_db] = _override_db(SessionLocal)
    api_app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(device_id="test-device", id="token-1", owner_id=1)
    try:
        with TestClient(api_app) as client:
            response = client.post(
                "/agents/presence",
                json={
                    "session_id": str(uuid4()),
                    "state": "idle",
                    "provider": "claude",
                    "dedupe_key": "hot-write-presence",
                },
                headers={"X-Agents-Token": "dev"},
            )
    finally:
        api_app.dependency_overrides.clear()
        engine.dispose()

    _assert_hot_backpressure(response, "presence_queue_timeout")


def test_heartbeat_returns_typed_hot_write_backpressure(tmp_path, monkeypatch):
    engine, SessionLocal, _token_id = _make_session_factory(tmp_path)

    monkeypatch.setattr("zerg.routers.heartbeat.get_write_serializer", lambda: _QueueTimeoutSerializer())
    api_app.dependency_overrides[get_db] = _override_db(SessionLocal)
    api_app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(device_id="test-device", id="token-1", owner_id=1)
    try:
        with TestClient(api_app, backend="asyncio") as client:
            response = client.post(
                "/agents/heartbeat",
                json={
                    "version": "0.5.0",
                    "daemon_pid": 12345,
                    "spool_pending_count": 0,
                    "parse_error_count_1h": 0,
                    "consecutive_ship_failures": 0,
                    "disk_free_bytes": 50_000_000_000,
                    "is_offline": False,
                },
                headers={"X-Agents-Token": "dev"},
            )
    finally:
        api_app.dependency_overrides.clear()
        engine.dispose()

    _assert_hot_backpressure(response, "heartbeat_queue_timeout")


def test_machine_presence_returns_typed_hot_write_backpressure(tmp_path, monkeypatch):
    engine, SessionLocal, token_id = _make_session_factory(tmp_path)

    def _token():
        return DeviceToken(id=token_id, owner_id=1, device_id="test-device", token_hash="1" * 64)

    monkeypatch.setattr("zerg.routers.agents_machine_presence.get_write_serializer", lambda: _QueueTimeoutSerializer())
    api_app.dependency_overrides[get_db] = _override_db(SessionLocal)
    api_app.dependency_overrides[verify_agents_token] = _token
    try:
        with TestClient(api_app) as client:
            response = client.post(
                "/agents/machine-presence",
                json={"state": "active", "source": "test", "idle_seconds": 0},
                headers={"X-Agents-Token": "dev"},
            )
    finally:
        api_app.dependency_overrides.clear()
        engine.dispose()

    _assert_hot_backpressure(response, "machine_presence_queue_timeout")
