from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.store import CatalogStore
from zerg.models.live_store import LiveArchiveOutbox
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionLaunchAttempt
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveUser
from zerg.services.console_turns import CatalogConsoleTurn
from zerg.services.session_runtime import RuntimeEventIngest


def test_catalog_console_session_is_idle_identity_not_launch(tmp_path):
    engine = create_catalog_engine(tmp_path / "catalog.db")
    initialize_catalog_schema(engine)
    session_id = uuid4()
    thread_id = uuid4()
    data = {
        "session_id": str(session_id),
        "thread_id": str(thread_id),
        "owner_id": 1,
        "provider": "codex",
        "device_id": "cinder",
        "cwd": "/tmp/longhouse",
        "project": "longhouse",
        "provider_config": {"permission_mode": "bypass"},
        "started_at": datetime.now(UTC),
    }

    result = CatalogStore(engine).create_console_session(data=data)

    assert result["created"] is True
    with Session(engine) as db:
        session = db.get(LiveSessionCatalog, str(session_id))
        thread = db.get(LiveSessionThread, str(thread_id))
        assert session.primary_thread_id == str(thread_id)
        assert thread.device_id == "cinder"
        assert thread.cwd == "/tmp/longhouse"
        assert db.query(LiveSessionRun).count() == 0
        assert db.query(LiveSessionLaunchAttempt).count() == 0
        assert db.query(LiveArchiveOutbox).count() == 1

    replay = CatalogStore(engine).create_console_session(data=data)
    assert replay["created"] is False
    assert replay["exact_replay"] is True


def test_catalog_console_turns_claim_and_wake_fifo(tmp_path):
    engine = create_catalog_engine(tmp_path / "catalog-turns.db")
    initialize_catalog_schema(engine)
    store = CatalogStore(engine)
    session_id = uuid4()
    thread_id = uuid4()
    with Session(engine) as db:
        db.add_all(
            [
                LiveUser(id=1, email="owner@example.com", is_active=True),
                LiveUser(id=42, email="other@example.com", is_active=True),
            ]
        )
        db.commit()
    store.create_console_session(
        data={
            "session_id": str(session_id),
            "thread_id": str(thread_id),
            "owner_id": 1,
            "provider": "codex",
            "device_id": "cinder",
            "cwd": "/tmp/longhouse",
            "project": "longhouse",
            "provider_config": {"permission_mode": "bypass"},
            "started_at": datetime.now(UTC),
        }
    )
    unauthorized = store.enqueue_console_turn(
        data={
            "session_id": str(session_id),
            "owner_id": 42,
            "message": "not yours",
            "client_request_id": "wrong-owner",
            "created_at": datetime.now(UTC),
        }
    )
    assert unauthorized == {"found": False}
    first = store.enqueue_console_turn(
        data={
            "session_id": str(session_id),
            "owner_id": 1,
            "message": "first",
            "client_request_id": "request-1",
            "created_at": datetime.now(UTC),
        }
    )
    second = store.enqueue_console_turn(
        data={
            "session_id": str(session_id),
            "owner_id": 1,
            "message": "second",
            "client_request_id": "request-2",
            "created_at": datetime.now(UTC),
        }
    )
    assert first["turn"]["state"] == "starting"
    assert first["turn"]["run_id"]
    assert first["turn"]["client_request_id"] == "request-1"
    assert second["turn"]["state"] == "queued"
    assert second["turn"]["run_id"] is None
    assert second["turn"]["client_request_id"] == "request-2"
    facts = store.read_session(session_id=str(session_id), owner_id=1)["facts"]
    assert facts["latest_console_turn"]["state"] == "starting"
    active = store.update_console_turn(
        data={
            "turn_id": first["turn"]["turn_id"],
            "run_id": first["turn"]["run_id"],
            "state": "active",
            "updated_at": datetime.now(UTC),
        }
    )
    assert active["turn"]["state"] == "active"
    provider_thread_id = "019f6b93-edf6-7bd0-a757-b5195a61abdd"
    store.apply_session_runtime(
        events=[
            RuntimeEventIngest(
                runtime_key=f"codex:{session_id}",
                session_id=session_id,
                thread_id=thread_id,
                run_id=first["turn"]["run_id"],
                provider="codex",
                device_id="cinder",
                source="codex_exec",
                kind="binding_signal",
                occurred_at=datetime.now(UTC),
                dedupe_key=f"binding:{first['turn']['run_id']}",
                payload={"provider_session_id": provider_thread_id},
            )
        ]
    )
    settled = store.update_console_turn(
        data={
            "run_id": first["turn"]["run_id"],
            "state": "completed",
            "updated_at": datetime.now(UTC),
        }
    )
    assert settled["turn"]["state"] == "completed"
    assert settled["next_turn"]["turn_id"] == second["turn"]["turn_id"]
    assert settled["next_turn"]["state"] == "starting"
    assert settled["next_turn"]["run_id"]
    assert settled["next_turn"]["resume_provider_thread_id"] == provider_thread_id


@pytest.mark.asyncio
async def test_agents_console_turn_uses_catalog_without_cold_session(monkeypatch):
    from zerg.routers import agents_sessions

    session_id = uuid4()
    turn_id = uuid4()
    run_id = uuid4()
    dispatched = {}

    async def enqueue(**kwargs):
        dispatched.update(kwargs)
        return CatalogConsoleTurn(
            turn_id=turn_id,
            run_id=run_id,
            state="active",
            created=True,
        )

    monkeypatch.setattr(agents_sessions.database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(agents_sessions, "enqueue_catalog_console_turn", enqueue)

    response = await agents_sessions.create_console_turn(
        session_id=session_id,
        body=agents_sessions.ConsoleTurnCreate(message="first message", client_request_id="request-1"),
        db=None,
        auth=SimpleNamespace(owner_id=1),
        _single=None,
    )

    assert response.turn_id == turn_id
    assert response.run_id == run_id
    assert response.state == "active"
    assert dispatched == {
        "owner_id": 1,
        "session_id": session_id,
        "message": "first message",
        "client_request_id": "request-1",
    }
