"""Tests for the machine-facing heartbeat health summary endpoint."""

from __future__ import annotations

import json
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

import zerg.services.agent_heartbeat_health as machine_health_service
from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.models.agents import AgentHeartbeat
from zerg.models.live_store import LiveBase
from zerg.models.live_store import LiveHeartbeatStamp
from zerg.routers.agents_machines import archive_backlog_control_command_type
from zerg.schemas.machines import ArchiveBacklogControlRequest


def test_archive_control_requires_lease_aware_engine_to_start_work():
    assert archive_backlog_control_command_type("paused") == "archive.backlog_control"
    assert archive_backlog_control_command_type("trickle") == "archive.backlog_control.v2"
    assert archive_backlog_control_command_type("drain") == "archive.backlog_control.v2"
    request = ArchiveBacklogControlRequest(mode="drain")
    assert archive_backlog_control_command_type(request.mode) == "archive.backlog_control.v2"


def _make_db(tmp_path):
    db_path = tmp_path / "test_agents_machine_health.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal


def _make_live_db(tmp_path):
    db_path = tmp_path / "test_live_machine_health.db"
    engine = make_engine(f"sqlite:///{db_path}")
    LiveBase.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def test_machine_health_service_reads_bounded_live_heartbeat_stamps(tmp_path, monkeypatch):
    SessionLocal = _make_live_db(tmp_path)
    pinned_now = datetime(2026, 7, 21, 7, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(machine_health_service, "utc_now", lambda: pinned_now)
    history_import = {
        "state": "inventory_ready",
        "inventory": {
            "schema_version": 1,
            "generation": 1,
            "content_sha256": "0" * 64,
            "observed_at": "2026-07-21T07:09:00Z",
            "scan_duration_ms": 724,
            "scan_error_count": 0,
            "source_count": 9947,
            "source_bytes": 24082968586,
            "wal_bytes": 66593864,
            "footprint_bytes": 24149562450,
            "providers": [
                {
                    "provider": "codex",
                    "source_count": 9947,
                    "source_bytes": 24082968586,
                    "wal_bytes": 66593864,
                    "footprint_bytes": 24149562450,
                }
            ],
        },
    }
    with SessionLocal() as db:
        db.add(
            LiveHeartbeatStamp(
                device_id="cinder",
                received_at=pinned_now - timedelta(minutes=1),
                version="0.1.28-dev+498e06c4",
                ship_attempts_1h=4,
                ship_successes_1h=4,
                disk_free_bytes=100,
                raw_json=json.dumps({"history_import": history_import}),
            )
        )
        db.commit()
        summaries, total = machine_health_service.list_machine_transport_health(
            db,
            heartbeat_model=LiveHeartbeatStamp,
            stale_after_seconds=3600,
        )

    assert total == 1
    assert summaries[0].device_id == "cinder"
    assert summaries[0].history_import.state == "inventory_ready"
    assert summaries[0].history_import.inventory is not None
    assert summaries[0].history_import.inventory.source_count == 9947

    catalog_summaries, catalog_total = machine_health_service.machine_transport_health_from_catalog_rows(
        [
            {
                "device_id": "cinder",
                "received_at": (pinned_now - timedelta(minutes=1)).isoformat(),
                "version": "0.1.28-dev+498e06c4",
                "ship_attempts_1h": 4,
                "ship_successes_1h": 4,
                "disk_free_bytes": 100,
                "raw_json": json.dumps({"history_import": history_import}),
            }
        ],
        stale_after_seconds=3600,
    )
    assert catalog_total == 1
    assert catalog_summaries[0].history_import.inventory is not None
    assert catalog_summaries[0].history_import.inventory.footprint_bytes == 24149562450


def _make_client(SessionLocal):
    from zerg.dependencies.agents_auth import require_single_tenant
    from zerg.dependencies.agents_auth import verify_agents_token
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        with SessionLocal() as db:
            yield db

    def override_verify_agents_token():
        return SimpleNamespace(device_id="testclient", id="token-1")

    def override_require_single_tenant():
        return None

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    api_app.dependency_overrides[require_single_tenant] = override_require_single_tenant
    client = TestClient(app, backend="asyncio")
    return client, api_app


def test_machine_health_route_returns_latest_row_per_device_and_sorts_by_state(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    pinned_now = datetime(2026, 4, 23, 20, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(machine_health_service, "utc_now", lambda: pinned_now)

    with SessionLocal() as db:
        db.add(
            AgentHeartbeat(
                device_id="broken-machine",
                received_at=pinned_now - timedelta(minutes=20),
                version="0.5.0",
                spool_pending=0,
                spool_dead=0,
                parse_errors_1h=0,
                consecutive_failures=0,
                ship_attempts_1h=1,
                ship_successes_1h=1,
                disk_free_bytes=100,
                is_offline=0,
            )
        )
        db.add(
            AgentHeartbeat(
                device_id="broken-machine",
                received_at=pinned_now - timedelta(minutes=1),
                version="0.6.0",
                last_ship_attempt_at=pinned_now - timedelta(minutes=1),
                last_ship_result="connect_error",
                last_ship_latency_ms=220,
                spool_pending=3,
                spool_dead=2,
                parse_errors_1h=0,
                consecutive_failures=1,
                ship_attempts_1h=5,
                ship_successes_1h=3,
                ship_connect_errors_1h=1,
                ship_latency_p50_ms_1h=120,
                ship_latency_p95_ms_1h=220,
                disk_free_bytes=100,
                is_offline=0,
            )
        )
        db.add(
            AgentHeartbeat(
                device_id="degraded-machine",
                received_at=pinned_now - timedelta(minutes=2),
                version="0.6.0",
                spool_pending=0,
                spool_dead=0,
                parse_errors_1h=0,
                consecutive_failures=2,
                ship_attempts_1h=4,
                ship_successes_1h=2,
                ship_server_errors_1h=2,
                disk_free_bytes=100,
                is_offline=0,
            )
        )
        db.add(
            AgentHeartbeat(
                device_id="healthy-machine",
                received_at=pinned_now - timedelta(minutes=3),
                version="0.6.0",
                spool_pending=0,
                spool_dead=0,
                parse_errors_1h=0,
                consecutive_failures=0,
                ship_attempts_1h=4,
                ship_successes_1h=4,
                disk_free_bytes=100,
                is_offline=0,
            )
        )
        db.commit()

    client, api_app_ref = _make_client(SessionLocal)

    try:
        response = client.get("/api/agents/machines/health?stale_after_seconds=3600&limit=2")
        assert response.status_code == 200

        payload = response.json()
        assert payload["total"] == 3
        assert [item["device_id"] for item in payload["machines"]] == [
            "broken-machine",
            "degraded-machine",
        ]

        dead_lettered = payload["machines"][0]
        assert dead_lettered["version"] == "0.6.0"
        assert dead_lettered["status"] == "degraded"
        assert dead_lettered["status_reason"] == "spool_dead"
        assert dead_lettered["status_summary"] == "2 dead-letter archive range(s) need attention."
        assert dead_lettered["heartbeat_age_seconds"] == 60
        assert dead_lettered["ship_success_rate_1h"] == 0.6
        assert dead_lettered["spool_dead"] == 2
        assert dead_lettered["reasons"] == ["spool_dead"]
        assert dead_lettered["last_ship_attempt_at"] == "2026-04-23T20:14:00Z"

        degraded = payload["machines"][1]
        assert degraded["status"] == "degraded"
        assert degraded["status_reason"] == "consecutive_failures"
        assert degraded["heartbeat_age_seconds"] == 120

        filtered = client.get("/api/agents/machines/health?status=broken&stale_after_seconds=3600")
        assert filtered.status_code == 200
        filtered_payload = filtered.json()
        assert filtered_payload["total"] == 0
        assert filtered_payload["machines"] == []
    finally:
        api_app_ref.dependency_overrides = {}


def test_machine_health_route_keeps_single_transient_connect_error_healthy(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    pinned_now = datetime(2026, 4, 23, 20, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(machine_health_service, "utc_now", lambda: pinned_now)

    with SessionLocal() as db:
        db.add(
            AgentHeartbeat(
                device_id="mostly-healthy-machine",
                received_at=pinned_now - timedelta(minutes=1),
                version="0.6.0",
                spool_pending=0,
                spool_dead=0,
                parse_errors_1h=0,
                consecutive_failures=0,
                ship_attempts_1h=65,
                ship_successes_1h=64,
                ship_connect_errors_1h=1,
                ship_latency_p50_ms_1h=320,
                ship_latency_p95_ms_1h=3400,
                disk_free_bytes=100,
                is_offline=0,
            )
        )
        db.commit()

    client, api_app_ref = _make_client(SessionLocal)

    try:
        response = client.get("/api/agents/machines/health?device_id=mostly-healthy-machine&stale_after_seconds=3600")
        assert response.status_code == 200

        payload = response.json()
        assert payload["total"] == 1
        machine = payload["machines"][0]
        assert machine["status"] == "healthy"
        assert machine["status_reason"] == "healthy"
        assert machine["status_summary"] == "Shipping healthy."
        assert machine["ship_connect_errors_1h"] == 1
        assert machine["reasons"] == []
    finally:
        api_app_ref.dependency_overrides = {}


def test_machine_archive_backlog_route_returns_latest_heartbeat_archive_state(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    pinned_now = datetime(2026, 6, 2, 20, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(machine_health_service, "utc_now", lambda: pinned_now)

    archive_backlog = {
        "state": "pending",
        "mode": "trickle",
        "pending_ranges": 6375,
        "pending_paths": 6374,
        "pending_sessions": 6306,
        "pending_bytes": 16_699_227_012,
        "dead_ranges": 0,
        "dead_bytes": 0,
    }
    with SessionLocal() as db:
        db.add(
            AgentHeartbeat(
                device_id="cinder",
                received_at=pinned_now - timedelta(seconds=30),
                version="0.6.0",
                spool_pending=6375,
                spool_dead=0,
                parse_errors_1h=0,
                consecutive_failures=0,
                ship_attempts_1h=10,
                ship_successes_1h=10,
                disk_free_bytes=100,
                is_offline=0,
                raw_json=json.dumps(
                    {
                        "archive_backlog": archive_backlog,
                        "history_import": {
                            "state": "inventory_ready",
                            "inventory": {
                                "schema_version": 1,
                                "generation": 3,
                                "content_sha256": "b" * 64,
                                "observed_at": "2026-06-02T20:14:29Z",
                                "scan_duration_ms": 32,
                                "scan_error_count": 0,
                                "source_count": 3,
                                "source_bytes": 4000,
                                "wal_bytes": 96,
                                "footprint_bytes": 4096,
                                "providers": [
                                    {
                                        "provider": "codex",
                                        "source_count": 3,
                                        "source_bytes": 4000,
                                        "wal_bytes": 96,
                                        "footprint_bytes": 4096,
                                        "oldest_modified_at_ms": 10,
                                        "newest_modified_at_ms": 20,
                                    }
                                ],
                            },
                        },
                    }
                ),
            )
        )
        db.commit()

    client, api_app_ref = _make_client(SessionLocal)

    try:
        response = client.get("/api/agents/machines/cinder/archive-backlog")
        assert response.status_code == 200
        payload = response.json()
        assert payload["device_id"] == "cinder"
        assert payload["archive_repair"]["state"] == "pending"
        assert payload["archive_repair"]["pending_ranges"] == 6375
        assert payload["archive_repair"]["pending_bytes"] == 16_699_227_012

        health = client.get("/api/agents/machines/health?device_id=cinder&stale_after_seconds=3600")
        assert health.status_code == 200
        machine = health.json()["machines"][0]
        assert machine["status"] == "healthy"
        assert machine["status_reason"] == "healthy"
        assert machine["archive_repair"]["pending_ranges"] == 6375
        assert machine["history_import"]["state"] == "inventory_ready"
        assert machine["history_import"]["inventory"]["source_count"] == 3
        assert machine["history_import"]["inventory"]["footprint_bytes"] == 4096
    finally:
        api_app_ref.dependency_overrides = {}


def test_machine_health_route_degrades_dead_archive_bytes_from_heartbeat_archive_state(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    pinned_now = datetime(2026, 6, 2, 20, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(machine_health_service, "utc_now", lambda: pinned_now)

    with SessionLocal() as db:
        db.add(
            AgentHeartbeat(
                device_id="archive-dead-machine",
                received_at=pinned_now - timedelta(seconds=30),
                version="0.6.0",
                spool_pending=0,
                spool_dead=0,
                parse_errors_1h=0,
                consecutive_failures=0,
                ship_attempts_1h=10,
                ship_successes_1h=10,
                disk_free_bytes=100,
                is_offline=0,
                raw_json=json.dumps(
                    {
                        "archive_backlog": {
                            "state": "draining",
                            "mode": "drain",
                            "pending_ranges": 0,
                            "pending_bytes": 0,
                            "dead_ranges": 0,
                            "dead_bytes": 62_675,
                        }
                    }
                ),
            )
        )
        db.commit()

    client, api_app_ref = _make_client(SessionLocal)

    try:
        response = client.get("/api/agents/machines/health?device_id=archive-dead-machine&stale_after_seconds=3600")
        assert response.status_code == 200

        payload = response.json()
        assert payload["total"] == 1
        machine = payload["machines"][0]
        assert machine["status"] == "degraded"
        assert machine["status_reason"] == "archive_dead_lettered"
        assert machine["status_summary"] == "62675 dead-letter archive byte(s) need attention."
        assert machine["spool_dead"] == 0
        assert machine["archive_repair"]["dead_ranges"] == 0
        assert machine["archive_repair"]["dead_bytes"] == 62_675
        assert machine["reasons"] == ["archive_dead_lettered"]
    finally:
        api_app_ref.dependency_overrides = {}


def test_machine_health_route_marks_transport_error_burst_degraded(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    pinned_now = datetime(2026, 4, 23, 20, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(machine_health_service, "utc_now", lambda: pinned_now)

    with SessionLocal() as db:
        db.add(
            AgentHeartbeat(
                device_id="bursty-machine",
                received_at=pinned_now - timedelta(minutes=1),
                version="0.6.0",
                spool_pending=0,
                spool_dead=0,
                parse_errors_1h=0,
                consecutive_failures=0,
                ship_attempts_1h=20,
                ship_successes_1h=18,
                ship_connect_errors_1h=2,
                ship_latency_p50_ms_1h=320,
                ship_latency_p95_ms_1h=3400,
                disk_free_bytes=100,
                is_offline=0,
                last_ship_result="connect_error",
                raw_json=json.dumps(
                    {
                        "last_ship_result": "connect_error",
                        "last_ship_error_kind": "connection_refused",
                        "last_ship_error_message": "connection refused",
                    }
                ),
            )
        )
        db.commit()

    client, api_app_ref = _make_client(SessionLocal)

    try:
        response = client.get("/api/agents/machines/health?device_id=bursty-machine&stale_after_seconds=3600")
        assert response.status_code == 200

        payload = response.json()
        assert payload["total"] == 1
        machine = payload["machines"][0]
        assert machine["status"] == "degraded"
        assert machine["status_reason"] == "connect_errors"
        assert machine["status_summary"] == "2 ship connect error(s) in the last hour. Last error: connection_refused."
        assert machine["ship_connect_errors_1h"] == 2
        assert machine["last_ship_error_kind"] == "connection_refused"
        assert machine["last_ship_error_message"] == "connection refused"
        assert machine["reasons"] == ["connect_errors"]
    finally:
        api_app_ref.dependency_overrides = {}


def test_machine_health_route_uses_active_transport_window_from_raw_json(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    pinned_now = datetime(2026, 4, 23, 20, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(machine_health_service, "utc_now", lambda: pinned_now)

    with SessionLocal() as db:
        db.add(
            AgentHeartbeat(
                device_id="recovered-machine",
                received_at=pinned_now - timedelta(minutes=1),
                version="0.6.0",
                spool_pending=0,
                spool_dead=0,
                parse_errors_1h=0,
                consecutive_failures=0,
                ship_attempts_1h=32,
                ship_successes_1h=20,
                ship_connect_errors_1h=12,
                ship_latency_p50_ms_1h=320,
                ship_latency_p95_ms_1h=3400,
                disk_free_bytes=100,
                is_offline=0,
                last_ship_result="ok",
                raw_json=json.dumps(
                    {
                        "ship_attempts_10m": 4,
                        "ship_successes_10m": 4,
                        "ship_connect_errors_10m": 0,
                        "last_ship_result": "ok",
                    }
                ),
            )
        )
        db.commit()

    client, api_app_ref = _make_client(SessionLocal)

    try:
        response = client.get("/api/agents/machines/health?device_id=recovered-machine&stale_after_seconds=3600")
        assert response.status_code == 200

        payload = response.json()
        assert payload["total"] == 1
        machine = payload["machines"][0]
        assert machine["status"] == "healthy"
        assert machine["status_reason"] == "healthy"
        assert machine["status_summary"] == "Shipping healthy."
        assert machine["ship_connect_errors_1h"] == 12
        assert machine["ship_attempts_10m"] == 4
        assert machine["ship_successes_10m"] == 4
        assert machine["ship_connect_errors_10m"] == 0
        assert machine["reasons"] == []
    finally:
        api_app_ref.dependency_overrides = {}


def test_machine_health_route_filters_by_device_and_marks_stale_rows_offline(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    pinned_now = datetime(2026, 4, 23, 20, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(machine_health_service, "utc_now", lambda: pinned_now)

    with SessionLocal() as db:
        db.add(
            AgentHeartbeat(
                device_id="sleepy-machine",
                received_at=pinned_now - timedelta(minutes=20),
                version="0.6.0",
                spool_pending=0,
                spool_dead=0,
                parse_errors_1h=0,
                consecutive_failures=0,
                disk_free_bytes=100,
                is_offline=0,
            )
        )
        db.add(
            AgentHeartbeat(
                device_id="offline-machine",
                received_at=pinned_now - timedelta(minutes=1),
                version="0.6.0",
                spool_pending=0,
                spool_dead=0,
                parse_errors_1h=0,
                consecutive_failures=0,
                disk_free_bytes=100,
                is_offline=1,
            )
        )
        db.commit()

    client, api_app_ref = _make_client(SessionLocal)

    try:
        response = client.get("/api/agents/machines/health?device_id=sleepy-machine&stale_after_seconds=600")
        assert response.status_code == 200

        payload = response.json()
        assert payload["total"] == 1
        machine = payload["machines"][0]
        assert machine["device_id"] == "sleepy-machine"
        assert machine["status"] == "offline"
        assert machine["status_reason"] == "heartbeat_stale"
        assert machine["is_stale"] is True
        assert machine["heartbeat_age_seconds"] == 1200

        offline = client.get("/api/agents/machines/health?device_id=offline-machine&stale_after_seconds=600")
        assert offline.status_code == 200
        offline_machine = offline.json()["machines"][0]
        assert offline_machine["status"] == "offline"
        assert offline_machine["status_reason"] == "reported_offline"
        assert offline_machine["is_offline"] is True
    finally:
        api_app_ref.dependency_overrides = {}


def test_machine_health_route_lets_stale_heartbeat_outrank_dead_archive_ranges(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    pinned_now = datetime(2026, 4, 23, 20, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(machine_health_service, "utc_now", lambda: pinned_now)

    with SessionLocal() as db:
        db.add(
            AgentHeartbeat(
                device_id="stale-broken-machine",
                received_at=pinned_now - timedelta(minutes=20),
                version="0.6.0",
                spool_pending=0,
                spool_dead=1,
                parse_errors_1h=0,
                consecutive_failures=0,
                disk_free_bytes=100,
                is_offline=0,
            )
        )
        db.commit()

    client, api_app_ref = _make_client(SessionLocal)

    try:
        response = client.get("/api/agents/machines/health?device_id=stale-broken-machine&stale_after_seconds=600")
        assert response.status_code == 200

        payload = response.json()
        assert payload["total"] == 1
        machine = payload["machines"][0]
        assert machine["status"] == "offline"
        assert machine["status_reason"] == "heartbeat_stale"
        assert machine["is_stale"] is True
        assert machine["reasons"] == ["heartbeat_stale", "spool_dead"]
    finally:
        api_app_ref.dependency_overrides = {}
