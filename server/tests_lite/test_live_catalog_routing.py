from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import zerg.database as database_module
from zerg.database import _live_database_enabled_for_process
from zerg.database import catalog_db_dependency
from zerg.database import get_catalog_session_factory
from zerg.database import get_db
from zerg.database import initialize_live_database
from zerg.database import make_live_engine
from zerg.database import make_sessionmaker
from zerg.database import refresh_database_settings_from_env
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.user import User
from zerg.routers.agents_sessions import SessionMessageCreate
from zerg.routers.agents_sessions import _attempt_catalog_message_delivery
from zerg.routers.agents_sessions import create_message
from zerg.routers.agents_sessions import set_session_loop_mode
from zerg.routers.agents_sessions import wall_query
from zerg.routers.runtime import _resume_live_snoozed_sessions
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


def test_catalog_dependency_stays_overrideable_during_tests(monkeypatch):
    monkeypatch.setattr(database_module._settings, "live_database_url", "sqlite:////tmp/live.db")
    monkeypatch.setattr(database_module._settings, "testing", True)
    monkeypatch.setenv("TESTING", "1")
    assert catalog_db_dependency() is get_db


def test_production_catalog_mode_does_not_construct_api_sqlite_engines(monkeypatch):
    settings = type("Settings", (), {"live_database_url": "sqlite:////data/longhouse-live.db", "testing": False})()
    monkeypatch.setenv("TESTING", "0")
    assert _live_database_enabled_for_process(settings) is False


def test_database_routing_refreshes_after_cli_sets_database_url(tmp_path, monkeypatch):
    monkeypatch.setattr(
        database_module,
        "_settings",
        SimpleNamespace(database_url="", live_database_url="", testing=False),
    )
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'longhouse.db'}")
    monkeypatch.delenv("TESTING", raising=False)

    refresh_database_settings_from_env()

    assert database_module._settings.database_url.endswith("longhouse.db")
    assert database_module._settings.live_database_url.endswith("longhouse-live.db")
    assert database_module.live_catalog_enabled() is True


def test_archive_route_process_keeps_catalog_auth_on_live_database(monkeypatch):
    live_sentinel = object()
    monkeypatch.setattr(database_module, "_archive_route_process", True)
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


def test_session_preference_mutation_uses_catalog_rpc_without_sqlite(monkeypatch):
    session_id = "00000000-0000-0000-0000-000000000001"
    observed = {}

    async def update_preferences(target_session_id, **preferences):
        observed["session_id"] = str(target_session_id)
        observed.update(preferences)
        from zerg.services.session_preferences import SessionPreferences

        return SessionPreferences(loop_mode="autopilot")

    monkeypatch.setattr(database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr("zerg.services.session_preferences.update_session_preferences", update_preferences)
    response = asyncio.run(
        set_session_loop_mode(
            session_id=session_id,
            body=SessionLoopModeRequest(loop_mode="autopilot"),
            db=None,
            _auth=None,
            _single=None,
        )
    )
    assert response.loop_mode.value == "autopilot"
    assert observed == {"session_id": session_id, "loop_mode": "autopilot"}


def test_runtime_activity_resumes_live_snoozed_session(tmp_path):
    live_engine = make_live_engine(f"sqlite:///{tmp_path / 'live-auto-resume.db'}")
    initialize_live_database(live_engine)
    LiveSession = make_sessionmaker(live_engine)
    session_id = "00000000-0000-0000-0000-000000000002"
    now = datetime.now(timezone.utc)
    with LiveSession() as live_db:
        live_db.add(
            LiveSessionCatalog(
                session_id=session_id,
                provider="codex",
                environment="production",
                started_at=now,
                user_state="snoozed",
            )
        )
        live_db.commit()

        updated = _resume_live_snoozed_sessions(
            live_db,
            [{"session_id": session_id, "auto_resume": True}],
            occurred_at=now,
        )
        live_db.commit()

        assert updated == 1
        assert live_db.get(LiveSessionCatalog, session_id).user_state == "active"


def _request_with_headers(**headers: str) -> Request:
    raw_headers = [(key.lower().replace("_", "-").encode(), value.encode()) for key, value in headers.items()]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw_headers})


