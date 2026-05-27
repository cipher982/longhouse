from __future__ import annotations

import asyncio
import json
from builtins import anext
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi import Response

import zerg.dependencies.auth as _auth_deps  # noqa: F401
import zerg.routers.timeline as timeline_router
import zerg.services.timeline_session_stream as timeline_stream
from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionRuntimeState
from zerg.services.agents_store import AgentsStore
from zerg.services.session_listing import SessionListParams
from zerg.services.session_listing import list_agent_sessions
from zerg.services.session_pubsub import TOPIC_TIMELINE
from zerg.services.session_pubsub import get_pubsub
from zerg.services.session_pubsub import reset_pubsub_for_test
from zerg.services.session_pubsub import topic_session
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.timeline_session_listing import TimelineSessionListParams


async def _noop_coro(*_args, **_kwargs) -> None:
    """No-op replacement for _wait_for_timeline_change in tests."""


def _make_db(tmp_path, name="timeline_stream.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _stream_params(**overrides) -> TimelineSessionListParams:
    values = {
        "project": None,
        "provider": None,
        "environment": None,
        "include_test": False,
        "hide_autonomous": True,
        "device_id": None,
        "days_back": 14,
        "query": None,
        "limit": 1,
        "offset": 0,
        "sort": None,
        "mode": "lexical",
        "context_mode": "forensic",
    }
    values.update(overrides)
    return TimelineSessionListParams(**values)


def _seed_session(
    db,
    *,
    started_at: datetime,
    ended_at: datetime | None = None,
    project: str = "zerg",
):
    session = AgentSession(
        provider="claude",
        environment="production",
        project=project,
        started_at=started_at,
        ended_at=ended_at,
        user_messages=2,
        assistant_messages=2,
        tool_calls=1,
        summary="Timeline stream test",
        summary_title="Timeline stream test",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _ingest_bridge_transcript(
    db,
    *,
    session_id,
    occurred_at: datetime,
    text: str,
    provider: str = "codex",
) -> None:
    ingest_runtime_events(
        db,
        [
            RuntimeEventIngest(
                runtime_key=f"{provider}:{session_id}",
                session_id=session_id,
                provider=provider,
                device_id="cinder",
                source="codex_bridge_live",
                kind="progress_signal",
                occurred_at=occurred_at,
                dedupe_key=f"bridge:live:{session_id}:thread-1:turn-1:1",
                payload={
                    "progress_kind": "bridge_live_transcript_delta",
                    "live_text": text,
                    "thread_id": "thread-1",
                    "turn_id": "turn-1",
                    "seq": 1,
                    "method": "item/agentMessage/delta",
                },
            )
        ],
    )
    db.commit()


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


class _DisconnectAfterFirstCycleRequest:
    def __init__(self) -> None:
        self._checks = 0

    async def is_disconnected(self) -> bool:
        self._checks += 1
        return self._checks > 1


def test_timeline_stream_emits_runtime_backed_session_upsert(tmp_path):
    session_local = _make_db(tmp_path, "timeline_stream_upsert.db")
    now = datetime.now(timezone.utc)

    with session_local() as db:
        old_runtime = _seed_session(
            db,
            started_at=now - timedelta(days=30),
            ended_at=None,
            project="old-runtime-stream",
        )
        _seed_session(
            db,
            started_at=now - timedelta(hours=2),
            ended_at=now - timedelta(hours=1),
            project="recent-history-stream",
        )
        db.add(
            SessionRuntimeState(
                runtime_key=f"claude:{old_runtime.id}",
                session_id=old_runtime.id,
                provider="claude",
                device_id="cinder",
                phase="running",
                phase_source="semantic",
                active_tool="bash",
                phase_started_at=now - timedelta(seconds=30),
                last_runtime_signal_at=now - timedelta(seconds=30),
                last_progress_at=now - timedelta(seconds=10),
                last_live_at=now - timedelta(seconds=30),
                timeline_anchor_at=now - timedelta(seconds=10),
                freshness_expires_at=now + timedelta(minutes=5),
                terminal_state=None,
                terminal_at=None,
                runtime_version=1,
            )
        )
        db.commit()

    async def _collect_events():
        stream = timeline_stream.stream_timeline_sessions_for_browser(
            _ConnectedRequest(),
            session_factory=session_local,
            params=_stream_params(),
            skip_initial_replay=False,
        )
        events = [await anext(stream), await anext(stream)]
        await stream.aclose()
        return events

    events = asyncio.run(_collect_events())
    upsert_payload = json.loads(events[1]["data"])

    assert events[0]["event"] == "connected"
    assert events[1]["event"] == "session_upsert"
    assert "Timeline session stream connected" in events[0]["data"]
    assert upsert_payload["session"]["thread_id"] == str(old_runtime.id)
    assert upsert_payload["session"]["head"]["project"] == "old-runtime-stream"
    assert upsert_payload["session"]["head"]["display_phase"] == "Running bash"


def test_session_window_signature_prefers_runtime_activity_anchor(tmp_path):
    session_local = _make_db(tmp_path, "timeline_stream_window_signature.db")
    now = datetime.now(timezone.utc)

    with session_local() as db:
        old_runtime = _seed_session(
            db,
            started_at=now - timedelta(days=30),
            ended_at=None,
            project="old-runtime-window",
        )
        _seed_session(
            db,
            started_at=now - timedelta(hours=1),
            ended_at=now - timedelta(minutes=30),
            project="recent-history-window",
        )
        db.add(
            SessionRuntimeState(
                runtime_key=f"claude:{old_runtime.id}",
                session_id=old_runtime.id,
                provider="claude",
                device_id="cinder",
                phase="running",
                phase_source="semantic",
                active_tool="bash",
                phase_started_at=now - timedelta(seconds=20),
                last_runtime_signal_at=now - timedelta(seconds=20),
                last_progress_at=now - timedelta(seconds=10),
                last_live_at=now - timedelta(seconds=20),
                timeline_anchor_at=now - timedelta(seconds=10),
                freshness_expires_at=now + timedelta(minutes=5),
                terminal_state=None,
                terminal_at=None,
                runtime_version=7,
            )
        )
        db.commit()

        store = AgentsStore(db)
        total, rows = store.list_session_window_signature(
            project=None,
            provider=None,
            environment=None,
            include_test=False,
            device_id=None,
            since=now - timedelta(days=14),
            query=None,
            limit=1,
            offset=0,
            hide_autonomous=True,
            context_mode="forensic",
            include_total=True,
        )

    assert total == 2
    assert rows[0][0] == str(old_runtime.id)
    assert rows[0][4] == 7
    assert rows[0][5] is not None


def test_session_window_signature_can_skip_total_count(tmp_path):
    session_local = _make_db(tmp_path, "timeline_stream_signature_no_total.db")
    now = datetime.now(timezone.utc)

    with session_local() as db:
        _seed_session(
            db,
            started_at=now - timedelta(minutes=5),
            ended_at=None,
            project="signature-no-total",
        )

        store = AgentsStore(db)
        total, rows = store.list_session_window_signature(
            project=None,
            provider=None,
            environment=None,
            include_test=False,
            device_id=None,
            since=now - timedelta(days=14),
            query=None,
            limit=1,
            offset=0,
            hide_autonomous=True,
            context_mode="forensic",
            include_total=False,
        )

    assert total is None
    assert len(rows) == 1
    assert rows[0][0] is not None


def test_timeline_stream_skips_full_rebuild_when_window_is_unchanged(tmp_path):
    session_local = _make_db(tmp_path, "timeline_stream_unchanged.db")
    now = datetime.now(timezone.utc)

    with session_local() as db:
        _seed_session(
            db,
            started_at=now - timedelta(minutes=5),
            ended_at=None,
            project="steady-stream",
        )

    list_sessions_calls = 0
    window_signature_kwargs: list[dict] = []
    original_list_timeline_sessions = timeline_stream.list_timeline_sessions_for_browser
    original_window_signature = timeline_stream.AgentsStore.list_timeline_thread_window_signature

    async def _counting_list_timeline_sessions(*args, **kwargs):
        nonlocal list_sessions_calls
        list_sessions_calls += 1
        return await original_list_timeline_sessions(*args, **kwargs)

    def _capturing_window_signature(self, *args, **kwargs):
        window_signature_kwargs.append(dict(kwargs))
        return original_window_signature(self, *args, **kwargs)

    async def _collect_events():
        stream = timeline_stream.stream_timeline_sessions_for_browser(
            _ConnectedRequest(),
            session_factory=session_local,
            params=_stream_params(),
            skip_initial_replay=False,
        )
        events = [await anext(stream), await anext(stream), await anext(stream)]
        await stream.aclose()
        return events

    with (
        patch.object(timeline_stream, "list_timeline_sessions_for_browser", new=_counting_list_timeline_sessions),
        patch.object(
            timeline_stream.AgentsStore,
            "list_timeline_thread_window_signature",
            new=_capturing_window_signature,
        ),
        patch.object(timeline_stream, "TIMELINE_STREAM_CHANGE_WAIT_SECONDS", 0),
        patch.object(timeline_stream, "TIMELINE_STREAM_HEARTBEAT_SECONDS", 0),
        patch.object(timeline_stream, "_wait_for_timeline_change", new=_noop_coro),
    ):
        events = asyncio.run(_collect_events())

    assert events[0]["event"] == "connected"
    assert events[1]["event"] == "session_upsert"
    assert events[2]["event"] == "heartbeat"
    assert list_sessions_calls == 1
    assert len(window_signature_kwargs) >= 1
    assert all(call.get("include_total") is False for call in window_signature_kwargs)


def test_timeline_stream_skip_initial_replay_avoids_redundant_rebuild_before_disconnect(tmp_path):
    session_local = _make_db(tmp_path, "timeline_stream_skip_initial_replay.db")
    now = datetime.now(timezone.utc)

    with session_local() as db:
        _seed_session(
            db,
            started_at=now - timedelta(minutes=5),
            ended_at=None,
            project="skip-initial-replay",
        )

    list_sessions_calls = 0
    original_list_timeline_sessions = timeline_stream.list_timeline_sessions_for_browser

    async def _counting_list_timeline_sessions(*args, **kwargs):
        nonlocal list_sessions_calls
        list_sessions_calls += 1
        return await original_list_timeline_sessions(*args, **kwargs)

    async def _collect_events():
        stream = timeline_stream.stream_timeline_sessions_for_browser(
            _DisconnectAfterFirstCycleRequest(),
            session_factory=session_local,
            params=_stream_params(),
            skip_initial_replay=True,
        )
        connected = await anext(stream)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        await stream.aclose()
        return connected

    with (
        patch.object(timeline_stream, "list_timeline_sessions_for_browser", new=_counting_list_timeline_sessions),
        patch.object(timeline_stream, "_wait_for_timeline_change", new=_noop_coro),
    ):
        event = asyncio.run(_collect_events())

    assert event["event"] == "connected"
    assert list_sessions_calls == 0


def test_timeline_stream_wakes_on_topic_timeline_publish(tmp_path):
    reset_pubsub_for_test()
    session_local = _make_db(tmp_path, "timeline_stream_topic_wake.db")
    now = datetime.now(timezone.utc)

    with session_local() as db:
        session = _seed_session(
            db,
            started_at=now - timedelta(minutes=5),
            ended_at=None,
            project="topic-timeline-wake",
        )

    async def _collect_after_publish():
        stream = timeline_stream.stream_timeline_sessions_for_browser(
            _ConnectedRequest(),
            session_factory=session_local,
            params=_stream_params(),
            skip_initial_replay=True,
        )
        try:
            connected = await anext(stream)
            next_event = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            get_pubsub().publish(TOPIC_TIMELINE, {"kind": "test", "session_id": str(session.id)})
            upsert = await asyncio.wait_for(next_event, timeout=0.5)
            return connected, upsert
        finally:
            await stream.aclose()

    connected, upsert = asyncio.run(_collect_after_publish())

    assert connected["event"] == "connected"
    assert upsert["event"] == "session_upsert"
    assert json.loads(upsert["data"])["session"]["thread_id"] == str(session.id)
    reset_pubsub_for_test()


def test_timeline_stream_skip_initial_replay_sends_targeted_update_only(tmp_path):
    reset_pubsub_for_test()
    session_local = _make_db(tmp_path, "timeline_stream_skip_initial_targeted.db")
    now = datetime.now(timezone.utc)

    with session_local() as db:
        targeted = _seed_session(
            db,
            started_at=now - timedelta(minutes=4),
            ended_at=None,
            project="skip-initial-targeted",
        )
        _seed_session(
            db,
            started_at=now - timedelta(minutes=5),
            ended_at=None,
            project="skip-initial-other",
        )

    async def _collect_after_publish():
        stream = timeline_stream.stream_timeline_sessions_for_browser(
            _ConnectedRequest(),
            session_factory=session_local,
            params=_stream_params(limit=2),
            skip_initial_replay=True,
        )
        try:
            connected = await anext(stream)
            next_event = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            get_pubsub().publish(TOPIC_TIMELINE, {"kind": "test", "session_id": str(targeted.id)})
            upsert = await asyncio.wait_for(next_event, timeout=0.5)

            followup = asyncio.create_task(anext(stream))
            done, pending = await asyncio.wait({followup}, timeout=0.05)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            assert not done
            return connected, upsert
        finally:
            await stream.aclose()

    connected, upsert = asyncio.run(_collect_after_publish())
    payload = json.loads(upsert["data"])

    assert connected["event"] == "connected"
    assert upsert["event"] == "session_upsert"
    assert payload["session"]["thread_id"] == str(targeted.id)
    assert "total" not in payload
    assert "has_real_sessions" not in payload
    reset_pubsub_for_test()


def test_timeline_stream_skip_initial_replay_targets_new_session_without_full_replay(tmp_path):
    reset_pubsub_for_test()
    session_local = _make_db(tmp_path, "timeline_stream_skip_initial_new_session.db")
    now = datetime.now(timezone.utc)

    with session_local() as db:
        _seed_session(
            db,
            started_at=now - timedelta(minutes=5),
            ended_at=None,
            project="skip-initial-existing",
        )

    async def _collect_after_new_session_publish():
        stream = timeline_stream.stream_timeline_sessions_for_browser(
            _ConnectedRequest(),
            session_factory=session_local,
            params=_stream_params(limit=2),
            skip_initial_replay=True,
        )
        try:
            connected = await anext(stream)
            next_event = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            with session_local() as db:
                new_session = _seed_session(
                    db,
                    started_at=now,
                    ended_at=None,
                    project="skip-initial-new",
                )
                new_session_id = str(new_session.id)
            get_pubsub().publish(TOPIC_TIMELINE, {"kind": "test", "session_id": new_session_id})
            upsert = await asyncio.wait_for(next_event, timeout=0.5)

            followup = asyncio.create_task(anext(stream))
            done, pending = await asyncio.wait({followup}, timeout=0.05)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            assert not done
            return connected, upsert, new_session_id
        finally:
            await stream.aclose()

    connected, upsert, new_session_id = asyncio.run(_collect_after_new_session_publish())
    payload = json.loads(upsert["data"])

    assert connected["event"] == "connected"
    assert upsert["event"] == "session_upsert"
    assert payload["session"]["thread_id"] == new_session_id
    assert payload["session"]["head"]["project"] == "skip-initial-new"
    reset_pubsub_for_test()


def test_timeline_stream_upserts_on_bridge_transcript_preview_only_change(tmp_path):
    reset_pubsub_for_test()
    session_local = _make_db(tmp_path, "timeline_stream_live_overlay.db")
    now = datetime.now(timezone.utc)

    with session_local() as db:
        session = _seed_session(
            db,
            started_at=now - timedelta(minutes=5),
            project="bridge-preview-stream",
        )
        session.provider = "codex"
        db.commit()

    async def _collect_after_overlay_insert():
        stream = timeline_stream.stream_timeline_sessions_for_browser(
            _ConnectedRequest(),
            session_factory=session_local,
            params=_stream_params(),
            skip_initial_replay=False,
        )
        try:
            connected = await anext(stream)
            initial = await anext(stream)

            next_event = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            with session_local() as db:
                _ingest_bridge_transcript(
                    db,
                    session_id=session.id,
                    occurred_at=now,
                    text="Bridge text arrived before the durable transcript.",
                )
            get_pubsub().publish(
                TOPIC_TIMELINE,
                {"kind": "runtime_update", "session_id": str(session.id)},
            )
            upsert = await asyncio.wait_for(next_event, timeout=0.5)
            return connected, initial, upsert
        finally:
            await stream.aclose()

    connected, initial, upsert = asyncio.run(_collect_after_overlay_insert())

    assert connected["event"] == "connected"
    assert initial["event"] == "session_upsert"
    assert upsert["event"] == "session_upsert"
    payload = json.loads(upsert["data"])
    assert (
        payload["session"]["head"]["transcript_preview"]["text"]
        == "Bridge text arrived before the durable transcript."
    )
    reset_pubsub_for_test()


def test_timeline_stream_known_session_update_uses_targeted_card(tmp_path):
    reset_pubsub_for_test()
    session_local = _make_db(tmp_path, "timeline_stream_targeted_update.db")
    now = datetime.now(timezone.utc)

    with session_local() as db:
        session = _seed_session(
            db,
            started_at=now - timedelta(minutes=5),
            project="targeted-update-stream",
        )
        db.commit()

    window_signature_calls = 0
    original_window_signature = AgentsStore.list_timeline_thread_window_signature

    def _counting_window_signature(self, *args, **kwargs):
        nonlocal window_signature_calls
        window_signature_calls += 1
        return original_window_signature(self, *args, **kwargs)

    async def _collect_after_known_session_update():
        stream = timeline_stream.stream_timeline_sessions_for_browser(
            _ConnectedRequest(),
            session_factory=session_local,
            params=_stream_params(),
            skip_initial_replay=False,
        )
        try:
            connected = await anext(stream)
            initial = await anext(stream)

            next_event = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            with session_local() as db:
                _ingest_bridge_transcript(
                    db,
                    session_id=session.id,
                    occurred_at=now,
                    text="Targeted card update",
                    provider="codex",
                )
            get_pubsub().publish(
                TOPIC_TIMELINE,
                {"kind": "runtime", "session_id": str(session.id), "provider": "codex"},
            )
            targeted = await asyncio.wait_for(next_event, timeout=0.5)
            return connected, initial, targeted
        finally:
            await stream.aclose()

    with patch.object(
        timeline_stream.AgentsStore,
        "list_timeline_thread_window_signature",
        new=_counting_window_signature,
    ):
        connected, initial, targeted = asyncio.run(_collect_after_known_session_update())

    assert connected["event"] == "connected"
    assert initial["event"] == "session_upsert"
    assert targeted["event"] == "session_upsert"
    assert window_signature_calls == 1
    payload = json.loads(targeted["data"])
    assert payload["session"]["head"]["transcript_preview"]["text"] == "Targeted card update"
    reset_pubsub_for_test()


def test_timeline_stream_ignores_session_only_topic_publish(tmp_path):
    reset_pubsub_for_test()
    session_local = _make_db(tmp_path, "timeline_stream_topic_isolation.db")
    now = datetime.now(timezone.utc)

    with session_local() as db:
        session = _seed_session(
            db,
            started_at=now - timedelta(minutes=5),
            ended_at=None,
            project="topic-timeline-isolation",
        )

    async def _collect_after_publishes():
        stream = timeline_stream.stream_timeline_sessions_for_browser(
            _ConnectedRequest(),
            session_factory=session_local,
            params=_stream_params(),
            skip_initial_replay=True,
        )
        try:
            connected = await anext(stream)
            next_event = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            get_pubsub().publish(topic_session("unrelated"), {"kind": "test"})
            done, pending = await asyncio.wait({next_event}, timeout=0.05)
            assert not done
            assert next_event in pending

            get_pubsub().publish(TOPIC_TIMELINE, {"kind": "test", "session_id": str(session.id)})
            upsert = await asyncio.wait_for(next_event, timeout=0.5)
            return connected, upsert
        finally:
            await stream.aclose()

    with patch.object(timeline_stream, "TIMELINE_STREAM_CHANGE_WAIT_SECONDS", 1.0):
        connected, upsert = asyncio.run(_collect_after_publishes())

    assert connected["event"] == "connected"
    assert upsert["event"] == "session_upsert"
    assert json.loads(upsert["data"])["session"]["thread_id"] == str(session.id)
    reset_pubsub_for_test()


def test_list_timeline_sessions_default_cards_open_writable_head_and_keep_thread_anchor(tmp_path):
    import pytest

    pytest.skip(
        "Session-identity-kernel cleanup removed thread_root_session_id, "
        "continued_from_session_id, and is_writable_head; multi-session "
        "thread coalescing into one timeline card no longer applies."
    )
    session_local = _make_db(tmp_path, "timeline_thread_cards_default.db")
    now = datetime.now(timezone.utc)
    thread_anchor = now - timedelta(seconds=5)

    with session_local() as db:
        root = _seed_session(
            db,
            started_at=now - timedelta(days=7),
            ended_at=now - timedelta(days=7),
            project="threaded-default",
        )
        root.is_writable_head = 0
        db.add(
            AgentEvent(
                session_id=root.id,
                role="user",
                content_text="Root session prompt",
                timestamp=now - timedelta(days=7),
            )
        )
        db.commit()

        head = AgentSession(
            provider="claude",
            environment="production",
            project="threaded-default",
            started_at=now - timedelta(days=6),
            ended_at=now - timedelta(days=6),
            thread_root_session_id=root.id,
            continued_from_session_id=root.id,
            user_messages=2,
            assistant_messages=2,
            tool_calls=1,
            summary="Writable head summary",
            summary_title="Writable head",
            is_writable_head=1,
        )
        db.add(head)
        db.commit()
        db.refresh(head)
        db.add(
            AgentEvent(
                session_id=head.id,
                role="user",
                content_text="Writable head prompt",
                timestamp=now - timedelta(days=6),
            )
        )
        db.commit()

        recent_single = _seed_session(
            db,
            started_at=now - timedelta(hours=1),
            ended_at=now - timedelta(minutes=30),
            project="recent-single",
        )

        db.add(
            SessionRuntimeState(
                runtime_key=f"claude:{root.id}",
                session_id=root.id,
                provider="claude",
                device_id="cinder",
                phase="running",
                phase_source="semantic",
                active_tool="bash",
                phase_started_at=now - timedelta(seconds=20),
                last_runtime_signal_at=now - timedelta(seconds=20),
                last_progress_at=thread_anchor,
                last_live_at=now - timedelta(seconds=20),
                timeline_anchor_at=thread_anchor,
                freshness_expires_at=now + timedelta(minutes=5),
                terminal_state=None,
                terminal_at=None,
                runtime_version=1,
            )
        )
        db.commit()

        response = asyncio.run(
            timeline_router.list_timeline_sessions(
                response=Response(),
                project=None,
                provider=None,
                environment=None,
                include_test=False,
                hide_autonomous=True,
                device_id=None,
                days_back=14,
                query=None,
                limit=20,
                offset=0,
                sort=None,
                mode="lexical",
                context_mode="forensic",
                db=db,
            )
        )
        agents_result = asyncio.run(
            list_agent_sessions(
                db=db,
                auth=object(),
                params=SessionListParams(
                    project=None,
                    provider=None,
                    environment=None,
                    include_test=False,
                    hide_autonomous=True,
                    device_id=None,
                    days_back=14,
                    query=None,
                    limit=20,
                    offset=0,
                    sort=None,
                    mode="lexical",
                    context_mode="forensic",
                ),
            )
        )

    assert response.total == 2
    assert len(response.sessions) == 2
    raw_sessions = {session.id: session for session in agents_result.response.sessions}
    raw_root = raw_sessions[str(root.id)]
    raw_head = raw_sessions[str(head.id)]

    top = response.sessions[0]
    assert top.thread_id == str(root.id)
    assert top.head.id == str(head.id)
    assert top.detail.id == str(head.id)
    assert top.root.id == str(root.id)
    assert top.timeline_anchor_at == thread_anchor.replace(tzinfo=None)
    assert top.timeline_anchor_at > response.sessions[1].timeline_anchor_at
    assert response.sessions[1].thread_id == str(recent_single.id)
    assert top.root.runtime_display == raw_root.runtime_display
    assert top.root.timeline_card == raw_root.timeline_card
    assert top.root.capabilities == raw_root.capabilities
    assert top.root.timeline_anchor_at == raw_root.timeline_anchor_at
    assert top.head.first_user_message == raw_head.first_user_message == "Writable head prompt"
    assert top.head.thread_root_session_id == raw_head.thread_root_session_id
    assert top.head.thread_head_session_id == raw_head.thread_head_session_id


def test_list_timeline_sessions_query_path_stays_raw_session_hits(tmp_path):
    import pytest

    pytest.skip(
        "Session-identity-kernel cleanup removed thread_root_session_id, "
        "continued_from_session_id, and is_writable_head columns. The "
        "timeline lexical-search test seeded multi-session threads to "
        "verify root vs head fan-out, which no longer applies."
    )
    session_local = _make_db(tmp_path, "timeline_thread_cards_lexical.db")
    now = datetime.now(timezone.utc)
    magic = "thread-card-needle"

    with session_local() as db:
        root = _seed_session(
            db,
            started_at=now - timedelta(days=2),
            ended_at=now - timedelta(days=2),
            project="threaded-search",
        )
        root.is_writable_head = 0
        db.add(
            AgentEvent(
                session_id=root.id,
                role="user",
                content_text=f"older continuation match {magic}",
                timestamp=now - timedelta(days=2),
            )
        )
        db.commit()

        head = AgentSession(
            provider="claude",
            environment="production",
            project="threaded-search",
            started_at=now - timedelta(hours=1),
            ended_at=now - timedelta(hours=1),
            thread_root_session_id=root.id,
            continued_from_session_id=root.id,
            user_messages=2,
            assistant_messages=2,
            tool_calls=1,
            summary="Head session",
            summary_title="Head session",
            is_writable_head=1,
        )
        db.add(head)
        db.commit()
        db.refresh(head)
        db.add(
            AgentEvent(
                session_id=head.id,
                role="user",
                content_text="newer continuation without the lexical token",
                timestamp=now - timedelta(hours=1),
            )
        )
        db.commit()

        with (
            patch.object(AgentsStore, "_fts_session_ids", return_value=[root.id]),
            patch.object(
                timeline_router._sessions_router,
                "list_sessions",
                side_effect=AssertionError("timeline listing should call the service, not the agents router"),
            ),
        ):
            payload = asyncio.run(
                timeline_router.list_timeline_sessions(
                    response=Response(),
                    project="threaded-search",
                    provider=None,
                    environment=None,
                    include_test=False,
                    hide_autonomous=True,
                    device_id=None,
                    days_back=14,
                    query=magic,
                    limit=20,
                    offset=0,
                    sort=None,
                    mode="lexical",
                    context_mode="forensic",
                    db=db,
                )
            )

    assert payload.status_code == 200
    body = json.loads(payload.body)
    assert body["total"] == 1
    assert len(body["sessions"]) == 1
    row = body["sessions"][0]
    assert row["id"] == str(root.id)
    assert "head" not in row
    assert row["thread_root_session_id"] == str(root.id)
    assert row["thread_head_session_id"] == str(head.id)
    assert row["match_event_id"] is not None
    assert magic in (row["match_snippet"] or "")


@pytest.mark.parametrize(
    ("query", "sort", "mode"),
    [
        ("thread-card-needle", None, "lexical"),
        (None, None, "hybrid"),
    ],
)
def test_timeline_stream_rejects_non_threaded_query_contracts(tmp_path, query, sort, mode):
    session_local = _make_db(tmp_path, "timeline_stream_reject_contract.db")
    now = datetime.now(timezone.utc)

    with session_local() as db:
        _seed_session(
            db,
            started_at=now - timedelta(minutes=5),
            ended_at=None,
            project="reject-stream-contract",
        )

        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(
                timeline_router.stream_timeline_sessions(
                    _ConnectedRequest(),
                    project=None,
                    provider=None,
                    environment=None,
                    include_test=False,
                    hide_autonomous=True,
                    device_id=None,
                    days_back=14,
                    query=query,
                    limit=20,
                    offset=0,
                    sort=sort,
                    mode=mode,
                    context_mode="forensic",
                )
            )

    assert excinfo.value.status_code == 400
    assert "default no-query lexical recency contract" in str(excinfo.value.detail)
