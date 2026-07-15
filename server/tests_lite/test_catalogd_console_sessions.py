from datetime import UTC
from datetime import datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.store import CatalogStore
from zerg.models.live_store import LiveArchiveOutbox
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionLaunchAttempt
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread


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
    assert second["turn"]["state"] == "queued"
    assert second["turn"]["run_id"] is None

    active = store.update_console_turn(
        data={
            "turn_id": first["turn"]["turn_id"],
            "run_id": first["turn"]["run_id"],
            "state": "active",
            "updated_at": datetime.now(UTC),
        }
    )
    assert active["turn"]["state"] == "active"
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
