from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from unittest.mock import patch

from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentsBase
from zerg.models.agents import SessionRuntimeState
from zerg.services.agents_store import AgentsStore

browser_auth_stub = types.ModuleType("zerg.dependencies.browser_auth")
browser_auth_stub.get_current_browser_user = lambda *args, **kwargs: None
browser_auth_stub.get_optional_browser_user = lambda *args, **kwargs: None
sys.modules.setdefault("zerg.dependencies.browser_auth", browser_auth_stub)

from zerg.routers.timeline import _timeline_sessions_stream

timeline_router = sys.modules[_timeline_sessions_stream.__module__]


def _make_db(tmp_path, name="timeline_stream.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    AgentsBase.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


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


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


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
        stream = _timeline_sessions_stream(
            _ConnectedRequest(),
            session_factory=session_local,
            project=None,
            provider=None,
            environment=None,
            include_test=False,
            hide_autonomous=True,
            device_id=None,
            days_back=14,
            query=None,
            limit=1,
            offset=0,
            sort=None,
            mode="lexical",
            context_mode="forensic",
        )
        events = [await anext(stream), await anext(stream)]
        await stream.aclose()
        return events

    events = asyncio.run(_collect_events())
    upsert_payload = json.loads(events[1]["data"])

    assert events[0]["event"] == "connected"
    assert events[1]["event"] == "session_upsert"
    assert "Timeline session stream connected" in events[0]["data"]
    assert upsert_payload["session"]["project"] == "old-runtime-stream"
    assert upsert_payload["session"]["display_phase"] == "Running bash"


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
    assert rows[0][5] == 7
    assert rows[0][6] is not None


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
    original_list_sessions = timeline_router.agents_router.list_sessions
    original_window_signature = timeline_router.AgentsStore.list_session_window_signature

    async def _counting_list_sessions(*args, **kwargs):
        nonlocal list_sessions_calls
        list_sessions_calls += 1
        return await original_list_sessions(*args, **kwargs)

    def _capturing_window_signature(self, *args, **kwargs):
        window_signature_kwargs.append(dict(kwargs))
        return original_window_signature(self, *args, **kwargs)

    async def _collect_events():
        stream = _timeline_sessions_stream(
            _ConnectedRequest(),
            session_factory=session_local,
            project=None,
            provider=None,
            environment=None,
            include_test=False,
            hide_autonomous=True,
            device_id=None,
            days_back=14,
            query=None,
            limit=1,
            offset=0,
            sort=None,
            mode="lexical",
            context_mode="forensic",
        )
        events = [await anext(stream), await anext(stream), await anext(stream)]
        await stream.aclose()
        return events

    with (
        patch.object(timeline_router.agents_router, "list_sessions", new=_counting_list_sessions),
        patch.object(timeline_router.AgentsStore, "list_session_window_signature", new=_capturing_window_signature),
        patch.object(timeline_router, "TIMELINE_STREAM_POLL_SECONDS", 0),
        patch.object(timeline_router, "TIMELINE_STREAM_HEARTBEAT_SECONDS", 0),
    ):
        events = asyncio.run(_collect_events())

    assert events[0]["event"] == "connected"
    assert events[1]["event"] == "session_upsert"
    assert events[2]["event"] == "heartbeat"
    assert list_sessions_calls == 1
    assert len(window_signature_kwargs) >= 1
    assert all(call.get("include_total") is False for call in window_signature_kwargs)
