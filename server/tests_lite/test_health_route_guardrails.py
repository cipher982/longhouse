from __future__ import annotations

import os
from types import SimpleNamespace

from cryptography.fernet import Fernet
from sqlalchemy import text

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import make_engine
from zerg.routers import health as health_router


def test_health_reports_saturated_writer_without_entering_writer_lane(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path}/health_writer.db")
    with engine.begin() as conn:
        conn.execute(text("CREATE VIRTUAL TABLE events_fts USING fts5(content_text)"))

    class SaturatedWriter:
        is_configured = True

        def get_metrics(self):
            return {
                "queue_depth": 999,
                "writer_active": True,
                "active_label": "ingest-replay",
                "active_age_ms": 30_000.0,
            }

        async def execute(self, *_args, **_kwargs):  # pragma: no cover - regression guard
            raise AssertionError("health must not enter the serialized writer lane")

        async def execute_or_direct(self, *_args, **_kwargs):  # pragma: no cover - regression guard
            raise AssertionError("health must not enter the serialized writer lane")

        async def execute_after_closing_request_session(self, *_args, **_kwargs):  # pragma: no cover - regression guard
            raise AssertionError("health must not enter the serialized writer lane")

    import zerg.database as database_module

    monkeypatch.setattr(database_module, "default_engine", engine)
    monkeypatch.setattr(database_module, "get_wal_bytes", lambda: 0)
    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: SaturatedWriter())
    monkeypatch.setattr(health_router, "_session_projection_lag_check", lambda: {"status": "pass", "pending_sessions": 0})
    monkeypatch.setattr(health_router, "_session_enrichment_lag_check", lambda: {"status": "pass", "pending_sessions": 0})

    payload = health_router.health_check(
        SimpleNamespace(
            client=SimpleNamespace(host="testclient"),
            headers={},
        )
    )

    assert payload["checks"]["write_serializer"]["status"] == "pass"
    assert payload["checks"]["write_serializer"]["queue_depth"] == 999
    assert payload["checks"]["write_serializer"]["writer_active"] is True