def test_catalog_wall_handles_empty_snapshot_without_message_rpc(monkeypatch):
    monkeypatch.setattr(database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(
        "zerg.routers.agents_sessions.timeline_snapshot",
        lambda _params: {"observed_at": datetime.now(timezone.utc).isoformat(), "rows": [], "total": 0},
    )

    async def fail_rpc(*_args, **_kwargs):
        raise AssertionError("empty wall must not call pending-count RPC")

    monkeypatch.setattr("zerg.routers.agents_sessions._catalog_message_call", fail_rpc)
    response = asyncio.run(
        wall_query(
            repo=None,
            project=None,
            days=7,
            limit=50,
            include_automation=False,
            db=None,
            _auth=SimpleNamespace(owner_id=7),
            _single=None,
        )
    )
    assert response.total == 0
    assert response.sessions == []


def test_catalog_message_create_uses_catalog_rpc_without_sqlite(monkeypatch):
    sender_id = UUID("00000000-0000-0000-0000-000000000011")
    target_id = UUID("00000000-0000-0000-0000-000000000012")
    snapshots = {
        sender_id: SimpleNamespace(id=sender_id, device_id="device-7"),
        target_id: SimpleNamespace(id=target_id, device_id="device-7", catalog_facts={"latest_run": None, "connections": []}),
    }
    observed = {}

    monkeypatch.setattr(database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(
        "zerg.services.live_control_catalog.load_live_control_session_snapshot",
        lambda session_id: snapshots.get(UUID(str(session_id))),
    )

    async def catalog_call(method, params):
        observed["method"] = method
        observed["params"] = params
        return {
            "message": {
                "id": 9,
                "from_session_id": str(sender_id),
                "to_session_id": str(target_id),
                "text": params["text"],
                "delivery_status": "stored_only",
                "delivery_attempts": 0,
            }
        }

    monkeypatch.setattr("zerg.routers.agents_sessions._catalog_message_call", catalog_call)
    response = asyncio.run(
        create_message(
            request=_request_with_headers(),
            payload=SessionMessageCreate(from_session_id=sender_id, to_session_id=target_id, text="catalog native"),
            db=None,
            _auth=SimpleNamespace(owner_id=7, device_id="device-7"),
            _single=None,
        )
    )

    assert response["id"] == 9
    assert observed["method"] == "session.message.create.v2"
    assert observed["params"]["owner_id"] == 7
    assert observed["params"]["from_session_id"] == str(sender_id)


def test_catalog_message_delivery_preserves_owner_and_expected_status(monkeypatch):
    sender_id = UUID("00000000-0000-0000-0000-000000000021")
    target_id = UUID("00000000-0000-0000-0000-000000000022")
    observed = {}

    async def create_input_response(**_kwargs):
        return SimpleNamespace(outcome="sent")

    async def catalog_call(method, params):
        observed["method"] = method
        observed["params"] = params
        return {"message": {"id": 13, "delivery_status": "delivered"}}

    monkeypatch.setattr("zerg.routers.session_chat._create_catalog_session_input_response", create_input_response)
    monkeypatch.setattr("zerg.routers.agents_sessions._catalog_message_call", catalog_call)
    response = asyncio.run(
        _attempt_catalog_message_delivery(
            owner_id=7,
            sender_session=SimpleNamespace(id=sender_id, device_name="clifford", device_id="device-7"),
            target_session=SimpleNamespace(
                id=target_id,
                catalog_facts={
                    "latest_run": {"id": "run-1", "ended_at": None},
                    "connections": [
                        {
                            "state": "attached",
                            "released_at": None,
                            "can_send_input": 1,
                        }
                    ],
                },
            ),
            message={
                "id": 13,
                "text": "deliver me",
                "delivery_status": "stored_only",
                "delivery_attempts": 0,
            },
        )
    )

    assert response["delivery_status"] == "delivered"
    assert observed["method"] == "session.message.delivery.v2"
    assert observed["params"]["owner_id"] == 7
    assert observed["params"]["expected_status"] == "stored_only"
