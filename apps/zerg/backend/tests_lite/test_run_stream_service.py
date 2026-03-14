from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from zerg.database import Base
from zerg.database import get_test_commis_id
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.database import reset_test_commis_id
from zerg.database import set_test_commis_id
from zerg.dependencies.oikos_auth import get_current_oikos_user
from zerg.events import EventType
from zerg.events.event_bus import event_bus
from zerg.models import Fiche
from zerg.models import Run
from zerg.models import Thread
from zerg.models import User
from zerg.models.enums import RunStatus
from zerg.models.enums import ThreadType
from zerg.models.enums import UserRole
from zerg.models.run_event import RunEvent
from zerg.routers import stream as stream_router
from zerg.services import run_stream as run_stream_service


def _make_db(tmp_path):
    db_path = tmp_path / "test_run_stream_service.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_run(session_local, *, status: RunStatus = RunStatus.RUNNING):
    with session_local() as db:
        user = User(email="stream@test.local", role=UserRole.USER.value)
        db.add(user)
        db.commit()
        db.refresh(user)

        fiche = Fiche(
            name="Stream Test",
            system_instructions="",
            task_instructions="",
            model="gpt-5",
            owner_id=user.id,
        )
        db.add(fiche)
        db.commit()
        db.refresh(fiche)

        thread = Thread(
            fiche_id=fiche.id,
            title="Primary",
            active=True,
            thread_type=ThreadType.CHAT.value,
        )
        db.add(thread)
        db.commit()
        db.refresh(thread)

        run = Run(
            fiche_id=fiche.id,
            thread_id=thread.id,
            status=status.value,
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        return user.id, run.id


def _append_run_event(session_local, *, run_id: int, event_type: str, payload: dict):
    with session_local() as db:
        event = RunEvent(run_id=run_id, event_type=event_type, payload=payload)
        db.add(event)
        db.commit()
        db.refresh(event)
        return event.id


@contextmanager
def _patched_stream_db(monkeypatch, session_local):
    @contextmanager
    def _db_session():
        with session_local() as db:
            yield db

    monkeypatch.setattr(stream_router, "db_session", _db_session)
    monkeypatch.setattr(run_stream_service, "db_session", _db_session)
    yield


def test_lifecycle_keep_open_extends_lease_only_for_live_events():
    state = run_stream_service.StreamLifecycleState()

    state.apply(
        "stream_control",
        {"action": "keep_open", "ttl_ms": 900_000},
        from_replay=True,
        now_monotonic=10.0,
    )
    assert state.stream_lease_until is None

    state.apply(
        "stream_control",
        {"action": "keep_open", "ttl_ms": 900_000},
        from_replay=False,
        now_monotonic=10.0,
    )
    assert state.stream_lease_until == 310.0


def test_lifecycle_close_waits_until_close_marker_is_streamed():
    state = run_stream_service.StreamLifecycleState()

    state.apply(
        "stream_control",
        {"action": "close", "event_id": 5},
        from_replay=False,
        now_monotonic=0.0,
    )

    assert state.should_close_after_live_event(event_id=4, now_monotonic=0.0) is False
    assert state.should_close_after_live_event(event_id=5, now_monotonic=0.0) is True


def test_lifecycle_starts_grace_window_after_oikos_complete_and_last_commis():
    state = run_stream_service.StreamLifecycleState()

    state.apply("commis_spawned", {}, from_replay=False, now_monotonic=1.0)
    state.apply("oikos_complete", {}, from_replay=False, now_monotonic=2.0)

    assert state.close_after_current_event is False

    state.apply("commis_complete", {}, from_replay=False, now_monotonic=3.0)

    assert state.awaiting_continuation_until == 8.0
    assert state.next_timeout(3.0) == 5.0
    assert state.should_close_on_timeout(7.9) is False
    assert state.should_close_on_timeout(8.0) is True


def test_lifecycle_oikos_deferred_respects_close_stream_flag():
    keep_open_state = run_stream_service.StreamLifecycleState()
    keep_open_state.apply(
        "oikos_deferred",
        {"close_stream": False},
        from_replay=False,
        now_monotonic=0.0,
    )
    assert keep_open_state.should_close_after_live_event(event_id=None, now_monotonic=0.0) is False

    close_state = run_stream_service.StreamLifecycleState()
    close_state.apply(
        "oikos_deferred",
        {},
        from_replay=False,
        now_monotonic=0.0,
    )
    assert close_state.should_close_after_live_event(event_id=None, now_monotonic=0.0) is True


def test_load_historical_run_events_uses_test_commis_id_context(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    _, run_id = _seed_run(session_local)
    seen_commis_ids = []

    def fake_get_events_after(*, db, run_id, after_id, include_tokens):
        seen_commis_ids.append(get_test_commis_id())
        return []

    outer_token = set_test_commis_id("outer-context")
    monkeypatch.setattr(run_stream_service.EventStore, "get_events_after", staticmethod(fake_get_events_after))
    try:
        with _patched_stream_db(monkeypatch, session_local):
            records = run_stream_service.load_historical_run_events(
                run_id=run_id,
                after_event_id=0,
                include_tokens=True,
                test_commis_id="replay-context",
            )

        assert records == []
        assert seen_commis_ids == ["replay-context"]
        assert get_test_commis_id() == "outer-context"
    finally:
        reset_test_commis_id(outer_token)


def test_load_historical_run_events_returns_serializable_records_after_session_closes(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    _, run_id = _seed_run(session_local)
    event_id = _append_run_event(
        session_local,
        run_id=run_id,
        event_type="oikos_started",
        payload={"message": "started"},
    )

    with _patched_stream_db(monkeypatch, session_local):
        records = run_stream_service.load_historical_run_events(
            run_id=run_id,
            after_event_id=0,
            include_tokens=True,
        )

    serialized = [asdict(record) for record in records]
    assert json.loads(json.dumps(serialized)) == [
        {
            "event_id": event_id,
            "event_type": "oikos_started",
            "payload": {"message": "started"},
            "timestamp": serialized[0]["timestamp"],
        }
    ]
    assert isinstance(serialized[0]["timestamp"], str)
    datetime.fromisoformat(serialized[0]["timestamp"].replace("Z", "+00:00"))


@pytest.mark.asyncio
async def test_stream_run_events_live_defaults_to_context_test_commis_id(monkeypatch):
    captured_kwargs = {}

    async def fake_replay_and_stream(**kwargs):
        captured_kwargs.update(kwargs)
        if False:
            yield {}

    monkeypatch.setattr(stream_router, "_replay_and_stream", fake_replay_and_stream)

    token = set_test_commis_id("live-context")
    try:
        generator = stream_router.stream_run_events_live(run_id=77, owner_id=11)
        connected = await anext(generator)

        assert connected["event"] == "connected"

        with pytest.raises(StopAsyncIteration):
            await anext(generator)

        assert captured_kwargs["test_commis_id"] == "live-context"
    finally:
        reset_test_commis_id(token)


@pytest.mark.asyncio
async def test_stream_run_events_live_emits_connected_then_replay(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    owner_id, run_id = _seed_run(session_local)
    _append_run_event(session_local, run_id=run_id, event_type="oikos_started", payload={"message": "started"})
    _append_run_event(session_local, run_id=run_id, event_type="stream_control", payload={"action": "close"})

    with _patched_stream_db(monkeypatch, session_local):
        generator = stream_router.stream_run_events_live(run_id=run_id, owner_id=owner_id)

        connected = await anext(generator)
        replay_started = await anext(generator)
        replay_close = await anext(generator)

        assert connected["event"] == "connected"
        assert json.loads(connected["data"])["run_id"] == run_id
        assert replay_started["event"] == "oikos_started"
        assert json.loads(replay_started["data"])["type"] == "oikos_started"
        assert replay_close["event"] == "stream_control"
        assert json.loads(replay_close["data"])["payload"]["action"] == "close"

        with pytest.raises(StopAsyncIteration):
            await anext(generator)


@pytest.mark.asyncio
async def test_stream_run_events_replays_before_live_and_skips_duplicate_live_event_ids(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    owner_id, run_id = _seed_run(session_local)
    replay_event_id = _append_run_event(
        session_local, run_id=run_id, event_type="oikos_started", payload={"message": "started"}
    )

    with _patched_stream_db(monkeypatch, session_local):
        generator = stream_router.stream_run_events_live(run_id=run_id, owner_id=owner_id)

        connected = await anext(generator)
        replay_started = await anext(generator)
        heartbeat = await anext(generator)

        await event_bus.publish(
            EventType.OIKOS_THINKING,
            {
                "event_type": "oikos_thinking",
                "owner_id": owner_id,
                "run_id": run_id,
                "event_id": replay_event_id,
                "message": "duplicate",
            },
        )
        await event_bus.publish(
            EventType.OIKOS_THINKING,
            {
                "event_type": "oikos_thinking",
                "owner_id": owner_id,
                "run_id": run_id,
                "event_id": replay_event_id + 1,
                "message": "live",
            },
        )

        live_event = await anext(generator)

        assert connected["event"] == "connected"
        assert replay_started["event"] == "oikos_started"
        assert heartbeat["event"] == "heartbeat"
        assert live_event["event"] == "oikos_thinking"
        assert live_event["id"] == str(replay_event_id + 1)
        assert json.loads(live_event["data"])["payload"]["message"] == "live"

        await generator.aclose()


@pytest.mark.asyncio
async def test_stream_run_events_closes_after_replay_close_marker(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    owner_id, run_id = _seed_run(session_local)
    _append_run_event(session_local, run_id=run_id, event_type="oikos_started", payload={"message": "started"})
    _append_run_event(session_local, run_id=run_id, event_type="stream_control", payload={"action": "close"})

    with _patched_stream_db(monkeypatch, session_local):
        generator = stream_router._replay_and_stream(
            run_id=run_id,
            owner_id=owner_id,
            status=RunStatus.RUNNING,
            after_event_id=0,
            include_tokens=True,
        )
        event_types = []
        async for event in generator:
            event_types.append(event["event"])

        assert event_types == ["oikos_started", "stream_control"]


def test_stream_run_replay_last_event_id_header_overrides_query_param(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    owner_id, run_id = _seed_run(session_local, status=RunStatus.SUCCESS)
    first_event_id = _append_run_event(
        session_local, run_id=run_id, event_type="oikos_started", payload={"message": "started"}
    )
    second_event_id = _append_run_event(
        session_local, run_id=run_id, event_type="oikos_complete", payload={"result": "done"}
    )

    with _patched_stream_db(monkeypatch, session_local):
        from zerg.main import api_app

        def override_current_user():
            return SimpleNamespace(id=owner_id)

        original_override = api_app.dependency_overrides.get(get_current_oikos_user)
        api_app.dependency_overrides[get_current_oikos_user] = override_current_user
        client = TestClient(api_app)

        try:
            with client.stream(
                "GET",
                f"/stream/runs/{run_id}?after_event_id=0",
                headers={"Last-Event-ID": str(first_event_id)},
            ) as response:
                body = "".join(response.iter_text())

            assert response.status_code == 200
            assert f"id: {first_event_id}" not in body
            assert f"id: {second_event_id}" in body
            assert "event: oikos_complete" in body
        finally:
            if original_override is None:
                api_app.dependency_overrides.pop(get_current_oikos_user, None)
            else:
                api_app.dependency_overrides[get_current_oikos_user] = original_override
