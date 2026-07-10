from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timezone

import pytest
from fastapi import HTTPException

import zerg.database as database_module
from zerg.database import get_catalog_session_factory
from zerg.database import get_db
from zerg.database import initialize_live_database
from zerg.database import make_live_engine
from zerg.database import make_sessionmaker
from zerg.models.user import User
from zerg.models.live_store import LiveSessionCatalog
from zerg.routers.agents_sessions import set_session_loop_mode
from zerg.services.session_views import SessionLoopModeRequest
from zerg.services.write_serializer import get_catalog_write_serializer
from zerg.services.write_serializer import get_live_write_serializer


def test_catalog_factory_uses_live_database_without_opening_archive(tmp_path, monkeypatch):
    live_engine = make_live_engine(f"sqlite:///{tmp_path / 'live.db'}")
    initialize_live_database(live_engine)
    LiveSession = make_sessionmaker(live_engine)
    with LiveSession() as live_db:
        live_db.add(User(id=23, email="live-only@example.com", role="USER"))
        live_db.commit()

    monkeypatch.setattr(database_module._settings, "live_database_url", str(live_engine.url))
    monkeypatch.setattr(database_module._settings, "testing", False)
    monkeypatch.setenv("TESTING", "0")
    monkeypatch.setattr(database_module, "get_live_session_factory", lambda: LiveSession)

    def fail_archive_factory():
        raise AssertionError("cold archive factory must not be opened")

    monkeypatch.setattr(database_module, "get_session_factory", fail_archive_factory)

    factory = get_catalog_session_factory()
    with factory() as catalog_db:
        assert catalog_db.query(User).one().email == "live-only@example.com"


def test_archive_route_process_keeps_catalog_auth_on_live_database(monkeypatch):
    live_sentinel = object()
    monkeypatch.setattr(database_module._settings, "database_url", "sqlite:///file:/tmp/archive.db?mode=ro&uri=true")
    monkeypatch.setattr(database_module._settings, "live_database_url", "sqlite:////tmp/live.db")
    monkeypatch.setattr(database_module, "get_session_factory", lambda: pytest.fail("catalog auth must not use cold rows"))
    monkeypatch.setattr(database_module, "get_live_session_factory", lambda: live_sentinel)
    assert get_catalog_session_factory() is live_sentinel


def test_catalog_serializer_follows_catalog_owner(monkeypatch):
    monkeypatch.setattr(database_module, "live_catalog_enabled", lambda: True)
    assert get_catalog_write_serializer() is get_live_write_serializer()


def test_runtime_archive_dependency_returns_typed_degradation(monkeypatch):
    monkeypatch.setattr(database_module._settings, "live_database_url", "sqlite:///live.db")
    monkeypatch.setattr(database_module._settings, "database_url", "sqlite:///archive.db")
    monkeypatch.setattr(database_module._settings, "testing", False)
    monkeypatch.setenv("TESTING", "0")
    dependency = get_db()
    with pytest.raises(HTTPException) as error:
        next(dependency)
    assert error.value.status_code == 503
    assert error.value.detail["code"] == "archive_route_unavailable"


def test_session_preference_mutation_uses_live_session_primary_key(tmp_path, monkeypatch):
    live_engine = make_live_engine(f"sqlite:///{tmp_path / 'live-preferences.db'}")
    initialize_live_database(live_engine)
    LiveSession = make_sessionmaker(live_engine)
    session_id = "00000000-0000-0000-0000-000000000001"
    with LiveSession() as live_db:
        live_db.add(
            LiveSessionCatalog(
                session_id=session_id,
                provider="codex",
                environment="production",
                started_at=datetime.now(timezone.utc),
            )
        )
        live_db.commit()
        monkeypatch.setattr(database_module, "live_catalog_enabled", lambda: True)
        response = asyncio.run(
            set_session_loop_mode(
                session_id=session_id,
                body=SessionLoopModeRequest(loop_mode="autopilot"),
                db=live_db,
                _auth=None,
                _single=None,
            )
        )
        assert response.loop_mode.value == "autopilot"
        assert live_db.get(LiveSessionCatalog, session_id).loop_mode == "autopilot"
