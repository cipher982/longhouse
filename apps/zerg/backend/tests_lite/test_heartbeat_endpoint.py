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

from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from zerg.database import get_db
from zerg.database import make_engine
from zerg.models.agents import AgentHeartbeat
from zerg.models.agents import AgentsBase


# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------


def _make_db(tmp_path):
    db_path = tmp_path / "test_heartbeat.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal


def _make_client(SessionLocal):
    """Create TestClient with get_db override + auth bypass."""
    from zerg.main import api_app
    from zerg.main import app
    from zerg.routers.agents import verify_agents_token

    def override_get_db():
        with SessionLocal() as db:
            yield db

    def override_verify_agents_token():
        # Return None so code falls back to IP-based device_id
        return None

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token

    client = TestClient(app, backend="asyncio")
    return client, api_app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


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
            assert hb.spool_pending == 3
            assert hb.disk_free_bytes == 50_000_000_000
            assert hb.is_offline == 0
    finally:
        api_app_ref.dependency_overrides = {}


def test_heartbeat_endpoint_upserts(tmp_path):
    """Two POSTs to /agents/heartbeat result in two rows for the same device."""
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
        db.add(AgentHeartbeat(
            device_id="testclient",  # matches fallback IP in test
            received_at=old_ts,
            version="0.4.0",
            spool_pending=0,
            parse_errors_1h=0,
            consecutive_failures=0,
            disk_free_bytes=0,
            is_offline=0,
        ))
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
