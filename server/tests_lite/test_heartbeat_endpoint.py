"""Tests for the agent heartbeat ingest endpoint.

Covers:
- POST /agents/heartbeat creates a new AgentHeartbeat row
- Subsequent POST updates (inserts another row) for the same device
- Prune: rows older than 30 days are removed for that device
- Auth: missing token falls back to client IP

Uses in-memory SQLite. HTTP-level tests use TestClient with dependency_overrides
targeting api_app. No shared conftest.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.models.agents import AgentHeartbeat
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionConnection
from zerg.models.agents import SessionRuntimeState
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.session_runtime import runtime_key_for_session

# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------


def _make_db(tmp_path):
    db_path = tmp_path / "test_heartbeat.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal


def _make_client(SessionLocal):
    """Create TestClient with get_db override + explicit machine auth."""
    from zerg.dependencies.agents_auth import verify_agents_token
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        with SessionLocal() as db:
            yield db

    def override_verify_agents_token():
        return SimpleNamespace(device_id="testclient", id="token-1")

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token

    client = TestClient(app, backend="asyncio")
    return client, api_app


def _machine_evidence_payload() -> dict[str, object]:
    observed_at = "2026-05-08T12:00:00Z"
    process = [
        {
            "provider": provider,
            "session_id": f"{provider}-session" if provider != "antigravity" else None,
            "provider_session_id": f"{provider}-provider-session",
            "role": "provider",
            "pid": 100 + index,
            "process_start_time": "Thu May  8 11:59:00 2026",
            "boot_id": "macos:1777970400:0",
            "cwd": f"/tmp/{provider}",
            "alive": True,
            "source": "provider_process_scan",
            "observed_at": observed_at,
        }
        for index, provider in enumerate(("codex", "claude", "opencode", "cursor", "antigravity"))
    ]
    control = [
        {
            "provider": provider,
            "session_id": f"{provider}-session",
            "provider_session_id": f"{provider}-provider-session",
            "ownership": "managed",
            "state": "attached",
            "bridge_status": "ready",
            "lease_ttl_ms": 900_000,
            "source": "provider_control_scan",
            "observed_at": observed_at,
        }
        for provider in ("codex", "claude", "opencode", "cursor")
    ]
    transcript = [
        {
            "provider": provider,
            "session_id": None if provider == "antigravity" else f"{provider}-session",
            "provider_session_id": f"{provider}-provider-session",
            "source_path": f"/tmp/{provider}.jsonl",
            "source_offset": 12,
            "source": "provider_transcript_scan",
            "observed_at": observed_at,
        }
        for provider in ("codex", "claude", "opencode", "antigravity")
    ]
    return {
        "schema_version": 1,
        "observed_at": observed_at,
        "process": process,
        "activity": [
            {
                "provider": "codex",
                "session_id": "codex-session",
                "phase": "running",
                "tool_name": "Shell",
                "source": "codex_bridge",
                "observed_at": observed_at,
            }
        ],
        "control": control,
        "transcript": transcript,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_heartbeat_accepts_and_retains_typed_machine_evidence_without_reducing_it(tmp_path):
    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    evidence = _machine_evidence_payload()

    try:
        response = client.post(
            "/api/agents/heartbeat",
            json={"version": "phase-2", "daemon_pid": 42, "machine_evidence": evidence},
        )
        assert response.status_code == 204

        with SessionLocal() as db:
            row = db.query(AgentHeartbeat).one()
            raw = json.loads(row.raw_json)
            retained = raw["machine_evidence"]
            assert retained["schema_version"] == 1
            assert {fact["provider"] for fact in retained["process"]} == {
                "codex",
                "claude",
                "opencode",
                "cursor",
                "antigravity",
            }
            assert retained["process"][0]["process_start_time"] == "Thu May  8 11:59:00 2026"
            # Typed control evidence is validation-only in Phase 2. It must
            # not silently become a second lifecycle/control reducer.
            assert db.query(SessionConnection).count() == 0
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_machine_evidence_rejects_unknown_schema_and_invalid_pid(tmp_path):
    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    evidence = _machine_evidence_payload()
    evidence["schema_version"] = 2
    process = evidence["process"]
    assert isinstance(process, list)
    process[0]["pid"] = 0

    try:
        response = client.post(
            "/api/agents/heartbeat",
            json={"version": "phase-2", "daemon_pid": 42, "machine_evidence": evidence},
        )
        assert response.status_code == 422
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_endpoint_creates_row(tmp_path):
    """POST /agents/heartbeat inserts a new AgentHeartbeat row."""
    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)

    try:
        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.5.0",
                "daemon_pid": 12345,
                "spool_pending_count": 3,
                "parse_error_count_1h": 0,
                "consecutive_ship_failures": 0,
                "disk_free_bytes": 50_000_000_000,
                "is_offline": False,
            },
        )
        assert response.status_code == 204, f"Expected 204, got {response.status_code}: {response.text}"

        with SessionLocal() as db:
            rows = db.query(AgentHeartbeat).all()
            assert len(rows) == 1
            hb = rows[0]
            assert hb.version == "0.5.0"
            assert hb.last_ship_attempt_at is None
            assert hb.last_ship_result is None
            assert hb.last_ship_latency_ms is None
            assert hb.last_ship_http_status is None
            assert hb.spool_pending == 3
            assert hb.spool_dead == 0
            assert hb.ship_attempts_1h == 0
            assert hb.ship_successes_1h == 0
            assert hb.ship_rate_limited_1h == 0
            assert hb.ship_server_errors_1h == 0
            assert hb.ship_payload_rejections_1h == 0
            assert hb.ship_payload_too_large_1h == 0
            assert hb.ship_retryable_client_errors_1h == 0
            assert hb.ship_connect_errors_1h == 0
            assert hb.ship_latency_p50_ms_1h is None
            assert hb.ship_latency_p95_ms_1h is None
            assert hb.disk_free_bytes == 50_000_000_000
            assert hb.is_offline == 0
    finally:
        api_app_ref.dependency_overrides = {}


@pytest.mark.asyncio
async def test_catalog_heartbeat_uses_one_rpc_without_opening_sqlite(monkeypatch):
    import zerg.routers.heartbeat as heartbeat_router

    calls: list[tuple[str, dict, float]] = []

    class CatalogClient:
        async def call(self, method, params, *, timeout_seconds):
            calls.append((method, params, timeout_seconds))
            return {"previous_sessions_digest": "digest-0", "commit_seq": "42", "exact_replay": False}

    class FakeRequest:
        client = SimpleNamespace(host="127.0.0.1")

        async def body(self):
            return b"{}"

    def fail_legacy_serializer():  # pragma: no cover - assertion is the behavior
        raise AssertionError("catalog heartbeat must not resolve a Runtime Host SQLite serializer")

    monkeypatch.setattr(heartbeat_router, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(heartbeat_router, "live_store_configured", lambda: True)
    monkeypatch.setattr(heartbeat_router, "get_catalogd_client", lambda: CatalogClient())
    monkeypatch.setattr(heartbeat_router, "get_write_serializer", fail_legacy_serializer)
    monkeypatch.setattr(heartbeat_router, "get_live_write_serializer", fail_legacy_serializer)

    session_id = uuid4()
    payload = heartbeat_router.HeartbeatIn(
        version="catalog-test",
        sessions_digest="digest-1",
        sessions_sequence=8,
        managed_sessions=[
            heartbeat_router.ManagedSessionLeaseIn(
                session_id=session_id,
                provider="codex",
                machine_id="cinder",
                sequence=8,
                state="attached",
                phase="idle",
            )
        ],
    )
    response = await heartbeat_router.ingest_heartbeat(
        payload,
        FakeRequest(),
        None,
        SimpleNamespace(id="token-1", device_id="cinder", owner_id=7),
    )

    assert response.status_code == 204
    assert len(calls) == 1
    method, params, timeout = calls[0]
    assert method == "machine.heartbeat.apply.v2"
    assert timeout == heartbeat_router._HOT_HEARTBEAT_QUEUE_TIMEOUT_SECONDS
    assert params["heartbeat"]["device_id"] == "cinder"
    assert params["heartbeat"]["sessions_digest"] == "digest-1"
    assert params["managed_leases_present"] is True
    assert params["managed_leases"][0]["session_id"] == str(session_id)
    assert params["owner_id"] == 7

def test_heartbeat_releases_request_db_before_serialized_write(tmp_path, monkeypatch):
    from zerg.dependencies.agents_auth import verify_agents_token
    from zerg.main import api_app

    engine = make_engine(f"sqlite:///{tmp_path}/heartbeat_release.db", pool_size=1, max_overflow=0)
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    observations: dict[str, int] = {}

    class ReleaseCheckingSerializer:
        is_configured = True

        async def execute_after_closing_request_session(self, fn, fallback_db, **_kwargs):
            observations["before_close"] = engine.pool.checkedout()
            fallback_db.close()
            observations["after_close"] = engine.pool.checkedout()
            with SessionLocal() as write_db:
                result = fn(write_db)
                write_db.commit()
                return result

        async def execute_or_direct(self, *_args, **_kwargs):  # pragma: no cover - regression guard
            raise AssertionError("heartbeat must release the request DB before waiting on serialized writes")

    def override_get_db():
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
            yield db

    def override_verify_agents_token():
        return SimpleNamespace(device_id="heartbeat-release", id="token-1")

    monkeypatch.setattr("zerg.routers.heartbeat.get_write_serializer", lambda: ReleaseCheckingSerializer())
    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
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
            )
        assert response.status_code == 204, response.text
    finally:
        api_app.dependency_overrides = {}
        engine.dispose()

    assert observations == {"before_close": 1, "after_close": 0}


@pytest.mark.asyncio
async def test_heartbeat_response_returns_after_stamp_before_bookkeeping(tmp_path, monkeypatch):
    import zerg.routers.heartbeat as heartbeat_router

    monkeypatch.delenv("TESTING", raising=False)

    engine = make_engine(f"sqlite:///{tmp_path}/heartbeat_split.db")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)

    stamp_done = asyncio.Event()
    bookkeeping_started = asyncio.Event()
    release_bookkeeping = asyncio.Event()

    class SplitSerializer:
        is_configured = True

        async def execute_after_closing_request_session(self, fn, fallback_db, **kwargs):
            assert kwargs["label"] == "heartbeat-stamp"
            fallback_db.close()
            with SessionLocal() as write_db:
                result = fn(write_db)
                write_db.commit()
            stamp_done.set()
            return result

        async def execute(self, fn, **kwargs):
            assert kwargs["label"] == "heartbeat-bookkeeping"
            bookkeeping_started.set()
            await release_bookkeeping.wait()
            return {}

    class _FakeRequest:
        client = SimpleNamespace(host="127.0.0.1")

        def __init__(self, body: bytes) -> None:
            self._body = body

        async def body(self) -> bytes:
            return self._body

    monkeypatch.setattr(heartbeat_router, "get_write_serializer", lambda: SplitSerializer())

    payload = heartbeat_router.HeartbeatIn(
        version="0.5.0",
        daemon_pid=12345,
        spool_pending_count=0,
        parse_error_count_1h=0,
        consecutive_ship_failures=0,
        disk_free_bytes=50_000_000_000,
        is_offline=False,
        managed_sessions=[
            heartbeat_router.ManagedSessionLeaseIn(
                session_id=uuid4(),
                provider="codex",
                machine_id="heartbeat-split",
                sequence=1,
                state="attached",
            )
        ],
    )
    request_db = SessionLocal()
    try:
        response = await asyncio.wait_for(
            heartbeat_router.ingest_heartbeat(
                payload,
                _FakeRequest(payload.model_dump_json().encode()),
                request_db,
                SimpleNamespace(device_id="heartbeat-split", id="token-1"),
            ),
            timeout=0.5,
        )
        assert response.status_code == 204
        assert stamp_done.is_set()
        await asyncio.wait_for(bookkeeping_started.wait(), timeout=0.5)
        assert not release_bookkeeping.is_set()
        with SessionLocal() as db:
            assert db.query(AgentHeartbeat).filter(AgentHeartbeat.device_id == "heartbeat-split").count() == 1
    finally:
        release_bookkeeping.set()
        await asyncio.sleep(0)
        engine.dispose()


def test_heartbeat_endpoint_appends_history_rows(tmp_path):
    """Two POSTs to /agents/heartbeat append two rows for the same device."""
    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)

    try:
        for i in range(2):
            response = client.post(
                "/api/agents/heartbeat",
                json={
                    "version": "0.5.0",
                    "daemon_pid": 99,
                    "spool_pending_count": i,
                    "parse_error_count_1h": 0,
                    "consecutive_ship_failures": 0,
                    "disk_free_bytes": 1_000_000,
                    "is_offline": False,
                },
            )
            assert response.status_code == 204

        with SessionLocal() as db:
            count = db.query(AgentHeartbeat).count()
            assert count == 2, "Two heartbeats should produce two rows"
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_prunes_old_rows(tmp_path):
    """Rows older than 30 days for the same device are pruned on next heartbeat."""
    SessionLocal = _make_db(tmp_path)

    # Insert an old row directly
    old_ts = datetime.now(timezone.utc) - timedelta(days=31)
    with SessionLocal() as db:
        db.add(
            AgentHeartbeat(
                device_id="testclient",  # matches fallback IP in test
                received_at=old_ts,
                version="0.4.0",
                spool_pending=0,
                parse_errors_1h=0,
                consecutive_failures=0,
                disk_free_bytes=0,
                is_offline=0,
            )
        )
        db.commit()
        assert db.query(AgentHeartbeat).count() == 1

    client, api_app_ref = _make_client(SessionLocal)

    try:
        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.5.0",
                "daemon_pid": 1,
                "spool_pending_count": 0,
                "parse_error_count_1h": 0,
                "consecutive_ship_failures": 0,
                "disk_free_bytes": 0,
                "is_offline": False,
            },
        )
        assert response.status_code == 204

        with SessionLocal() as db:
            rows = db.query(AgentHeartbeat).all()
            # Old row should be pruned; new row remains
            assert len(rows) == 1, f"Expected 1 row after prune, got {len(rows)}"
            assert rows[0].version == "0.5.0"
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_endpoint_persists_transport_summary_fields(tmp_path):
    """Heartbeat raw_json preserves the engine ship telemetry payload."""
    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)

    try:
        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.5.0",
                "daemon_pid": 42,
                "last_ship_attempt_at": "2026-04-23T20:00:03Z",
                "last_ship_result": "rate_limited",
                "last_ship_latency_ms": 187,
                "last_ship_http_status": 429,
                "last_ship_error_kind": "rate_limited",
                "last_ship_error_message": "429: rate limited",
                "spool_pending_count": 7,
                "spool_dead_count": 2,
                "parse_error_count_1h": 2,
                "consecutive_ship_failures": 1,
                "ship_attempts_1h": 12,
                "ship_successes_1h": 8,
                "ship_rate_limited_1h": 3,
                "ship_server_errors_1h": 1,
                "ship_payload_rejections_1h": 0,
                "ship_payload_too_large_1h": 0,
                "ship_retryable_client_errors_1h": 0,
                "ship_connect_errors_1h": 0,
                "ship_latency_p50_ms_1h": 140,
                "ship_latency_p95_ms_1h": 260,
                "ship_lanes": {
                    "archive": {
                        "attempts_1h": 4,
                        "successes_1h": 3,
                        "backpressure_1h": 1,
                        "events_1h": 120,
                        "bytes_1h": 524288,
                        "events_per_sec_ewma_10s": 42.5,
                        "bytes_per_sec_ewma_10s": 131072.0,
                    }
                },
                "events_per_sec_ewma_10s": 12.5,
                "bytes_per_sec_ewma_10s": 65536.0,
                "disk_free_bytes": 50_000_000,
                "is_offline": False,
            },
        )
        assert response.status_code == 204

        with SessionLocal() as db:
            row = db.query(AgentHeartbeat).one()
            raw = json.loads(row.raw_json)
            assert row.last_ship_attempt_at is not None
            # SQLite drops timezone info on round-trip; Postgres preserves UTC.
            assert row.last_ship_attempt_at.replace(tzinfo=None) == datetime(2026, 4, 23, 20, 0, 3)
            assert row.last_ship_result == "rate_limited"
            assert row.last_ship_latency_ms == 187
            assert row.last_ship_http_status == 429
            assert row.spool_dead == 2
            assert row.ship_attempts_1h == 12
            assert row.ship_successes_1h == 8
            assert row.ship_rate_limited_1h == 3
            assert row.ship_server_errors_1h == 1
            assert row.ship_payload_rejections_1h == 0
            assert row.ship_payload_too_large_1h == 0
            assert row.ship_retryable_client_errors_1h == 0
            assert row.ship_connect_errors_1h == 0
            assert row.ship_latency_p50_ms_1h == 140
            assert row.ship_latency_p95_ms_1h == 260
            assert raw["last_ship_attempt_at"] == "2026-04-23T20:00:03Z"
            assert raw["last_ship_result"] == "rate_limited"
            assert raw["last_ship_latency_ms"] == 187
            assert raw["last_ship_http_status"] == 429
            assert raw["last_ship_error_kind"] == "rate_limited"
            assert raw["last_ship_error_message"] == "429: rate limited"
            assert raw["spool_dead_count"] == 2
            assert raw["ship_attempts_1h"] == 12
            assert raw["ship_successes_1h"] == 8
            assert raw["ship_rate_limited_1h"] == 3
            assert raw["ship_latency_p50_ms_1h"] == 140
            assert raw["ship_latency_p95_ms_1h"] == 260
            assert raw["ship_lanes"]["archive"]["attempts_1h"] == 4
            assert raw["ship_lanes"]["archive"]["backpressure_1h"] == 1
            assert raw["ship_lanes"]["archive"]["bytes_per_sec_ewma_10s"] == 131072.0
            assert raw["events_per_sec_ewma_10s"] == 12.5
            assert raw["bytes_per_sec_ewma_10s"] == 65536.0
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_resolved_sessions_materialize_managed_control(tmp_path):
    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    session_id = uuid4()

    try:
        with SessionLocal() as db:
            db.add(
                AgentSession(
                    id=session_id,
                    provider="codex",
                    environment="laptop",
                    started_at=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
                    last_activity_at=datetime(2026, 5, 5, 11, 59, tzinfo=timezone.utc),
                                                                                user_messages=1,
                    assistant_messages=1,
                    tool_calls=0,
                                    )
            )
            db.commit()

        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.7.0",
                "daemon_pid": 42,
                "sessions": [
                    {
                        "session_id": str(session_id),
                        "provider": "codex",
                        "provider_session_id": "thread-codex",
                        "control_path": "managed",
                        "presentation_state": "managed_attached",
                        "state": "attached",
                        "phase": "thinking",
                        "tool_name": None,
                        "phase_observed_at": "2026-05-05T11:59:58Z",
                        "last_activity_at": "2026-05-05T11:59:58Z",
                        "workspace": {"cwd": "/Users/test/git/zerg", "label": "zerg"},
                        "process": {
                            "pid": 4201,
                            "process_start_time": "Mon May  5 11:20:00 2026",
                            "boot_id": "macos:1777970400:0",
                            "started_at": "2026-05-05T11:20:00Z",
                        },
                        "bridge": {
                            "bridge_pid": 4202,
                            "app_server_pid": 4203,
                            "heartbeat_at": "2026-05-05T11:59:58Z",
                            "status": "ready",
                            "thread_subscription_status": "subscribed",
                        },
                        "evidence": {"process_observed": True, "transcript_observed": True},
                        "reason_codes": [],
                    }
                ],
            },
        )

        assert response.status_code == 204, response.text
        with SessionLocal() as db:
            connection = db.query(SessionConnection).one()
            row = db.query(AgentHeartbeat).one()
            raw = json.loads(row.raw_json)

            assert connection.control_plane == "codex_bridge"
            assert connection.state == "attached"
            assert connection.device_id == "testclient"
            assert connection.last_health_at is not None
            assert raw["sessions"][0]["control_path"] == "managed"
            assert raw["sessions"][0]["process"]["process_start_time"] == "Mon May  5 11:20:00 2026"
            assert raw["sessions"][0]["process"]["boot_id"] == "macos:1777970400:0"
            assert raw["managed_sessions"] == []
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_resolved_sessions_ignore_legacy_session_identity(tmp_path):
    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    resolved_session_id = uuid4()
    legacy_session_id = uuid4()

    try:
        with SessionLocal() as db:
            db.add_all(
                [
                    AgentSession(
                        id=resolved_session_id,
                        provider="codex",
                        environment="laptop",
                        started_at=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
                                                                                                user_messages=1,
                        assistant_messages=1,
                        tool_calls=0,
                                            ),
                    AgentSession(
                        id=legacy_session_id,
                        provider="claude",
                        environment="laptop",
                        started_at=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
                                                                                                user_messages=1,
                        assistant_messages=1,
                        tool_calls=0,
                                            ),
                ]
            )
            db.commit()

        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.7.0",
                "daemon_pid": 42,
                "sessions": [
                    {
                        "session_id": str(resolved_session_id),
                        "provider": "codex",
                        "provider_session_id": "thread-codex",
                        "control_path": "managed",
                        "presentation_state": "managed_attached",
                        "state": "attached",
                        "phase": "idle",
                        "last_activity_at": "2026-05-05T11:59:58Z",
                        "workspace": {"cwd": "/Users/test/git/zerg", "label": "zerg"},
                        "process": {"pid": 4201},
                        "bridge": {
                            "bridge_pid": 4202,
                            "app_server_pid": 4203,
                            "heartbeat_at": "2026-05-05T11:59:58Z",
                            "status": "ready",
                            "thread_subscription_status": "subscribed",
                        },
                        "evidence": {"process_observed": True, "transcript_observed": True},
                        "reason_codes": [],
                    }
                ],
                "managed_sessions": [
                    {
                        "session_id": str(legacy_session_id),
                        "provider": "claude",
                        "machine_id": "legacy-machine",
                        "sequence": 1,
                        "state": "attached",
                        "phase": "idle",
                        "bridge_status": "ready",
                        "observed_at": "2026-05-05T11:59:58Z",
                        "lease_ttl_ms": 900000,
                    }
                ],
            },
        )

        assert response.status_code == 204, response.text
        with SessionLocal() as db:
            connections = db.query(SessionConnection).all()
            assert len(connections) == 1
            assert connections[0].control_plane == "codex_bridge"
            assert connections[0].device_id == "testclient"
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_resolved_managed_unknown_state_does_not_attach(tmp_path):
    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    session_id = uuid4()

    try:
        with SessionLocal() as db:
            db.add(
                AgentSession(
                    id=session_id,
                    provider="codex",
                    environment="laptop",
                    started_at=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
                                                                                user_messages=1,
                    assistant_messages=1,
                    tool_calls=0,
                                    )
            )
            db.commit()

        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.7.0",
                "daemon_pid": 42,
                "sessions": [
                    {
                        "session_id": str(session_id),
                        "provider": "codex",
                        "provider_session_id": "thread-codex",
                        "control_path": "managed",
                        "presentation_state": "managed_attached",
                        "state": "future_state",
                        "phase": "idle",
                        "last_activity_at": "2026-05-05T11:59:58Z",
                        "workspace": {"cwd": "/Users/test/git/zerg", "label": "zerg"},
                        "process": {"pid": 4201},
                        "bridge": {
                            "bridge_pid": 4202,
                            "app_server_pid": 4203,
                            "heartbeat_at": "2026-05-05T11:59:58Z",
                            "status": "ready",
                            "thread_subscription_status": "subscribed",
                        },
                        "evidence": {"process_observed": True, "transcript_observed": True},
                        "reason_codes": ["future_state"],
                    }
                ],
            },
        )

        assert response.status_code == 204, response.text
        with SessionLocal() as db:
            assert db.query(SessionConnection).count() == 0
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_legacy_managed_sessions_still_materialize_control(tmp_path):
    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    session_id = uuid4()

    try:
        with SessionLocal() as db:
            db.add(
                AgentSession(
                    id=session_id,
                    provider="claude",
                    environment="laptop",
                    started_at=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
                                                                                user_messages=1,
                    assistant_messages=1,
                    tool_calls=0,
                                    )
            )
            db.commit()

        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.6.0",
                "daemon_pid": 42,
                "managed_sessions": [
                    {
                        "session_id": str(session_id),
                        "provider": "claude",
                        "machine_id": "cinder",
                        "sequence": 42,
                        "state": "attached",
                        "phase": "idle",
                        "bridge_status": "ready",
                        "observed_at": "2026-05-05T11:59:58Z",
                        "lease_ttl_ms": 900000,
                    }
                ],
            },
        )

        assert response.status_code == 204, response.text
        with SessionLocal() as db:
            connection = db.query(SessionConnection).one()
            assert connection.control_plane == "claude_channel_bridge"
            assert connection.state == "attached"
            assert connection.device_id == "testclient"
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_rejects_null_resolved_sessions(tmp_path):
    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)

    try:
        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.7.0",
                "daemon_pid": 42,
                "sessions": None,
            },
        )

        assert response.status_code == 422
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_empty_resolved_sessions_detaches_missing_managed_control(tmp_path):
    from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    session_id = uuid4()

    try:
        with SessionLocal() as db:
            session = AgentSession(
                id=session_id,
                provider="codex",
                environment="laptop",
                started_at=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
                                                                user_messages=1,
                assistant_messages=1,
                tool_calls=0,
                            )
            db.add(session)
            db.flush()
            _thread, _run, connection = seed_managed_kernel_rows(db, session, control_plane="codex_bridge")
            connection.device_id = "testclient"
            db.commit()

        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.7.0",
                "daemon_pid": 42,
                "sessions": [],
            },
        )

        assert response.status_code == 204, response.text
        with SessionLocal() as db:
            connection = db.query(SessionConnection).one()
            assert connection.state == "detached"
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_omission_detaches_managed_control_without_ending_the_run(tmp_path):
    from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    session_id = uuid4()
    now = datetime.now(timezone.utc)

    try:
        with SessionLocal() as db:
            session = AgentSession(
                id=session_id,
                provider="codex",
                environment="laptop",
                started_at=now - timedelta(minutes=5),
                user_messages=1,
                assistant_messages=1,
                tool_calls=0,
            )
            db.add(session)
            db.flush()
            _thread, _run, connection = seed_managed_kernel_rows(db, session, control_plane="codex_bridge")
            connection.device_id = "testclient"
            ingest_runtime_events(
                db,
                [
                    RuntimeEventIngest(
                        runtime_key=runtime_key_for_session("codex", str(session_id)),
                        session_id=session_id,
                        provider="codex",
                        device_id="testclient",
                        source="codex_bridge",
                        kind="phase_signal",
                        phase="thinking",
                        occurred_at=now,
                        freshness_ms=90_000,
                        dedupe_key="managed-phase-before-omission",
                        payload={},
                    )
                ],
            )
            db.commit()

        response = client.post(
            "/api/agents/heartbeat",
            json={"version": "0.7.0", "daemon_pid": 42, "sessions": []},
        )

        assert response.status_code == 204, response.text
        with SessionLocal() as db:
            connection = db.query(SessionConnection).one()
            runtime = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session_id).one()
            assert connection.state == "detached"
            assert runtime.terminal_state is None
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_reattach_does_not_erase_a_terminal_run(tmp_path):
    from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    session_id = uuid4()
    now = datetime.now(timezone.utc)

    try:
        with SessionLocal() as db:
            session = AgentSession(
                id=session_id,
                provider="codex",
                environment="laptop",
                started_at=now - timedelta(minutes=5),
                user_messages=1,
                assistant_messages=1,
                tool_calls=0,
            )
            db.add(session)
            db.flush()
            _thread, _run, connection = seed_managed_kernel_rows(db, session, control_plane="codex_bridge")
            connection.device_id = "testclient"
            ingest_runtime_events(
                db,
                [
                    RuntimeEventIngest(
                        runtime_key=runtime_key_for_session("codex", str(session_id)),
                        session_id=session_id,
                        provider="codex",
                        device_id="testclient",
                        source="engine_attached_lease",
                        kind="terminal_signal",
                        occurred_at=now,
                        dedupe_key="legacy-managed-terminal",
                        payload={"terminal_state": "process_gone"},
                    )
                ],
            )
            db.commit()

        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.7.0",
                "daemon_pid": 42,
                "managed_sessions": [
                    {
                        "session_id": str(session_id),
                        "provider": "codex",
                        "machine_id": "testclient",
                        "sequence": 1,
                        "state": "attached",
                        "observed_at": now.isoformat(),
                        "lease_ttl_ms": 900_000,
                    }
                ],
            },
        )

        assert response.status_code == 204, response.text
        with SessionLocal() as db:
            runtime = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session_id).one()
            assert runtime.terminal_state == "process_gone"
            assert runtime.terminal_source == "engine_attached_lease"
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_resolved_opencode_server_bridge_keeps_live_control(tmp_path):
    from tests_lite._kernel_test_helpers import seed_managed_kernel_rows
    from zerg.services.agents.kernel_capabilities import project_session_capabilities

    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    session_id = uuid4()

    try:
        with SessionLocal() as db:
            session = AgentSession(
                id=session_id,
                provider="opencode",
                environment="laptop",
                started_at=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
                                                                device_id="testclient",
                user_messages=1,
                assistant_messages=1,
                tool_calls=0,
                            )
            db.add(session)
            db.flush()
            _thread, _run, connection = seed_managed_kernel_rows(
                db,
                session,
                control_plane="opencode_server_bridge",
                can_terminate=True,
            )
            connection.device_id = "testclient"
            db.commit()

        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.7.0",
                "daemon_pid": 42,
                "sessions": [
                    {
                        "session_id": str(session_id),
                        "provider": "opencode",
                        "provider_session_id": "opencode-native-session",
                        "control_path": "managed",
                        "presentation_state": "managed_attached",
                        "state": "attached",
                        "phase": "idle",
                        "phase_observed_at": "2026-05-05T11:59:58Z",
                        "last_activity_at": "2026-05-05T11:59:58Z",
                        "workspace": {"cwd": "/Users/test/git/zerg", "label": "zerg"},
                        "process": {"pid": 4301, "started_at": "2026-05-05T11:20:00Z"},
                        "bridge": {
                            "bridge_pid": 4301,
                            "heartbeat_at": "2026-05-05T11:59:58Z",
                            "status": "ready",
                            "launch_mode": "server_bridge",
                        },
                        "evidence": {
                            "process_observed": True,
                            "transcript_observed": True,
                            "join_keys": [
                                f"session_id={session_id}",
                                "provider_session_id=opencode-native-session",
                                "opencode_pid=4301",
                            ],
                        },
                        "reason_codes": [],
                    }
                ],
            },
        )

        assert response.status_code == 204, response.text
        with SessionLocal() as db:
            connection = db.query(SessionConnection).one()
            assert connection.control_plane == "opencode_server_bridge"
            assert connection.state == "attached"
            assert connection.device_id == "testclient"
            caps = project_session_capabilities(db, session_id=session_id)
            assert caps.live_control_available is True
            assert caps.can_send_input is True
            assert caps.can_steer_active_turn is False
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_repeated_opencode_digest_repairs_live_send_capabilities(monkeypatch, tmp_path):
    import zerg.routers.heartbeat as heartbeat_router
    from tests_lite._kernel_test_helpers import seed_managed_kernel_rows
    from zerg.services.agents.kernel_capabilities import project_session_capabilities

    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    session_id = uuid4()

    def fail_upsert(*args, **kwargs):
        raise AssertionError("unchanged digest should refresh without full managed lease upsert")

    try:
        with SessionLocal() as db:
            session = AgentSession(
                id=session_id,
                provider="opencode",
                environment="laptop",
                started_at=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
                                                                device_id="testclient",
                user_messages=1,
                assistant_messages=1,
                tool_calls=0,
                            )
            db.add(session)
            db.flush()
            _thread, _run, connection = seed_managed_kernel_rows(
                db,
                session,
                control_plane="opencode_server_bridge",
                can_send_input=False,
                can_interrupt=False,
                can_terminate=False,
            )
            connection.device_id = "testclient"
            connection.can_send_input = 0
            connection.can_interrupt = 0
            connection.can_terminate = 0
            connection.can_tail_output = 0
            connection.can_resume = 0
            db.add(
                AgentHeartbeat(
                    device_id="testclient",
                    received_at=datetime(2026, 5, 5, 11, 1, tzinfo=timezone.utc),
                    raw_json=json.dumps({"sessions_digest": "opencode-digest-1"}),
                    sessions_digest="opencode-digest-1",
                    sessions_sequence=1,
                )
            )
            db.commit()

        monkeypatch.setattr(heartbeat_router, "upsert_managed_control_leases", fail_upsert)

        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.7.0",
                "daemon_pid": 42,
                "sessions_digest": "opencode-digest-1",
                "sessions_sequence": 2,
                "sessions": [
                    {
                        "session_id": str(session_id),
                        "provider": "opencode",
                        "provider_session_id": "opencode-native-session",
                        "control_path": "managed",
                        "presentation_state": "managed_attached",
                        "state": "attached",
                        "phase": "idle",
                        "phase_observed_at": "2026-05-05T11:59:58Z",
                        "last_activity_at": "2026-05-05T11:59:58Z",
                        "workspace": {"cwd": "/Users/test/git/zerg", "label": "zerg"},
                        "process": {"pid": 4301, "started_at": "2026-05-05T11:20:00Z"},
                        "bridge": {
                            "bridge_pid": 4301,
                            "heartbeat_at": "2026-05-05T11:59:58Z",
                            "status": "ready",
                            "launch_mode": "server_bridge",
                        },
                        "evidence": {
                            "process_observed": True,
                            "transcript_observed": True,
                            "join_keys": [
                                f"session_id={session_id}",
                                "provider_session_id=opencode-native-session",
                                "opencode_pid=4301",
                            ],
                        },
                        "reason_codes": [],
                    }
                ],
            },
        )

        assert response.status_code == 204, response.text
        with SessionLocal() as db:
            connection = db.query(SessionConnection).one()
            assert connection.control_plane == "opencode_server_bridge"
            assert connection.state == "attached"
            assert connection.can_send_input == 1
            assert connection.can_interrupt == 1
            assert connection.can_terminate == 1
            assert connection.can_tail_output == 1
            # Kernel can_resume is host reattach, not provider continue.
            assert connection.can_resume == 1
            caps = project_session_capabilities(db, session_id=session_id)
            assert caps.live_control_available is True
            assert caps.host_reattach_available is True
            assert caps.can_send_input is True
            assert caps.can_interrupt is True
            assert caps.can_resume is True
            assert caps.can_steer_active_turn is False
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_empty_resolved_sessions_does_not_detach_other_device_control(tmp_path):
    from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    first_session_id = uuid4()
    other_session_id = uuid4()

    try:
        with SessionLocal() as db:
            first_session = AgentSession(
                id=first_session_id,
                provider="codex",
                environment="laptop",
                started_at=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
                                                                user_messages=1,
                assistant_messages=1,
                tool_calls=0,
                            )
            other_session = AgentSession(
                id=other_session_id,
                provider="codex",
                environment="desktop",
                started_at=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
                                                                user_messages=1,
                assistant_messages=1,
                tool_calls=0,
                            )
            db.add_all([first_session, other_session])
            db.flush()
            _thread, _run, first_connection = seed_managed_kernel_rows(db, first_session, control_plane="codex_bridge")
            first_connection.device_id = "testclient"
            _thread, _run, other_connection = seed_managed_kernel_rows(db, other_session, control_plane="codex_bridge")
            other_connection.device_id = "other-device"
            db.commit()

        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.7.0",
                "daemon_pid": 42,
                "sessions": [],
            },
        )

        assert response.status_code == 204, response.text
        with SessionLocal() as db:
            states_by_device = {row.device_id: row.state for row in db.query(SessionConnection).all()}
            assert states_by_device == {
                "testclient": "detached",
                "other-device": "attached",
            }
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_empty_resolved_sessions_does_not_detach_unknown_device_control(tmp_path):
    from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    session_id = uuid4()

    try:
        with SessionLocal() as db:
            session = AgentSession(
                id=session_id,
                provider="codex",
                environment="laptop",
                started_at=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
                                                                user_messages=1,
                assistant_messages=1,
                tool_calls=0,
                            )
            db.add(session)
            db.flush()
            seed_managed_kernel_rows(db, session, control_plane="codex_bridge")
            db.commit()

        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.7.0",
                "daemon_pid": 42,
                "sessions": [],
            },
        )

        assert response.status_code == 204, response.text
        with SessionLocal() as db:
            connection = db.query(SessionConnection).one()
            assert connection.device_id is None
            assert connection.state == "attached"
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_repeated_sessions_digest_refreshes_health_without_snapshot_work(monkeypatch, tmp_path):
    import zerg.routers.heartbeat as heartbeat_router
    from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    session_id = uuid4()
    old_health = datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc)

    def fail_upsert(*args, **kwargs):
        raise AssertionError("unchanged digest should not upsert managed leases")

    def fail_mark_missing(*args, **kwargs):
        raise AssertionError("unchanged digest should not scan missing managed leases")

    try:
        with SessionLocal() as db:
            session = AgentSession(
                id=session_id,
                provider="codex",
                environment="laptop",
                started_at=datetime(2026, 5, 5, 10, 0, tzinfo=timezone.utc),
                                                                user_messages=1,
                assistant_messages=1,
                tool_calls=0,
                            )
            db.add(session)
            db.flush()
            _thread, _run, connection = seed_managed_kernel_rows(db, session, control_plane="codex_bridge")
            connection.device_id = "testclient"
            connection.last_health_at = old_health
            db.add(
                AgentHeartbeat(
                    device_id="testclient",
                    received_at=datetime(2026, 5, 5, 11, 1, tzinfo=timezone.utc),
                    raw_json=json.dumps({"sessions_digest": "digest-1"}),
                    sessions_digest="digest-1",
                    sessions_sequence=1,
                )
            )
            db.commit()

        monkeypatch.setattr(heartbeat_router, "upsert_managed_control_leases", fail_upsert)
        monkeypatch.setattr(heartbeat_router, "mark_missing_managed_control_leases", fail_mark_missing)

        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.7.0",
                "daemon_pid": 42,
                "sessions_digest": "digest-1",
                "sessions_sequence": 2,
                "sessions": [
                    {
                        "session_id": str(session_id),
                        "provider": "codex",
                        "provider_session_id": "thread-codex",
                        "control_path": "managed",
                        "presentation_state": "managed_attached",
                        "state": "attached",
                        "phase": "idle",
                        "workspace": {"cwd": "/Users/test/git/zerg", "label": "zerg"},
                        "process": {"pid": 4201},
                        "bridge": {"status": "ready", "thread_subscription_status": "subscribed"},
                        "evidence": {"process_observed": True, "transcript_observed": True},
                        "reason_codes": [],
                    }
                ],
            },
        )

        assert response.status_code == 204, response.text
        with SessionLocal() as db:
            connection = db.query(SessionConnection).one()
            assert connection.state == "attached"
            assert connection.last_health_at is not None
            refreshed_at = connection.last_health_at
            if refreshed_at.tzinfo is None:
                refreshed_at = refreshed_at.replace(tzinfo=timezone.utc)
            assert refreshed_at > old_health
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_missing_managed_detach_can_be_disabled(monkeypatch, tmp_path):
    from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

    monkeypatch.setenv("LONGHOUSE_DISABLE_MISSING_MANAGED_LEASE_DETACH", "1")
    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    session_id = uuid4()

    try:
        with SessionLocal() as db:
            session = AgentSession(
                id=session_id,
                provider="codex",
                environment="laptop",
                started_at=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
                                                                user_messages=1,
                assistant_messages=1,
                tool_calls=0,
                            )
            db.add(session)
            db.flush()
            _thread, _run, connection = seed_managed_kernel_rows(db, session, control_plane="codex_bridge")
            connection.device_id = "testclient"
            db.commit()

        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.7.0",
                "daemon_pid": 42,
                "sessions": [],
            },
        )

        assert response.status_code == 204, response.text
        with SessionLocal() as db:
            connection = db.query(SessionConnection).one()
            assert connection.device_id == "testclient"
            assert connection.state == "attached"
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_empty_resolved_sessions_closes_stale_unmanaged_session(tmp_path):
    SessionLocal = _make_db(tmp_path)
    client, api_app_ref = _make_client(SessionLocal)
    session_id = uuid4()
    provider_session_id = "codex-thread-gone"
    now = datetime.now(timezone.utc)

    try:
        with SessionLocal() as db:
            session = AgentSession(
                id=session_id,
                provider="codex",
                environment="laptop",
                started_at=now - timedelta(minutes=20),
                last_activity_at=now - timedelta(minutes=10),
                                                user_messages=1,
                assistant_messages=1,
                tool_calls=0,
            )
            db.add(session)
            db.flush()
            from zerg.services.agents.kernel_writes import ensure_primary_thread
            from zerg.services.agents.kernel_writes import record_thread_alias

            thread = ensure_primary_thread(db, session)
            record_thread_alias(
                db,
                thread=thread,
                provider="codex",
                alias_kind="provider_session_id",
                alias_value=provider_session_id,
            )
            db.commit()
            ingest_runtime_events(
                db,
                [
                    RuntimeEventIngest(
                        runtime_key=runtime_key_for_session("codex", provider_session_id),
                        session_id=session_id,
                        provider="codex",
                        device_id="testclient",
                        source="codex_hook",
                        kind="phase_signal",
                        phase="thinking",
                        occurred_at=now - timedelta(minutes=10),
                        freshness_ms=90 * 1000,
                        dedupe_key="resolved-snapshot-unmanaged-phase",
                        payload={},
                    )
                ],
            )
            db.commit()

        response = client.post(
            "/api/agents/heartbeat",
            json={
                "version": "0.7.0",
                "daemon_pid": 42,
                "sessions": [],
            },
        )

        assert response.status_code == 204, response.text
        with SessionLocal() as db:
            state = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session_id).one()
            assert state.terminal_state == "process_gone"
            assert state.terminal_source == "engine_process_snapshot"
    finally:
        api_app_ref.dependency_overrides = {}
