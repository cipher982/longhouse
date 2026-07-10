from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

import zerg.database as database_module
from zerg.database import initialize_live_database
from zerg.database import make_live_engine
from zerg.database import make_sessionmaker
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionInputReceipt
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.routers.session_chat import SessionInputRequest
from zerg.routers.session_chat import _create_session_input_response
from zerg.services.live_control_catalog import live_control_capability_available
from zerg.services.live_control_catalog import load_live_control_session
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
from zerg.services.managed_control_dispatcher import ManagedControlDispatchResult
from zerg.services.write_serializer import WriteSerializer


def _seed_live_control(db):
    now = datetime.now(timezone.utc)
    session_id = uuid4()
    thread_id = uuid4()
    run_id = uuid4()
    db.add(
        LiveSessionCatalog(
            session_id=str(session_id),
            provider="codex",
            environment="production",
            project="longhouse",
            device_id="cinder",
            cwd="/workspace/longhouse",
            started_at=now,
            last_activity_at=now,
            primary_thread_id=str(thread_id),
            created_at=now,
            updated_at=now,
        )
    )
    db.add(
        LiveSessionThread(
            id=str(thread_id),
            session_id=str(session_id),
            provider="codex",
            branch_kind="root",
            is_primary=1,
            created_at=now,
            updated_at=now,
        )
    )
    db.add(
        LiveSessionRun(
            id=str(run_id),
            thread_id=str(thread_id),
            provider="codex",
            host_id="cinder",
            cwd="/workspace/longhouse",
            launch_origin="longhouse_spawned",
            started_at=now,
        )
    )
    db.add(
        LiveSessionConnection(
            run_id=str(run_id),
            control_plane="codex_bridge",
            acquisition_kind="launch",
            state="attached",
            device_id="cinder",
            can_send_input=1,
            can_interrupt=1,
            can_terminate=1,
            acquired_at=now,
        )
    )
    db.commit()
    return session_id


def test_live_control_projection_never_needs_archive_models(tmp_path):
    engine = make_live_engine(f"sqlite:///{tmp_path / 'live.db'}")
    initialize_live_database(engine)
    factory = make_sessionmaker(engine)
    with factory() as db:
        session_id = _seed_live_control(db)
        session = load_live_control_session(db, session_id)
        assert session is not None
        assert session.id == session_id
        assert session.device_id == "cinder"
        assert live_control_capability_available(db, session_id=session_id, capability="send") is True
        assert live_control_capability_available(db, session_id=session_id, capability="interrupt") is True


@pytest.mark.asyncio
async def test_catalog_input_dispatches_and_projects_live_receipt_only(tmp_path, monkeypatch):
    engine = make_live_engine(f"sqlite:///{tmp_path / 'live.db'}")
    initialize_live_database(engine)
    factory = make_sessionmaker(engine)
    with factory() as db:
        session_id = _seed_live_control(db)

    monkeypatch.setattr(database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(database_module, "get_live_session_factory", lambda: factory)
    monkeypatch.setattr(database_module, "get_live_write_session_factory", lambda: factory)
    serializer = WriteSerializer(name="catalog-control-test")
    serializer.configure(factory)

    import zerg.services.managed_control_dispatcher as dispatcher
    import zerg.services.live_session_inputs as live_inputs
    import zerg.services.session_chat_impl as chat_impl
    import zerg.routers.session_chat as session_chat

    async def fake_dispatch(**_kwargs):
        return ManagedControlDispatchResult(
            ok=True,
            transport=MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL,
            data={"exit_code": 0, "turn_id": "turn-1"},
        )

    monkeypatch.setattr(dispatcher, "dispatch_managed_control_command", fake_dispatch)
    monkeypatch.setattr(chat_impl, "_schedule_catalog_lock_release", lambda **_kwargs: None)
    monkeypatch.setattr(live_inputs, "get_live_write_serializer", lambda: serializer)
    monkeypatch.setattr(session_chat, "get_live_write_serializer", lambda: serializer)

    with factory() as db:
        session = load_live_control_session(db, session_id)
        assert session is not None
        response = await _create_session_input_response(
            source_session=session,
            owner_id=7,
            body=SessionInputRequest(text="keep the hot loop alive", client_request_id="catalog-control-1"),
            db=db,
        )

    assert response.outcome == "sent"
    assert response.input_id is None
    with factory() as db:
        receipt = db.query(LiveSessionInputReceipt).one()
        assert receipt.status == "delivered"
        assert receipt.client_request_id == "catalog-control-1"
        assert receipt.archive_session_input_id is None
